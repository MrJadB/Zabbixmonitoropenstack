#!/usr/bin/env python3
"""Build the OS-API-Probe-Project Zabbix 6.0 template (YAML) for os_probe.py with
scope = project.

    python3 tools/build_api_probe_project.py zbx-template-os-api-probe-project.yaml

Kept apart from OS-API-Probe (scope = cloud) on purpose:
  - own template name and UUID namespace: importing one never touches the other
  - every item, item prototype, trigger and trigger prototype carries scope=project,
    so cloud Services / SLAs / dashboards can exclude these problems
  - no admin-only parts: service records, hypervisors, placement, Cinder pools,
    IP availability, OpenSDN, Octavia amphorae, S3
  - no quota usage (os_probe.py project_quota = 0)
Item keys are the same as OS-API-Probe (os.probe.<tier> ...): fine, because the
two templates are linked to different hosts and must never share one.
"""
import sys
import uuid
import yaml

T = 'OS-API-Probe-Project'
NS = uuid.UUID('5d9a6b2a-3467-4662-afff-f4e04db5f89e')     # not the OS-API-Probe namespace
GROUP = 'Templates/OpenStack'
GROUP_UUID = '64434e5f307a4d1ea6a34cb447f64166'           # same group as OS-API-Probe
SCOPE = 'project'


def uid(name):
    """Deterministic UUIDv4-shaped id: stable across rebuilds, valid for import."""
    return uuid.UUID(bytes=uuid.uuid5(NS, name).bytes, version=4).hex


def fn(f, key, *args):
    return '%s(/%s/%s%s)' % (f, T, key, ''.join(',' + a for a in args))


def tags(**kw):
    """Every tag list gets scope=project first."""
    out = [{'tag': 'scope', 'value': SCOPE}]
    return out + [{'tag': k, 'value': str(v)} for k, v in kw.items()]


def trig(name, expr, prio, tg, desc='', deps=(), recovery=None, manual=False, opdata=''):
    t = {'uuid': uid('trig:' + name + '|' + expr), 'expression': expr, 'name': name,
         'priority': prio}
    if recovery:
        t['recovery_mode'] = 'RECOVERY_EXPRESSION'
        t['recovery_expression'] = recovery
    if opdata:
        t['opdata'] = opdata
    if manual:
        t['manual_close'] = 'YES'
    if desc:
        t['description'] = desc
    if deps:
        t['dependencies'] = [{'name': d['name'], 'expression': d['expression'],
                              **({'recovery_expression': d['recovery_expression']}
                                 if 'recovery_expression' in d else {})} for d in deps]
    t['tags'] = tg
    return t


def item(name, key, master, path, vt='FLOAT', units='', vmap='', history='31d',
         trends=None, discard=None, mult=None, desc='', tg=None, proto=False):
    it = {'uuid': uid(('proto:' if proto else 'item:') + key), 'name': name,
          'type': 'DEPENDENT', 'key': key, 'delay': '0', 'history': history}
    if vt in ('CHAR', 'TEXT'):
        it['trends'] = '0'
    elif trends is not None:
        it['trends'] = trends
    if vt != 'UNSIGNED':
        it['value_type'] = vt
    if units:
        it['units'] = units
    if desc:
        it['description'] = desc
    if vmap:
        it['valuemap'] = {'name': vmap}
    pp = [{'type': 'JSONPATH', 'parameters': [path]}]
    if mult:
        pp.append({'type': 'MULTIPLIER', 'parameters': [mult]})
    if discard:
        pp.append({'type': 'DISCARD_UNCHANGED_HEARTBEAT', 'parameters': [discard]})
    it['preprocessing'] = pp
    it['master_item'] = {'key': master}
    it['tags'] = tg or tags()
    return it


def lld_js(fields):
    lines = ['var a = JSON.parse(value), out = [];',
             'for (var i = 0; i < a.length; i++) {',
             '  out.push({' + ', '.join('%s: a[i].%s' % (f, f) for f in fields) + '});',
             '}',
             'return JSON.stringify(out);']
    return '\n'.join(lines)


