#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
os_zabbix_setup_project.py - Services, SLAs and a dashboard for project-scope hosts
(template OS-API-Probe-Project) in Zabbix 6.0, through the API. Idempotent: objects
are found by name and updated, so it is safe to re-run.

    export ZBX_TOKEN=<API token>            # or ZBX_USER / ZBX_PASSWORD
    python3 os_zabbix_setup_project.py -c deploy/os_zabbix_setup_project.example.json --dry-run
    python3 os_zabbix_setup_project.py -c /etc/zabbix/os_zabbix_setup_project.json
    python3 os_zabbix_setup_project.py -c ... --only services,sla

Services tree (one branch per project, separate root from the cloud tree):

  <root>                                      tags scope=project
    Project <name>                            tags scope=project, project=<name>
      Project <name> · Nova · Availability    problem tags scope=project project=<name>
                                                           service=nova impact=availability
      Project <name> · Nova · Response Time   ... service=nova impact=stability target=vip

Separation from the cloud objects of os_zabbix_setup.py:
  - every Service, SLA and dashboard filter here matches scope=project + project=<name>
  - cloud leaves match az=<AZ>; project hosts have no az tag, so their problems never
    reach a cloud Service / SLA
  - names start with "Project " / "OpenStack · Projects", so a name lookup never finds
    a cloud object

Host tags needed on every project host: scope=project, project=<name>.

