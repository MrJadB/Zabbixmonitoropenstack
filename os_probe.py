#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
os_probe.py - OpenStack (Kolla-Ansible) API probe for Zabbix 6.0, v2.

Runs on the Zabbix proxy host of each AZ from systemd timers (plan A:
timer + trapper) and pushes ONE JSON document per tier to the trapper item
os.probe.<tier> of the Zabbix host named `zabbix_host` (template OS-API-Probe):

    os_probe.py --tier fast --send          # every 2 min
    os_probe.py --tier mid  --send          # every 5 min
    os_probe.py --tier slow --send          # every 30 min

Config: /etc/zabbix/os_probe.ini, one [section] per AZ (see
deploy/os_probe.ini.example). chmod 600, owned by the timer user.
Authentication supports both Application Credential (cred_id + cred_secret)
and OpenStack User Password (username + password + project_name).

Testing (prints instead of sending):
    os_probe.py --tier fast -c ./os_probe.ini -s bkk --pretty
    os_probe.py --tier mid --params params.json --pretty

Tiers are split by what their cost grows with, not by how slow a call is:
  fast  constant            L2 root per VIP and per node, Horizon login, noVNC,
                            OpenSDN per-node roots,
                            token, L3 functional probes, os-services records
  mid   cluster structure   hypervisors, placement RPs, Cinder pools,
                            server-side filtered error counts, OpenSDN process status,
                            transitional states (creating / deleting / pending ...):
                            count now (busy) + count older than stuck_minutes (stuck)
  slow  customer data       Glance canary (Range 0-0), stuck images / stacks / clusters,
                            Heat FAILED, Octavia amphorae/certs, credential expiry
One token per tier per round.

S3 (Ceph RGW, optional, s3_* keys in the ini) runs without a Keystone token.
One endpoint: s3_url (rows target=vip). Several RGW zones checked from one
section (one proxy, one Zabbix host): s3_zones = bkk,non + s3_<zone>_url,
s3_<zone>_bucket, s3_<zone>_static_url (rows target=<zone>); every zone reads
the markers of all other zones -> replication lag in both directions.
  fast  s3 root @ vip (anonymous GET /), static website (s3_static_url),
        canary PUT -> GET (content compared) -> DELETE in s3_bucket
  mid   ListBuckets + HEAD s3_bucket (SigV4), multisite replication:
        write a time marker to s3_bucket, read the peer zone's marker from
        s3_peer_bucket through this zone's endpoint -> s3_repl_lag_s
  slow  s3 endpoints join the TLS certificate expiry check

TLS certificates (slow): one row per https host:port in tls_certs[] - every
catalog endpoint of the interfaces in tls_interfaces (default public) plus
auth_url, horizon_url, novnc_url, s3_url, s3_static_url and tls_extra.

Scope (ini key "scope", default cloud):
  cloud    everything above, with all_tenants / global_tenant (needs the monitoring role).
  project  what one project's own member / reader can see, no admin API, no policy change:
           token + catalog, one or more L3 functional probes for EVERY catalog service
           (PROJECT_FAST_L3), error / stuck counts of this project only (+ Designate
           zones, Neutron routers / floating IPs / ports), credential expiry, Glance canary.
           Quota usage of Nova, Cinder, Neutron, Octavia and Manila (mid) is off by default
           (project_quota = 1 turns it on; the project template has no quota items).
           Error / stuck / trend resource counts (mid + slow) are off by default too
           (project_resources = 1 turns them on) - the cloud scope already counts them.
           Dropped: service records, hypervisors, placement, Cinder pools,
           IP availability, OpenSDN, Octavia amphorae, Glance stores.
           L2 roots / Horizon / noVNC (project_l2 = 1) and TLS (project_tls = 1) are off by
           default - the cloud-scope host already checks them, so they would alert twice.
           Output gets "scope": "project" and "project": {id, name}.

Status contract - every call is one record in checks[]:
  http_status = the real HTTP code (200, 300, 403, 503 ...)
                0  = no response (timeout, refused, DNS, TLS)
               -1  = skipped (deadline, skip_types, no token) - never a false DOWN
The script never decides what an outage is. The template triggers do:
  0 or >= 500 -> availability,  400-499 -> monitoring.