def lld(name, key, master, array, fields, protos, tprotos=None, desc='', overrides=None):
    r = {'uuid': uid('lld:' + key), 'name': name, 'type': 'DEPENDENT', 'key': key,
         'delay': '0', 'lifetime': '{$OS_LLD.LIFETIME}'}
    if desc:
        r['description'] = desc
    r['item_prototypes'] = protos
    if tprotos:
        r['trigger_prototypes'] = tprotos
    r['master_item'] = {'key': master}
    r['lld_macro_paths'] = [{'lld_macro': '{#%s}' % f.upper(), 'path': '$.%s' % f} for f in fields]
    r['preprocessing'] = [
        {'type': 'JSONPATH', 'parameters': ['$.' + array]},
        {'type': 'JAVASCRIPT', 'parameters': [lld_js(fields)]},
        {'type': 'DISCARD_UNCHANGED_HEARTBEAT', 'parameters': ['1h']},
    ]
    if overrides:
        r['overrides'] = overrides
    return r


def sel(array, field, macro='{#ID}'):
    return "$.%s[?(@.id=='%s')].%s.first()" % (array, macro, field)


def down_expr(key, n=2):
    return '%s+%s>=%d' % (fn('count', key, '#%d' % n, '"eq"', '"0"'),
                          fn('count', key, '#%d' % n, '"ge"', '"500"'), n)


def down_recovery(key):
    return '%s+%s=0' % (fn('count', key, '#2', '"eq"', '"0"'),
                        fn('count', key, '#2', '"ge"', '"500"'))


def fourxx_expr(key, accept=True):
    e = '%s-%s>=2' % (fn('count', key, '#2', '"ge"', '"400"'),
                      fn('count', key, '#2', '"ge"', '"500"'))
    if accept:
        e += ' and %s<>{$OS_API.ACCEPT:"{#SERVICE}"}' % fn('last', key)
    return e


items, llds = [], []
P = 'OpenStack project'          # trigger name prefix: never mistaken for a cloud-wide problem

# ------------------------------------------------------------------ masters
MASTER_DESC = {
    'fast': ('Every {$OS_FAST.INTERVAL} by os-probe-project-fast.timer on the proxy host.\n'
             'Token, catalog, L3 functional probes of this project (L2 roots only with project_l2 = 1).'),
    'mid': ('Every {$OS_MID.INTERVAL} by os-probe-project-mid.timer. Nothing project-specific\n'
            'by default (quota and resource counts are off); kept as a heartbeat.'),
    'slow': ('Every {$OS_SLOW.INTERVAL} by os-probe-project-slow.timer. Glance canary,\n'
             'credential expiry (TLS only with project_tls = 1).'),
}
M = {}
for tier in ('fast', 'mid', 'slow'):
    M[tier] = 'os.probe.' + tier
    items.append({
        'uuid': uid('item:' + M[tier]), 'name': 'Probe %s: raw JSON' % tier, 'type': 'TRAP',
        'key': M[tier], 'history': '{$OS_PROBE.RAW.HISTORY}', 'trends': '0',
        'value_type': 'TEXT',
        'description': MASTER_DESC[tier] + '\nPushed by: os_probe.py --tier %s --send '
                       '(ini: scope = project)' % tier,
        'tags': tags(service='monitoring', layer='L3', component='os-probe-' + tier)})

