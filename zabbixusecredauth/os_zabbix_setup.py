#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
os_zabbix_setup.py - build the OpenStack Services tree, SLAs and dashboards in
Zabbix 6.0 through the API. Idempotent: objects are found by name and updated,
so it is safe to re-run after every change of the config.

    export ZBX_TOKEN=<API token>            # or ZBX_USER / ZBX_PASSWORD
    python3 os_zabbix_setup.py -c deploy/os_zabbix_setup.example.json --dry-run
    python3 os_zabbix_setup.py -c /etc/zabbix/os_zabbix_setup.json
    python3 os_zabbix_setup.py -c ... --only services,sla

Services tree (per AZ, products as in the config):

  <root>
    AZ-BKK
      BKK · Compute            tags sla=compute, az=BKK  <- SLA "Compute BKK"
        BKK · Nova             (component, may have several product parents)
          BKK · Nova · VIP                  problem tags service=nova target=vip
          BKK · Nova · controllers          "most critical if ALL children have problems"
            BKK · Nova · ctl1               most critical of
              BKK · Nova · ctl1 · API       service=nova target=<ctl1 ip>
              BKK · Nova · ctl1 · services  service=nova node like <ctl1 hostname>
        BKK · MariaDB          quorum: >= 2 children with Average+ -> Disaster
          BKK · MariaDB · ctl1              service=mariadb node=<zabbix host>
        BKK · RabbitMQ         service=rabbitmq (cluster-wide triggers)

There is no all-in-one "OpenStack Overall" Service/SLA: every service (and every
product bundle) is judged on its own. Overall Services/SLAs created by older
versions are deleted on the next run.

Every leaf also matches impact=availability and az=<AZ>: capacity, resource,
stability and monitoring problems never reach the SLA. The az tag comes from
the host tag az=<AZ> - set it on every host of the AZ.