Python 3.6+, standard library only.
"""

import argparse
import base64
import calendar
import json
import os
import re
import struct
import sys
import time
import urllib.request
import zlib

PERIODS = {'daily': 0, 'weekly': 1, 'monthly': 2, 'quarterly': 3, 'annually': 4}
ALG_ANY = 2                     # most critical of child services
TAG_EQUALS = 0
SCOPE = 'project'
DRAW_LINE, DRAW_POINTS, DRAW_STAIRCASE, DRAW_BAR = 0, 1, 2, 3

LABELS = {
    'keystone': 'Keystone', 'nova': 'Nova', 'placement': 'Placement', 'neutron': 'Neutron',
    'glance': 'Glance', 'cinder': 'Cinder', 'heat': 'Heat', 'octavia': 'Octavia',
    'barbican': 'Barbican', 'trove': 'Trove', 'magnum': 'Magnum', 'ironic': 'Ironic',
    'manila': 'Manila', 'designate': 'Designate', 'swift': 'Swift', 'aodh': 'Aodh',
    'gnocchi': 'Gnocchi',
}
# Only list services that are in this cloud's catalog: a Service with no probe rows
# stays green and its SLA reads 100%. Placement has no API a project member may call.
DEFAULT_SERVICES = ['keystone', 'nova', 'cinder', 'neutron', 'glance', 'heat', 'octavia']


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
            if method == 'host.get':
                hosts = params.get('filter', {}).get('host', [])
                return [{'hostid': str(10000 + i), 'host': h} for i, h in enumerate(hosts)]
            return []
        self.n += 1
        obj = method.split('.')[0]
        name = params.get('name', '')
        if obj == 'service':
            pt = ' '.join('%s=%s' % (t['tag'], t['value']) for t in params.get('problem_tags') or [])
            print('  service  %-52s%s' % (name, ('  [' + pt + ']') if pt else ''))
        elif obj == 'sla':
            st = ' '.join('%s=%s' % (t['tag'], t['value']) for t in params.get('service_tags') or [])
            print('  sla      %-52s  {%s}' % (name, st))
        elif obj == 'dashboard':
            pages = params.get('pages') or []
            pinfo = ', '.join('%s (%dw)' % (p['name'], len(p.get('widgets') or [])) for p in pages)
            print('  %-8s %-36s [%d pages: %s]' % (obj, name, len(pages), pinfo))
        elif obj == 'map':
            print('  map      %-52s  [%d hexagons]' % (name, len(params.get('selements') or [])))
        elif obj == 'image':
            pass
        else:
            print('  %-8s %s' % (obj, name))
        return {obj + 'ids': [str(self.n)], 'sysmapids': [str(self.n)]}


def ptag(tag, value):
    return {'tag': tag, 'operator': TAG_EQUALS, 'value': value}


def stag(tag, value):
    return {'tag': tag, 'value': value}


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------
class Builder(object):
    def __init__(self, api, cfg):
        self.api, self.cfg = api, cfg
        self.order = 0
        self._map_cache = {}

    def projects(self):
        out = []
        for p in self.cfg.get('projects') or []:
            if not p.get('name') or not p.get('zabbix_host'):
                raise SystemExit('every project needs "name" and "zabbix_host"')
            svcs = [s for s in (p.get('services') or DEFAULT_SERVICES) if s in LABELS]
            out.append(dict(p, services=svcs))
        return out

    def ensure_service(self, name, parents=(), problem_tags=(), tags=()):
        self.order += 1
        params = {'name': name, 'algorithm': ALG_ANY, 'sortorder': min(self.order, 999),
                  'problem_tags': list(problem_tags), 'tags': list(tags), 'status_rules': [],
                  'parents': [{'serviceid': p} for p in parents]}
        found = self.api.call('service.get', {'output': ['serviceid'], 'filter': {'name': name}})
        if found:
            params['serviceid'] = found[0]['serviceid']
            self.api.call('service.update', params)
            return found[0]['serviceid']
        return self.api.call('service.create', params)['serviceids'][0]

    def build_services(self):
        root = self.ensure_service(self.cfg.get('root', 'OpenStack Projects'),
                                   tags=[stag('scope', SCOPE)])
        for p in self.projects():
            pn = p['name']
            base_tags = [stag('scope', SCOPE), stag('project', pn)]
            pid = self.ensure_service('Project %s' % pn, [root], tags=base_tags)
            common = [ptag('scope', SCOPE), ptag('project', pn)]
            for comp in p['services']:
                lb = LABELS[comp]
                svc_tags = base_tags + [stag('service', comp)]
                self.ensure_service(
                    'Project %s · %s · Availability' % (pn, lb), [pid],
                    problem_tags=common + [ptag('service', comp), ptag('impact', 'availability')],
                    tags=svc_tags + [stag('sla', '%s-avail' % comp), stag('sla_type', 'availability')])
                self.ensure_service(
                    'Project %s · %s · Response Time' % (pn, lb), [pid],
                    problem_tags=common + [ptag('service', comp), ptag('impact', 'stability'),
                                           ptag('target', 'vip')],
                    tags=svc_tags + [stag('sla', '%s-resp' % comp), stag('sla_type', 'response_time')])

    # ---- SLA ----
    def ensure_sla(self, name, slo, desc, service_tags, sc, eff, downs, ids):
        params = {'name': name, 'slo': str(slo),
                  'period': PERIODS[sc.get('period', 'monthly')],
                  'timezone': sc.get('timezone', 'Asia/Bangkok'),
                  'effective_date': eff, 'status': 1, 'description': desc,
                  'service_tags': service_tags, 'schedule': [], 'excluded_downtimes': downs}
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
        for p in self.projects():
            pn = p['name']
            slo = p.get('slo', default_slo)
            for comp in p['services']:
                lb = LABELS[comp]
                for kind, suffix, what in (('avail', 'Availability', 'down >= 5 minutes'),
                                           ('resp', 'Response Time', '> 5s for 3 rounds')):
                    self.ensure_sla(
                        'SLA - Project %s - %s - %s' % (pn, lb, suffix), slo,
                        '%s %s seen from project %s (%s).' % (lb, suffix, pn, what),
                        [ptag('scope', SCOPE), ptag('project', pn), ptag('sla', '%s-%s' % (comp, kind))],
                        sc, eff, downs, ids)
        return ids

    # ---- honeycomb maps (Network Map of hexagon images, like the cloud one) ----
    def ensure_image(self, name, png_bytes):
        b64 = base64.b64encode(png_bytes).decode('ascii')
        found = self.api.call('image.get', {'output': ['imageid'], 'filter': {'name': name}})
        if found:
            self.api.call('image.update', {'imageid': found[0]['imageid'], 'image': b64})
            return found[0]['imageid']
        return self.api.call('image.create', {'name': name, 'imagetype': 1, 'image': b64})['imageids'][0]

    def avail_triggers(self, host, comp=None):
        """Availability triggers of one project host (optionally one service)."""
        hosts = self.api.call('host.get', {'output': ['hostid'], 'filter': {'host': [host]}})
        if not hosts:
            return []
        tags = [{'tag': 'scope', 'value': SCOPE, 'operator': 1},
                {'tag': 'impact', 'value': 'availability', 'operator': 1}]
        if comp:
            tags.append({'tag': 'service', 'value': comp, 'operator': 1})
        return self.api.call('trigger.get', {'output': ['triggerid'], 'filter': {'status': 0},
                                             'hostids': [hosts[0]['hostid']], 'tags': tags})

    def honeycomb(self, mname, tiles):
        """tiles = [(image key, label, triggers)]. Green = no trigger in problem,
        Red = any. A tile with no trigger yet stays green (static image)."""
        if mname in self._map_cache:
            return self._map_cache[mname]
        selements, cols = [], 6
        for idx, (key, label, trigs) in enumerate(tiles):
            up = self.ensure_image('os_hex_%s_up' % key, make_hex_png(label, 'UP', (26, 124, 17), (62, 184, 50)))
            down = self.ensure_image('os_hex_%s_down' % key,
                                     make_hex_png(label, 'DOWN', (214, 54, 56), (255, 112, 112)))
            col, row = idx % cols, idx // cols
            el = {'selementid': str(idx + 1), 'label': '', 'iconid_off': up,
                  'x': 24 + col * 172 + (86 if row % 2 else 0), 'y': 16 + row * 108}
            if trigs:
                el.update({'elementtype': 2, 'elements': [{'triggerid': t['triggerid']} for t in trigs],
                           'iconid_on': down, 'iconid_disabled': up, 'iconid_maintenance': up})
            else:
                el.update({'elementtype': 4, 'elements': []})
            selements.append(el)
        rows = max(1, (len(tiles) + cols - 1) // cols)
        params = {'name': mname, 'width': 1160, 'height': 32 + rows * 108, 'label_type': 4,
                  'highlight': 0, 'markelements': 0, 'expandproblem': 0,
                  'selements': selements, 'links': []}
        found = self.api.call('map.get', {'output': ['sysmapid'], 'filter': {'name': mname}})
        if found:
            params['sysmapid'] = found[0]['sysmapid']
            self.api.call('map.update', params)
            res = (found[0]['sysmapid'], rows)
        else:
            res = (self.api.call('map.create', params)['sysmapids'][0], rows)
        self._map_cache[mname] = res
        return res

    def projects_honeycomb(self, projects):
        tiles = [('proj_' + re.sub(r'[^a-z0-9]+', '_', p['name'].lower()), p['name'],
                  self.avail_triggers(p['zabbix_host'])) for p in projects]
        return self.honeycomb('OpenStack · Projects · Honeycomb', tiles)

    def project_honeycomb(self, p):
        # same image keys as the cloud honeycomb (os_hex_<service>_up/down): identical images
        tiles = [(comp, LABELS[comp], self.avail_triggers(p['zabbix_host'], comp))
                 for comp in p['services']]
        return self.honeycomb('OpenStack · Project %s · Service Honeycomb' % p['name'], tiles)

    def remove_map(self, name):
        found = self.api.call('map.get', {'output': ['sysmapid'], 'filter': {'name': name}})
        if found:
            self.api.call('map.delete', [m['sysmapid'] for m in found])

    def remove_dashboard(self, name):
        found = self.api.call('dashboard.get', {'output': ['dashboardid'], 'filter': {'name': name}})
        if found:
            self.api.call('dashboard.delete', [d['dashboardid'] for d in found])

    # ---- dashboard ----
    # One dashboard, pages in this order:
    #   Overview            every project together (honeycomb, outages, status matrix, latency)
    #   Available SLA       Availability SLA report cards for all services
    #   Response SLA        Response Time SLA report cards for all services
    #   <Service> ...       one page per service: HTTP status, latency, outages and problems
    def overview_page(self, projects):
        hosts = [p['zabbix_host'] for p in projects]
        p_admin = next((p for p in projects if p['name'] == 'admin'), projects[0])
        map_id, rows = self.project_honeycomb(p_admin)
        mh = map_height(rows)
        y = 0
        ws = [
            widget('map', 'Project %s: service honeycomb (Green = UP / Red = DOWN)' % p_admin['name'],
                   0, y, 24, mh, map_fields(map_id)),
        ]
        y += mh

        # HTTP status (all services) & API response time (all services)
        ws += [
            widget('svggraph', 'API HTTP status · all services',
                   0, y, 12, 7,
                   graph_ds([(hosts, ['* @ vip: HTTP status', 'Keystone: token request status'],
                              '1A7C11', DRAW_STAIRCASE)])),
            widget('svggraph', 'API response time · all services',
                   12, y, 12, 7,
                   graph_ds([(hosts, ['* @ vip: response time', 'Keystone: token request time'],
                              '2774A4', DRAW_LINE)])),
        ]
        y += 7

        # API checks down & API checks rejected
        ws += [
            widget('svggraph', 'API checks down (0 or 5xx) per project - 0 = OK',
                   0, y, 12, 7,
                   graph_fields(hosts, 'Probe fast: checks down (0 or 5xx)', 'E45959')),
            widget('svggraph', 'API checks rejected (4xx) per project',
                   12, y, 12, 7,
                   graph_fields(hosts, 'Probe fast: checks 4xx', 'F69100')),
        ]
        y += 7

        # Keystone token time per project
        ws.append(
            widget('svggraph', 'Keystone token time per project',
                   0, y, 24, 7,
                   graph_fields(hosts, 'Keystone: token request time'))
        )
        y += 7

        # Problems closing
        ws += [
            widget('problems', 'Outages (impact=availability) · all projects',
                   0, y, 12, 8,
                   problem_fields([('scope', SCOPE), ('impact', 'availability')])),
            widget('problems', 'All problems · all projects',
                   12, y, 12, 8,
                   problem_fields([('scope', SCOPE)])),
        ]
        return {'name': 'Overview', 'widgets': ws}

    def sla_page(self, title, suffix, comps, projects, sla_ids):
        ws, x, y = [], 0, 0
        for comp in comps:
            lb = LABELS[comp]
            users = [p for p in projects if comp in p['services']]
            for p in users:
                sname = 'SLA - Project %s - %s - %s' % (p['name'], lb, suffix)
                sid = sla_ids.get(sname)
                if sid is None:
                    continue
                ws.append(widget('slareport', sname, x, y, 8, 4, [fld(10, 'slaid', sid)]))
                x += 8
                if x >= 24:
                    x, y = 0, y + 4
        return {'name': title, 'widgets': ws}

    def service_page(self, comp, projects, sla_ids=None):
        lb = LABELS[comp]
        users = [p for p in projects if comp in p['services']]
        hosts = [p['zabbix_host'] for p in users]
        tg = [('scope', SCOPE), ('service', comp)]

        if comp == 'keystone':
            status_pats = ['Keystone: token request status', 'Keystone * @ vip: HTTP status']
            ms_pats = ['Keystone: token request time', 'Keystone * @ vip: response time']
        else:
            status_pats = ['%s * @ vip: HTTP status' % lb]
            ms_pats = ['%s * @ vip: response time' % lb]

        ws = [
            widget('svggraph', '%s: API HTTP status' % lb,
                   0, 0, 12, 8,
                   graph_ds([(hosts, status_pats, '1A7C11', DRAW_STAIRCASE)])),
            widget('svggraph', '%s: API response time' % lb,
                   12, 0, 12, 8,
                   graph_ds([(hosts, ms_pats, '2774A4', DRAW_LINE)])),
            widget('problems', '%s: outages (impact=availability) · all projects' % lb,
                   0, 8, 12, 8,
                   problem_fields(tg + [('impact', 'availability')])),
            widget('problems', '%s: all problems · all projects' % lb,
                   12, 8, 12, 8,
                   problem_fields(tg)),
        ]
        return {'name': lb, 'widgets': ws}

    def build_dashboards(self, sla_ids):
        projects = self.projects()
        comps = []
        for p in projects:
            for c in p['services']:
                if c not in comps:
                    comps.append(c)
        pages = [
            self.overview_page(projects),
            self.sla_page('Available SLA', 'Availability', comps, projects, sla_ids),
            self.sla_page('Response SLA', 'Response Time', comps, projects, sla_ids),
        ]
        pages += [self.service_page(c, projects, sla_ids) for c in comps]
        name = self.cfg.get('dashboard', 'OpenStack · ProjectScope')
        if name != 'OpenStack · Projects':
            self.remove_dashboard('OpenStack · Projects')
        params = {'name': name, 'display_period': 30, 'auto_start': 1, 'private': 0, 'pages': pages}
        found = self.api.call('dashboard.get', {'output': ['dashboardid'], 'filter': {'name': name}})
        if found:
            params['dashboardid'] = found[0]['dashboardid']
            self.api.call('dashboard.update', params)
        else:
            self.api.call('dashboard.create', params)


def fld(typ, name, value):
    return {'type': typ, 'name': name, 'value': value}


def widget(typ, name, x, y, w, h, fields):
    return {'type': typ, 'name': name, 'x': x, 'y': y, 'width': w, 'height': h,
            'view_mode': 0, 'fields': fields}


def problem_fields(tag_pairs, operator=1):
    """operator 1 = equals (Problems widget tag filter)."""
    f = [fld(0, 'show', 3), fld(0, 'show_tags', 3), fld(0, 'show_opdata', 1), fld(0, 'evaltype', 0)]
    for i, (t, v) in enumerate(tag_pairs):
        f += [fld(1, 'tags.tag.%d' % i, t), fld(0, 'tags.operator.%d' % i, operator),
              fld(1, 'tags.value.%d' % i, v)]
    return f


def map_fields(map_id):
    return [fld(0, 'source_type', 1), fld(8, 'sysmapid', int(map_id))]


def map_height(rows):
    """Dashboard rows for a honeycomb of <rows> hexagon rows (108 px each)."""
    return max(4, 2 + rows * 2)


def trigover_fields(tag_pairs, operator=1, show=2, style=0, hostids=None):
    """Trigger overview widget: show=2 (any state -> Green = OK, Red = problem)."""
    f = [fld(0, 'show', show), fld(0, 'style', style), fld(0, 'evaltype', 0)]
    if hostids:
        f += [fld(3, 'hostids.%d' % i, hid) for i, hid in enumerate(hostids)]
    for i, (t, v) in enumerate(tag_pairs):
        f += [fld(1, 'tags.tag.%d' % i, t), fld(0, 'tags.operator.%d' % i, operator),
              fld(1, 'tags.value.%d' % i, v)]
    return f


def graph_ds(datasets):
    """svggraph data sets: [(hosts, item_patterns, color_hex, [draw_type]), ...]."""
    f = []
    for k, ds in enumerate(datasets):
        hosts, items, color = ds[0], ds[1], ds[2]
        draw_type = ds[3] if len(ds) > 3 else DRAW_LINE
        if isinstance(items, str):
            items = [items]
        f += [fld(1, 'ds.hosts.%d.%d' % (k, i), h) for i, h in enumerate(hosts)]
        f += [fld(1, 'ds.items.%d.%d' % (k, j), it) for j, it in enumerate(items)]
        f += [fld(1, 'ds.color.%d' % k, color), fld(0, 'ds.type.%d' % k, draw_type),
              fld(0, 'ds.transparency.%d' % k, 0), fld(0, 'ds.fill.%d' % k, 0)]
    return f


def graph_fields(hosts, item_pattern, color='1A7C11', draw_type=DRAW_LINE):
    return graph_ds([(hosts, [item_pattern], color, draw_type)])


# Hexagon tiles - same drawing as os_zabbix_setup.py (cloud), so both honeycombs match.
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


def make_hex_png(label, sublabel, fill_rgb, border_rgb, w=156, h=104):
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


def main():
    ap = argparse.ArgumentParser(description='Project-scope Services / SLA / dashboard (Zabbix 6.0)')
    ap.add_argument('-c', '--config', required=True)
    ap.add_argument('--only', default='services,sla,dashboards',
                    help='comma list of: services, sla, dashboards')
    ap.add_argument('--dry-run', action='store_true', help='print, do not call the API')
    ap.add_argument('--insecure', action='store_true', help='do not verify the API TLS cert')
    args = ap.parse_args()
    with open(args.config) as fh:
        cfg = json.load(fh)
    alias = {'service': 'services', 'dashboard': 'dashboards', 'slas': 'sla'}
    only = set(alias.get(x.strip().lower(), x.strip().lower()) for x in args.only.split(',') if x.strip())
    unknown = only - {'services', 'sla', 'dashboards'}
    if unknown:
        sys.stderr.write('Unknown --only value(s): %s\n' % ', '.join(sorted(unknown)))
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