# ------------------------------------------------------------------ per-tier common items
FAIL, NODATA = {}, {}
for tier in ('fast', 'mid', 'slow'):
    m, p, comp = M[tier], 'os.%s.' % tier, 'os-probe-' + tier
    tg = tags(service='monitoring', component=comp)
    NODATA[tier] = trig('%s probe %s: no data' % (P, tier),
                        '%s=1' % fn('nodata', p + 'total_ms', '{$OS_%s.NODATA}' % tier.upper()),
                        'HIGH', tags(impact='monitoring', service='monitoring', component=comp),
                        'No value from the %s timer. Check the systemd timer on the proxy host, '
                        'journalctl -u os-probe-project@%s, and that zabbix_host in the ini matches '
                        'this host name.' % (tier, tier))
    FAIL[tier] = trig('%s probe %s: script failing' % (P, tier),
                      '%s=0' % fn('last', p + 'collect_ok'), 'HIGH',
                      tags(impact='monitoring', service='monitoring', component=comp),
                      'Config error or crash - see item "Probe %s: global error". '
                      'This is the probe, not the cloud, so it never counts as downtime.' % tier)
    items += [
        item('Probe %s: script completed' % tier, p + 'collect_ok', m, '$.summary.collect_ok',
             vt='UNSIGNED', vmap='Collection status', tg=tg),
        item('Probe %s: global error' % tier, p + 'global_error', m, '$.global_error',
             vt='CHAR', history='7d', discard='1h', desc='Empty when healthy.', tg=tg),
        item('Probe %s: total runtime' % tier, p + 'total_ms', m, '$.summary.total_ms',
             units='ms', tg=tg),
        item('Probe %s: requests' % tier, p + 'requests', m, '$.summary.requests', tg=tg),
        item('Probe %s: bytes received' % tier, p + 'bytes', m, '$.summary.bytes', units='B', tg=tg),
        item('Probe %s: checks down (0 or 5xx)' % tier, p + 'checks_down', m,
             '$.summary.checks_down', tg=tg),
        item('Probe %s: checks 4xx' % tier, p + 'checks_4xx', m, '$.summary.checks_4xx', tg=tg),
        item('Probe %s: checks skipped by deadline' % tier, p + 'checks_deadline', m,
             '$.summary.checks_deadline', tg=tg),
    ]
    items[-1]['triggers'] = [trig(
        '%s probe %s: hitting its deadline' % (P, tier),
        '%s>0' % fn('min', p + 'checks_deadline', '#3'), 'WARNING',
        tags(impact='monitoring', service='monitoring', component=comp),
        'Checks were skipped (http_status -1) because the run reached %s_deadline_ms.' % tier,
        deps=[FAIL[tier]])]
    for it in items:
        if it['key'] == p + 'total_ms':
            it['triggers'] = [NODATA[tier], trig(
                '%s probe %s: running slow' % (P, tier),
                '%s>{$OS_%s.RUNTIME.WARN}' % (fn('min', p + 'total_ms', '#3'), tier.upper()),
                'WARNING', tags(impact='monitoring', service='monitoring', component=comp),
                'Close to %s_deadline_ms.' % tier, deps=[FAIL[tier]])]
        if it['key'] == p + 'collect_ok':
            it['triggers'] = [FAIL[tier]]

# ------------------------------------------------------------------ fast fixed
f, m = 'os.fast.', M['fast']
NET = trig('%s probe host cannot reach any endpoint' % P,
           '%s=1' % fn('last', f + 'probe_net_down'), 'HIGH',
           tags(impact='monitoring', service='monitoring', component='os-probe-fast'),
           'ref_url is unreachable (set ref_url in the ini: with project_l2 = 0 it is the only '
           'way to tell a cut-off probe host from a cloud outage).', deps=[FAIL['fast']])
AUTH_DOWN = trig('%s Keystone: cannot issue tokens (HTTP {ITEM.VALUE})' % P,
                 down_expr(f + 'auth.status', 4).replace('>=4', '>=3'), 'HIGH',
                 tags(service='keystone', layer='L3', impact='availability', component='token',
                      http_status='{ITEM.VALUE}'),
                 '3 of the last 4 token requests of this project got no response or 5xx. '
                 'The cloud-scope host raises the Disaster for Keystone itself.',
                 deps=[FAIL['fast'], NET],
                 recovery='%s>=200 and %s<300' % (fn('last', f + 'auth.status'), fn('last', f + 'auth.status')),
                 opdata='HTTP {ITEM.LASTVALUE}')