Python 3.6+, standard library only.
"""

import argparse
import base64
import calendar
import json
import os
import struct
import sys
import time
import urllib.request
import zlib

PERIODS = {'daily': 0, 'weekly': 1, 'monthly': 2, 'quarterly': 3, 'annually': 4}
ALG_OK, ALG_ALL, ALG_ANY = 0, 1, 2      # set OK / most critical if all children / most critical
TAG_EQUALS, TAG_LIKE = 0, 2
TAG_NOT_EQUAL = 3       # widget / trigger.get tag operator "does not equal" (Zabbix 6.0)

LABELS = {
    'keystone': 'Keystone', 'nova': 'Nova', 'placement': 'Placement', 'neutron': 'Neutron',
    'glance': 'Glance', 'cinder': 'Cinder', 'heat': 'Heat', 'octavia': 'Octavia',
    'barbican': 'Barbican', 'trove': 'Trove', 'magnum': 'Magnum', 'ironic': 'Ironic',
    'manila': 'Manila', 'designate': 'Designate', 'horizon': 'Horizon', 'swift': 'Swift',
    'mariadb': 'MariaDB', 'rabbitmq': 'RabbitMQ', 'memcached': 'memcached',
    'keepalived': 'keepalived', 'haproxy': 'HAProxy', 'opensdn': 'OpenSDN', 'ceph': 'Ceph',
    's3': 'S3',
}
# api      VIP leaf + L3 cluster leaf + per-controller (API by IP, service records by hostname), HA rule
# vip      VIP leaf only (Horizon and S3 have no per-node check; S3 = root, static web, canary)
# quorum   per-node leaves + "2 of 3 with problems -> Disaster"
# pernode  per-node leaves, HA rule only
# cluster  one leaf: the template already decides when it is an outage
KINDS = {
    'keystone': 'api', 'nova': 'api', 'placement': 'api', 'neutron': 'api', 'glance': 'api',
    'cinder': 'api', 'heat': 'api', 'octavia': 'api', 'barbican': 'api', 'trove': 'api',
    'magnum': 'api', 'ironic': 'api', 'manila': 'api', 'designate': 'api', 'swift': 'api',
    'horizon': 'vip', 's3': 'vip', 'mariadb': 'quorum', 'memcached': 'pernode',
    'rabbitmq': 'cluster', 'keepalived': 'cluster', 'haproxy': 'cluster',
    'opensdn': 'cluster', 'ceph': 'cluster',
}

# Services modelled when the config has no "services" list. There is deliberately
# no "OpenStack Overall" / all-in-one SLA: every service (and every product bundle
# in the config) has its own SLA and an outage of one never counts against another.
DEFAULT_SERVICES = [
    'keystone', 'mariadb', 'rabbitmq', 'haproxy', 'keepalived', 'memcached',
    'nova', 'placement', 'glance', 'cinder', 'neutron', 'opensdn', 'ceph',
    'octavia', 'horizon', 'heat', 'magnum', 'barbican', 'trove', 'ironic',
    'manila', 'designate', 's3',
]


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
class ApiError(Exception):
    pass


class Api(object):
    def __init__(self, url, token='', user='', password='', verify=True):
        self.url, self.auth, self.n = url, token, 0
        self.ctx = None
        if not verify:
            import ssl
            self.ctx = ssl._create_unverified_context()
        if not token:
            self.auth = None
            self.auth = self.call('user.login', {'username': user, 'password': password})

    def call(self, method, params):
        self.n += 1
        body = {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': self.n}
        if self.auth and method != 'user.login':
            body['auth'] = self.auth            # 6.0: token goes in the body
        req = urllib.request.Request(self.url, json.dumps(body).encode('utf-8'),
                                     {'Content-Type': 'application/json-rpc'})
        with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
            resp = json.loads(r.read().decode('utf-8'))
        if 'error' in resp:
            e = resp['error']
            raise ApiError('%s: %s %s' % (method, e.get('message'), e.get('data')))
        return resp['result']


class DryApi(object):
    """Prints what would be sent. Every lookup finds nothing, so all are creates."""

    def __init__(self):
        self.n, self.counts = 0, {}

    def call(self, method, params):
        self.counts[method] = self.counts.get(method, 0) + 1
        if method.endswith('.get'):
            return []
        self.n += 1
        obj = method.split('.')[0]
        name = params.get('name', '')
        if obj == 'service':
            pt = ' '.join('%s%s%s' % (t['tag'], '=' if t['operator'] == TAG_EQUALS else '~', t['value'])
                          for t in params.get('problem_tags') or [])
            print('  service  %-52s alg=%s%s' % (name, params['algorithm'],
                                                  ('  [' + pt + ']') if pt else ''))
        elif obj == 'dashboard':
            pages = params.get('pages') or []
            pinfo = ', '.join('%s (%dw)' % (p['name'], len(p.get('widgets') or [])) for p in pages)
            print('  %-8s %-36s [%d pages: %s]' % (obj, name, len(pages), pinfo))
        elif obj == 'map':
            print('  %-8s %-36s [%d hexagons]' % (obj, name, len(params.get('selements') or [])))
        elif obj == 'image':
            pass
        else:
            print('  %-8s %s' % (obj, name))
        return {obj + 'ids': [str(self.n)], 'dashboardids': [str(self.n)],
                'sysmapids': [str(self.n)], 'imageids': [str(self.n)]}


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------
def ptag(tag, value, op=TAG_EQUALS):
    return {'tag': tag, 'operator': op, 'value': value}


def stag(tag, value):
    return {'tag': tag, 'value': value}


class Builder(object):
    def __init__(self, api, cfg):
        self.api, self.cfg = api, cfg
        self.order = 0

    def ensure_service(self, name, parents=(), algorithm=ALG_ANY, problem_tags=(), tags=(),
                       status_rules=()):
        self.order += 1
        params = {'name': name, 'algorithm': algorithm, 'sortorder': min(self.order, 999),
                  'problem_tags': list(problem_tags), 'tags': list(tags),
                  'status_rules': list(status_rules),
                  'parents': [{'serviceid': p} for p in parents]}
        found = self.api.call('service.get', {'output': ['serviceid'], 'filter': {'name': name}})
        if found:
            params['serviceid'] = found[0]['serviceid']
            self.api.call('service.update', params)
            return found[0]['serviceid']
        return self.api.call('service.create', params)['serviceids'][0]

    def component(self, az, azid, comp, prod_parents=()):
        kind = (self.cfg.get('kinds') or {}).get(comp) or KINDS.get(comp, 'cluster')
        base = '%s · %s' % (az['name'], LABELS.get(comp, comp))
        common = [ptag('impact', 'availability'), ptag('az', az['name'])]
        svc_tags = [stag('az', az['name']), stag('service', comp)]
        avail_tags = svc_tags + [stag('sla', '%s-avail' % comp), stag('sla_type', 'availability')]
        ctls = az.get('controllers') or []
        top_parents = [azid] + list(prod_parents)

        if kind == 'cluster' or (kind in ('pernode', 'quorum') and not ctls):
            extra = [ptag('component', 'rabbitmq')] if comp == 'rabbitmq' else []
            return self.ensure_service(base, top_parents,
                                       problem_tags=[ptag('service', comp)] + extra + common,
                                       tags=avail_tags)
        if kind in ('pernode', 'quorum'):
            rules = []
            if kind == 'quorum':
                q = int(az.get('quorum_fail', 2))
                # >= q nodes with Average or worse -> Disaster even if one node is still fine
                rules = [{'type': 0, 'limit_value': q, 'limit_status': 3, 'new_status': 5}]
            sid = self.ensure_service(base, top_parents, ALG_ALL, status_rules=rules,
                                      tags=avail_tags)
            for c in ctls:
                self.ensure_service('%s · %s' % (base, c['host']), [sid],
                                    problem_tags=[ptag('service', comp), ptag('node', c['host'])] + common)
            return sid

        if comp == 's3' and az.get('s3_zones'):
            return self.s3_zones(az, azid, base, svc_tags, avail_tags, common, prod_parents)

        # api / vip: 2 Services per OpenStack service (Availability + Response Time) under AZ
        sid = self.ensure_service(base, [azid], ALG_ANY, tags=svc_tags)
        avail_id = self.ensure_service(base + ' · Availability', [sid] + list(prod_parents), ALG_ANY,
                                       tags=avail_tags)
        self.ensure_service(base + ' · VIP', [avail_id],
                            problem_tags=[ptag('service', comp), ptag('target', 'vip')] + common)
        if kind == 'api':
            self.ensure_service(base + ' · L3 Cluster', [avail_id],
                                problem_tags=[ptag('service', comp), ptag('layer', 'L3')] + common)
        if kind == 'api' and ctls:
            # ALG_ALL: OK as long as running nodes > 0; triggers only if ALL nodes have problems
            grp = self.ensure_service(base + ' · controllers', [avail_id], ALG_ALL)
            for c in ctls:
                node = self.ensure_service('%s · %s' % (base, c['host']), [grp], ALG_ANY)
                self.ensure_service('%s · %s · API' % (base, c['host']), [node],
                                    problem_tags=[ptag('service', comp), ptag('target', c['ip'])] + common)
                self.ensure_service('%s · %s · services' % (base, c['host']), [node],
                                    problem_tags=[ptag('service', comp),
                                                  ptag('node', c.get('hostname') or c['host'], TAG_LIKE)]
                                    + common)

        # Response Time (> 5s for 3 consecutive rounds -> target=vip, impact=stability)
        resp_tags = svc_tags + [stag('sla', '%s-resp' % comp), stag('sla_type', 'response_time')]
        if comp == 'keystone':
            resp_id = self.ensure_service(base + ' · Response Time', [sid], ALG_ANY,
                                          tags=resp_tags)
            self.ensure_service(base + ' · VIP Latency', [resp_id],
                                problem_tags=[ptag('service', comp), ptag('target', 'vip'),
                                              ptag('impact', 'stability'), ptag('az', az['name'])])
            self.ensure_service(base + ' · Token Latency', [resp_id],
                                problem_tags=[ptag('service', comp), ptag('component', 'token'),
                                              ptag('impact', 'stability'), ptag('az', az['name'])])
        else:
            self.ensure_service(base + ' · Response Time', [sid], ALG_ANY,
                                problem_tags=[ptag('service', comp), ptag('target', 'vip'),
                                              ptag('impact', 'stability'), ptag('az', az['name'])],
                                tags=resp_tags)
        return avail_id

    def s3_zones(self, az, azid, base, svc_tags, avail_tags, common, prod_parents):
        """S3 with several RGW zones on one Zabbix host (os_probe s3_zones): the
        S3 Availability / Response Time services keep their SLA (any zone down)
        and get one child per zone with its own SLA. Probe rows carry target=<zone>."""
        sid = self.ensure_service(base, [azid], ALG_ANY, tags=svc_tags)
        avail_id = self.ensure_service(base + ' · Availability', [sid] + list(prod_parents),
                                       ALG_ANY, tags=avail_tags)
        resp_id = self.ensure_service(base + ' · Response Time', [sid], ALG_ANY,
                                      tags=svc_tags + [stag('sla', 's3-resp'),
                                                       stag('sla_type', 'response_time')])
        for z in az['s3_zones']:
            zl, zu = z.lower(), z.upper()
            ztags = svc_tags + [stag('s3_zone', zl)]
            self.ensure_service('%s · %s' % (base, zu), [avail_id],
                                problem_tags=[ptag('service', 's3'), ptag('target', zl)] + common,
                                tags=ztags + [stag('sla', 's3-%s-avail' % zl),
                                              stag('sla_type', 'availability')])
            self.ensure_service('%s · %s · Response Time' % (base, zu), [resp_id],
                                problem_tags=[ptag('service', 's3'), ptag('target', zl),
                                              ptag('impact', 'stability'), ptag('az', az['name'])],
                                tags=ztags + [stag('sla', 's3-%s-resp' % zl),
                                              stag('sla_type', 'response_time')])
        # single-endpoint leaf from before s3_zones was set
        old = self.api.call('service.get', {'output': ['serviceid'],
                                            'filter': {'name': base + ' · VIP'}})
        if old:
            self.api.call('service.delete', [o['serviceid'] for o in old])
        return avail_id

    def remove_legacy_overall(self, azn):
        """Delete retired Overall Services / SLAs (and retired Product bundles if removed)."""
        slas = [r['slaid'] for r in self.api.call(
            'sla.get', {'output': ['slaid', 'name'],
                        'search': {'name': 'SLA - %s - OpenStack Overall' % azn}})
                if r['name'].startswith('SLA - %s - OpenStack Overall - ' % azn)]
        svcs = [r['serviceid'] for r in self.api.call(
            'service.get', {'output': ['serviceid', 'name'],
                            'search': {'name': '%s · OpenStack Overall' % azn}})
                if r['name'] == '%s · OpenStack Overall' % azn
                or r['name'].startswith('%s · OpenStack Overall · ' % azn)]
        if not self.cfg.get('products'):
            for pname in ('Compute', 'Block Storage', 'Network', 'Load Balancer', 'Identity'):
                for r in self.api.call('sla.get', {'output': ['slaid'],
                                                   'filter': {'name': '%s %s' % (pname, azn)}}):
                    slas.append(r['slaid'])
                for r in self.api.call('service.get', {'output': ['serviceid'],
                                                       'filter': {'name': '%s · %s' % (azn, pname)}}):
                    svcs.append(r['serviceid'])
        if slas:
            self.api.call('sla.delete', slas)
        if svcs:
            self.api.call('service.delete', svcs)

    def build_services(self):
        root = self.ensure_service(self.cfg.get('root', 'OpenStack'))

        for az in self.cfg['azs']:
            azid = self.ensure_service('AZ-' + az['name'], [root])

            # Every service hangs directly under AZ-<name> and is judged on its own.
            prods, members = {}, {}
            for pname, p in (self.cfg.get('products') or {}).items():
                prods[pname] = self.ensure_service(
                    '%s · %s' % (az['name'], pname), [azid], ALG_ANY,
                    tags=[stag('sla', p['sla']), stag('az', az['name'])])
                for comp in p['components']:
                    members.setdefault(comp, []).append(prods[pname])

            for comp in self.cfg.get('services') or list(DEFAULT_SERVICES):
                if comp in LABELS:
                    members.setdefault(comp, [])

            for comp, prod_parents in members.items():
                self.component(az, azid, comp, prod_parents)

            self.remove_legacy_overall(az['name'])

    # ---- SLA ----
    def ensure_sla(self, name, slo, desc, service_tags, sc, eff, downs, ids):
        params = {'name': name, 'slo': str(slo),
                  'period': PERIODS[sc.get('period', 'monthly')],
                  'timezone': sc.get('timezone', 'Asia/Bangkok'),
                  'effective_date': eff, 'status': 1,
                  'description': desc,
                  'service_tags': service_tags,
                  'schedule': [], 'excluded_downtimes': downs}
        found = self.api.call('sla.get', {'output': ['slaid'], 'filter': {'name': name}})
        if found:
            params['slaid'] = found[0]['slaid']
            self.api.call('sla.update', params)
            ids[name] = found[0]['slaid']
        else:
            ids[name] = self.api.call('sla.create', params)['slaids'][0]

    def build_sla(self):
        sc = self.cfg.get('sla') or {}
        default_slo = sc.get('slo', 99.9)
        eff = calendar.timegm(time.strptime(sc.get('effective_date', '2026-01-01'), '%Y-%m-%d'))
        downs = [{'name': d['name'],
                  'period_from': calendar.timegm(time.strptime(d['from'], '%Y-%m-%d %H:%M')),
                  'period_to': calendar.timegm(time.strptime(d['to'], '%Y-%m-%d %H:%M'))}
                 for d in sc.get('excluded_downtimes') or []]
        ids = {}
        for az in self.cfg['azs']:
            azn = az['name']
            # 1) Product bundle SLAs (if configured)
            for pname, p in (self.cfg.get('products') or {}).items():
                name = '%s %s' % (pname, azn)
                self.ensure_sla(
                    name, p.get('slo', default_slo),
                    'Availability of %s in %s. Only impact=availability problems count.' % (pname, azn),
                    [ptag('sla', p['sla']), ptag('az', azn)], sc, eff, downs, ids)

            # 2) Per-service SLAs (Availability >= 5m, Response Time > 5s x 3 rounds, SLO 99.9%)
            seen = []
            for p in (self.cfg.get('products') or {}).values():
                for comp in p['components']:
                    if comp not in seen:
                        seen.append(comp)
            for comp in self.cfg.get('services') or list(DEFAULT_SERVICES):
                if comp in LABELS and comp not in seen:
                    seen.append(comp)

            for comp in seen:
                lb = LABELS.get(comp, comp)
                kind = (self.cfg.get('kinds') or {}).get(comp) or KINDS.get(comp, 'cluster')
                self.ensure_sla(
                    'SLA - %s - %s - Availability' % (azn, lb), default_slo,
                    '%s Availability in %s (down >= 5 minutes).' % (lb, azn),
                    [ptag('sla', '%s-avail' % comp), ptag('az', azn)], sc, eff, downs, ids)
                if kind in ('api', 'vip'):
                    self.ensure_sla(
                        'SLA - %s - %s - Response Time' % (azn, lb), default_slo,
                        '%s Response Time in %s (> 5s for 3 consecutive rounds).' % (lb, azn),
                        [ptag('sla', '%s-resp' % comp), ptag('az', azn)], sc, eff, downs, ids)
                if comp == 's3':
                    for z in az.get('s3_zones') or []:
                        zl, zu = z.lower(), z.upper()
                        self.ensure_sla(
                            'SLA - %s - S3 %s - Availability' % (azn, zu), default_slo,
                            'S3 zone %s Availability in %s (down >= 5 minutes).' % (zu, azn),
                            [ptag('sla', 's3-%s-avail' % zl), ptag('az', azn)], sc, eff, downs, ids)
                        self.ensure_sla(
                            'SLA - %s - S3 %s - Response Time' % (azn, zu), default_slo,
                            'S3 zone %s Response Time in %s (> 5s for 3 consecutive rounds).'
                            % (zu, azn),
                            [ptag('sla', 's3-%s-resp' % zl), ptag('az', azn)], sc, eff, downs, ids)
        return ids

    # ---- dashboards & honeycomb map ----
    def ensure_image(self, name, png_bytes):
        b64 = base64.b64encode(png_bytes).decode('ascii')
        found = self.api.call('image.get', {'output': ['imageid'], 'filter': {'name': name}})
        if found:
            iid = found[0]['imageid']
            self.api.call('image.update', {'imageid': iid, 'image': b64})
            return iid
        return self.api.call('image.create',
                             {'name': name, 'imagetype': 1, 'image': b64})['imageids'][0]

    def build_honeycomb_map(self, az, seen):
        """Create/update a Zabbix Network Map of Hexagon tiles (one per service)
        with the service name drawn inside: Green when UP, Red when DOWN."""
        azn = az['name']
        hnames = [az['zabbix_host']] + [c['host'] for c in az.get('controllers') or []]
        hosts = self.api.call('host.get', {'output': ['hostid'], 'filter': {'host': hnames}})
        hostids = [h['hostid'] for h in hosts]

        selements = []
        # Arrange tiles into 3 neat staggered honeycomb rows (e.g. 19 services -> 6, 7, 6)
        total = len(seen)
        base = total // 3
        rem = total % 3
        if rem == 1:
            r0, r1, r2 = base, base + 1, base
            off0, off1, off2 = True, False, True
        elif rem == 2:
            r0, r1, r2 = base + 1, base, base + 1
            off0, off1, off2 = False, True, False
        else:
            r0, r1, r2 = base, base, base
            off0, off1, off2 = False, True, False

        max_cols = max(r0, r1, r2)
        X0, Y0 = 32, 16
        Sx, Sy = 160, 92
        W, H = 156, 100

        for idx, comp in enumerate(seen):
            lb = LABELS.get(comp, comp)
            kind = (self.cfg.get('kinds') or {}).get(comp) or KINDS.get(comp, 'cluster')
            up_icon = self.ensure_image('os_hex_%s_up' % comp,
                                        make_hex_png(lb, 'UP', (26, 124, 17), (62, 184, 50), W, H))
            down_icon = self.ensure_image('os_hex_%s_down' % comp,
                                          make_hex_png(lb, 'DOWN', (214, 54, 56), (255, 112, 112), W, H))

            t_params = {
                'output': ['triggerid'],
                'selectTags': ['tag', 'value'],
                'filter': {'status': 0},
                'tags': [
                    {'tag': 'service', 'value': comp, 'operator': 1},
                    {'tag': 'impact', 'value': 'availability', 'operator': 1},
                    # never pick triggers of project-scope hosts (OS-API-Probe-Project)
                    {'tag': 'scope', 'value': 'project', 'operator': TAG_NOT_EQUAL},
                ],
            }
            if hostids:
                t_params['hostids'] = hostids
            trigs = self.api.call('trigger.get', t_params)
            if not trigs and hostids:
                t_params.pop('hostids', None)
                trigs = self.api.call('trigger.get', t_params)

            # For API/VIP services, prefer VIP + L3 cluster triggers so a single
            # controller node hiccup does not turn the whole service hexagon red
            if kind in ('api', 'vip') and trigs:
                vip_l3 = [t for t in trigs if any(
                    (tg.get('tag') == 'target' and tg.get('value') == 'vip')
                    or (tg.get('tag') == 'layer' and tg.get('value') == 'L3')
                    for tg in (t.get('tags') or []))]
                if vip_l3:
                    trigs = vip_l3

            if idx < r0:
                row = 0
                col = idx
                x = X0 + (80 if off0 else 0) + col * Sx
            elif idx < r0 + r1:
                row = 1
                col = idx - r0
                x = X0 + (80 if off1 else 0) + col * Sx
            else:
                row = 2
                col = idx - (r0 + r1)
                x = X0 + (80 if off2 else 0) + col * Sx
            y = Y0 + row * Sy

            if trigs:
                selements.append({
                    'selementid': str(idx + 1),
                    'elementtype': 2,
                    'elements': [{'triggerid': t['triggerid']} for t in trigs],
                    'iconid_off': up_icon,
                    'iconid_on': down_icon,
                    'iconid_disabled': up_icon,
                    'iconid_maintenance': up_icon,
                    'label': '',
                    'x': x,
                    'y': y,
                })
            else:
                selements.append({
                    'selementid': str(idx + 1),
                    'elementtype': 4,
                    'elements': [],
                    'iconid_off': up_icon,
                    'label': '',
                    'x': x,
                    'y': y,
                })

        mname = 'OpenStack · %s · Service Honeycomb' % azn
        mparams = {
            'name': mname,
            'width': X0 * 2 + max_cols * Sx,
            'height': Y0 * 2 + 2 * Sy + H,
            'label_type': 4,        # 4 = Nothing (service name is inside the hexagon)
            'highlight': 0,         # no square box around hexagons
            'markelements': 0,
            'expandproblem': 0,
            'selements': selements,
            'links': [],
        }
        found = self.api.call('map.get', {'output': ['sysmapid'], 'filter': {'name': mname}})
        if found:
            mparams['sysmapid'] = found[0]['sysmapid']
            self.api.call('map.update', mparams)
            return found[0]['sysmapid']
        return self.api.call('map.create', mparams)['sysmapids'][0]

    def ensure_dashboard(self, name, pages):
        params = {'name': name, 'display_period': 30, 'auto_start': 1, 'private': 0, 'pages': pages}
        found = self.api.call('dashboard.get', {'output': ['dashboardid'], 'filter': {'name': name}})
        if found:
            params['dashboardid'] = found[0]['dashboardid']
            self.api.call('dashboard.update', params)
        else:
            self.api.call('dashboard.create', params)

    def remove_dashboard(self, name):
        found = self.api.call('dashboard.get', {'output': ['dashboardid'], 'filter': {'name': name}})
        if found:
            self.api.call('dashboard.delete', [d['dashboardid'] for d in found])

    def remove_map(self, name):
        found = self.api.call('map.get', {'output': ['sysmapid'], 'filter': {'name': name}})
        if found:
            self.api.call('map.delete', [m['sysmapid'] for m in found])

    def service_page(self, comp, azs, sla_ids):
        lb = LABELS.get(comp, comp)
        kind = (self.cfg.get('kinds') or {}).get(comp) or KINDS.get(comp, 'cluster')
        az_hosts = [az['zabbix_host'] for az in azs]
        ctl_hosts = []
        for az in azs:
            for c in az.get('controllers') or []:
                if c['host'] not in ctl_hosts:
                    ctl_hosts.append(c['host'])
        ctl_or_any = ctl_hosts or ['*']

            # L4 Infrastructure dedicated sub-pages
        if comp == 'haproxy':
            ws = [
                widget('svggraph', 'HAProxy: backend state (1=Up, 0=Down)', 0, 0, 12, 6,
                       graph_ds([(az_hosts, ['HAProxy backend *: state'], '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'HAProxy: active backend servers', 12, 0, 12, 6,
                       graph_ds([(az_hosts, ['HAProxy backend *: active servers'], '199C0D', DRAW_STAIRCASE)])),
                widget('svggraph', 'HAProxy: per-server state (1=Up, 0=Down)', 0, 6, 12, 6,
                       graph_ds([(az_hosts, ['HAProxy */*: state'], '2774A4', DRAW_STAIRCASE)])),
                widget('svggraph', 'HAProxy: backend session usage (%)', 12, 6, 12, 6,
                       graph_ds([(az_hosts, ['HAProxy backend *: session usage'], 'F69100', DRAW_LINE)])),
            ]
            y = 12
        elif comp == 'rabbitmq':
            ws = [
                widget('svggraph', 'RabbitMQ: node state (1=Up, 0=Down) & cluster running nodes', 0, 0, 12, 6,
                       graph_ds([(az_hosts, ['RabbitMQ *: running'], '1A7C11', DRAW_STAIRCASE),
                                 (az_hosts, ['RabbitMQ: nodes running'], '2774A4', DRAW_STAIRCASE),
                                 (az_hosts, ['RabbitMQ: nodes total'], '8E44AD', DRAW_STAIRCASE)])),
                widget('svggraph', 'RabbitMQ: alarms & network partitions (0=OK, >0=Problem)', 12, 0, 12, 6,
                       graph_ds([(az_hosts, ['RabbitMQ *: memory alarm'], 'E45959', DRAW_STAIRCASE),
                                 (az_hosts, ['RabbitMQ *: disk alarm'], 'D46A00', DRAW_STAIRCASE),
                                 (az_hosts, ['RabbitMQ: network partitions'], '8E44AD', DRAW_STAIRCASE)])),
                widget('svggraph', 'RabbitMQ: connections & consumers', 0, 6, 12, 6,
                       graph_ds([(az_hosts, ['RabbitMQ: connections'], '2774A4', DRAW_LINE),
                                 (az_hosts, ['RabbitMQ: consumers'], '1A7C11', DRAW_LINE)])),
                widget('svggraph', 'RabbitMQ: messages ready & unacknowledged', 12, 6, 12, 6,
                       graph_ds([(az_hosts, ['RabbitMQ: messages'], '2774A4', DRAW_LINE),
                                 (az_hosts, ['RabbitMQ: messages unacknowledged'], 'E45959', DRAW_LINE)])),
            ]
            y = 12
        elif comp == 'mariadb':
            ws = [
                widget('svggraph', 'MariaDB & Galera: node state (1=Up, 0=Down)', 0, 0, 12, 6,
                       graph_ds([(ctl_or_any, ['MariaDB: ping', 'Galera: ready',
                                               'Galera: primary component', 'Galera: synced'],
                                  '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'Galera: cluster size per node', 12, 0, 12, 6,
                       graph_ds([(ctl_or_any, ['Galera: cluster size'], '2774A4', DRAW_STAIRCASE)])),
                widget('svggraph', 'Galera: flow control paused ratio & receive queue', 0, 6, 12, 6,
                       graph_ds([(ctl_or_any, ['Galera: flow control paused ratio',
                                               'Galera: local receive queue'], 'F69100', DRAW_LINE)])),
                widget('svggraph', 'MariaDB: threads connected vs max used', 12, 6, 12, 6,
                       graph_ds([(ctl_or_any, ['MariaDB: threads connected',
                                               'MariaDB: max used connections'], '2774A4', DRAW_LINE)])),
            ]
            y = 12
        elif comp == 'keepalived':
            ws = [
                widget('svggraph', 'keepalived: controllers holding VIP (1=Normal, 0=Down, >1=Split-brain)',
                       0, 0, 12, 6,
                       graph_ds([(az_hosts, ['keepalived: controllers holding * VIP'],
                                  '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'keepalived: VIP present per controller node (1=Yes, 0=No)',
                       12, 0, 12, 6,
                       graph_ds([(ctl_or_any, ['keepalived: * VIP on this node'],
                                  '2774A4', DRAW_STAIRCASE)])),
            ]
            y = 6
        elif comp == 'memcached':
            ws = [
                widget('svggraph', 'memcached: node state (1=Up, 0=Down)', 0, 0, 12, 6,
                       graph_ds([(ctl_or_any, ['memcached: ping'], '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'memcached: connections & hit ratio', 12, 0, 12, 6,
                       graph_ds([(ctl_or_any, ['memcached: current connections', 'memcached: hit ratio'],
                                  '2774A4', DRAW_LINE)])),
            ]
            y = 6
        elif comp == 'opensdn':
            ws = [
                widget('svggraph', 'OpenSDN: API HTTP status per node (config / analytics / control)',
                       0, 0, 12, 6,
                       graph_ds([(az_hosts, ['OpenSDN * @ *: HTTP status',
                                             'OpenSDN mid uve-*: HTTP status'], '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'OpenSDN: API response time (ms)', 12, 0, 12, 6,
                       graph_ds([(az_hosts, ['OpenSDN * @ *: response time',
                                             'OpenSDN mid uve-*: response time'], '2774A4', DRAW_LINE)])),
                widget('svggraph', 'OpenSDN: process state on nodes (1=Up, 0=Down)', 0, 6, 12, 6,
                       graph_ds([(az_hosts, ['OpenSDN * on *: state'], '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'OpenSDN: module instances up vs processes down', 12, 6, 12, 6,
                       graph_ds([(az_hosts, ['OpenSDN * (*): instances up'], '1A7C11', DRAW_STAIRCASE),
                                 (az_hosts, ['OpenSDN: processes down',
                                             'OpenSDN * on *: connections down'], 'E45959', DRAW_STAIRCASE)])),
            ]
            y = 12
        else:
            # OpenStack API / VIP service: 5-row diagnostic & resource pipeline layout
            api_status_pats = SERVICE_API_STATUS.get(comp) or ['%s *: HTTP status' % lb]
            api_ms_pats = SERVICE_API_MS.get(comp) or ['%s *: response time' % lb]

            if comp == 's3':
                ws = [
                    widget('svggraph', '%s: key API endpoints HTTP status (VIP vs nodes)' % lb,
                           0, 0, 12, 6,
                           graph_ds([(az_hosts, api_status_pats, '1A7C11', DRAW_STAIRCASE)])),
                    widget('svggraph', '%s: API response time (VIP vs nodes, ms)' % lb,
                           12, 0, 12, 6,
                           graph_ds([(az_hosts, api_ms_pats, '2774A4', DRAW_LINE)])),
                ]
                y = 6
            else:
                # Row 1: HAProxy Backend State (1=Up/0=Down + Active Servers) vs Key API HTTP Status
                ws = [
                    widget('svggraph', '%s: HAProxy backend state (1=Up, 0=Down) & active servers' % lb,
                           0, 0, 12, 6,
                           graph_ds([(az_hosts, ['HAProxy backend *%s*: state' % comp], '1A7C11', DRAW_STAIRCASE),
                                     (az_hosts, ['HAProxy backend *%s*: active servers' % comp],
                                      '2774A4', DRAW_STAIRCASE)])),
                    widget('svggraph', '%s: key API endpoints HTTP status (VIP vs nodes)' % lb,
                           12, 0, 12, 6,
                           graph_ds([(az_hosts, api_status_pats, '1A7C11', DRAW_STAIRCASE)])),
                ]

                # Row 2: Node / Daemon / Backend State (1=Up, 0=Down) vs API Response Time (ms)
                if comp == 'cinder':
                    node_ds = [(az_hosts, ['Cinder * on *: state', 'Cinder pool *: backend state'],
                                '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['Cinder pool *: free'], '2774A4', DRAW_LINE)]
                    node_title = 'Cinder: scheduler/volume/backup & storage pool state (1=Up, 0=Down)'
                elif comp == 'nova':
                    node_ds = [(az_hosts, ['Nova * on *: state', 'Hypervisor *: state'],
                                '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['Hypervisors: schedulable', 'Hypervisors: down'],
                                '2774A4', DRAW_STAIRCASE)]
                    node_title = 'Nova: scheduler/conductor/compute & hypervisor state (1=Up, 0=Down)'
                elif comp == 'glance':
                    node_ds = [(az_hosts, ['Glance: stores'], '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['Glance slow canary: HTTP status'], '2774A4', DRAW_STAIRCASE)]
                    node_title = 'Glance: active stores & canary image status'
                elif comp == 'neutron':
                    node_ds = [(az_hosts, ['OpenSDN * (*): instances up', 'OpenSDN * on *: state'],
                                '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['HAProxy *neutron*/*: state'], '2774A4', DRAW_STAIRCASE)]
                    node_title = 'Neutron / SDN: backend server & SDN module state (1=Up, 0=Down)'
                elif comp == 'octavia':
                    node_ds = [(ctl_or_any, ['Octavia health manager: UDP listening'],
                                '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['Octavia: spare amphorae'], '2774A4', DRAW_STAIRCASE)]
                    node_title = 'Octavia: health-manager UDP state (1=Up, 0=Down) & spare amphorae'
                elif comp == 'keystone':
                    node_ds = [(az_hosts, ['HAProxy *keystone*/*: state'], '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['Keystone: catalog services'], '2774A4', DRAW_STAIRCASE)]
                    node_title = 'Keystone: per-node backend state (1=Up, 0=Down) & catalog count'
                elif comp == 'placement':
                    node_ds = [(az_hosts, ['HAProxy *placement*/*: state'], '1A7C11', DRAW_STAIRCASE),
                               (az_hosts, ['Placement: root resource providers'], '2774A4', DRAW_STAIRCASE)]
                    node_title = 'Placement: per-node backend state (1=Up, 0=Down) & resource providers'
                else:
                    node_ds = [(az_hosts, ['%s * on *: state' % lb, 'HAProxy *%s*/*: state' % comp],
                                '1A7C11', DRAW_STAIRCASE)]
                    node_title = '%s: service daemons & per-node backend state (1=Up, 0=Down)' % lb

                ws += [
                    widget('svggraph', node_title, 0, 6, 12, 6, graph_ds(node_ds)),
                    widget('svggraph', '%s: API response time (VIP vs nodes, ms)' % lb, 12, 6, 12, 6,
                           graph_ds([(az_hosts, api_ms_pats, '2774A4', DRAW_LINE)])),
                ]
                y = 12

            # Row 3: Resource Pipeline Counts (Creating / Busy vs Stuck & Error)
            busy_pats = SERVICE_RES_BUSY.get(comp)
            bad_pats = SERVICE_RES_BAD.get(comp)
            if busy_pats or bad_pats:
                if busy_pats:
                    ws.append(widget('svggraph',
                                     '%s: resources creating / in-progress (count)' % lb,
                                     0, y, 12 if bad_pats else 24, 6,
                                     graph_ds([(az_hosts, busy_pats, '2774A4', DRAW_LINE)])))
                if bad_pats:
                    bad_ds = [(az_hosts, bad_pats[0], 'F69100', DRAW_STAIRCASE)]
                    if len(bad_pats) > 1 and bad_pats[1]:
                        bad_ds.append((az_hosts, bad_pats[1], 'E45959', DRAW_STAIRCASE))
                    ws.append(widget('svggraph',
                                     '%s: resources stuck & error / failed (count)' % lb,
                                     12 if busy_pats else 0, y, 12 if busy_pats else 24, 6,
                                     graph_ds(bad_ds)))
                y += 6

            # Nova Capacity Row (merged from retired OpenStack · Capacity dashboard)
            if comp == 'nova':
                ws += [
                    widget('svggraph', 'Nova: Hypervisor vCPU allocated (%)', 0, y, 12, 6,
                           graph_fields(az_hosts, 'Hypervisor * vCPU allocated')),
                    widget('svggraph', 'Nova: Hypervisor memory allocated (%)', 12, y, 12, 6,
                           graph_fields(az_hosts, 'Hypervisor * memory allocated')),
                    widget('svggraph', 'Nova: Cluster vCPU & running VMs', 0, y + 6, 12, 6,
                           graph_ds([(az_hosts, ['Cluster: vCPU total', 'Cluster: vCPU allocated'],
                                      '2774A4', DRAW_LINE),
                                     (az_hosts, ['Cluster: running VMs'], '1A7C11', DRAW_LINE)])),
                    widget('svggraph', 'Nova: Cluster memory allocated vs total', 12, y + 6, 12, 6,
                           graph_ds([(az_hosts, ['Cluster: memory total', 'Cluster: memory allocated'],
                                      'F69100', DRAW_LINE)])),
                ]
                y += 12

        # Row 4: Active Problems for this service
        ws.append(widget('problems', '%s: problems (root cause & layer tags)' % lb, 0, y, 24, 6,
                         problem_fields([('service', comp)], operator=1)))
        y += 6

        # Row 5: SLA Reports for this service (Availability + Response Time)
        sx = 0
        for az in azs:
            for suffix in ('Availability', 'Response Time'):
                sname = 'SLA - %s - %s - %s' % (az['name'], lb, suffix)
                sid = sla_ids.get(sname)
                if sid is None:
                    continue
                w = 12 if kind in ('api', 'vip') else 24
                ws.append(widget('slareport', sname, sx, y, w, 5, [fld(10, 'slaid', sid)]))
                sx += w
                if sx >= 24:
                    sx, y = 0, y + 5

        return {'name': lb, 'widgets': ws}

    def build_dashboards(self, sla_ids):
        azs, prods = self.cfg['azs'], self.cfg.get('products') or {}
        az_hosts = [az['zabbix_host'] for az in azs]

        # Collect ordered list of all configured services (Tier 1 -> Tier 2 -> Tier 3)
        seen = []
        for comp in self.cfg.get('services') or list(DEFAULT_SERVICES):
            if comp in LABELS and comp not in seen:
                seen.append(comp)
        for p in prods.values():
            for comp in p['components']:
                if comp in LABELS and comp not in seen:
                    seen.append(comp)

        # 1) Main Dashboard: OpenStack · SystemScope (Master Overview & Control Plane per AZ)
        ov_pages = []
        for az in azs:
            azn = az['name']
            hn = [az['zabbix_host']]
            ctl = [c['host'] for c in az.get('controllers') or []]
            hnames = hn + ctl
            hosts = self.api.call('host.get', {'output': ['hostid'], 'filter': {'host': hnames}})
            hostids = [h['hostid'] for h in hosts] if hosts else []

            map_id = self.build_honeycomb_map(az, seen)

            ws = [
                # Row 1: Honeycomb Service Status Map (Green=UP / Red=DOWN)
                widget('map', '%s: Service Status Honeycomb (Green=UP / Red=DOWN)' % azn,
                       0, 0, 24, 8,
                       map_fields(map_id)),
            ]
            ty = 8

            # Row 2: Historical Up/Down status @ VIP + API response time
            ws += [
                widget('svggraph', '%s: API HTTP status via VIP (200/300=Up, 0/5xx=Down)' % azn,
                       0, ty, 12, 6,
                       graph_ds([(hn, ['* root: HTTP status', '* root @ vip: HTTP status',
                                       'Horizon login: HTTP status', 'Horizon login @ vip: HTTP status',
                                       'Keystone: token request status'], '1A7C11', DRAW_STAIRCASE)])),
                widget('svggraph', 'API response time',
                       12, ty, 12, 6,
                       graph_ds([
                           (hn, ['Keystone root: response time', 'Keystone catalog: response time'], '2774A4', DRAW_LINE),
                           (hn, ['Nova root: response time', 'Nova novnc: response time', 'Nova os-services: response time'], '1A7C11', DRAW_LINE),
                           (hn, ['Placement root: response time'], '00897B', DRAW_LINE),
                           (hn, ['Glance root: response time', 'Glance images: response time', 'Glance stores: response time'], 'D46A00', DRAW_LINE),
                           (hn, ['Cinder root: response time', 'Cinder os-services: response time'], '8E44AD', DRAW_LINE),
                           (hn, ['Neutron root: response time', 'Neutron networks: response time'], 'E45959', DRAW_LINE),
                           (hn, ['Octavia root: response time', 'Octavia providers: response time'], 'C2185B', DRAW_LINE),
                           (hn, ['Horizon login: response time'], '3949AB', DRAW_LINE),
                           (hn, ['Heat root: response time', 'Heat services: response time',
                                 'Magnum root: response time', 'Magnum mservices: response time',
                                 'Barbican root: response time', 'Trove root: response time',
                                 'Trove datastores: response time', 'Manila root: response time',
                                 'Manila services: response time', 'Ironic root: response time',
                                 'Ironic conductors: response time', 'S3 *: response time',
                                 '* @ vip: response time'], '5D4037', DRAW_LINE),
                       ])),
            ]
            ty += 6

            # Row 3: API checks down (0 or 5xx) + Keystone auth request time
            ws += [
                widget('svggraph', 'API checks down (0 or 5xx)', 0, ty, 12, 6,
                       graph_ds([(hn, ['Probe fast: checks down (0 or 5xx)'], 'E45959', DRAW_LINE),
                                 (hn, ['Probe mid: checks down (0 or 5xx)'], 'D46A00', DRAW_LINE),
                                 (hn, ['Probe slow: checks down (0 or 5xx)'], '8E44AD', DRAW_LINE)])),
                widget('svggraph', 'auth request time', 12, ty, 12, 6,
                       graph_ds([(hn, ['Keystone: token request time'], '2774A4', DRAW_LINE)])),
            ]
            ty += 6

            # Row 4: RabbitMQ messages + Probe runtime
            ws += [
                widget('svggraph', 'RabbitMQ messages', 0, ty, 12, 6,
                       graph_ds([(hn, ['RabbitMQ: messages'], '2774A4', DRAW_LINE),
                                 (hn, ['RabbitMQ: messages unacknowledged'], 'E45959', DRAW_LINE)])),
                widget('svggraph', 'probe runtime', 12, ty, 12, 6,
                       graph_ds([(hn, ['Probe fast: total runtime'], '1A7C11', DRAW_LINE),
                                 (hn, ['Probe mid: total runtime'], '2774A4', DRAW_LINE),
                                 (hn, ['Probe slow: total runtime'], '8E44AD', DRAW_LINE)])),
            ]
            ty += 6

            # Row 5: Active Problems in this AZ
            ws.append(widget('problems', '%s: active problems (all services & layers)' % azn,
                             0, ty, 24, 6,
                             problem_fields([('az', azn)], operator=1)))
            ov_name = '%s Overview' % azn if len(azs) > 1 else 'Overview'
            ov_pages.append({'name': ov_name, 'widgets': ws})

        # 2) Tab 2: Availability SLA
        avail_ws, x, y = [], 0, 0
        for az in azs:
            for pname in prods:
                sid = sla_ids.get('%s %s' % (pname, az['name']))
                if sid is not None:
                    avail_ws.append(widget('slareport', 'SLA %s %s' % (pname, az['name']), x, y, 8, 4,
                                           [fld(10, 'slaid', sid)]))
                    x += 8
                    if x >= 24:
                        x, y = 0, y + 4
        if x:
            x, y = 0, y + 4

        for az in azs:
            for comp in seen:
                lb = LABELS.get(comp, comp)
                sname = 'SLA - %s - %s - Availability' % (az['name'], lb)
                sid = sla_ids.get(sname)
                if sid is None:
                    continue
                avail_ws.append(widget('slareport', sname, x, y, 8, 4, [fld(10, 'slaid', sid)]))
                x += 8
                if x >= 24:
                    x, y = 0, y + 4
        if x:
            x, y = 0, y + 4

        avail_ws.append(widget('problems', 'Outages (impact=availability)', 0, y, 24, 6,
                               problem_fields([('impact', 'availability')])))
        avail_page = {'name': 'Availability SLA', 'widgets': avail_ws}

        # 3) Tab 3: Response SLA
        resp_ws, x, y = [], 0, 0
        for az in azs:
            for comp in seen:
                lb = LABELS.get(comp, comp)
                sname = 'SLA - %s - %s - Response Time' % (az['name'], lb)
                sid = sla_ids.get(sname)
                if sid is None:
                    continue
                resp_ws.append(widget('slareport', sname, x, y, 8, 4, [fld(10, 'slaid', sid)]))
                x += 8
                if x >= 24:
                    x, y = 0, y + 4
        if x:
            x, y = 0, y + 4

        resp_ws.append(widget('problems', 'Latency / Stability Problems (impact=stability)', 0, y, 24, 6,
                              problem_fields([('impact', 'stability')])))
        resp_page = {'name': 'Response SLA', 'widgets': resp_ws}

        # 4) Tab 4+: Per-Service Drill-Down Pages (excluding memcached and keepalived)
        svc_pages = [self.service_page(comp, azs, sla_ids) for comp in seen
                     if comp not in ('memcached', 'keepalived')]

        # Unified System-Scope Dashboard
        all_pages = ov_pages + [avail_page, resp_page] + svc_pages

        # Remove standalone / legacy dashboards merged into OpenStack · SystemScope
        self.remove_dashboard('OpenStack · Service Status & SLA')
        self.remove_dashboard('OpenStack · Executive SLA')
        self.remove_dashboard('OpenStack · Control plane')
        self.remove_dashboard('OpenStack · Capacity')
        self.remove_dashboard('OpenStack · Per service')
        self.remove_dashboard('Openstack · SystemScope')

        # Ensure unified OpenStack · SystemScope dashboard
        self.ensure_dashboard('OpenStack · SystemScope', all_pages)


DRAW_LINE, DRAW_POINTS, DRAW_STAIRCASE, DRAW_BAR = 0, 1, 2, 3

# Key API status patterns per service (fast VIP/node checks + key domain endpoints)
SERVICE_API_STATUS = {
    'keystone': ['Keystone: token request status', 'Keystone *: HTTP status'],
    'nova': ['Nova *: HTTP status'],
    'cinder': ['Cinder *: HTTP status'],
    'glance': ['Glance *: HTTP status'],
    'neutron': ['Neutron *: HTTP status'],
    'octavia': ['Octavia *: HTTP status'],
    'placement': ['Placement *: HTTP status'],
    'heat': ['Heat *: HTTP status'],
    'magnum': ['Magnum *: HTTP status'],
    'trove': ['Trove *: HTTP status'],
    'manila': ['Manila *: HTTP status'],
    'ironic': ['Ironic *: HTTP status'],
}

SERVICE_API_MS = {
    'keystone': ['Keystone: token request time', 'Keystone *: response time'],
    'cinder': ['Cinder *: response time'],
    'nova': ['Nova *: response time'],
    'glance': ['Glance *: response time'],
    'neutron': ['Neutron *: response time'],
    'octavia': ['Octavia *: response time'],
}

# Resource pipeline counts per service: in-progress/busy vs ([stuck], [error/failed])
SERVICE_RES_BUSY = {
    'cinder': ['Cinder: volumes_creating', 'Cinder: volumes_attaching', 'Cinder: volumes_detaching',
               'Cinder: volumes_deleting', 'Cinder: volumes_extending',
               'Cinder: snapshots_creating', 'Cinder: snapshots_deleting',
               'Cinder: backups_creating', 'Cinder: backups_restoring', 'Cinder: backups_deleting'],
    'nova': ['Nova: servers_build', 'Nova: servers_deleting', 'Nova: servers_migrating',
             'Nova: servers_resize'],
    'glance': ['Glance: images_queued', 'Glance: images_saving', 'Glance: images_importing',
               'Glance: images_deleting'],
    'octavia': ['Octavia: lb_pending_create', 'Octavia: lb_pending_update',
                'Octavia: lb_pending_delete', 'Octavia: amphorae_booting',
                'Octavia: amphorae_deleting', 'Octavia: spare amphorae'],
    'heat': ['Heat: stacks_creating', 'Heat: stacks_updating', 'Heat: stacks_deleting'],
    'magnum': ['Magnum: clusters_creating', 'Magnum: clusters_updating', 'Magnum: clusters_deleting'],
    'trove': ['Trove: instances_build', 'Trove: instances_deleting', 'Trove: instances_resize'],
    'manila': ['Manila: shares_creating', 'Manila: shares_deleting', 'Manila: shares_extending'],
    'ironic': ['Ironic: nodes_deploying', 'Ironic: nodes_cleaning', 'Ironic: nodes_wait_callback'],
    'keystone': ['Keystone: catalog services', 'Probe credential: days until expiry'],
    'placement': ['Placement: root resource providers'],
}

SERVICE_RES_BAD = {
    'cinder': (['Cinder: *_stuck'],
               ['Cinder: volumes_error*', 'Cinder: snapshots_error*', 'Cinder: backups_error']),
    'nova': (['Nova: *_stuck'], ['Nova: servers_error']),
    'glance': (['Glance: *_stuck'], []),
    'octavia': (['Octavia: *_stuck'],
                ['Octavia: lb_provisioning_error', 'Octavia: lb_operating_error',
                 'Octavia: amphorae_error']),
    'heat': (['Heat: *_stuck'], ['Heat: stacks_failed']),
    'magnum': (['Magnum: *_stuck'], ['Magnum: clusters_failed']),
    'trove': (['Trove: *_stuck'], ['Trove: instances_error']),
    'manila': (['Manila: *_stuck'], ['Manila: shares_error']),
    'ironic': (['Ironic: *_stuck'],
               ['Ironic: nodes_error', 'Ironic: nodes_deploy_failed', 'Ironic: nodes_clean_failed']),
}


def fld(typ, name, value):
    return {'type': typ, 'name': name, 'value': value}


def widget(typ, name, x, y, w, h, fields):
    return {'type': typ, 'name': name, 'x': x, 'y': y, 'width': w, 'height': h,
            'view_mode': 0, 'fields': fields}


def tag_filter(tag_pairs, operator):
    """Widget tag filter fields. Every cloud widget also gets scope <> project, so
    problems of project-scope hosts (OS-API-Probe-Project) never show up here."""
    rows = [(t, v, operator) for t, v in tag_pairs] + [('scope', 'project', TAG_NOT_EQUAL)]
    f = []
    for i, (t, v, op) in enumerate(rows):
        f += [fld(1, 'tags.tag.%d' % i, t), fld(0, 'tags.operator.%d' % i, op),
              fld(1, 'tags.value.%d' % i, v)]
    return f


def problem_fields(tag_pairs, operator=1):
    """operator 0 = contains, 1 = equals (Problems widget tag filter)."""
    f = [fld(0, 'show', 3), fld(0, 'show_tags', 3), fld(0, 'show_opdata', 1), fld(0, 'evaltype', 0)]
    return f + tag_filter(tag_pairs, operator)


def map_fields(map_id):
    return [fld(0, 'source_type', 1), fld(8, 'sysmapid', int(map_id))]


def trigover_fields(tag_pairs, operator=1, show=2, style=0, hostids=None):
    """Trigger overview widget: show=2 (Any -> Green=OK/Up, Red=Problem/Down)."""
    f = [fld(0, 'show', show), fld(0, 'style', style), fld(0, 'evaltype', 0)]
    if hostids:
        f += [fld(3, 'hostids.%d' % i, hid) for i, hid in enumerate(hostids)]
    return f + tag_filter(tag_pairs, operator)


def graph_ds(datasets):
    """Build Zabbix 6.0 svggraph dataset fields.
    Each dataset tuple is (hosts, item_patterns, color_hex, draw_type)."""
    f = []
    for k, (hosts, items, color, draw_type) in enumerate(datasets):
        if isinstance(items, str):
            items = [items]
        f += [fld(1, 'ds.hosts.%d.%d' % (k, i), h) for i, h in enumerate(hosts)]
        f += [fld(1, 'ds.items.%d.%d' % (k, j), it) for j, it in enumerate(items)]
        f += [fld(1, 'ds.color.%d' % k, color),
              fld(0, 'ds.type.%d' % k, draw_type),
              fld(0, 'ds.transparency.%d' % k, 0),
              fld(0, 'ds.fill.%d' % k, 0)]
    return f


def graph_fields(hosts, item_pattern):
    return graph_ds([(hosts, [item_pattern], '1A7C11', DRAW_LINE)])


FONT_5X7 = {
    'A': [0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'B': [0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E],
    'C': [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
    'D': [0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C],
    'E': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
    'F': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10],
    'G': [0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F],
    'H': [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'I': [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    'J': [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C],
    'K': [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
    'L': [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
    'M': [0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11],
    'N': [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
    'O': [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'P': [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
    'Q': [0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D],
    'R': [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
    'S': [0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E],
    'T': [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    'U': [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'V': [0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04],
    'W': [0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11],
    'X': [0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11],
    'Y': [0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04],
    'Z': [0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F],
    '0': [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],
    '1': [0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E],
    '2': [0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F],
    '3': [0x1E, 0x01, 0x01, 0x0E, 0x01, 0x01, 0x1E],
    '4': [0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02],
    '5': [0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E],
    '6': [0x0E, 0x11, 0x10, 0x1E, 0x11, 0x11, 0x0E],
    '7': [0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
    '8': [0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E],
    '9': [0x0E, 0x11, 0x11, 0x0F, 0x01, 0x11, 0x0E],
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    '-': [0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00],
}


def make_hex_png(label, sublabel, fill_rgb, border_rgb, w=156, h=100):
    """Generate an anti-aliased RGBA PNG of a flat-topped Hexagon tile
    with centered service label & status text using 2x SSAA downsampling."""
    sw, sh = w * 2, h * 2
    sbuf = bytearray(sw * sh * 4)
    cx, cy = sw / 2.0, sh / 2.0
    rx, ry = sw / 2.0 - 7.0, sh / 2.0 - 7.0

    for y in range(sh):
        dy = abs(y - cy) / ry
        row_off = y * sw * 4
        for x in range(sw):
            dx = abs(x - cx) / rx
            d = max(dy, dx + 0.5 * dy)
            if d <= 1.0:
                c = border_rgb if d > 0.89 else fill_rgb
                shade = 0.93 + 0.14 * (1.0 - y / sh)
                idx = row_off + x * 4
                sbuf[idx] = min(255, int(c[0] * shade))
                sbuf[idx + 1] = min(255, int(c[1] * shade))
                sbuf[idx + 2] = min(255, int(c[2] * shade))
                sbuf[idx + 3] = 255

    def draw_text(text, cy_pos, scale, color):
        text = text.upper()
        char_w = 5 * scale
        gap = scale
        total_w = len(text) * char_w + max(0, len(text) - 1) * gap
        sx = int(cx - total_w / 2)
        sy = int(cy_pos - (7 * scale) / 2)
        r_c, g_c, b_c, a_c = color
        sa = a_c / 255.0
        for ci, ch in enumerate(text):
            glyph = FONT_5X7.get(ch, FONT_5X7[' '])
            ox = sx + ci * (char_w + gap)
            for r in range(7):
                row_bits = glyph[r]
                for c in range(5):
                    if row_bits & (1 << (4 - c)):
                        for dy in range(scale):
                            py = sy + r * scale + dy
                            if 0 <= py < sh:
                                row_off = py * sw * 4
                                for dx in range(scale):
                                    px = ox + c * scale + dx
                                    if 0 <= px < sw:
                                        idx = row_off + px * 4
                                        da = sbuf[idx + 3] / 255.0
                                        out_a = sa + da * (1.0 - sa)
                                        if out_a > 0:
                                            sbuf[idx] = min(255, max(0, int((r_c * sa + sbuf[idx] * da * (1.0 - sa)) / out_a)))
                                            sbuf[idx + 1] = min(255, max(0, int((g_c * sa + sbuf[idx + 1] * da * (1.0 - sa)) / out_a)))
                                            sbuf[idx + 2] = min(255, max(0, int((b_c * sa + sbuf[idx + 2] * da * (1.0 - sa)) / out_a)))
                                            sbuf[idx + 3] = min(255, max(0, int(out_a * 255)))

    # Adaptive scale on 2x canvas (scale 4 downsamples to crisp 2x, scale 3 for longer words)
    scale = 3 if len(label) > 8 else 4
    draw_text(label, int(sh * 0.40) + 2, scale, (0, 0, 0, 160))
    draw_text(label, int(sh * 0.40), scale, (255, 255, 255, 255))
    draw_text(sublabel, int(sh * 0.72) + 2, 4, (0, 0, 0, 160))
    draw_text(sublabel, int(sh * 0.72), 4, (255, 255, 255, 240))

    # Downsample 2x2 box filter to target size (w, h)
    out = bytearray(w * h * 4)
    for y in range(h):
        y2 = y * 2
        r0 = y2 * sw * 4
        r1 = (y2 + 1) * sw * 4
        out_r = y * w * 4
        for x in range(w):
            x2 = x * 8
            idx = out_r + x * 4
            out[idx] = (sbuf[r0 + x2] + sbuf[r0 + x2 + 4] + sbuf[r1 + x2] + sbuf[r1 + x2 + 4]) >> 2
            out[idx + 1] = (sbuf[r0 + x2 + 1] + sbuf[r0 + x2 + 5] + sbuf[r1 + x2 + 1] + sbuf[r1 + x2 + 5]) >> 2
            out[idx + 2] = (sbuf[r0 + x2 + 2] + sbuf[r0 + x2 + 6] + sbuf[r1 + x2 + 2] + sbuf[r1 + x2 + 6]) >> 2
            out[idx + 3] = (sbuf[r0 + x2 + 3] + sbuf[r0 + x2 + 7] + sbuf[r1 + x2 + 3] + sbuf[r1 + x2 + 7]) >> 2

    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(out[y * w * 4:(y + 1) * w * 4])

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', ihdr) +
            chunk(b'IDAT', zlib.compress(bytes(raw), 9)) +
            chunk(b'IEND', b''))


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description='Services / SLA / dashboards for OpenStack (Zabbix 6.0)')
    ap.add_argument('-c', '--config', required=True)
    ap.add_argument('--only', default='services,sla,dashboards',
                    help='comma list of: services, sla, dashboards')
    ap.add_argument('--dry-run', action='store_true', help='print, do not call the API')
    ap.add_argument('--insecure', action='store_true', help='do not verify the API TLS cert')
    args = ap.parse_args()
    with open(args.config) as fh:
        cfg = json.load(fh)
    raw_only = set(x.strip().lower() for x in args.only.split(',') if x.strip())
    alias = {'service': 'services', 'dashboard': 'dashboards', 'slas': 'sla'}
    only = set(alias.get(x, x) for x in raw_only)
    unknown = only - {'services', 'sla', 'dashboards'}
    if unknown:
        sys.stderr.write('Unknown --only value(s): %s (expected: services, sla, dashboards)\n'
                         % ', '.join(sorted(unknown)))
        return 2

    if args.dry_run:
        api = DryApi()
    else:
        z = cfg.get('zabbix') or {}
        api = Api(z['url'], os.environ.get('ZBX_TOKEN', ''), os.environ.get('ZBX_USER', ''),
                  os.environ.get('ZBX_PASSWORD', ''), not args.insecure)
    b = Builder(api, cfg)
    sla_ids = {}
    try:
        if 'services' in only:
            print('== services')
            b.build_services()
        if 'sla' in only or 'dashboards' in only:
            print('== sla')
            sla_ids = b.build_sla()
        if 'dashboards' in only:
            print('== dashboards')
            b.build_dashboards(sla_ids)
    except ApiError as e:
        sys.stderr.write('API error: %s\n' % e)
        return 1
    if args.dry_run:
        print('calls:', json.dumps(api.counts, sort_keys=True))
    print('done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