Python 3.6+, standard library only.
"""

import argparse
import binascii
import fnmatch
import configparser
import hashlib
import hmac
import json
import math
import os
import re
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

PROBE_NAME = 'os-probe'
VERSION = 2
TIERS = ('fast', 'mid', 'slow')
DOWN, UP, MAINTENANCE = 0, 1, 2
SKIPPED, NO_RESPONSE = -1, 0
HV_MICROVERSION = 'compute 2.87'      # 2.88 dropped vcpus/memory_mb/running_vms
CINDER_COUNT_MV = 'volume 3.45'       # with_count
MAX_PAGES = 50

# Catalog type -> service tag (tag schema). When two types map to the same
# service (block-storage + volumev3), the one listed first wins.
CATALOG_TYPES = [
    ('identity', 'keystone'), ('compute', 'nova'), ('placement', 'placement'),
    ('network', 'neutron'), ('image', 'glance'),
    ('block-storage', 'cinder'), ('volumev3', 'cinder'), ('volumev2', 'cinder'),
    ('volume', 'cinder'),
    ('orchestration', 'heat'), ('load-balancer', 'octavia'),
    ('key-manager', 'barbican'), ('database', 'trove'),
    ('container-infrastructure-management', 'magnum'), ('container-infra', 'magnum'),
    ('baremetal', 'ironic'), ('object-store', 'swift'),
    ('shared-file-system', 'manila'), ('sharev2', 'manila'),
    ('dns', 'designate'), ('alarming', 'aodh'), ('metric', 'gnocchi'),
]
IGNORED_TYPES = ('computev3', 'cloudformation', 'identityv2')   # legacy duplicates
MAP_TYPES = dict(CATALOG_TYPES)

LABELS = {
    'keystone': 'Keystone', 'nova': 'Nova', 'placement': 'Placement',
    'neutron': 'Neutron', 'glance': 'Glance', 'cinder': 'Cinder', 'heat': 'Heat',
    'octavia': 'Octavia', 'barbican': 'Barbican', 'trove': 'Trove',
    'magnum': 'Magnum', 'ironic': 'Ironic', 'swift': 'Swift', 'manila': 'Manila',
    'designate': 'Designate', 'aodh': 'Aodh', 'gnocchi': 'Gnocchi',
    'horizon': 'Horizon', 'opensdn': 'OpenSDN', 's3': 'S3', 'novnc': 'noVNC',
}

OPENSDN_UVES = ('config-node', 'control-node', 'analytics-node', 'database-node',
                'config-database-node')

# ---- fast: L3 functional probes (cheap, constant cost) ----
# (service, check, paths tried in order - first non-404 wins, headers, missing_ok)
FAST_L3 = [
    ('keystone', 'catalog', ['/auth/catalog'], None, False),
    ('neutron', 'networks', ['/v2.0/networks?limit=1&fields=id',
                             '/networks?limit=1&fields=id'], None, False),
    ('glance', 'images', ['/images?limit=1'], None, False),
    # 404 = multi-store not enabled -> skipped, not a probe error
    ('glance', 'stores', ['/info/stores'], None, True),
    ('trove', 'datastores', ['/datastores'], None, False),
    ('octavia', 'providers', ['/v2/lbaas/providers', '/lbaas/providers'], None, False),
]

# ---- scope = project ----
def _neutron_l3(check, res):
    q = '%s?limit=1&fields=id' % res
    return ('neutron', check, ['/v2.0/' + q, '/' + q], None, False)


PROJECT_FAST_L3 = [
    ('nova', 'servers', ['/servers?limit=1'], None, False),
    ('nova', 'flavors', ['/flavors?limit=1'], None, False),
    ('nova', 'keypairs', ['/os-keypairs?limit=1'], None, False),
    ('nova', 'availability_zones', ['/os-availability-zone'], None, False),
    ('cinder', 'availability_zones', ['/os-availability-zone'], None, False),
    ('cinder', 'volumes', ['/volumes?limit=1'], None, False),
    ('cinder', 'volume_types', ['/types'], None, False),
    ('cinder', 'snapshots', ['/snapshots?limit=1'], None, False),
    ('cinder', 'backups', ['/backups?limit=1'], None, False),
    ('neutron', 'extensions', ['/v2.0/extensions', '/extensions'], None, False),
    _neutron_l3('subnets', 'subnets'),
    _neutron_l3('ports', 'ports'),
    _neutron_l3('routers', 'routers'),
    _neutron_l3('floatingips', 'floatingips'),
    _neutron_l3('security_groups', 'security-groups'),
    ('heat', 'stacks', ['/stacks?limit=1'], None, False),
    ('octavia', 'loadbalancers', ['/v2/lbaas/loadbalancers?limit=1&fields=id',
                                  '/lbaas/loadbalancers?limit=1&fields=id'], None, False),
    ('magnum', 'clusters', ['/clusters?limit=1'], None, False),
    ('magnum', 'clustertemplates', ['/clustertemplates?limit=1'], None, False),
    ('trove', 'instances', ['/instances?limit=1'], None, False),
    ('barbican', 'secrets', ['/v1/secrets?limit=1', '/secrets?limit=1'], None, False),
    ('barbican', 'containers', ['/v1/containers?limit=1', '/containers?limit=1'], None, False),
    ('swift', 'account', ['?format=json&limit=1'], None, False),
    ('manila', 'shares', ['/shares?limit=1'], None, False),
    ('designate', 'zones', ['/v2/zones?limit=1', '/zones?limit=1'], None, False),
    ('ironic', 'nodes', ['/v1/nodes?limit=1&fields=uuid', '/nodes?limit=1&fields=uuid'],
     {'X-OpenStack-Ironic-API-Version': '1.8'}, False),
    ('aodh', 'alarms', ['/v2/alarms?limit=1'], None, False),
    ('gnocchi', 'resources', ['/v1/resource/generic?limit=1'], None, False),
]

PROJECT_MID_COUNTS = [
    ('designate', 'zones_error', ['/v2/zones?status=ERROR&limit={cap}',
                                  '/zones?status=ERROR&limit={cap}'], None, 'zones', None),
    ('neutron', 'routers_error', ['/v2.0/routers?status=ERROR&fields=id&limit={cap}',
                                  '/routers?status=ERROR&fields=id&limit={cap}'], None, 'routers', None),
    ('neutron', 'floatingips_error',
     ['/v2.0/floatingips?status=ERROR&fields=id&limit={cap}',
      '/floatingips?status=ERROR&fields=id&limit={cap}'], None, 'floatingips', None),
    ('neutron', 'ports_error', ['/v2.0/ports?status=ERROR&fields=id&limit={cap}',
                                '/ports?status=ERROR&fields=id&limit={cap}'], None, 'ports', None),
    ('octavia', 'lb_degraded', ['/v2/lbaas/loadbalancers?operating_status=DEGRADED&fields=id&limit={cap}',
                                '/lbaas/loadbalancers?operating_status=DEGRADED&fields=id&limit={cap}'],
     None, 'loadbalancers', None),
    ('octavia', 'lb_offline', ['/v2/lbaas/loadbalancers?operating_status=OFFLINE&fields=id&limit={cap}',
                               '/lbaas/loadbalancers?operating_status=OFFLINE&fields=id&limit={cap}'],
     None, 'loadbalancers', None),
    ('octavia', 'pools_error', ['/v2/lbaas/pools?provisioning_status=ERROR&fields=id&limit={cap}',
                                '/lbaas/pools?provisioning_status=ERROR&fields=id&limit={cap}'],
     None, 'pools', None),
    ('octavia', 'pools_down', ['/v2/lbaas/pools?operating_status=ERROR&fields=id&limit={cap}',
                               '/lbaas/pools?operating_status=ERROR&fields=id&limit={cap}'],
     None, 'pools', None),
    ('octavia', 'listeners_error',
     ['/v2/lbaas/listeners?provisioning_status=ERROR&fields=id&limit={cap}',
      '/lbaas/listeners?provisioning_status=ERROR&fields=id&limit={cap}'], None, 'listeners', None),
]

PROJECT_MID_TRENDS = [
    ('nova', 'servers_shutoff', ['/servers?status=SHUTOFF&limit={cap}'], None, 'servers', None),
    ('nova', 'servers_paused', ['/servers?status=PAUSED&limit={cap}'], None, 'servers', None),
    ('nova', 'servers_suspended', ['/servers?status=SUSPENDED&limit={cap}'], None, 'servers', None),
]
PROJECT_MID_STATES = [
    ('designate', 'zones_pending', ['/v2/zones?status=PENDING&limit={cap}',
                                    '/zones?status=PENDING&limit={cap}'], None, 'zones',
     lambda o: str(o.get('status') or '').upper() == 'PENDING', ('updated_at', 'created_at')),
]
CLOUD_ONLY_CHECKS = {
    ('glance', 'stores'), ('octavia', 'amphorae_error'), ('octavia', 'amphorae_booting'),
    ('octavia', 'amphorae_deleting'),
}
ALL_PROJECTS_RE = re.compile(r'&(all_tenants=1|global_tenant=true)')

QUOTA_LIMITS = {
    'nova': [('instances', 'maxTotalInstances', 'totalInstancesUsed'),
             ('cores', 'maxTotalCores', 'totalCoresUsed'),
             ('ram_mb', 'maxTotalRAMSize', 'totalRAMUsed'),
             ('server_groups', 'maxServerGroups', 'totalServerGroupsUsed')],
    'cinder': [('volumes', 'maxTotalVolumes', 'totalVolumesUsed'),
               ('gigabytes', 'maxTotalVolumeGigabytes', 'totalGigabytesUsed'),
               ('snapshots', 'maxTotalSnapshots', 'totalSnapshotsUsed'),
               ('backups', 'maxTotalBackups', 'totalBackupsUsed'),
               ('backup_gigabytes', 'maxTotalBackupGigabytes', 'totalBackupGigabytesUsed')],
}

# ---- fast: service records (one LLD row per binary@host) ----
AGENT_SOURCES = [
    {'service': 'nova', 'check': 'os-services', 'paths': ['/os-services'], 'key': 'services'},
    {'service': 'cinder', 'check': 'os-services', 'paths': ['/os-services'], 'key': 'services'},
    {'service': 'manila', 'check': 'services', 'paths': ['/services'], 'key': 'services'},
    {'service': 'heat', 'check': 'services', 'paths': ['/services'], 'key': 'services'},
    {'service': 'magnum', 'check': 'mservices', 'paths': ['/mservices'], 'key': 'mservices'},
    {'service': 'ironic', 'check': 'conductors', 'paths': ['/v1/conductors', '/conductors'],
     'key': 'conductors', 'headers': {'X-OpenStack-Ironic-API-Version': '1.49'}},
]
AGENT_IMPACT = {'nova-compute': 'stability'}

CINDER_HDR = {'OpenStack-API-Version': CINDER_COUNT_MV}
IRONIC_HDR = {'X-OpenStack-Ironic-API-Version': '1.8'}

# ---- mid: ask for a count, never list everything ----
MID_COUNTS = [
    ('cinder', 'volumes_error',
     ['/volumes?status=error&all_tenants=1&with_count=true&limit=1'], CINDER_HDR, 'volumes', None),
    ('cinder', 'snapshots_error',
     ['/snapshots?status=error&all_tenants=1&with_count=true&limit=1'], CINDER_HDR, 'snapshots', None),
    ('cinder', 'backups_error',
     ['/backups?status=error&all_tenants=1&with_count=true&limit=1'], CINDER_HDR, 'backups', None),
    ('nova', 'servers_error',
     ['/servers?status=ERROR&all_tenants=1&limit={cap}'], None, 'servers', None),
    ('octavia', 'lb_provisioning_error',
     ['/v2/lbaas/loadbalancers?provisioning_status=ERROR&fields=id&limit={cap}',
      '/lbaas/loadbalancers?provisioning_status=ERROR&fields=id&limit={cap}'], None, 'loadbalancers', None),
    ('octavia', 'lb_operating_error',
     ['/v2/lbaas/loadbalancers?operating_status=ERROR&fields=id&limit={cap}',
      '/lbaas/loadbalancers?operating_status=ERROR&fields=id&limit={cap}'], None, 'loadbalancers', None),
    ('trove', 'instances_error', ['/instances?limit={cap}'], None, 'instances',
     lambda o, p: o.get('status') == 'ERROR'),
    ('magnum', 'clusters_failed', ['/clusters?limit={cap}'], None, 'clusters',
     lambda o, p: 'FAILED' in str(o.get('status') or '')),
    ('ironic', 'nodes_error',
     ['/v1/nodes?provision_state=error&fields=uuid&limit={cap}',
      '/nodes?provision_state=error&fields=uuid&limit={cap}'], IRONIC_HDR, 'nodes', None),
    ('ironic', 'nodes_maintenance',
     ['/v1/nodes?maintenance=true&fields=uuid&limit={cap}',
      '/nodes?maintenance=true&fields=uuid&limit={cap}'], IRONIC_HDR, 'nodes', None),
    ('cinder', 'volumes_error_deleting',
     ['/volumes?status=error_deleting&all_tenants=1&with_count=true&limit=1'], CINDER_HDR,
     'volumes', None),
    ('cinder', 'volumes_error_extending',
     ['/volumes?status=error_extending&all_tenants=1&with_count=true&limit=1'], CINDER_HDR,
     'volumes', None),
    ('cinder', 'snapshots_error_deleting',
     ['/snapshots?status=error_deleting&all_tenants=1&with_count=true&limit=1'], CINDER_HDR,
     'snapshots', None),
    ('ironic', 'nodes_deploy_failed',
     ['/v1/nodes?provision_state=deploy%20failed&fields=uuid&limit={cap}',
      '/nodes?provision_state=deploy%20failed&fields=uuid&limit={cap}'], IRONIC_HDR, 'nodes', None),
    ('ironic', 'nodes_clean_failed',
     ['/v1/nodes?provision_state=clean%20failed&fields=uuid&limit={cap}',
      '/nodes?provision_state=clean%20failed&fields=uuid&limit={cap}'], IRONIC_HDR, 'nodes', None),
    ('manila', 'shares_error', ['/shares?status=error&all_tenants=1&limit={cap}'], None,
     'shares', None),
]


def _is(field, value):
    return lambda o: str(o.get(field) or '').lower() == value.lower()


# ---- mid: resources in a transitional state (creating / deleting / ...) ----
NOVA_TS = ('updated', 'created')
OS_TS = ('updated_at', 'created_at')
IRONIC_TS = ('provision_updated_at', 'updated_at', 'created_at')
IRONIC_STATE_FIELDS = 'fields=uuid,provision_state,provision_updated_at,updated_at,created_at'


def _nova(what, query, match):
    return ('nova', what, ['/servers/detail?%s&all_tenants=1&limit={cap}' % query], None,
            'servers', match, NOVA_TS)


def _cinder(kind, state):
    return ('cinder', '%s_%s' % (kind, state),
            ['/%s/detail?status=%s&all_tenants=1&limit={cap}' % (kind, state)], None, kind,
            _is('status', state), OS_TS)


def _octavia(state):
    return ('octavia', 'lb_%s' % state.lower(),
            ['/v2/lbaas/loadbalancers?provisioning_status=%s&limit={cap}' % state,
             '/lbaas/loadbalancers?provisioning_status=%s&limit={cap}' % state], None,
            'loadbalancers', _is('provisioning_status', state), OS_TS)


def _ironic(what, state):
    q = 'provision_state=%s&%s&limit={cap}' % (state.replace(' ', '%20'), IRONIC_STATE_FIELDS)
    return ('ironic', what, ['/v1/nodes?' + q, '/nodes?' + q], IRONIC_HDR, 'nodes',
            _is('provision_state', state), IRONIC_TS)


def _manila(state):
    return ('manila', 'shares_%s' % state,
            ['/shares/detail?status=%s&all_tenants=1&limit={cap}' % state], None, 'shares',
            _is('status', state), OS_TS)


MID_STATES = [
    _nova('servers_build', 'status=BUILD', _is('status', 'BUILD')),
    _nova('servers_deleting', 'task_state=deleting', _is('OS-EXT-STS:task_state', 'deleting')),
    _nova('servers_migrating', 'status=MIGRATING', _is('status', 'MIGRATING')),
    _nova('servers_resize', 'status=RESIZE', _is('status', 'RESIZE')),
    _cinder('volumes', 'creating'), _cinder('volumes', 'deleting'),
    _cinder('volumes', 'attaching'), _cinder('volumes', 'detaching'),
    _cinder('volumes', 'extending'),
    _cinder('snapshots', 'creating'), _cinder('snapshots', 'deleting'),
    _cinder('backups', 'creating'), _cinder('backups', 'restoring'),
    _cinder('backups', 'deleting'),
    _octavia('PENDING_CREATE'), _octavia('PENDING_UPDATE'), _octavia('PENDING_DELETE'),
    _ironic('nodes_deploying', 'deploying'), _ironic('nodes_cleaning', 'cleaning'),
    _ironic('nodes_wait_callback', 'wait call-back'),
    _manila('creating'), _manila('deleting'), _manila('extending'),
]


def _in(field, *values):
    vals = set(v.lower() for v in values)
    return lambda o: str(o.get(field) or '').lower() in vals


def _has(field, word):
    w = word.lower()
    return lambda o: w in str(o.get(field) or '').lower()


# ---- slow: grows with customer data ----
SLOW_COUNTS = [
    ('heat', 'stacks_failed', ['/stacks?status=FAILED&global_tenant=true&limit={cap}'], None,
     'stacks', None),
    ('octavia', 'amphorae_error', ['/v2/octavia/amphorae?status=ERROR&fields=id&limit={cap}',
                                   '/octavia/amphorae?status=ERROR&fields=id&limit={cap}'],
     None, 'amphorae', None),
]

SLOW_STATES = [
    ('glance', 'images_queued', ['/images?status=queued&limit={cap}'], None, 'images',
     [('images_queued', _is('status', 'queued'))], OS_TS, True),
    ('glance', 'images_saving', ['/images?status=saving&limit={cap}'], None, 'images',
     [('images_saving', _is('status', 'saving'))], OS_TS, True),
    ('glance', 'images_importing', ['/images?status=importing&limit={cap}'], None, 'images',
     [('images_importing', _is('status', 'importing'))], OS_TS, True),
    ('glance', 'images_deleting', ['/images?status=pending_delete&limit={cap}'], None, 'images',
     [('images_deleting', _is('status', 'pending_delete'))], OS_TS, True),
    ('heat', 'stacks_creating',
     ['/stacks?status=CREATE_IN_PROGRESS&global_tenant=true&limit={cap}'], None, 'stacks',
     [('stacks_creating', _in('stack_status', 'CREATE_IN_PROGRESS'))],
     ('updated_time', 'creation_time'), True),
    ('heat', 'stacks_updating',
     ['/stacks?status=UPDATE_IN_PROGRESS&global_tenant=true&limit={cap}'], None, 'stacks',
     [('stacks_updating', _in('stack_status', 'UPDATE_IN_PROGRESS'))],
     ('updated_time', 'creation_time'), True),
    ('heat', 'stacks_deleting',
     ['/stacks?status=DELETE_IN_PROGRESS&global_tenant=true&limit={cap}'], None, 'stacks',
     [('stacks_deleting', _in('stack_status', 'DELETE_IN_PROGRESS'))],
     ('updated_time', 'creation_time'), True),
    ('trove', 'instances_states', ['/instances?limit={cap}'], None, 'instances',
     [('instances_build', _is('status', 'BUILD')),
      ('instances_deleting', _in('status', 'SHUTDOWN', 'DELETING')),
      ('instances_resize', _is('status', 'RESIZE'))], NOVA_TS, False),
    ('magnum', 'clusters_states', ['/clusters/detail?limit={cap}'], None, 'clusters',
     [('clusters_creating', _is('status', 'CREATE_IN_PROGRESS')),
      ('clusters_updating', _is('status', 'UPDATE_IN_PROGRESS')),
      ('clusters_deleting', _is('status', 'DELETE_IN_PROGRESS'))], OS_TS, False),
    ('octavia', 'amphorae_booting',
     ['/v2/octavia/amphorae?status=BOOTING&limit={cap}',
      '/octavia/amphorae?status=BOOTING&limit={cap}'], None, 'amphorae',
     [('amphorae_booting', _is('status', 'BOOTING'))], OS_TS, True),
    ('octavia', 'amphorae_deleting',
     ['/v2/octavia/amphorae?status=PENDING_DELETE&limit={cap}',
      '/octavia/amphorae?status=PENDING_DELETE&limit={cap}'], None, 'amphorae',
     [('amphorae_deleting', _is('status', 'PENDING_DELETE'))], OS_TS, True),
]
# Cinder transitional states moved to MID_STATES (one counter per state).

DEFAULTS = {
    'scope': 'cloud',            # cloud | project (see the docstring)
    'project_l2': '0',           # scope = project: also run L2 roots / Horizon / noVNC
    'project_tls': '0',          # scope = project: also run the TLS certificate check
    'project_quota': '0',        # scope = project: also collect quota usage (mid tier)
    'project_resources': '0',    # scope = project: also count error / stuck resources
    # Authentication: Application Credential OR OpenStack User Password
    'cred_id': '',
    'cred_secret': '',
    'username': '',
    'password': '',
    'user_id': '',
    'user_domain_name': 'Default',
    'user_domain_id': '',
    'project_name': '',
    'project_id': '',
    'project_domain_name': 'Default',
    'project_domain_id': '',
    'system_scope': '',
    'iface': 'internal',
    'nodes': '',                 # controller IPs for per-node L2 checks
    'node_scheme': 'http',       # Kolla backends listen plain http behind HAProxy
    'horizon_url': '',
    'novnc_url': '',             # e.g. https://PUBLIC-VIP:6080 (not in the catalog)
    'novnc_port': '6080',        # per-node novncproxy port, used with nodes
    'opensdn_nodes': '',         # OpenSDN controller IPs for per-node L2 checks
    'opensdn_ports': 'config-api:8082,analytics-api:8081,control:8083',
    'opensdn_analytics_url': '', # e.g. http://INTERNAL-VIP:8081 -> mid-tier process status
    'opensdn_vrouters': '0',
    'ref_url': '',               # something outside OpenStack, tells probe-side network loss
    'maint_hosts': '',
    'canary_id': '',
    'skip_types': 'swift',       # service tags or catalog types: L2/L3 reported skipped (empty = check swift)
    'skip_checks': '',           # check names to skip, wildcards ok (e.g. pools, heat:stacks*)
    'skip_azs': '',              # AZs / cells to skip in agents/hypervisors (e.g. kkn)
    'skip_hosts_regex': '',      # regex of hostnames to skip in agents/hypervisors
    'catalog_cache': '',         # file; lets fast tier keep L2 checks when Keystone is down
    'http_timeout_ms': '10000',
    'fast_deadline_ms': '40000',
    'mid_deadline_ms': '120000',
    'slow_deadline_ms': '300000',
    'stuck_minutes': '60',
    'list_cap': '1000',
    'workers': '8',
    'verify_tls': '1',
    'ca_file': '',
    'zabbix_server': '127.0.0.1',
    'zabbix_port': '10051',
    'zabbix_host': '',
    'item_prefix': 'os.probe',
    's3_url': '',                
    's3_region': 'bkk',    
    's3_access_key': '',
    's3_secret_key': '',
    's3_bucket': '',             
    's3_peer_bucket': '',        
    's3_static_url': '',         
    's3_zones': '',              
    # TLS certificate expiry (slow tier)
    'tls_interfaces': 'public',  
    'tls_extra': '',            
}

S3_CANARY_KEY = 'zbx-canary/rw'
S3_MARKER_KEY = 'zbx-canary/repl-marker'


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class ProbeError(Exception):
    """No HTTP response at all -> http_status 0."""


class Skipped(Exception):
    """Not attempted -> http_status -1."""


def now_ms():
    return int(time.time() * 1000)


def clean(s, n=150):
    if s is None:
        return ''
    return re.sub(r'[\r\n]+', ' ', str(s))[:n]


def num(v):
    """parseInt semantics: '12abc' -> 12, junk -> 0."""
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (ValueError, OverflowError):
            return 0
    m = re.match(r'\s*([+-]?\d+)', str(v))
    return int(m.group(1)) if m else 0


def fnum(v):
    """Float or -1 for 'infinite' / 'unknown' / junk (Cinder pool capabilities)."""
    try:
        f = float(v)
        return f if math.isfinite(f) else -1.0
    except (TypeError, ValueError):
        return -1.0


def pct(used, total):
    used, total = float(used or 0), float(total or 0)
    if total <= 0 or used < 0:
        return -1
    return round(used * 100.0 / total, 2)


def split_csv(s):
    return [p.strip() for p in str(s or '').split(',') if p.strip()]


def root_of(url):
    m = re.match(r'^(https?)://([^/]+)', str(url))
    return (m.group(1) + '://' + m.group(2) + '/') if m else ''


def port_of(url):
    m = re.match(r'^(https?)://[^/:]+:(\d+)', str(url))
    if m:
        return num(m.group(2))
    return 443 if str(url).startswith('https:') else 80


def parse_ts(v):
    """ISO-8601 variants used by OpenStack -> epoch seconds (UTC), or None."""
    if not v:
        return None
    s = str(v).replace(' ', 'T')[:19]
    try:
        dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None
    return (dt - datetime(1970, 1, 1)).total_seconds()


def err_detail(status, body):
    """Turn an OpenStack error body into 'HTTP 403: <policy reason>'."""
    msg = 'HTTP %s' % status
    if not body or not isinstance(body, str):
        return msg
    try:
        j = json.loads(body)
    except ValueError:
        return msg
    m = ''
    if isinstance(j, dict):
        m = j.get('message') or ''
        errs = j.get('errors')
        if not m and isinstance(errs, list) and errs and isinstance(errs[0], dict):
            m = errs[0].get('detail') or errs[0].get('title') or ''
        if not m and j.get('title') and (j.get('description') or j.get('detail')):
            m = j.get('description') or j.get('detail')
        if not m:
            for v in j.values():
                if isinstance(v, dict) and v.get('message'):
                    m = v['message']
                    break
    if m:
        msg += ': ' + clean(m, 100)
    return msg


def next_link(parsed, key):
    """'next page' URL in the shapes OpenStack services use."""
    if not isinstance(parsed, dict):
        return ''
    nxt = parsed.get('next')                       # Glance, Barbican, Magnum
    if isinstance(nxt, str) and nxt:
        return nxt
    for lk in ((key or '') + '_links', 'links'):   # Nova / Octavia / Trove
        links = parsed.get(lk)
        if isinstance(links, dict) and isinstance(links.get('next'), str):
            return links['next']
        if isinstance(links, list):
            for ln in links:
                if isinstance(ln, dict) and ln.get('rel') == 'next' and ln.get('href'):
                    return ln['href']
    return ''


def join_next(base, nxt):
    """Resolve a (possibly relative) next link against the endpoint base."""
    if re.match(r'^https?://', nxt):
        return nxt
    if not nxt.startswith('/'):
        nxt = '/' + nxt
    m = re.match(r'^(https?://[^/]+)(/.*)?$', base)
    if not m:
        return base + nxt
    root, path = m.group(1), (m.group(2) or '')
    if path and (nxt.startswith(path + '/') or nxt.startswith(path + '?')):
        return root + nxt
    seg = re.match(r'^/[^/?]+', nxt)
    if seg and path.endswith(seg.group(0)):
        return root + path[:-len(seg.group(0))] + nxt
    return base + nxt


def s3_err(status, body, step=''):
    """S3 XML error body -> '<step> HTTP 403: AccessDenied'."""
    msg = ('%s HTTP %s' % (step, status)).strip()
    m = re.search(r'<Code>([^<]+)</Code>', body if isinstance(body, str) else '')
    return msg + (': ' + clean(m.group(1), 80) if m else '')


def _hmac(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def s3_sign(method, url, access_key, secret_key, region, body=b''):
    """AWS Signature V4 headers for one S3 request (works with Ceph RGW)."""
    u = urllib.parse.urlsplit(url)
    t = datetime.utcnow()
    amz_date, day = t.strftime('%Y%m%dT%H%M%SZ'), t.strftime('%Y%m%d')
    payload = hashlib.sha256(body or b'').hexdigest()
    query = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'), urllib.parse.quote(v, safe='-_.~'))
                     for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    hdrs = {'host': u.netloc, 'x-amz-content-sha256': payload, 'x-amz-date': amz_date}
    signed = ';'.join(sorted(hdrs))
    canonical = '\n'.join([method, u.path or '/', query,
                           ''.join('%s:%s\n' % (k, hdrs[k]) for k in sorted(hdrs)),
                           signed, payload])
    scope = '%s/%s/s3/aws4_request' % (day, region)
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                         hashlib.sha256(canonical.encode('utf-8')).hexdigest()])
    k = _hmac(('AWS4' + secret_key).encode('utf-8'), day)
    for part in (region, 's3', 'aws4_request'):
        k = _hmac(k, part)
    sig = hmac.new(k, to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    return {'x-amz-content-sha256': payload, 'x-amz-date': amz_date,
            'Authorization': 'AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                             % (access_key, scope, signed, sig)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Stats(object):
    """Per-run load counters, reported as requests / bytes items."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = 0
        self.bytes = 0

    def add(self, n):
        with self.lock:
            self.requests += 1
            self.bytes += n