AUTH_4XX = trig('%s credential rejected by Keystone (HTTP {ITEM.VALUE})' % P,
                fourxx_expr(f + 'auth.status', accept=False), 'HIGH',
                tags(service='monitoring', layer='L3', impact='monitoring', component='token',
                     http_status='{ITEM.VALUE}'),
                '401: application credential expired, deleted or wrong secret. 403: the user lost '
                'its role in the project.', deps=[FAIL['fast']], opdata='HTTP {ITEM.LASTVALUE}')
gtg = tags(service='monitoring', component='os-probe-fast')
items += [
    item('Keystone: token request status', f + 'auth.status', m, '$.auth.http_status',
         vmap='HTTP status', trends='0', tg=tags(service='keystone', layer='L3', component='token'),
         desc='201 = token issued. 0 = no response.'),
    item('Keystone: token request time', f + 'auth.ms', m, '$.auth.ms', units='ms',
         tg=tags(service='keystone', layer='L3', component='token')),
    item('Keystone: token request error', f + 'auth.error', m, '$.auth.error', vt='CHAR',
         history='7d', discard='1h', tg=tags(service='keystone', layer='L3', component='token')),
    item('Keystone: catalog services', f + 'catalog_services', m, '$.summary.catalog_services',
         tg=tags(service='keystone', layer='L3', component='catalog'),
         desc='Services in the catalog this project sees. -1 = no token.'),
    item('Keystone: project name', f + 'project.name', m, '$.project.name', vt='CHAR',
         history='7d', discard='1d', tg=tags(service='keystone', layer='L3', component='token'),
         desc='Project the application credential is scoped to.'),
    item('Probe fast: probe host cut off', f + 'probe_net_down', m, '$.summary.probe_net_down',
         vt='UNSIGNED', vmap='Probe network', tg=gtg),
]
for it in items:
    k = it['key']
    if k == f + 'auth.status':
        it['triggers'] = [AUTH_DOWN, AUTH_4XX]
    elif k == f + 'auth.ms':
        it['triggers'] = [trig('%s Keystone: token issue is slow' % P,
                               '%s>{$OS_AUTH.WARN}' % fn('min', k, '#3'), 'WARNING',
                               tags(service='keystone', layer='L3', impact='stability', component='token'),
                               deps=[AUTH_DOWN])]
    elif k == f + 'catalog_services':
        it['triggers'] = [trig('%s Keystone: catalog lost a service' % P,
                               '%s>=0 and %s<%s' % (fn('last', k), fn('last', k), fn('max', k, '1h')),
                               'HIGH', tags(service='keystone', layer='L3', impact='availability',
                                            component='catalog'),
                               'Fewer services in this project\'s catalog than during the last hour '
                               '(endpoint filter / project endpoint group changed?).',
                               deps=[FAIL['fast']], manual=True)]
    elif k == f + 'probe_net_down':
        it['triggers'] = [NET]

# ------------------------------------------------------------------ fast LLD: API checks
CHECK_FIELDS = ['id', 'service', 'label', 'check', 'layer', 'target', 'node', 'impact']
ck = 'os.api.status["{#ID}"]'
P_DOWN = trig('%s {#LABEL}: {#CHECK} @ {#TARGET} is down (HTTP {ITEM.VALUE})' % P,
              down_expr(ck, 3), 'HIGH',
              tags(service='{#SERVICE}', layer='{#LAYER}', impact='availability',
                   component='{#CHECK}', node='{#NODE}', target='{#TARGET}', http_status='{ITEM.VALUE}'),
              '3 rounds in a row (~5-6m) with no response (0) or 5xx when this project calls the API.',
              deps=[FAIL['fast'], NET], recovery=down_recovery(ck), opdata='HTTP {ITEM.LASTVALUE}')
P_4XX = trig('%s {#LABEL}: {#CHECK} @ {#TARGET} rejects the probe (HTTP {ITEM.VALUE})' % P,
             fourxx_expr(ck), 'AVERAGE',
             tags(service='{#SERVICE}', layer='{#LAYER}', impact='monitoring',
                  component='{#CHECK}', node='{#NODE}', target='{#TARGET}', http_status='{ITEM.VALUE}'),
             '401/403: the probe user lacks a role in this project, or the service policy does not '
             'let a member / reader list. 404: path not supported. Accept a code with '
             '{$OS_API.ACCEPT:"<service>"} = <code>, or skip it with skip_checks in the ini.',
             deps=[FAIL['fast']], opdata='HTTP {ITEM.LASTVALUE}')
P_SLOW = trig('%s {#LABEL}: {#CHECK} @ {#TARGET} is slow' % P,
              '%s>{$OS_LATENCY.WARN:"{#SERVICE}"}' % fn('min', 'os.api.ms["{#ID}"]', '#3'),
              'WARNING', tags(service='{#SERVICE}', layer='{#LAYER}', impact='stability',
                              component='{#CHECK}', node='{#NODE}', target='{#TARGET}'),
              deps=[P_DOWN], opdata='{ITEM.LASTVALUE}')
ptg = tags(service='{#SERVICE}', layer='{#LAYER}', component='{#CHECK}', node='{#NODE}')
llds.append(lld(
    'API checks', 'os.fast.discovery.checks', M['fast'], 'checks', CHECK_FIELDS,
    [item('{#LABEL} {#CHECK} @ {#TARGET}: HTTP status', ck, M['fast'], sel('checks', 'http_status'),
          vmap='HTTP status', trends='0', tg=ptg, proto=True,
          desc='Real HTTP code. 0 = no response, -1 = skipped (deadline, skip_types, no token).'),
     item('{#LABEL} {#CHECK} @ {#TARGET}: response time', 'os.api.ms["{#ID}"]', M['fast'],
          sel('checks', 'ms'), units='ms', tg=ptg, proto=True),
     item('{#LABEL} {#CHECK} @ {#TARGET}: error', 'os.api.error["{#ID}"]', M['fast'],
          sel('checks', 'error'), vt='CHAR', history='7d', discard='1h', tg=ptg, proto=True)],
    [P_DOWN, P_4XX, P_SLOW],
    desc='One row per check: L3 functional probes of this project (catalog, servers, volumes, '
         'networks, images, stacks, load balancers ...). L2 roots appear only with project_l2 = 1.'))

# ------------------------------------------------------------------ fast LLD: availability zones
# From Nova / Cinder /os-availability-zone (the availability_zones API checks).
items += [
    item('Availability zones: total', f + 'azs.total', M['fast'], '$.summary.azs_total',
         tg=tags(service='nova', layer='L3', component='availability_zone')),
    item('Availability zones: not available', f + 'azs.down', M['fast'], '$.summary.azs_down',
         tg=tags(service='nova', layer='L3', component='availability_zone'),
         desc='Nova + Cinder AZs that report available = false.'),
]
zk = 'os.az.available["{#ID}"]'
llds.append(lld(
    'Availability zones', 'os.fast.discovery.az', M['fast'], 'azs', ['id', 'service', 'label', 'zone'],
    [item('{#LABEL} AZ {#ZONE}: available', zk, M['fast'], sel('azs', 'available'),
          vt='UNSIGNED', vmap='Yes/No', trends='0', proto=True,
          tg=tags(service='{#SERVICE}', layer='L3', component='availability_zone', zone='{#ZONE}'),
          desc='zoneState.available. 0 = every nova-compute (Nova) or cinder-volume (Cinder) '
               'of the AZ is down or disabled: nothing new can be placed there.')],
    [trig('%s {#LABEL}: availability zone {#ZONE} is not available' % P,
          '%s>=2' % fn('count', zk, '#2', '"eq"', '"0"'), 'HIGH',
          tags(service='{#SERVICE}', layer='L3', impact='availability',
               component='availability_zone', zone='{#ZONE}', target='vip'),
          'This project cannot create {#LABEL} resources in {#ZONE} (2 rounds in a row). '
          'The cloud-scope host shows which service records / hypervisors are down.',
          deps=[FAIL['fast'], NET], recovery='%s=1' % fn('last', zk))],
    desc='One row per Nova and Cinder availability zone the project sees.'))