class Http(object):
    def __init__(self, timeout_s, stats, verify_tls=True, ca_file='', check_hostname=True):
        self.timeout = timeout_s
        self.stats = stats
        # system store + Kolla CA: Horizon may use a public cert, APIs the internal CA
        ctx = ssl.create_default_context()
        if ca_file:
            ctx.load_verify_locations(cafile=ca_file)
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif not check_hostname:
            # per-node checks dial an IP: keep the CA check, drop the name check
            ctx.check_hostname = False
        # ProxyHandler({}) ignores http_proxy/https_proxy: internal endpoints
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx))

    def request(self, method, url, headers=None, data=None, sink=False):
        """Returns (status, headers, body). With sink=True body is the byte count.
        Raises ProbeError when there is no HTTP response at all."""
        hdrs = {'Accept': 'application/json',
                'Content-Type': 'application/json'}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            with resp:
                if sink:
                    n = 0
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        n += len(chunk)
                    self.stats.add(n)
                    return resp.getcode(), resp.headers, n
                raw = resp.read()
                self.stats.add(len(raw))
                return resp.getcode(), resp.headers, raw.decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b''
            self.stats.add(len(raw))
            return e.code, e.headers, (0 if sink else raw.decode('utf-8', 'replace'))
        except urllib.error.URLError as e:
            self.stats.add(0)
            raise ProbeError('cannot get URL %s: %s' % (url, e.reason))
        except (socket.timeout, OSError) as e:
            self.stats.add(0)
            raise ProbeError('cannot get URL %s: %s' % (url, e or 'timed out'))
        except Exception as e: 
            self.stats.add(0)
            raise ProbeError('cannot get URL %s: %s' % (url, e))


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------
class Probe(object):
    def __init__(self, p, tier):
        self.tier = tier
        self.scope = str(p.get('scope') or 'cloud').strip().lower()
        if self.scope not in ('cloud', 'project'):
            raise ValueError('scope must be cloud or project, not %r' % self.scope)
        self.project = self.scope == 'project'
        self.project_l2 = num(p.get('project_l2')) == 1
        self.project_tls = num(p.get('project_tls')) == 1
        self.project_quota = self.project and num(p.get('project_quota')) == 1
        self.count_res = not self.project or num(p.get('project_resources')) == 1
        self.project_id, self.project_name = '', ''
        self.quotas = []
        self.p = p
        self.auth_url = re.sub(r'/v3$', '', re.sub(r'/+$', '', str(p.get('auth_url') or '')))
        self.cred_id = str(p.get('cred_id') or '').strip()
        self.cred_secret = str(p.get('cred_secret') or '').strip()
        self.username = str(p.get('username') or '').strip()
        self.password = str(p.get('password') or '').strip()
        self.user_id_auth = str(p.get('user_id') or '').strip()
        self.user_domain_name = str(p.get('user_domain_name') or 'Default').strip()
        self.user_domain_id = str(p.get('user_domain_id') or '').strip()
        self.project_name = str(p.get('project_name') or '').strip()
        self.project_id = str(p.get('project_id') or '').strip()
        self.project_domain_name = str(p.get('project_domain_name') or 'Default').strip()
        self.project_domain_id = str(p.get('project_domain_id') or '').strip()
        self.system_scope = str(p.get('system_scope') or '').strip()
        self.auth_method = ''
        self.iface = str(p.get('iface') or 'internal')
        self.nodes = split_csv(p.get('nodes'))
        self.node_scheme = str(p.get('node_scheme') or 'http')
        self.horizon_url = str(p.get('horizon_url') or '').rstrip('/')
        self.novnc_url = str(p.get('novnc_url') or '').rstrip('/')
        self.novnc_port = num(p.get('novnc_port') or 6080)
        self.sdn_nodes = split_csv(p.get('opensdn_nodes'))
        self.sdn_ports = []
        for item in split_csv(p.get('opensdn_ports')):
            name, _, port = item.partition(':')
            if name and num(port):
                self.sdn_ports.append((name.strip(), num(port)))
        self.sdn_url = str(p.get('opensdn_analytics_url') or '').rstrip('/')
        self.sdn_vrouters = num(p.get('opensdn_vrouters')) == 1
        self.ref_url = str(p.get('ref_url') or '')
        self.maint = set(split_csv(p.get('maint_hosts')))
        self.canary = str(p.get('canary_id') or '')
        self.skip = set(split_csv(p.get('skip_types')))
        self.skip_checks = set(x.lower() for x in split_csv(p.get('skip_checks')))
        self.skip_azs = set(x.lower() for x in split_csv(p.get('skip_azs')))
        shr = str(p.get('skip_hosts_regex') or '').strip()
        self.skip_hosts_re = re.compile(shr, re.I) if shr else None
        self.catalog_cache = str(p.get('catalog_cache') or '')
        self.s3_region = str(p.get('s3_region') or 'us-east-1')
        self.s3_ak = str(p.get('s3_access_key') or '')
        self.s3_sk = str(p.get('s3_secret_key') or '')
        self.s3z = self.s3_zone_list(p)
        self.tls_ifaces = set(split_csv(p.get('tls_interfaces') if p.get('tls_interfaces') is not None
                                        else 'public'))
        self.tls_extra = split_csv(p.get('tls_extra'))
        self.tls_eps = {}
        self.stuck_s = max(1, num(p.get('stuck_minutes') or 60)) * 60
        self.cap = max(1, num(p.get('list_cap') or 1000))
        self.deadline_ms = num(p.get(tier + '_deadline_ms') or DEFAULTS[tier + '_deadline_ms'])
        self.workers = max(1, num(p.get('workers') or 8))
        timeout = num(p.get('http_timeout_ms') or 10000) / 1000.0
        verify = str(p.get('verify_tls', '1')).strip().lower() not in ('0', 'false', 'no')
        ca = str(p.get('ca_file') or '')
        self.ca_file = ca
        self.stats = Stats()
        self.http = Http(timeout, self.stats, verify, ca, True)
        self.http_node = Http(timeout, self.stats, verify, ca, False)
        self.token = ''
        self.user_id = ''
        self.ep = {}
        self.lock = threading.Lock()
        self.checks, self.agents, self.resources = [], {}, []
        self.started = 0

    # ---- plumbing ----
    def over_budget(self):
        return (now_ms() - self.started) > self.deadline_ms

    def label(self, svc):
        return LABELS.get(svc, svc)

    def skipped_svc(self, svc):
        e = self.ep.get(svc) or {}
        return svc in self.skip or e.get('type') in self.skip

    def cloud_only(self, svc, check):
        """True for an admin-only check while running with scope = project."""
        return self.project and (svc, check) in CLOUD_ONLY_CHECKS

    def path(self, p):
        """List path with {cap} filled; scope = project drops the all-projects filter."""
        p = p.format(cap=self.cap)
        return ALL_PROJECTS_RE.sub('', p) if self.project else p

    def skipped_check(self, check, svc=None):
        c = str(check or '').lower()
        names = [c] + (['%s:%s' % (str(svc).lower(), c)] if svc else [])
        return any(fnmatch.fnmatchcase(n, pat) for pat in self.skip_checks for n in names)

    def skipped_target(self, host='', zone=''):
        z = str(zone or '').strip().lower()
        h = str(host or '').strip()
        if z and z in self.skip_azs:
            return True
        if self.skip_hosts_re and h and self.skip_hosts_re.search(h):
            return True
        for az in self.skip_azs:
            if h and re.search(r'(^|[-_.])%s($|[-_.0-9])' % re.escape(az), h, re.I):
                return True
        return False

    def api_base(self, svc):
        base = self.ep[svc]['base']
        if svc == 'glance' and not re.search(r'/v\d+(\.\d+)?$', base):
            base += '/v2'
        return base

    def older(self, obj, fields):
        """True when the first timestamp found is older than stuck_minutes."""
        for f in fields:
            ts = parse_ts(obj.get(f))
            if ts is not None:
                return (time.time() - ts) > self.stuck_s
        return False

    def call(self, url, headers=None, sink=False, auth=True, client=None):
        if self.over_budget():
            raise Skipped('skipped: probe deadline reached')
        hdrs = dict(headers or {})
        if auth and self.token:
            hdrs['X-Auth-Token'] = self.token
        return (client or self.http).request('GET', url, hdrs, sink=sink)

    def new_rec(self, svc, check, target, layer, impact):
        return {'id': '%s:%s@%s' % (svc, check, target), 'service': svc,
                'label': self.label(svc), 'check': check, 'layer': layer,
                'target': target, 'node': '' if target == 'vip' else target,
                'impact': impact, 'http_status': SKIPPED, 'ms': 0, 'error': '',
                'skipped': 0}

    def add_check(self, rec):
        with self.lock:
            self.checks.append(rec)

    def skip_rec(self, svc, check, target, layer, impact, reason):
        rec = self.new_rec(svc, check, target, layer, impact)
        rec['skipped'], rec['error'] = 1, reason
        self.add_check(rec)
        return rec

    def check(self, svc, check, target, layer, impact, urls, headers=None,
              auth=True, client=None, sink=False):
        """One check = one record. First candidate URL that is not 404 wins.
        Returns (record, status, body)."""
        rec = self.new_rec(svc, check, target, layer, impact)
        status, body = SKIPPED, ''
        t0 = now_ms()
        try:
            last_exc, answered = None, False
            for u in urls:
                try:
                    status, _, body = self.call(u, headers, sink, auth, client)
                except ProbeError as e:
                    last_exc = e
                    continue
                answered = True
                if status != 404:
                    break
            if not answered:
                raise last_exc or ProbeError('no URL to probe')
            rec['http_status'] = status
            if status >= 400:
                rec['error'] = err_detail(status, '' if sink else body)
        except Skipped as e:
            rec['skipped'], rec['error'] = 1, str(e)
            status, body = SKIPPED, ''
        except ProbeError as e:
            rec['http_status'], rec['error'] = NO_RESPONSE, clean(e)
            status, body = NO_RESPONSE, ''
        rec['ms'] = now_ms() - t0
        self.add_check(rec)
        return rec, status, body

    def get_list(self, base, url, key, headers=None):
        """Follow pagination with self.call. Returns the joined list."""
        items, seen, pages = [], set([url]), 0
        while True:
            pages += 1
            status, _, body = self.call(url, headers)
            if status >= 400:
                raise ProbeError('page %d: %s' % (pages, err_detail(status, body)))
            parsed = json.loads(body)
            lst = parsed.get(key) if (key and isinstance(parsed, dict)) else parsed
            if isinstance(lst, list):
                items.extend(lst)
            nxt = next_link(parsed, key)
            if not nxt or pages >= MAX_PAGES:
                return items
            url = join_next(base, nxt)
            if url in seen:
                return items
            seen.add(url)

    # ---- authenticate + catalog ----
    def validate(self):
        if not self.auth_url:
            raise ValueError('auth_url is empty')
        if not re.match(r'^https?://[^/\s]+$', self.auth_url):
            raise ValueError('auth_url is not usable: >>>%s<<< expected exactly '
                             'http(s)://HOST:5000 with no trailing path' % self.auth_url)
        # Determine authentication method:
        # 1. Application Credential (cred_id + cred_secret)
        # 2. OpenStack User Password (username + password)
        if self.cred_id and self.cred_secret:
            self.auth_method = 'application_credential'
        elif (self.username or self.user_id_auth) and self.password:
            self.auth_method = 'password'
            # In password authentication, scope is required
            if not self.project_name and not self.project_id and not self.system_scope:
                sec = str(self.p.get('_section') or '')
                if self.project and sec:
                    self.project_name = sec
                elif not self.project:
                    self.project_name = 'admin'
        else:
            raise ValueError(
                'no valid OpenStack authentication configured: provide either '
                'Application Credential (cred_id + cred_secret) OR '
                'OpenStack User (username + password)'
            )

    def authenticate(self, out):
        """Fills out['auth']. Returns the catalog, or None when no token."""
        if self.auth_method == 'application_credential':
            auth_body = {
                'identity': {
                    'methods': ['application_credential'],
                    'application_credential': {'id': self.cred_id, 'secret': self.cred_secret}
                }
            }
        else:
            user_dict = {'password': self.password}
            if self.user_id_auth:
                user_dict['id'] = self.user_id_auth
            else:
                user_dict['name'] = self.username
                if self.user_domain_id:
                    user_dict['domain'] = {'id': self.user_domain_id}
                else:
                    user_dict['domain'] = {'name': self.user_domain_name or 'Default'}
            auth_body = {
                'identity': {
                    'methods': ['password'],
                    'password': {'user': user_dict}
                }
            }
            if self.system_scope:
                auth_body['scope'] = {'system': {'all': True}}
            elif self.project_id:
                auth_body['scope'] = {'project': {'id': self.project_id}}
            elif self.project_name:
                proj = {'name': self.project_name}
                if self.project_domain_id:
                    proj['domain'] = {'id': self.project_domain_id}
                else:
                    proj['domain'] = {'name': self.project_domain_name or 'Default'}
                auth_body['scope'] = {'project': proj}

        payload = json.dumps({'auth': auth_body})
        t0 = now_ms()
        try:
            status, hdrs, body = self.http.request(
                'POST', self.auth_url + '/v3/auth/tokens', data=payload.encode('utf-8'))
        except ProbeError as e:
            out['auth'] = {'http_status': NO_RESPONSE, 'ms': now_ms() - t0, 'error': clean(e)}
            return None
        out['auth'] = {'http_status': status, 'ms': now_ms() - t0, 'error': ''}
        if status != 201:
            out['auth']['error'] = err_detail(status, body)
            return None
        token = hdrs.get('X-Subject-Token') or ''
        if not token:
            out['auth']['error'] = 'no X-Subject-Token in the Keystone response'
            return None
        self.token = token
        tok = json.loads(body).get('token') or {}
        self.user_id = (tok.get('user') or {}).get('id') or ''
        proj = tok.get('project') or {}
        self.project_id = proj.get('id') or self.project_id
        self.project_name = proj.get('name') or self.project_name
        return tok.get('catalog') or []

    def walk_catalog(self, catalog):
        mapping_all = dict(CATALOG_TYPES)
        for ent in catalog:
            t = ent.get('type')
            if not t or t in IGNORED_TYPES:
                continue
            svc_t = mapping_all.get(t, t)
            if svc_t in self.skip or t in self.skip:
                continue
            for ep in ent.get('endpoints') or []:
                if ep.get('interface') in self.tls_ifaces:
                    self.tls_add(svc_t, ep.get('url'))
        by_type = {}
        for ent in catalog:
            t = ent.get('type')
            if not t or t in IGNORED_TYPES or t in by_type:
                continue
            for ep in ent.get('endpoints') or []:
                if ep.get('interface') == self.iface:
                    by_type[t] = (ent.get('name') or t, re.sub(r'/+$', '', str(ep.get('url') or '')))
                    break
        known = [t for t, _ in CATALOG_TYPES]
        order = known + sorted(t for t in by_type if t not in known)
        mapping = dict(CATALOG_TYPES)
        for t in order:
            if t not in by_type:
                continue
            svc = mapping.get(t, t)
            if svc in self.ep:
                continue
            name, base = by_type[t]
            if not base:
                continue
            if svc == 'keystone':
                base = re.sub(r'/v3$', '', base) + '/v3'
            self.ep[svc] = {'type': t, 'name': name, 'base': base,
                            'root': root_of(base), 'port': port_of(base)}

    def save_catalog(self):
        if not self.catalog_cache or not self.ep:
            return
        tmp = self.catalog_cache + '.tmp'
        try:
            with open(tmp, 'w') as fh:
                json.dump(self.ep, fh)
            os.replace(tmp, self.catalog_cache)
        except OSError:
            pass

    def load_catalog(self):
        if not self.catalog_cache:
            return False
        try:
            with open(self.catalog_cache) as fh:
                self.ep = json.load(fh)
            return bool(self.ep)
        except (OSError, ValueError):
            return False

    # ---- fast tier ----
    def fast_l2(self, pool):
        jobs = []
        for svc in sorted(self.ep):
            e = self.ep[svc]
            if self.skipped_svc(svc):
                self.skip_rec(svc, 'root', 'vip', 'L2', 'availability', 'skipped: skip_types')
                for n in self.nodes:
                    self.skip_rec(svc, 'root', n, 'L2', 'availability', 'skipped: skip_types')
                continue
            if self.skipped_check('root', svc):
                self.skip_rec(svc, 'root', 'vip', 'L2', 'availability', 'skipped: skip_checks')
                for n in self.nodes:
                    self.skip_rec(svc, 'root', n, 'L2', 'availability', 'skipped: skip_checks')
                continue
            jobs.append(pool.submit(self.check, svc, 'root', 'vip', 'L2', 'availability',
                                    [e['root']], None, False))
            for n in self.nodes:
                url = '%s://%s:%s/' % (self.node_scheme, n, e['port'])
                jobs.append(pool.submit(self.check, svc, 'root', n, 'L2', 'availability',
                                        [url], None, False, self.http_node))
        if self.horizon_url:
            jobs.append(pool.submit(self.check, 'horizon', 'login', 'vip', 'L2', 'availability',
                                    [self.horizon_url + '/auth/login/'],
                                    {'Accept': 'text/html'}, False))
        if self.novnc_url:
            # noVNC >= 1.2 ships vnc_lite.html, older images vnc_auto.html
            pages = ['/vnc_lite.html', '/vnc_auto.html']
            jobs.append(pool.submit(self.check, 'nova', 'novnc', 'vip', 'L2', 'availability',
                                    [self.novnc_url + pg for pg in pages],
                                    {'Accept': 'text/html'}, False))
            for n in self.nodes:
                base = '%s://%s:%d' % (self.node_scheme, n, self.novnc_port)
                jobs.append(pool.submit(self.check, 'nova', 'novnc', n, 'L2', 'availability',
                                        [base + pg for pg in pages], {'Accept': 'text/html'},
                                        False, self.http_node))
        # OpenSDN is not in the Keystone catalog: per-node roots from the ini.
        # The token is sent when we have one (config-api may run with aaa_mode=rbac).
        for n in self.sdn_nodes:
            for name, port in self.sdn_ports:
                jobs.append(pool.submit(self.check, 'opensdn', name, n, 'L2', 'availability',
                                        ['http://%s:%d/' % (n, port)], None, True,
                                        self.http_node))
        return jobs

    def fast_l3(self, svc, check, paths, headers, missing_ok, out):
        base = self.api_base(svc)
        rec, st, body = self.check(svc, check, 'vip', 'L3', 'availability',
                                   [base + p for p in paths], headers)
        if missing_ok and st == 404:
            rec['http_status'], rec['skipped'] = SKIPPED, 1
            rec['error'] = 'skipped: not enabled on this cloud (404)'
        if svc == 'glance' and check == 'stores' and 200 <= st < 300:
            try:
                out['glance_stores'] = len(json.loads(body).get('stores') or [])
            except (ValueError, AttributeError):
                pass
        if check == 'availability_zones' and 200 <= st < 300 and self.project:
            self.project_azs(svc, rec, body, out)

    def project_azs(self, svc, rec, body, out):
        """Nova / Cinder /os-availability-zone -> one row per AZ: available 1 / 0.
        A whole AZ that is not available (all nova-compute or cinder-volume of it
        down) is visible to any project member, no admin API needed."""
        try:
            zones = json.loads(body).get('availabilityZoneInfo') or []
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        rows = []
        for z in zones:
            name = str(z.get('zoneName') or '')
            if not name or self.skipped_target(zone=name):
                continue
            ok = bool((z.get('zoneState') or {}).get('available'))
            rows.append({'id': '%s:%s' % (svc, name), 'service': svc, 'label': self.label(svc),
                         'zone': name, 'available': 1 if ok else 0})
        with self.lock:
            out['azs'].extend(rows)

    def collect_agents(self, src):
        svc = src['service']
        base = self.ep[svc]['base']
        rec, st, body = self.check(svc, src['check'], 'vip', 'L3', 'availability',
                                   [base + p for p in src['paths']], src.get('headers'))
        if not (200 <= st < 300):
            return
        try:
            rows = (json.loads(body) or {}).get(src['key']) or []
        except ValueError:
            rec['error'] = 'response is not JSON'
            return
        rank = {UP: 2, MAINTENANCE: 1, DOWN: 0}
        for sv in rows:
            a = self.agent_from(svc, sv)
            if self.skipped_target(a['host'], a['zone']):
                continue
            with self.lock:
                old = self.agents.get(a['id'])
                # Heat keeps dead engine records per worker: any live record wins
                if old is None or rank[a['state']] > rank[old['state']]:
                    self.agents[a['id']] = a

    def agent_from(self, svc, sv):
        if svc == 'ironic':
            host, binary = sv.get('hostname') or '', 'ironic-conductor'
            up, enabled, forced, reason = bool(sv.get('alive')), True, False, ''
            zone = sv.get('conductor_group') or ''
        else:
            host, binary = sv.get('host') or '', sv.get('binary') or ''
            zone = sv.get('zone') or sv.get('availability_zone') or ''
            reason = str(sv.get('disabled_reason') or '')
            forced = bool(sv.get('forced_down'))
            if svc == 'heat':
                up, enabled = sv.get('status') == 'up', True
            elif svc == 'magnum':
                up, enabled = sv.get('state') == 'up', not sv.get('disabled')
            else:
                up, enabled = sv.get('state') == 'up', sv.get('status') != 'disabled'
        maint = host in self.maint or (not enabled and re.search('maintenance', reason, re.I))
        if maint:
            state = MAINTENANCE
        else:
            state = UP if (up and not forced) else DOWN
        return {'id': '%s@%s' % (binary, host), 'service': svc, 'label': self.label(svc),
                'binary': binary, 'host': host, 'zone': zone, 'state': state,
                'disabled': 0 if maint or (enabled and not forced) else 1,
                'reason': clean(reason, 100), 'impact': AGENT_IMPACT.get(binary, 'availability')}

    def run_fast(self, out, pool):
        out['agents'] = []
        out['glance_stores'] = -1
        out['s3_canary_mismatch'] = -1
        if self.project:
            out['azs'] = []
        jobs = self.s3_jobs(pool, out)
        if not self.project or self.project_l2:
            jobs += self.fast_l2(pool)
        l3 = FAST_L3 + (PROJECT_FAST_L3 if self.project else [])
        l3 = [x for x in l3 if not self.cloud_only(x[0], x[1])]
        agent_sources = [] if self.project else AGENT_SOURCES
        if self.token:
            for svc, check, paths, headers, missing_ok in l3:
                if svc not in self.ep:
                    continue
                if self.skipped_svc(svc):
                    self.skip_rec(svc, check, 'vip', 'L3', 'availability', 'skipped: skip_types')
                    continue
                if self.skipped_check(check, svc):
                    self.skip_rec(svc, check, 'vip', 'L3', 'availability', 'skipped: skip_checks')
                    continue
                jobs.append(pool.submit(self.fast_l3, svc, check, paths, headers, missing_ok, out))
            for src in agent_sources:
                if src['service'] in self.ep and not self.skipped_svc(src['service']):
                    if self.skipped_check(src['check'], src['service']):
                        self.skip_rec(src['service'], src['check'], 'vip', 'L3', 'availability',
                                      'skipped: skip_checks')
                        continue
                    jobs.append(pool.submit(self.collect_agents, src))
        else:
            # no token: keep L3 rows alive as skipped so items show -1, not stale data
            for svc, check, _, _, _ in l3:
                if svc in self.ep:
                    self.skip_rec(svc, check, 'vip', 'L3', 'availability', 'skipped: no token')
            for src in agent_sources:
                if src['service'] in self.ep:
                    self.skip_rec(src['service'], src['check'], 'vip', 'L3', 'availability',
                                  'skipped: no token')
        ref = pool.submit(self.ref_down) if self.ref_url else None
        for j in jobs:
            j.result()
        out['agents'] = sorted(self.agents.values(), key=lambda a: a['id'])
        if self.project:
            out['azs'] = sorted(out['azs'], key=lambda x: x['id'])
        out['probe_net_down'] = self.net_down(out, ref)

    def ref_down(self):
        try:
            self.http_node.request('GET', self.ref_url)
            return 0
        except ProbeError:
            return 1

    def net_down(self, out, ref):
        """1 = the probe host itself looks cut off (monitoring, not an outage).
        With ref_url: that URL got no response. Without: every attempted call,
        Keystone included, got no response from at least 2 different hosts."""
        if ref is not None:
            return ref.result()
        attempted = [(c['target'], c['http_status']) for c in self.checks
                     if c['layer'] == 'L2' and not c['skipped']]
        attempted.append(('auth', out['auth']['http_status']))
        hosts = set(t for t, _ in attempted)
        return 1 if len(hosts) >= 2 and all(s == NO_RESPONSE for _, s in attempted) else 0

    # ---- S3 / Ceph RGW (no Keystone token needed) ----
    @staticmethod
    def s3_zone_list(p):
        """[{zone, url, bucket, static, peers}] from s3_zones or the single s3_url."""
        def g(k):
            return str(p.get(k) or '').strip()
        zones = []
        names = [z.lower() for z in split_csv(p.get('s3_zones'))]
        if names:
            for z in names:
                if g('s3_%s_url' % z):
                    zones.append({'zone': z, 'url': g('s3_%s_url' % z).rstrip('/'),
                                  'bucket': g('s3_%s_bucket' % z),
                                  'static': g('s3_%s_static_url' % z), 'peers': []})
            for z in zones:                      # read every other zone's marker
                z['peers'] = [(o['zone'], o['bucket']) for o in zones
                              if o is not z and o['bucket']]
        elif g('s3_url'):
            zones.append({'zone': 'vip', 'url': g('s3_url').rstrip('/'), 'bucket': g('s3_bucket'),
                          'static': g('s3_static_url'),
                          'peers': [('peer', g('s3_peer_bucket'))] if g('s3_peer_bucket') else []})
        return zones

    def s3_call(self, z, method, bucket='', key='', body=None):
        """One signed S3 request on zone z's endpoint. Returns (status, body)."""
        if self.over_budget():
            raise Skipped('skipped: probe deadline reached')
        path = '/' + '/'.join(urllib.parse.quote(x, safe='-_.~/') for x in (bucket, key) if x)
        url = z['url'] + path
        hdrs = s3_sign(method, url, self.s3_ak, self.s3_sk, self.s3_region, body or b'')
        hdrs.update({'Accept': '*/*', 'Content-Type': 'application/octet-stream'})
        st, _, txt = self.http.request(method, url, hdrs, data=body)
        return st, txt

    def s3_check(self, z, check, layer, impact, fn):
        """Wrap S3 steps into one check record (target = zone, or vip for a single
        endpoint). fn(rec) returns the last HTTP status and sets rec['error'] itself."""
        rec = self.new_rec('s3', check, z['zone'], layer, impact)
        t0 = now_ms()
        try:
            rec['http_status'] = fn(rec)
        except Skipped as e:
            rec['skipped'], rec['error'] = 1, str(e)
        except ProbeError as e:
            rec['http_status'], rec['error'] = NO_RESPONSE, clean(e)
        rec['ms'] = now_ms() - t0
        self.add_check(rec)
        return rec

    def s3_step(self, z, rec, label, method, bucket='', key='', body=None):
        st, txt = self.s3_call(z, method, bucket, key, body)
        if st >= 300:
            rec['error'] = s3_err(st, txt, label)
        return st, txt

    def s3_anon(self, z, check, url):
        # anonymous GET: 200 ListAllMyBucketsResult or 403 AccessDenied = RGW answers
        rec, st, body = self.check('s3', check, z['zone'], 'L2', 'availability',
                                   [url], {'Accept': '*/*'}, False)
        if st >= 400:
            rec['error'] = s3_err(st, body)

    def s3_canary(self, z, out):
        payload = ('zbx-canary %d %s' % (time.time(), binascii.hexlify(os.urandom(8)).decode())
                   ).encode('utf-8')

        def run(rec):
            st, _ = self.s3_step(z, rec, 'PUT', 'PUT', z['bucket'], S3_CANARY_KEY, payload)
            if st >= 300:
                return st
            st, txt = self.s3_step(z, rec, 'GET', 'GET', z['bucket'], S3_CANARY_KEY)
            if st >= 300:
                return st
            bad = 0 if txt == payload.decode('utf-8') else 1
            with self.lock:
                out['s3_canary_mismatch'] = max(out['s3_canary_mismatch'], 0) + bad
            if bad:
                rec['error'] = 'GET returned different content than PUT'
            return self.s3_step(z, rec, 'DELETE', 'DELETE', z['bucket'], S3_CANARY_KEY)[0]
        self.s3_check(z, 'canary', 'L3', 'availability', run)

    def s3_repl_read(self, z, peer, bucket, out):
        def run(rec):
            st, txt = self.s3_step(z, rec, 'GET', 'GET', bucket, S3_MARKER_KEY)
            if st == 404:
                rec['error'] += (' (no marker from %s yet: its probe is not writing or '
                                 'replication does not reach this zone)' % peer)
            elif st < 300:
                ts = num(txt)
                if ts > 0:
                    lag = max(0, int(time.time()) - ts)
                    with self.lock:
                        out['s3_repl'].append({'id': '%s>%s' % (peer, z['zone']), 'from': peer,
                                               'to': z['zone'], 'lag_s': lag})
                        out['s3_repl_lag_s'] = max(out['s3_repl_lag_s'], lag)
                else:
                    rec['error'] = 'marker is not an epoch timestamp'
            return st
        check = 'repl_read' if peer == 'peer' else 'repl_from_%s' % peer
        self.s3_check(z, check, 'L3', 'monitoring', run)

    def s3_jobs(self, pool, out):
        """Submit the S3 checks of this tier for every zone. Independent of the token."""
        jobs = []
        keys = bool(self.s3_ak and self.s3_sk)
        for z in self.s3z:
            if self.tier == 'fast':
                jobs.append(pool.submit(self.s3_anon, z, 'root', z['url'] + '/'))
                if z['static']:
                    jobs.append(pool.submit(self.s3_anon, z, 'static_web', z['static']))
                if keys and z['bucket']:
                    jobs.append(pool.submit(self.s3_canary, z, out))
            elif self.tier == 'mid' and keys:
                jobs.append(pool.submit(self.s3_check, z, 'list_buckets', 'L3', 'monitoring',
                                        lambda rec, z=z: self.s3_step(z, rec, 'ListBuckets', 'GET')[0]))
                if z['bucket']:
                    jobs.append(pool.submit(self.s3_check, z, 'head_bucket', 'L3', 'monitoring',
                                            lambda rec, z=z: self.s3_step(
                                                z, rec, 'HEAD', 'HEAD', z['bucket'])[0]))
                    body = str(int(time.time())).encode('utf-8')
                    jobs.append(pool.submit(self.s3_check, z, 'repl_write', 'L3', 'monitoring',
                                            lambda rec, z=z, body=body: self.s3_step(
                                                z, rec, 'PUT', 'PUT', z['bucket'], S3_MARKER_KEY,
                                                body)[0]))
                for peer, bucket in z['peers']:
                    jobs.append(pool.submit(self.s3_repl_read, z, peer, bucket, out))
        return jobs

    # ---- mid tier ----
    def mid_hypervisors(self, out):
        url = self.ep['nova']['base'] + '/os-hypervisors/detail'
        hdr = {'OpenStack-API-Version': HV_MICROVERSION}
        rec, st, body = self.check('nova', 'hypervisors', 'vip', 'L3', 'monitoring', [url], hdr)
        if not (200 <= st < 300):
            return
        try:
            parsed = json.loads(body)
            rows = parsed.get('hypervisors') or []
            nxt = next_link(parsed, 'hypervisors')
            if nxt:
                base = self.ep['nova']['base']
                rows.extend(self.get_list(base, join_next(base, nxt), 'hypervisors', hdr))
        except (ValueError, ProbeError, Skipped) as e:
            rec['error'] = clean(e)
        hvs = []
        for h in rows:
            name = h.get('hypervisor_hostname') or ''
            sv = h.get('service') or {}
            host = sv.get('host') or name
            if self.skipped_target(host or name) or self.skipped_target(name):
                continue
            reason = str(sv.get('disabled_reason') or '')
            disabled = h.get('status') == 'disabled'
            if name in self.maint or host in self.maint or \
                    (disabled and re.search('maintenance', reason, re.I)):
                state = MAINTENANCE
            else:
                state = UP if h.get('state') == 'up' else DOWN
            hvs.append({
                'id': name, 'host': host, 'state': state,
                'schedulable': 1 if (state == UP and not disabled) else 0,
                'vcpus': num(h.get('vcpus')), 'vcpus_used': num(h.get('vcpus_used')),
                'vcpus_pct': pct(h.get('vcpus_used'), h.get('vcpus')),
                'memory_mb': num(h.get('memory_mb')), 'memory_mb_used': num(h.get('memory_mb_used')),
                'memory_pct': pct(h.get('memory_mb_used'), h.get('memory_mb')),
                'disk_gb': num(h.get('local_gb')), 'disk_gb_used': num(h.get('local_gb_used')),
                'disk_pct': pct(h.get('local_gb_used'), h.get('local_gb')),
                'running_vms': num(h.get('running_vms')),
            })
        out['hypervisors'] = sorted(hvs, key=lambda x: x['id'])

    def mid_placement(self, out):
        url = self.ep['placement']['base'] + '/resource_providers'
        rec, st, body = self.check('placement', 'resource_providers', 'vip', 'L3', 'monitoring',
                                   [url], {'OpenStack-API-Version': 'placement 1.14'})
        if not (200 <= st < 300):
            return
        try:
            rps = json.loads(body).get('resource_providers') or []
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        # nested providers (vGPU, NUMA) are children: count roots only
        out['placement_rp_roots'] = sum(
            1 for r in rps if r.get('root_provider_uuid', r.get('uuid')) == r.get('uuid'))

    def mid_pools(self, out):
        url = self.ep['cinder']['base'] + '/scheduler-stats/get_pools?detail=true'
        rec, st, body = self.check('cinder', 'pools', 'vip', 'L3', 'monitoring', [url])
        if not (200 <= st < 300):
            return
        try:
            rows = json.loads(body).get('pools') or []
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        pools = []
        for pl in rows:
            c = pl.get('capabilities') or {}
            pname = pl.get('name') or ''
            bname = c.get('volume_backend_name') or ''
            if self.skipped_target(pname) or self.skipped_target(bname):
                continue
            total, free = fnum(c.get('total_capacity_gb')), fnum(c.get('free_capacity_gb'))
            pools.append({
                'id': pname, 'backend': bname,
                'state': DOWN if str(c.get('backend_state') or 'up').lower() == 'down' else UP,
                'total_gb': total, 'free_gb': free,
                'free_pct': round(free * 100.0 / total, 2) if total > 0 and free >= 0 else -1,
            })
        out['pools'] = sorted(pools, key=lambda x: x['id'])

    def mid_ip_avail(self, out):
        base = self.api_base('neutron')
        urls = [base + p for p in ('/v2.0/network-ip-availabilities', '/network-ip-availabilities')]
        rec, st, body = self.check('neutron', 'ip_avail', 'vip', 'L3', 'monitoring', urls)
        if st == 404:
            rec['http_status'], rec['skipped'] = SKIPPED, 1
            rec['error'] = 'skipped: network-ip-availability extension not enabled (404)'
            return
        if not (200 <= st < 300):
            return
        try:
            rows = (json.loads(body) or {}).get('network_ip_availabilities') or []
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        nets = []
        for n in rows:
            if not isinstance(n, dict):
                continue
            total, used = num(n.get('total_ips')), num(n.get('used_ips'))
            if total <= 0:
                continue
            nid = str(n.get('network_id') or '')
            name = str(n.get('network_name') or nid)
            free = max(0, total - used)
            nets.append({
                'id': nid or name, 'name': name,
                'total_ips': total, 'used_ips': used, 'free_ips': free,
                'free_pct': round(free * 100.0 / total, 2),
            })
        out['ip_avail'] = sorted(nets, key=lambda x: (x['name'], x['id']))

    def count_job(self, svc, what, paths, headers, key, flt, kind='bad'):
        """kind=bad: error count (trigger); kind=busy: trend only (no trigger)."""
        base = self.api_base(svc)
        rec, st, body = self.check(svc, what, 'vip', 'L3', 'monitoring',
                                   [base + self.path(p) for p in paths], headers)
        res = {'id': '%s:%s' % (svc, what), 'service': svc, 'label': self.label(svc),
               'what': what, 'kind': kind, 'count': -1, 'capped': 0}
        if 200 <= st < 300:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and 'count' in parsed:
                    res['count'] = num(parsed['count'])
                elif 'with_count=true' in paths[0]:
                    rec['error'] = 'no count in response (needs %s)' % CINDER_COUNT_MV
                else:
                    lst = (parsed.get(key) if isinstance(parsed, dict) else parsed) or []
                    res['capped'] = 1 if len(lst) >= self.cap else 0
                    if flt:
                        lst = [o for o in lst if isinstance(o, dict) and flt(o, self)]
                    res['count'] = len(lst)
            except (ValueError, AttributeError, TypeError) as e:
                rec['error'] = 'bad response: %s' % clean(e, 60)
        with self.lock:
            self.resources.append(res)

    def state_job(self, svc, check_name, paths, headers, key, matchers, ts_fields, server_filtered=True):
        """Transitional state scan -> <what> (busy, now) and <what>_stuck (bad)."""
        if callable(matchers):
            matchers = [(check_name, matchers)]
        base = self.api_base(svc)
        rec, st, body = self.check(svc, check_name, 'vip', 'L3', 'monitoring',
                                   [base + self.path(p) for p in paths], headers)
        rows = []
        for w, _ in matchers:
            busy = {'id': '%s:%s' % (svc, w), 'service': svc, 'label': self.label(svc),
                    'what': w, 'kind': 'busy', 'count': -1, 'capped': 0}
            stuck = dict(busy, id=busy['id'] + '_stuck', what=w + '_stuck', kind='bad')
            rows.append((busy, stuck))
        if 200 <= st < 300:
            try:
                parsed = json.loads(body)
                lst = (parsed.get(key) if isinstance(parsed, dict) else parsed) or []
                lst = [o for o in lst if isinstance(o, dict)]
                if server_filtered and len(matchers) == 1 and any(not matchers[0][1](o) for o in lst):
                    # the API ignored the filter and listed everything: a count
                    # from that would be wrong, and the call is expensive
                    rec['error'] = ('filter ignored by the API (policy?) - %d of %d rows do '
                                    'not match' % (sum(1 for o in lst if not matchers[0][1](o)), len(lst)))
                else:
                    capped = 1 if len(lst) >= self.cap else 0
                    for (busy, stuck), (_, match) in zip(rows, matchers):
                        matched = [o for o in lst if match(o)]
                        busy['count'], busy['capped'] = len(matched), capped
                        stuck['count'] = sum(1 for o in matched if self.older(o, ts_fields))
                        stuck['capped'] = capped
            except (ValueError, AttributeError, TypeError) as e:
                rec['error'] = 'bad response: %s' % clean(e, 60)
        with self.lock:
            for busy, stuck in rows:
                self.resources.extend([busy, stuck])

    def mid_opensdn(self, uve, out):
        url = '%s/analytics/uves/%s/*?flat&cfilt=NodeStatus:process_status' % (self.sdn_url, uve)
        rec, st, body = self.check('opensdn', 'uve-' + uve, 'vip', 'L3', 'monitoring', [url])
        if not (200 <= st < 300):
            return
        try:
            parsed = json.loads(body)
        except ValueError:
            rec['error'] = 'response is not JSON'
            return
        rows = parsed.get('value') if isinstance(parsed, dict) else parsed
        procs = []
        for node in rows or []:
            if not isinstance(node, dict):
                continue
            host = str(node.get('name') or '')
            if self.skipped_target(host):
                continue
            ns = (node.get('value') or {}).get('NodeStatus') or {}
            for ps in ns.get('process_status') or []:
                module = str(ps.get('module_id') or '')
                down = ['%s:%s' % (c.get('type') or '', c.get('name') or '')
                        for c in ps.get('connection_infos') or [] if c.get('status') != 'Up']
                desc = str(ps.get('description') or '')
                # active/backup modules (schema, svc-monitor, device-manager): backup is normal
                ok = ps.get('state') == 'Functional' or 'backup' in desc.lower()
                procs.append({
                    'id': '%s:%s:%s' % (uve, host, module), 'type': uve, 'host': host,
                    'module': module, 'state': UP if ok else DOWN,
                    'down_count': len(down), 'down': clean(', '.join(down), 200),
                    'description': clean(ps.get('description') or '', 120)})
        with self.lock:
            out['opensdn'].extend(procs)

    # ---- quota usage (scope = project) ----
    def quota_row(self, svc, resource, used, limit):
        used, limit = num(used), num(limit)
        # limit -1 = unlimited, 0 = nothing allowed: no percentage either way
        pct = round(used * 100.0 / limit, 1) if limit > 0 else -1
        return {'id': '%s:%s' % (svc, resource), 'service': svc, 'label': self.label(svc),
                'resource': resource, 'used': used, 'limit': limit, 'pct': pct}

    def add_quotas(self, rows):
        with self.lock:
            self.quotas.extend(rows)

    def quota_limits(self, svc):
        """Nova / Cinder GET /limits: absolute max* and total*Used of this project."""
        rec, st, body = self.check(svc, 'quota', 'vip', 'L3', 'monitoring',
                                   [self.api_base(svc) + '/limits'])
        if not (200 <= st < 300):
            return
        try:
            ab = (json.loads(body).get('limits') or {}).get('absolute') or {}
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        self.add_quotas([self.quota_row(svc, res, ab.get(used), ab.get(mx))
                         for res, mx, used in QUOTA_LIMITS[svc] if mx in ab and used in ab])

    def quota_neutron(self):
        """Neutron GET /quotas/{project}/details: {resource: {used, limit, reserved}}."""
        base, pid = self.ep['neutron']['base'], self.project_id
        rec, st, body = self.check('neutron', 'quota', 'vip', 'L3', 'monitoring',
                                   [base + '/v2.0/quotas/%s/details' % pid,
                                    base + '/quotas/%s/details' % pid])
        if st == 404:            # quota-details extension not loaded (some plugins)
            rec['http_status'], rec['skipped'] = SKIPPED, 1
            rec['error'] = 'skipped: quota details not supported here (404)'
            return
        if not (200 <= st < 300):
            return
        try:
            q = json.loads(body).get('quota') or {}
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        self.add_quotas([self.quota_row('neutron', res, v.get('used'), v.get('limit'))
                         for res, v in sorted(q.items()) if isinstance(v, dict)])

    def quota_octavia(self):
        """Octavia has no usage in its quota API: limit from /quotas/{project},
        used = number of this project's load balancers."""
        base, pid = self.ep['octavia']['base'], self.project_id
        rec, st, body = self.check('octavia', 'quota', 'vip', 'L3', 'monitoring',
                                   [base + '/v2/lbaas/quotas/%s' % pid, base + '/lbaas/quotas/%s' % pid])
        if not (200 <= st < 300):
            return
        try:
            q = json.loads(body).get('quota') or {}
            limit = q.get('load_balancer', q.get('loadbalancer'))
            lbs = self.get_list(base, base + '/v2/lbaas/loadbalancers?fields=id&limit=%d' % self.cap,
                                'loadbalancers')
        except (ProbeError, ValueError, AttributeError) as e:
            rec['error'] = clean(e)
            return
        except Skipped:
            return
        if limit is not None:
            self.add_quotas([self.quota_row('octavia', 'load_balancers', len(lbs), limit)])

    def quota_manila(self):
        """Manila GET /quota-sets/{project}/detail (API 2.25+): {res: {in_use, limit, reserved}}."""
        base, pid = self.ep['manila']['base'], self.project_id
        rec, st, body = self.check('manila', 'quota', 'vip', 'L3', 'monitoring',
                                   [base + '/quota-sets/%s/detail' % pid],
                                   {'X-OpenStack-Manila-API-Version': '2.25'})
        if not (200 <= st < 300):
            return
        try:
            q = json.loads(body).get('quota_set') or {}
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        self.add_quotas([self.quota_row('manila', res, v.get('in_use'), v.get('limit'))
                         for res, v in sorted(q.items()) if isinstance(v, dict)])

    def quota_jobs(self, pool):
        jobs = []
        for svc in ('nova', 'cinder', 'neutron', 'octavia', 'manila'):
            if svc not in self.ep or self.skipped_svc(svc):
                continue
            if self.skipped_check('quota', svc):
                self.skip_rec(svc, 'quota', 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
                continue
            if svc in ('neutron', 'octavia', 'manila') and not self.project_id:
                self.skip_rec(svc, 'quota', 'vip', 'L3', 'monitoring',
                              'skipped: token has no project')
                continue
            fn = {'neutron': self.quota_neutron, 'octavia': self.quota_octavia,
                  'manila': self.quota_manila}.get(svc)
            jobs.append(pool.submit(fn) if fn else pool.submit(self.quota_limits, svc))
        return jobs

    def run_mid(self, out, pool):
        out.update({'hypervisors': [], 'pools': [], 'ip_avail': [], 'resources': [],
                    'placement_rp_roots': -1, 'opensdn': [], 'opensdn_modules': [],
                    's3_repl_lag_s': -1})
        out['s3_repl'] = []
        if self.project_quota:
            out['quotas'] = []
        jobs = self.s3_jobs(pool, out)
        if not self.token:
            for j in jobs:
                j.result()
            return
        if self.project:
            if self.project_quota:
                jobs += self.quota_jobs(pool)
        else:
            self.mid_cloud(out, pool, jobs)
        if self.count_res:
            counts = MID_COUNTS + (PROJECT_MID_COUNTS if self.project else [])
            states = MID_STATES + (PROJECT_MID_STATES if self.project else [])
            for svc, what, paths, headers, key, flt in counts:
                if svc in self.ep and not self.skipped_svc(svc) and not self.cloud_only(svc, what):
                    if self.skipped_check(what, svc):
                        self.skip_rec(svc, what, 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
                    else:
                        jobs.append(pool.submit(self.count_job, svc, what, paths, headers, key, flt))
            for svc, what, paths, headers, key, flt in (PROJECT_MID_TRENDS if self.project else []):
                if svc in self.ep and not self.skipped_svc(svc):
                    if self.skipped_check(what, svc):
                        self.skip_rec(svc, what, 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
                    else:
                        jobs.append(pool.submit(self.count_job, svc, what, paths, headers, key, flt,
                                                'busy'))
            for svc, what, paths, headers, key, match, ts in states:
                if svc in self.ep and not self.skipped_svc(svc) and not self.cloud_only(svc, what):
                    if self.skipped_check(what, svc):
                        self.skip_rec(svc, what, 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
                    else:
                        jobs.append(pool.submit(self.state_job, svc, what, paths, headers, key, match, ts))
        if self.sdn_url and not self.project:
            for uve in OPENSDN_UVES + (('vrouter',) if self.sdn_vrouters else ()):
                jobs.append(pool.submit(self.mid_opensdn, uve, out))
        for j in jobs:
            j.result()
        out['resources'] = sorted(self.resources, key=lambda x: x['id'])
        if self.project_quota:
            out['quotas'] = sorted(self.quotas, key=lambda x: x['id'])
        out['opensdn'] = sorted(out['opensdn'], key=lambda x: x['id'])
        # one module down on one node = stability; on every node = availability
        mods = {}
        for pr in out['opensdn']:
            m = mods.setdefault('%s:%s' % (pr['type'], pr['module']),
                                {'id': '%s:%s' % (pr['type'], pr['module']), 'type': pr['type'],
                                 'module': pr['module'], 'up': 0, 'total': 0})
            m['total'] += 1
            m['up'] += 1 if pr['state'] == UP else 0
        out['opensdn_modules'] = sorted(mods.values(), key=lambda x: x['id'])

    def mid_cloud(self, out, pool, jobs):
        """Cluster-wide collectors (admin API): scope = cloud only."""
        if 'nova' in self.ep:
            if self.skipped_check('hypervisors', 'nova'):
                self.skip_rec('nova', 'hypervisors', 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
            else:
                jobs.append(pool.submit(self.mid_hypervisors, out))
        if 'placement' in self.ep:
            if self.skipped_check('resource_providers', 'placement'):
                self.skip_rec('placement', 'resource_providers', 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
            else:
                jobs.append(pool.submit(self.mid_placement, out))
        if 'cinder' in self.ep:
            if self.skipped_check('pools', 'cinder'):
                self.skip_rec('cinder', 'pools', 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
            else:
                jobs.append(pool.submit(self.mid_pools, out))
        if 'neutron' in self.ep and not self.skipped_svc('neutron'):
            if self.skipped_check('ip_avail', 'neutron'):
                self.skip_rec('neutron', 'ip_avail', 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
            else:
                jobs.append(pool.submit(self.mid_ip_avail, out))

    # ---- slow tier ----
    def slow_canary(self):
        url = self.api_base('glance') + '/images/' + self.canary + '/file'
        rec, st, _ = self.check('glance', 'canary', 'vip', 'L3', 'availability', [url],
                                {'Accept': '*/*', 'Range': 'bytes=0-0'}, sink=True)
        if st == 404:
            rec['error'] = 'canary image %s not found' % self.canary

    def slow_amphorae(self, out):
        base = self.ep['octavia']['base']
        urls = [base + p + '?limit=%d' % self.cap for p in ('/v2/octavia/amphorae', '/octavia/amphorae')]
        rec, st, body = self.check('octavia', 'amphorae', 'vip', 'L3', 'monitoring', urls)
        if not (200 <= st < 300):
            return
        try:
            amps = json.loads(body).get('amphorae') or []
        except (ValueError, AttributeError):
            rec['error'] = 'response is not JSON'
            return
        out['octavia_amp_spare'] = sum(1 for a in amps if a.get('status') == 'READY'
                                       and not a.get('load_balancer_id'))
        days = [(parse_ts(a.get('cert_expiration')) - time.time()) / 86400.0
                for a in amps if a.get('status') not in ('DELETED', 'ERROR')
                and parse_ts(a.get('cert_expiration')) is not None]
        out['octavia_cert_min_days'] = round(min(days), 1) if days else -1

    def slow_credential(self, out):
        if not self.cred_id or self.auth_method != 'application_credential':
            out['cred_expires_days'] = -1
            return
        url = '%s/users/%s/application_credentials/%s' % (
            self.ep['keystone']['base'], self.user_id, self.cred_id)
        rec, st, body = self.check('keystone', 'app_credential', 'vip', 'L3', 'monitoring', [url])
        if not (200 <= st < 300):
            return
        try:
            exp = (json.loads(body).get('application_credential') or {}).get('expires_at')
        except (ValueError, AttributeError):
            return
        ts = parse_ts(exp)
        out['cred_expires_days'] = -1 if ts is None else round((ts - time.time()) / 86400.0, 1)

    def tls_add(self, svc, url):
        m = re.match(r'^https://([^/:\s]+)(?::(\d+))?', str(url or ''))
        if m:
            lst = self.tls_eps.setdefault((m.group(1).lower(), num(m.group(2) or 443)), [])
            if svc not in lst:
                lst.append(svc)

    def tls_one(self, host, port):
        """Days until the certificate of host:port expires, and an error text.
        A cert that fails verification (expired, unknown CA) is read again
        without verification, so its days are still reported."""
        ctx = ssl.create_default_context()
        if self.ca_file:
            ctx.load_verify_locations(cafile=self.ca_file)
        ctx.check_hostname = False
        err = ''
        try:
            with socket.create_connection((host, port), self.http.timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as ssock:
                    na = (ssock.getpeercert() or {}).get('notAfter')
        except ssl.SSLError as e:
            err = clean('verify failed: %s' % (getattr(e, 'verify_message', '') or e), 120)
            try:
                na = self.tls_unverified(host, port)
            except Exception as e2:
                return -1, clean('%s; %s' % (err, e2), 150)
        except Exception as e:
            return -1, clean(e or 'timed out', 150)
        if not na:
            return -1, err or 'no certificate'
        return round((ssl.cert_time_to_seconds(na) - time.time()) / 86400.0, 1), err

    def tls_unverified(self, host, port):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), self.http.timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
        fd, path = tempfile.mkstemp(suffix='.pem')
        try:
            with os.fdopen(fd, 'w') as fh:
                fh.write(ssl.DER_cert_to_PEM_cert(der))
            return ssl._ssl._test_decode_cert(path).get('notAfter')   # stdlib has no public DER parser
        finally:
            os.unlink(path)

    def slow_tls(self, out):
        for svc, u in (('keystone', self.auth_url), ('horizon', self.horizon_url),
                       ('novnc', self.novnc_url)):
            self.tls_add(svc, u)
        for z in self.s3z:
            self.tls_add('s3', z['url'])
            self.tls_add('s3', z['static'])
        for e in self.ep.values():                 # https internal roots, if any
            svc_t = MAP_TYPES.get(e.get('type'), e.get('type'))
            if not self.skipped_svc(svc_t):
                self.tls_add(svc_t, e.get('root'))
        for x in self.tls_extra:
            name, _, hp = x.rpartition('=')
            self.tls_add(name or 'extra', 'https://' + hp)
        rows = []
        if self.tls_eps:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(self.tls_eps))) as tp:
                futs = {k: tp.submit(self.tls_one, k[0], k[1]) for k in sorted(self.tls_eps)}
            for (host, port), f in sorted(futs.items()):
                days, err = f.result()
                svcs = self.tls_eps[(host, port)]
                rows.append({'id': '%s:%d' % (host, port), 'service': svcs[0],
                             'label': ', '.join(self.label(x) for x in svcs),
                             'host': host, 'port': port, 'days': days, 'error': err})
        out['tls_certs'] = rows
        days = [r['days'] for r in rows if r['days'] != -1 or not r['error']]
        if days:
            out['tls_cert_min_days'] = min(days)

    def run_slow(self, out, pool):
        out.update({'resources': [], 'tls_certs': [], 'cred_expires_days': -1,
                    'octavia_amp_spare': -1, 'octavia_cert_min_days': -1,
                    'tls_cert_min_days': -1})
        jobs = []
        if not self.project or self.project_tls:
            jobs.append(pool.submit(self.slow_tls, out))
        if not self.token:
            for j in jobs:
                j.result()
            return
        if self.canary and 'glance' in self.ep:
            jobs.append(pool.submit(self.slow_canary))
        if 'octavia' in self.ep and not self.skipped_svc('octavia') and not self.project:
            jobs.append(pool.submit(self.slow_amphorae, out))
        if 'keystone' in self.ep and self.user_id:
            jobs.append(pool.submit(self.slow_credential, out))
        if self.count_res:
            for svc, what, paths, headers, key, flt in SLOW_COUNTS:
                if svc in self.ep and not self.skipped_svc(svc) and not self.cloud_only(svc, what):
                    if self.skipped_check(what, svc):
                        self.skip_rec(svc, what, 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
                    else:
                        jobs.append(pool.submit(self.count_job, svc, what, paths, headers, key, flt))
            for svc, what, paths, headers, key, match, ts, sf in SLOW_STATES:
                if svc in self.ep and not self.skipped_svc(svc) and not self.cloud_only(svc, what):
                    if self.skipped_check(what, svc):
                        self.skip_rec(svc, what, 'vip', 'L3', 'monitoring', 'skipped: skip_checks')
                    else:
                        jobs.append(pool.submit(self.state_job, svc, what, paths, headers, key, match, ts, sf))
        for j in jobs:
            j.result()
        out['resources'] = sorted(self.resources, key=lambda x: x['id'])

    # ---- main ----
    def run(self):
        out = {'probe_name': PROBE_NAME, 'version': VERSION, 'tier': self.tier,
               'auth': {'http_status': SKIPPED, 'ms': 0, 'error': ''},
               'global_error': '', 'checks': []}
        self.started = now_ms()
        try:
            self.validate()
            catalog = self.authenticate(out)
            if catalog is not None:
                self.walk_catalog(catalog)
                if self.tier == 'fast':
                    self.save_catalog()
            elif self.tier == 'fast':
                self.load_catalog()
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                getattr(self, 'run_' + self.tier)(out, pool)
        except Exception as e:
            out['global_error'] = clean('%s: %s' % (type(e).__name__, e), 250)
        if self.project:
            out['scope'] = 'project'
            out['project'] = {'id': self.project_id, 'name': self.project_name}
        out['checks'] = sorted(self.checks, key=lambda c: c['id'])
        out['summary'] = self.summary(out)
        return out

    def summary(self, out):
        cs = out['checks']
        s = {
            'collect_ok': 0 if out['global_error'] else 1,
            'auth_status': out['auth']['http_status'],
            'auth_ms': out['auth']['ms'],
            'checks_total': len(cs),
            'checks_skipped': sum(1 for c in cs if c['skipped']),
            'checks_deadline': sum(1 for c in cs if c['error'] == 'skipped: probe deadline reached'),
            'checks_down': sum(1 for c in cs if not c['skipped'] and
                               (c['http_status'] == 0 or c['http_status'] >= 500)),
            'checks_4xx': sum(1 for c in cs if 400 <= c['http_status'] < 500),
            'requests': self.stats.requests,
            'bytes': self.stats.bytes,
        }
        if self.tier == 'fast':
            ag = out.get('agents') or []
            s.update({
                'catalog_services': len(self.ep) if self.token else -1,
                'probe_net_down': out.get('probe_net_down', 0),
                'glance_stores': out.get('glance_stores', -1),
                'agents_total': len(ag),
                'agents_down': sum(1 for a in ag if a['state'] == DOWN),
                'agents_maint': sum(1 for a in ag if a['state'] == MAINTENANCE),
                'agents_disabled': sum(1 for a in ag if a['disabled']),
                's3_canary_mismatch': out.get('s3_canary_mismatch', -1),
            })
            if self.project:
                azs = out.get('azs') or []
                s['azs_total'] = len(azs)
                s['azs_down'] = sum(1 for z in azs if not z['available'])
        elif self.tier == 'mid':
            hv = out.get('hypervisors') or []
            got = any(c['check'] == 'hypervisors' and 200 <= c['http_status'] < 300 for c in cs)
            s.update({
                'hypervisors_total': len(hv) if got else -1,
                'hypervisors_down': sum(1 for h in hv if h['state'] == DOWN) if got else -1,
                'hypervisors_maint': sum(1 for h in hv if h['state'] == MAINTENANCE) if got else -1,
                'hypervisors_schedulable': sum(1 for h in hv if h.get('schedulable')) if got else -1,
                'vcpus_total': sum(h['vcpus'] for h in hv),
                'vcpus_used': sum(h['vcpus_used'] for h in hv),
                'memory_mb_total': sum(h['memory_mb'] for h in hv),
                'memory_mb_used': sum(h['memory_mb_used'] for h in hv),
                'running_vms': sum(h['running_vms'] for h in hv),
                'placement_rp_roots': out.get('placement_rp_roots', -1),
                'pools_total': len(out.get('pools') or []),
                'opensdn_processes': len(out.get('opensdn') or []),
                'opensdn_down': sum(1 for x in out.get('opensdn') or [] if x['state'] == DOWN),
                's3_repl_lag_s': out.get('s3_repl_lag_s', -1),
            })
            if self.project_quota:
                qs = out.get('quotas') or []
                s['quotas_total'] = len(qs)
                s['quota_max_pct'] = max([q['pct'] for q in qs] or [-1])
        else:
            s.update({
                'cred_expires_days': out.get('cred_expires_days', -1),
                'octavia_amp_spare': out.get('octavia_amp_spare', -1),
                'octavia_cert_min_days': out.get('octavia_cert_min_days', -1),
                'tls_cert_min_days': out.get('tls_cert_min_days', -1),
                'tls_certs_total': len(out.get('tls_certs') or []),
                'tls_certs_failed': sum(1 for r in out.get('tls_certs') or [] if r['days'] == -1),
            })
        s['total_ms'] = now_ms() - self.started
        return s


# --------------------------------------------------------------------------
# Zabbix sender protocol (trapper) - no zabbix_sender binary needed
# --------------------------------------------------------------------------
def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProbeError('connection closed by Zabbix after %d/%d bytes' % (len(buf), n))
        buf += chunk
    return buf


def zbx_send(server, port, host, key, value, clock, timeout=15):
    """Send one value to a trapper item. Returns the 'info' string."""
    payload = json.dumps({'request': 'sender data',
                          'data': [{'host': host, 'key': key, 'value': value,
                                    'clock': int(clock), 'ns': 0}]},
                         ensure_ascii=False).encode('utf-8')
    packet = b'ZBXD\x01' + struct.pack('<II', len(payload), 0) + payload
    with socket.create_connection((server, int(port)), timeout) as s:
        s.sendall(packet)
        head = _recv_exact(s, 5)
        if head[:4] != b'ZBXD':
            raise ProbeError('not a Zabbix response from %s:%s' % (server, port))
        flags = head[4]
        if flags & 0x04:                      # large packet: 8-byte lengths
            dlen, _ = struct.unpack('<QQ', _recv_exact(s, 16))
        else:
            dlen, _ = struct.unpack('<II', _recv_exact(s, 8))
        data = _recv_exact(s, dlen)
        if flags & 0x02:                      # compressed
            data = zlib.decompress(data)
    resp = json.loads(data.decode('utf-8'))
    info = resp.get('info', '')
    if resp.get('response') != 'success':
        raise ProbeError('Zabbix refused: %s' % (resp,))
    m = re.search(r'failed:\s*(\d+)', info)
    if m and int(m.group(1)) > 0:
        raise ProbeError('Zabbix rejected the value (%s) - check host name "%s", item key '
                         '"%s", item type = Zabbix trapper, and that the host is monitored '
                         'by this proxy' % (info, host, key))
    return info


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def load_sections(args):
    if args.params:
        with open(args.params) as fh:
            p = dict(DEFAULTS)
            p.update(json.load(fh))
        p['_section'] = 'params'
        if args.host:
            p['zabbix_host'] = args.host
        return [('params', p)]
    cp = configparser.ConfigParser(defaults=DEFAULTS, interpolation=None)  # secrets may contain %
    if not cp.read(args.config):
        raise SystemExit('cannot read config %s' % args.config)
    names = args.section or cp.sections()
    if not names:
        raise SystemExit('no sections in %s' % args.config)
    out = []
    for n in names:
        if not cp.has_section(n):
            raise SystemExit('section [%s] not found in %s' % (n, args.config))
        p = dict(cp.items(n))
        p['_section'] = n
        if args.host and len(names) == 1:
            p['zabbix_host'] = args.host
        out.append((n, p))
    return out


def run_section(name, p, args):
    clock = time.time()
    try:
        result = Probe(p, args.tier).run()
    except Exception as e:                       # never leave the item silent
        result = {'probe_name': PROBE_NAME, 'version': VERSION, 'tier': args.tier,
                  'global_error': clean('probe crashed: %s' % e), 'checks': [],
                  'summary': {'collect_ok': 0}}
    blob = json.dumps(result, ensure_ascii=False,
                      indent=2 if args.pretty else None,
                      separators=None if args.pretty else (',', ':'))
    s = result.get('summary', {})
    line = ('[%s/%s] collect_ok=%s auth=%s checks=%s down=%s 4xx=%s skipped=%s '
            'requests=%s bytes=%s total_ms=%s size=%dB%s' % (
                name, args.tier, s.get('collect_ok'), s.get('auth_status'), s.get('checks_total'),
                s.get('checks_down'), s.get('checks_4xx'), s.get('checks_skipped'),
                s.get('requests'), s.get('bytes'), s.get('total_ms'), len(blob),
                (' error=' + result['global_error']) if result.get('global_error') else ''))
    if not args.send:
        return blob, line, True
    host = p.get('zabbix_host') or ''
    if not host:
        return None, line + ' | send FAILED: zabbix_host not set', False
    key = '%s.%s' % (p.get('item_prefix') or 'os.probe', args.tier)
    try:
        info = zbx_send(p['zabbix_server'], p['zabbix_port'], host, key, blob, clock)
        return None, line + ' | sent %s to %s:%s host="%s": %s' % (
            key, p['zabbix_server'], p['zabbix_port'], host, info), True
    except Exception as e:
        return None, line + ' | send FAILED: %s' % e, False


def main():
    ap = argparse.ArgumentParser(description='OpenStack API probe for Zabbix 6.0 (trapper, 3 tiers)')
    ap.add_argument('--tier', required=True, choices=TIERS)
    ap.add_argument('-c', '--config', default='/etc/zabbix/os_probe.ini')
    ap.add_argument('-s', '--section', action='append',
                    help='config section (AZ) to run; repeatable; default all')
    ap.add_argument('--params', help='JSON file with one section as an object (testing)')
    ap.add_argument('--host', help='override zabbix_host (single section only)')
    ap.add_argument('--send', action='store_true', help='push to the trapper item instead of printing')
    ap.add_argument('--pretty', action='store_true', help='indent the JSON output')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='summary line on stderr in print mode (always on with --send)')
    args = ap.parse_args()

    sections = load_sections(args)
    with ThreadPoolExecutor(max_workers=len(sections)) as pool:
        results = list(pool.map(lambda sp: run_section(sp[0], sp[1], args), sections))

    ok = True
    for blob, line, good in results:
        ok = ok and good
        if blob is not None:
            sys.stdout.write(blob + '\n')
        if args.send or args.verbose:
            sys.stderr.write(line + '\n')
    sys.stdout.flush()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