# ------------------------------------------------------------------ mid / slow calls LLD
for tier in ('mid', 'slow'):
    ck = 'os.%s.call.status["{#ID}"]' % tier
    ctg = tags(service='{#SERVICE}', layer='{#LAYER}', component='{#CHECK}')
    c_down = trig('%s {#LABEL}: %s call {#CHECK} failing (HTTP {ITEM.VALUE})' % (P, tier),
                  down_expr(ck), 'WARNING',
                  tags(service='{#SERVICE}', layer='{#LAYER}', impact='{#IMPACT}',
                       component='{#CHECK}', http_status='{ITEM.VALUE}'),
                  'No response or 5xx on a %s-tier call. impact comes from the script: monitoring '
                  'for collector calls, availability for the Glance canary (High by override).' % tier,
                  deps=[FAIL[tier]], recovery=down_recovery(ck), opdata='HTTP {ITEM.LASTVALUE}')
    c_4xx = trig('%s {#LABEL}: %s call {#CHECK} rejected (HTTP {ITEM.VALUE})' % (P, tier),
                 fourxx_expr(ck, accept=False), 'AVERAGE',
                 tags(service='{#SERVICE}', layer='{#LAYER}', impact='monitoring',
                      component='{#CHECK}', http_status='{ITEM.VALUE}'),
                 '401/403: policy does not let a project member / reader make this call. '
                 'Skip it with skip_checks = <service>:<check> in the ini.',
                 deps=[FAIL[tier]], opdata='HTTP {ITEM.LASTVALUE}')
    llds.append(lld(
        '%s calls' % tier.capitalize(), 'os.%s.discovery.checks' % tier, M[tier], 'checks',
        CHECK_FIELDS,
        [item('{#LABEL} %s {#CHECK}: HTTP status' % tier, ck, M[tier], sel('checks', 'http_status'),
              vmap='HTTP status', trends='0', tg=ctg, proto=True),
         item('{#LABEL} %s {#CHECK}: response time' % tier, 'os.%s.call.ms["{#ID}"]' % tier,
              M[tier], sel('checks', 'ms'), units='ms', tg=ctg, proto=True),
         item('{#LABEL} %s {#CHECK}: error' % tier, 'os.%s.call.error["{#ID}"]' % tier, M[tier],
              sel('checks', 'error'), vt='CHAR', history='7d', discard='1h', tg=ctg, proto=True)],
        [c_down, c_4xx],
        overrides=[{
            'name': 'availability calls are High', 'step': '1',
            'filter': {'evaltype': 'AND', 'conditions': [
                {'macro': '{#IMPACT}', 'value': '^availability$', 'formulaid': 'A'}]},
            'operations': [{'operationobject': 'TRIGGER_PROTOTYPE', 'operator': 'LIKE',
                            'value': 'failing', 'severity': 'HIGH'}]}]))

# ------------------------------------------------------------------ mid / slow resources LLD
# Removed: os_probe.py does not count error / stuck resources in project scope
# (project_resources = 0). The cloud scope (OS-API-Probe) counts them for every project.

# ------------------------------------------------------------------ mid: quota usage
# Removed: os_probe.py does not collect quota in project scope (project_quota = 0).

# ------------------------------------------------------------------ slow fixed
s, m = 'os.slow.', M['slow']
items += [
    item('Probe credential: days until expiry', s + 'cred.expire_days', m, '$.summary.cred_expires_days',
         units='d', tg=tags(service='monitoring', layer='L3', component='app_credential'),
         desc='-1 = never expires or not readable.'),
    item('TLS endpoints: soonest certificate expiry', s + 'tls.cert_days', m,
         '$.summary.tls_cert_min_days', units='d',
         tg=tags(service='keystone', layer='L2', component='tls_cert'),
         desc='Only with project_tls = 1. -1 = not collected.'),
]
items[-2]['triggers'] = [trig(
    '%s credential expires in {ITEM.VALUE} days' % P,
    '%s>=0 and %s<{$OS_CRED.EXPIRE.WARN}' % (fn('last', s + 'cred.expire_days'), fn('last', s + 'cred.expire_days')),
    'WARNING', tags(service='monitoring', layer='L3', impact='monitoring', component='app_credential'),
    'Create a new application credential in this project and update the ini on the proxy host.',
    deps=[FAIL['slow']])]

# ------------------------------------------------------------------ slow LLD: TLS (project_tls = 1)
TLS_FIELDS = ['id', 'service', 'label', 'host', 'port']
tk, te = 'os.tls.days["{#ID}"]', 'os.tls.error["{#ID}"]'
ttg = tags(service='{#SERVICE}', layer='L2', component='tls_cert', endpoint='{#ID}')


def tls_trig(level, macro, prio, deps=()):
    return trig('%s TLS certificate {#ID} ({#LABEL}) expires in {ITEM.VALUE} days' % P +
                ('' if level == 'high' else ' (%s)' % level),
                '%s<>-1 and %s<{%s:"{#ID}"}' % (fn('last', tk), fn('last', tk), macro), prio,
                tags(service='{#SERVICE}', layer='L2', impact='stability', component='tls_cert',
                     endpoint='{#ID}'), deps=deps, opdata='{ITEM.LASTVALUE}')


tls_high = tls_trig('high', '$OS_TLS.HIGH', 'HIGH', [FAIL['slow']])
tls_avg = tls_trig('average', '$OS_TLS.AVERAGE', 'AVERAGE', [tls_high])
tls_warn = tls_trig('warning', '$OS_TLS.WARN', 'WARNING', [tls_avg])
llds.append(lld(
    'TLS certificates', 'os.slow.discovery.tls', M['slow'], 'tls_certs', TLS_FIELDS,
    [item('TLS cert {#ID} ({#LABEL}): days until expiry', tk, M['slow'], sel('tls_certs', 'days'),
          units='d', tg=ttg, proto=True, desc='-1 = not readable.'),
     item('TLS cert {#ID} ({#LABEL}): error', te, M['slow'], sel('tls_certs', 'error'),
          vt='CHAR', history='7d', discard='1h', tg=ttg, proto=True)],
    [tls_high, tls_avg, tls_warn],
    desc='Empty unless project_tls = 1 (the cloud-scope host already checks the certificates).'))

# ------------------------------------------------------------------ macros / valuemaps
MACROS = [
    ('{$OS_API.ACCEPT}', '0', 'Extra 4xx code that is normal for one service. Context = service tag.'),
    ('{$OS_AUTH.WARN}', '5000', 'Token issue time warning, ms (3 rounds).'),
    ('{$OS_CRED.EXPIRE.WARN}', '14', 'Probe application credential expiry warning, days.'),
    ('{$OS_FAST.INTERVAL}', '2m', 'Documentation only: must match os-probe-project-fast.timer.'),
    ('{$OS_FAST.NODATA}', '10m', 'nodata() window for the fast master.'),
    ('{$OS_FAST.RUNTIME.WARN}', '30000', 'Warn when the fast run takes longer, ms.'),
    ('{$OS_LATENCY.WARN}', '5000', 'API response time warning, ms (3 rounds). Context = service tag.'),
    ('{$OS_LLD.LIFETIME}', '7d', 'How long an undiscovered row survives.'),
    ('{$OS_MID.INTERVAL}', '5m', 'Documentation only: must match os-probe-project-mid.timer.'),
    ('{$OS_MID.NODATA}', '20m', 'nodata() window for the mid master.'),
    ('{$OS_MID.RUNTIME.WARN}', '90000', 'Warn when the mid run takes longer, ms.'),
    ('{$OS_PROBE.RAW.HISTORY}', '1h', 'History of the 3 raw JSON masters.'),
    ('{$OS_SLOW.INTERVAL}', '30m', 'Documentation only: must match os-probe-project-slow.timer.'),
    ('{$OS_SLOW.NODATA}', '75m', 'nodata() window for the slow master.'),
    ('{$OS_SLOW.RUNTIME.WARN}', '240000', 'Warn when the slow run takes longer, ms.'),
    ('{$OS_TLS.AVERAGE}', '7', 'TLS certificate expiry, days - Average (project_tls = 1 only).'),
    ('{$OS_TLS.HIGH}', '3', 'TLS certificate expiry, days - High (project_tls = 1 only).'),
    ('{$OS_TLS.WARN}', '30', 'TLS certificate expiry, days - Warning (project_tls = 1 only).'),
]
HTTP = [('-1', 'Skipped'), ('0', 'No response'), ('200', 'OK'), ('201', 'Created'),
        ('202', 'Accepted'), ('204', 'No Content'), ('206', 'Partial Content'),
        ('300', 'Multiple Choices'), ('301', 'Moved Permanently'), ('302', 'Found'),
        ('400', 'Bad Request'), ('401', 'Unauthorized'), ('403', 'Forbidden'),
        ('404', 'Not Found'), ('405', 'Method Not Allowed'), ('406', 'Not Acceptable'),
        ('409', 'Conflict'), ('413', 'Payload Too Large'), ('415', 'Unsupported Media Type'),
        ('429', 'Too Many Requests'), ('500', 'Internal Server Error'), ('501', 'Not Implemented'),
        ('502', 'Bad Gateway'), ('503', 'Service Unavailable'), ('504', 'Gateway Timeout')]
VMAPS = {
    'HTTP status': HTTP,
    'Collection status': [('0', 'Failed'), ('1', 'OK')],
    'Probe network': [('0', 'OK'), ('1', 'Cut off')],
    'Yes/No': [('0', 'No'), ('1', 'Yes')],
}

DESC = """OpenStack API monitoring of ONE project (scope = project) for Zabbix 6.0.

DATA: os_probe.py (ini: scope = project) runs on the Zabbix proxy host from 3 systemd timers
(os-probe-project@fast|mid|slow) and pushes one JSON per tier to a trapper master.
  os.probe.fast  every 2m   token, catalog, L3 functional probes of the project
  os.probe.mid   every 5m   heartbeat only (quota / resource counts off)
  os.probe.slow  every 30m  Glance canary, credential expiry

Needs only a member or reader role in the project - no admin API, no policy change.

SEPARATION FROM OS-API-Probe (cloud scope):
  every item and trigger has tag scope=project; set host tags scope=project and
  project=<name> and NO az tag, so cloud Services / SLAs (they need az=<AZ>) never count
  these problems. Never link this template and OS-API-Probe to the same host.

STATUS: every check stores the real HTTP code. 0 = no response, -1 = skipped.
  0 or >= 500 -> impact=availability (project SLA), 400-499 -> impact=monitoring.
"""

root_triggers = []
for it in items:
    root_triggers += it.pop('triggers', [])
root_triggers.sort(key=lambda t: t['name'])

tpl = {
    'uuid': uid('template:' + T), 'template': T, 'name': T, 'description': DESC,
    'groups': [{'name': GROUP}],
    'items': items, 'discovery_rules': llds,
    'tags': [{'tag': 'scope', 'value': SCOPE}],
    'macros': [{'macro': k, 'value': v, 'description': dsc} for k, v, dsc in MACROS],
    'valuemaps': [{'uuid': uid('vmap:' + n), 'name': n,
                   'mappings': [{'value': a, 'newvalue': b} for a, b in mp]}
                  for n, mp in VMAPS.items()],
}
doc = {'zabbix_export': {'version': '6.0', 'date': '2026-10-27T00:00:00Z',
                         'groups': [{'uuid': GROUP_UUID, 'name': GROUP}],
                         'templates': [tpl], 'triggers': root_triggers}}


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):      # Zabbix import does not read YAML anchors
        return True


def str_rep(d, s):
    if '\n' in s:
        return d.represent_scalar('tag:yaml.org,2002:str', s, style='|')
    return d.represent_scalar('tag:yaml.org,2002:str', s)


Dumper.add_representer(str, str_rep)
out = yaml.dump(doc, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=4096,
                default_flow_style=False, indent=2)
open(sys.argv[1], 'w').write(out)
print('items', len(items), 'llds', len(llds))
