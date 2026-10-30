#!/usr/bin/env python3
"""Build the OS-API-Probe Zabbix 6.0 template (YAML) for os_probe.py v2.

    python3 tools/build_api_probe.py zbx-template-os-api-probe.yaml
"""
import sys
import uuid
import yaml

T = 'OS-API-Probe'
NS = uuid.UUID('6f1c1d1e-3b8f-4c43-9a43-0b7c0a9e5a11')


def uid(name):
    """Deterministic UUIDv4-shaped id: stable across rebuilds, valid for import."""
    return uuid.UUID(bytes=uuid.uuid5(NS, name).bytes, version=4).hex


def fn(f, key, *args):
    return '%s(/%s/%s%s)' % (f, T, key, ''.join(',' + a for a in args))


def tags(**kw):
    return [{'tag': k, 'value': str(v)} for k, v in kw.items()]


# Triggers whose expression changed after their first import keep the uuid
# Zabbix already has, so a re-import updates them instead of recreating them.
KEEP_UUID = {
    'OpenStack {#LABEL}: {#CHECK} @ {#TARGET} is down (HTTP {ITEM.VALUE})': 'b77d32eb00404f709e080606b62845cf',
    'OpenStack {#LABEL}: {#BINARY} on {#HOST} is down': 'b8a5b5f893a64c9493de13fcb2f11427',
}


def trig(name, expr, prio, tg, desc='', deps=(), recovery=None, manual=False, opdata=''):
    # name alone is not unique (mid and slow share prototype names): seed with the expression too
    t = {'uuid': KEEP_UUID.get(name) or uid('trig:' + name + '|' + expr), 'expression': expr,
         'name': name, 'priority': prio}
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
    it['tags'] = tg or []
    return it


def lld_js(fields):
    lines = ['var a = JSON.parse(value), out = [];',
             'for (var i = 0; i < a.length; i++) {',
             '  out.push({' + ', '.join(
                 "at: a[i].target === 'vip' ? '' : ' @ ' + a[i].target"
                 if f == 'at' else '%s: a[i].%s' % (f, f)
                 for f in fields) + '});',
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
        # keep only the static fields so the unchanged-discard below works:
        # LLD then runs once an hour or when something is added / removed
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
MON = dict(service='monitoring', layer='L3', impact='monitoring')

# ------------------------------------------------------------------ masters
MASTER_DESC = {
    'fast': ('Every {$OS_FAST.INTERVAL} by systemd timer os-probe-fast.timer on the proxy host.\n'
             'L2 root per VIP and per node, Horizon login, token, L3 functional probes,\n'
             'os-services / services / mservices / conductors.'),
    'mid': ('Every {$OS_MID.INTERVAL} by os-probe-mid.timer. Hypervisors, placement root\n'
            'providers, Cinder pools, server-side filtered error counts.'),
    'slow': ('Every {$OS_SLOW.INTERVAL} by os-probe-slow.timer. Glance canary (Range 0-0),\n'
             'stuck resources, Heat FAILED, Octavia amphorae / certs, credential expiry.'),
}
M = {}
for tier in ('fast', 'mid', 'slow'):
    M[tier] = 'os.probe.' + tier
    items.append({
        'uuid': uid('item:' + M[tier]), 'name': 'Probe %s: raw JSON' % tier, 'type': 'TRAP',
        'key': M[tier], 'history': '{$OS_PROBE.RAW.HISTORY}', 'trends': '0',
        'value_type': 'TEXT',
        'description': MASTER_DESC[tier] + '\nPushed by: os_probe.py --tier %s --send' % tier,
        'tags': tags(service='monitoring', layer='L3', component='os-probe-' + tier)})

# ------------------------------------------------------------------ per-tier common items
FAIL, NODATA = {}, {}
for tier in ('fast', 'mid', 'slow'):
    m, p, comp = M[tier], 'os.%s.' % tier, 'os-probe-' + tier
    tg = tags(service='monitoring', component=comp)
    NODATA[tier] = trig('OpenStack probe %s: no data' % tier,
                        '%s=1' % fn('nodata', p + 'total_ms', '{$OS_%s.NODATA}' % tier.upper()),
                        'HIGH', tags(impact='monitoring', **dict(service='monitoring', component=comp)),
                        'No value from the %s timer. Check the systemd timer on the proxy host, '
                        'journalctl -u os-probe@%s, and that zabbix_host in the ini matches this '
                        'host name.' % (tier, tier))
    FAIL[tier] = trig('OpenStack probe %s: script failing' % tier,
                      '%s=0' % fn('last', p + 'collect_ok'), 'HIGH',
                      tags(impact='monitoring', service='monitoring', component=comp),
                      'Config error or crash - see item "Probe %s: global error". '
                      'This is the probe, not the cloud, so it never counts as downtime.' % tier)
    items += [
        item('Probe %s: script completed' % tier, p + 'collect_ok', m, '$.summary.collect_ok',
             vt='UNSIGNED', vmap='Collection status', tg=tg),
        item('Probe %s: global error' % tier, p + 'global_error', m, '$.global_error',
             vt='CHAR', history='7d', discard='1h',
             desc='Empty when healthy.', tg=tg),
        item('Probe %s: total runtime' % tier, p + 'total_ms', m, '$.summary.total_ms',
             units='ms', tg=tg),
        item('Probe %s: requests' % tier, p + 'requests', m, '$.summary.requests', tg=tg,
             desc='HTTP requests this round. Use it to tune the tier split.'),
        item('Probe %s: bytes received' % tier, p + 'bytes', m, '$.summary.bytes', units='B', tg=tg),
        item('Probe %s: checks down (0 or 5xx)' % tier, p + 'checks_down', m,
             '$.summary.checks_down', tg=tg),
        item('Probe %s: checks 4xx' % tier, p + 'checks_4xx', m, '$.summary.checks_4xx', tg=tg),
        item('Probe %s: checks skipped by deadline' % tier, p + 'checks_deadline', m,
             '$.summary.checks_deadline', tg=tg),
    ]
    items[-1]['triggers'] = [trig(
        'OpenStack probe %s: hitting its deadline' % tier,
        '%s>0' % fn('min', p + 'checks_deadline', '#3'), 'WARNING',
        tags(impact='monitoring', service='monitoring', component=comp),
        'Checks were skipped (http_status -1) because the run reached %s_deadline_ms. '
        'Raise the deadline in the ini or move checks to a slower tier.' % tier,
        deps=[FAIL[tier]])]
    # runtime + nodata triggers live on total_ms
    for it in items:
        if it['key'] == p + 'total_ms':
            it['triggers'] = [NODATA[tier], trig(
                'OpenStack probe %s: running slow' % tier,
                '%s>{$OS_%s.RUNTIME.WARN}' % (fn('min', p + 'total_ms', '#3'), tier.upper()),
                'WARNING', tags(impact='monitoring', service='monitoring', component=comp),
                'Close to %s_deadline_ms. Past the deadline the probe reports skipped checks.' % tier,
                deps=[FAIL[tier]])]
        if it['key'] == p + 'collect_ok':
            it['triggers'] = [FAIL[tier]]

# ------------------------------------------------------------------ fast fixed
f, m = 'os.fast.', M['fast']
NET = trig('OpenStack probe host cannot reach any endpoint',
           '%s=1' % fn('last', f + 'probe_net_down'), 'HIGH',
           tags(impact='monitoring', service='monitoring', component='os-probe-fast'),
           'Every attempted call got no response (or ref_url is unreachable). The proxy host '
           'network, routing or CA is broken - not the cloud. Every availability trigger '
           'depends on this one so a probe-side outage never counts as downtime.',
           deps=[FAIL['fast']])
AUTH_DOWN = trig('OpenStack Keystone: cannot issue tokens (HTTP {ITEM.VALUE})',
                 down_expr(f + 'auth.status', 4).replace('>=4', '>=3'), 'DISASTER',
                 tags(service='keystone', layer='L3', impact='availability', component='token',
                      http_status='{ITEM.VALUE}'),
                 '3 of the last 4 token requests got no response or 5xx. Keystone, its DB '
                 'or memcached is down - every OpenStack API needs a token.',
                 deps=[FAIL['fast'], NET],
                 recovery='%s>=200 and %s<300' % (fn('last', f + 'auth.status'), fn('last', f + 'auth.status')),
                 opdata='HTTP {ITEM.LASTVALUE}')
AUTH_4XX = trig('OpenStack probe credential rejected by Keystone (HTTP {ITEM.VALUE})',
                fourxx_expr(f + 'auth.status', accept=False), 'HIGH',
                tags(service='monitoring', layer='L3', impact='monitoring', component='token',
                     http_status='{ITEM.VALUE}'),
                '401: application credential expired, deleted or wrong secret. 403: role removed. '
                'The cloud may be fine - only the probe lost access.',
                deps=[FAIL['fast']], opdata='HTTP {ITEM.LASTVALUE}')
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
         desc='Services found in the catalog for the probed interface. -1 = no token.'),
    item('Probe fast: probe host cut off', f + 'probe_net_down', m, '$.summary.probe_net_down',
         vt='UNSIGNED', vmap='Probe network', tg=gtg),
    item('Glance: stores', f + 'glance_stores', m, '$.summary.glance_stores',
         tg=tags(service='glance', layer='L3', component='stores'),
         desc='-1 = multi-store not enabled or not collected.'),
    item('Service records: total', f + 'agents.total', m, '$.summary.agents_total', tg=gtg),
    item('Service records: down', f + 'agents.down', m, '$.summary.agents_down', tg=gtg),
    item('Service records: in maintenance', f + 'agents.maint', m, '$.summary.agents_maint', tg=gtg),
    item('Service records: disabled or forced down', f + 'agents.disabled', m,
         '$.summary.agents_disabled', tg=gtg),
]
for it in items:
    k = it['key']
    if k == f + 'auth.status':
        it['triggers'] = [AUTH_DOWN, AUTH_4XX]
    elif k == f + 'auth.ms':
        it['triggers'] = [trig('OpenStack Keystone: token issue is slow',
                               '%s>{$OS_AUTH.WARN}' % fn('min', k, '#3'), 'WARNING',
                               tags(service='keystone', layer='L3', impact='stability', component='token'),
                               deps=[AUTH_DOWN])]
    elif k == f + 'catalog_services':
        it['triggers'] = [trig('OpenStack Keystone: catalog lost a service',
                               '%s>=0 and %s<%s' % (fn('last', k), fn('last', k), fn('max', k, '1h')),
                               'HIGH', tags(service='keystone', layer='L3', impact='availability',
                                            component='catalog'),
                               'Fewer services in the catalog than during the last hour. Clears by '
                               'itself an hour after a planned removal, or close it manually.',
                               deps=[FAIL['fast']], manual=True)]
    elif k == f + 'probe_net_down':
        it['triggers'] = [NET]
    elif k == f + 'glance_stores':
        it['triggers'] = [trig('OpenStack Glance: a store disappeared',
                               '%s>=0 and %s<%s' % (fn('last', k), fn('last', k), fn('max', k, '1h')),
                               'HIGH', tags(service='glance', layer='L3', impact='availability',
                                            component='stores'),
                               deps=[FAIL['fast']], manual=True)]

# ------------------------------------------------------------------ fast LLD: API checks
CHECK_FIELDS = ['id', 'service', 'label', 'check', 'layer', 'target', 'node', 'impact', 'at']
ck = 'os.api.status["{#ID}"]'
P_DOWN = trig('OpenStack {#LABEL}: {#CHECK} @ {#TARGET} is down (HTTP {ITEM.VALUE})',
              down_expr(ck, 3), 'HIGH',
              tags(service='{#SERVICE}', layer='{#LAYER}', impact='availability',
                   component='{#CHECK}', node='{#NODE}', target='{#TARGET}', http_status='{ITEM.VALUE}'),
              '3 rounds in a row (~5-6m) with no response (0) or 5xx. 502/503/504 usually HAProxy or the '
              'WSGI workers; 500 the service or its DB / RabbitMQ; 0 VIP, keepalived, network or TLS. '
              'A VIP root check is raised to Disaster by an LLD override.',
              deps=[FAIL['fast'], NET], recovery=down_recovery(ck), opdata='HTTP {ITEM.LASTVALUE}')
P_4XX = trig('OpenStack {#LABEL}: {#CHECK} @ {#TARGET} rejects the probe (HTTP {ITEM.VALUE})',
             fourxx_expr(ck), 'AVERAGE',
             tags(service='{#SERVICE}', layer='{#LAYER}', impact='monitoring',
                  component='{#CHECK}', node='{#NODE}', target='{#TARGET}', http_status='{ITEM.VALUE}'),
             '401/403: probe credential, role or policy. 404: probe path does not match this '
             'service version. Not counted as downtime. A code that is normal for one service '
             'can be accepted with {$OS_API.ACCEPT:"<service>"} = <code>.',
             deps=[FAIL['fast']], opdata='HTTP {ITEM.LASTVALUE}')
P_SLOW = trig('OpenStack {#LABEL}: {#CHECK} @ {#TARGET} is slow',
              '%s>{$OS_LATENCY.WARN:"{#SERVICE}"}' % fn('min', 'os.api.ms["{#ID}"]', '#3'),
              'WARNING', tags(service='{#SERVICE}', layer='{#LAYER}', impact='stability',
                              component='{#CHECK}', node='{#NODE}', target='{#TARGET}'),
              deps=[P_DOWN], opdata='{ITEM.LASTVALUE}')
ptg = tags(service='{#SERVICE}', layer='{#LAYER}', component='{#CHECK}', node='{#NODE}')
llds.append(lld(
    'API checks', 'os.fast.discovery.checks', M['fast'], 'checks', CHECK_FIELDS,
    [item('{#LABEL} {#CHECK}{#AT}: HTTP status', ck, M['fast'], sel('checks', 'http_status'),
          vmap='HTTP status', trends='0', tg=ptg, proto=True,
          desc='Real HTTP code. 0 = no response, -1 = skipped (deadline, skip_types, no token).'),
     item('{#LABEL} {#CHECK}{#AT}: response time', 'os.api.ms["{#ID}"]', M['fast'],
          sel('checks', 'ms'), units='ms', tg=ptg, proto=True),
     item('{#LABEL} {#CHECK}{#AT}: error', 'os.api.error["{#ID}"]', M['fast'],
          sel('checks', 'error'), vt='CHAR', history='7d', discard='1h', tg=ptg, proto=True)],
    [P_DOWN, P_4XX, P_SLOW],
    desc='One row per check: L2 root per VIP and per node ({#TARGET}), L3 functional probes and '
         'service-record calls. All rows come from the fast master item - no extra API calls.',
    overrides=[{
        'name': 'VIP root check is Disaster', 'step': '1',
        'filter': {'evaltype': 'AND', 'conditions': [
            {'macro': '{#TARGET}', 'value': '^vip$', 'formulaid': 'A'},
            {'macro': '{#LAYER}', 'value': '^L2$', 'formulaid': 'B'}]},
        'operations': [{'operationobject': 'TRIGGER_PROTOTYPE', 'operator': 'LIKE',
                        'value': 'is down', 'severity': 'DISASTER'}]},
        {'name': 'S3 zone root check is Disaster', 'step': '2',
         'filter': {'evaltype': 'AND', 'conditions': [
             {'macro': '{#SERVICE}', 'value': '^s3$', 'formulaid': 'A'},
             {'macro': '{#CHECK}', 'value': '^root$', 'formulaid': 'B'}]},
         'operations': [{'operationobject': 'TRIGGER_PROTOTYPE', 'operator': 'LIKE',
                         'value': 'is down', 'severity': 'DISASTER'}]}]))
llds[-1]['filter'] = {'evaltype': 'AND', 'conditions': [
    {'macro': '{#TARGET}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'A'},
    {'macro': '{#NODE}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'}]}

# ------------------------------------------------------------------ fast LLD: service records
AG_FIELDS = ['id', 'service', 'label', 'binary', 'host', 'zone', 'impact']
sk = 'os.agent.state["{#ID}"]'
atg = tags(service='{#SERVICE}', layer='L3', component='{#BINARY}', node='{#HOST}')
llds.append(lld(
    'Service records', 'os.fast.discovery.agents', M['fast'], 'agents', AG_FIELDS,
    [item('{#LABEL} {#BINARY} on {#HOST}: state', sk, M['fast'], sel('agents', 'state'),
          vt='UNSIGNED', vmap='Service state', tg=atg, proto=True,
          desc='0 down / 1 up / 2 maintenance (disabled with a reason containing "maintenance", '
               'or listed in maint_hosts).'),
     item('{#LABEL} {#BINARY} on {#HOST}: disabled', 'os.agent.disabled["{#ID}"]', M['fast'],
          sel('agents', 'disabled'), vt='UNSIGNED', vmap='Yes/No', tg=atg, proto=True,
          desc='1 = disabled for a reason other than maintenance, or forced_down.'),
     item('{#LABEL} {#BINARY} on {#HOST}: disabled reason', 'os.agent.reason["{#ID}"]', M['fast'],
          sel('agents', 'reason'), vt='CHAR', history='7d', discard='1h', tg=atg, proto=True)],
    [trig('OpenStack {#LABEL}: {#BINARY} on {#HOST} is down',
          '%s>=3' % fn('count', sk, '#3', '"eq"', '"0"'), 'HIGH',
          tags(service='{#SERVICE}', layer='L3', impact='{#IMPACT}', component='{#BINARY}',
               node='{#HOST}'),
          'Service record reports down 3 rounds in a row (~5-6m). impact comes from the script: '
          'nova-compute = stability (one host), everything else = availability.',
          deps=[FAIL['fast']], recovery='%s<>0' % fn('last', sk)),
     trig('OpenStack {#LABEL}: {#BINARY} on {#HOST} is disabled',
          '%s=1' % fn('last', 'os.agent.disabled["{#ID}"]'), 'WARNING',
          tags(service='{#SERVICE}', layer='L3', impact='stability', component='{#BINARY}',
               node='{#HOST}'),
          'Disabled without "maintenance" in the reason, or forced_down. Use '
          '--disable-reason maintenance for planned work.', deps=[FAIL['fast']]),
     trig('OpenStack {#LABEL}: {#BINARY} on {#HOST} in maintenance longer than {$OS_MAINT.STALE}',
          '%s=2 and %s=2' % (fn('min', sk, '{$OS_MAINT.STALE}'), fn('max', sk, '{$OS_MAINT.STALE}')),
          'INFO', tags(service='{#SERVICE}', layer='L3', impact='stability',
                       component='{#BINARY}', node='{#HOST}'))],
    desc='Nova / Cinder os-services, Manila services, Heat services, Magnum mservices, Ironic '
         'conductors. One row per binary@host. Heat duplicates per worker are merged (any live '
         'record wins).'))
llds[-1]['filter'] = {'evaltype': 'AND', 'conditions': [
    {'macro': '{#ZONE}', 'value': '{$OS_SKIP.AZ.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'A'},
    {'macro': '{#HOST}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'}]}

# ------------------------------------------------------------------ mid / slow calls LLD
for tier in ('mid', 'slow'):
    ck = 'os.%s.call.status["{#ID}"]' % tier
    ctg = tags(service='{#SERVICE}', layer='{#LAYER}', component='{#CHECK}')
    c_down = trig('OpenStack {#LABEL}: %s call {#CHECK} failing (HTTP {ITEM.VALUE})' % tier,
                  down_expr(ck), 'WARNING',
                  tags(service='{#SERVICE}', layer='{#LAYER}', impact='{#IMPACT}',
                       component='{#CHECK}', http_status='{ITEM.VALUE}'),
                  'No response or 5xx on a %s-tier collector call. Its data (capacity, counts) is '
                  'stale. impact comes from the script: monitoring for collector calls, '
                  'availability for the Glance canary (raised to High by an LLD override).' % tier,
                  deps=[FAIL[tier]], recovery=down_recovery(ck), opdata='HTTP {ITEM.LASTVALUE}')
    c_4xx = trig('OpenStack {#LABEL}: %s call {#CHECK} rejected (HTTP {ITEM.VALUE})' % tier,
                 fourxx_expr(ck, accept=False), 'AVERAGE',
                 tags(service='{#SERVICE}', layer='{#LAYER}', impact='monitoring',
                      component='{#CHECK}', http_status='{ITEM.VALUE}'),
                 '401/403: the monitoring role lacks this policy (all_tenants, os-hypervisors, '
                 'get_pools ...). 404: path or microversion not supported here.',
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
RES_FIELDS = ['id', 'service', 'label', 'what', 'kind']
for tier in ('mid', 'slow'):
    rk = 'os.%s.res["{#ID}"]' % tier
    llds.append(lld(
        '%s resource counts' % tier.capitalize(), 'os.%s.discovery.res' % tier, M[tier],
        'resources', RES_FIELDS,
        [item('{#LABEL}: {#WHAT}', rk, M[tier], sel('resources', 'count'),
              tg=tags(service='{#SERVICE}', layer='L3', component='{#WHAT}', kind='{#KIND}'), proto=True,
              desc='-1 = not collected (see the matching call item). kind=busy is currently '
                   'in-progress (creating / deleting / pending ...); kind=bad is error or '
                   'stuck longer than stuck_minutes.')],
        [trig('OpenStack {#LABEL}: {#WHAT} = {ITEM.VALUE}',
              '%s>{$OS_RES.MAX:"{#ID}"}' % fn('min', rk, '#2'), 'WARNING',
              tags(service='{#SERVICE}', layer='L3', impact='resource', component='{#WHAT}'),
              'Customer resources in a bad or stuck state. Accept a known number with '
              '{$OS_RES.MAX:"{#ID}"} = <n>.', deps=[FAIL[tier]], opdata='{ITEM.LASTVALUE}')],
        overrides=[{
            'name': 'busy transitional counts do not create triggers', 'step': '1',
            'filter': {'evaltype': 'AND', 'conditions': [
                {'macro': '{#KIND}', 'value': '^busy$', 'formulaid': 'A'}]},
            'operations': [{'operationobject': 'TRIGGER_PROTOTYPE', 'operator': 'LIKE',
                            'value': '=', 'discover': 'NO_DISCOVER'}]}]))

# ------------------------------------------------------------------ mid fixed
d, m = 'os.mid.', M['mid']
ntg = tags(service='nova', layer='L3', component='hypervisors')
items += [
    item('Hypervisors: total', d + 'hv.total', m, '$.summary.hypervisors_total', tg=ntg),
    item('Hypervisors: down', d + 'hv.down', m, '$.summary.hypervisors_down', tg=ntg),
    item('Hypervisors: in maintenance', d + 'hv.maint', m, '$.summary.hypervisors_maint', tg=ntg),
    item('Hypervisors: schedulable', d + 'hv.schedulable', m, '$.summary.hypervisors_schedulable',
         tg=ntg, desc='state=up and status=enabled. -1 = not collected.'),
    item('Cluster: vCPU total', d + 'vcpus.total', m, '$.summary.vcpus_total', tg=ntg),
    item('Cluster: vCPU allocated', d + 'vcpus.used', m, '$.summary.vcpus_used', tg=ntg),
    item('Cluster: memory total', d + 'mem.total', m, '$.summary.memory_mb_total', units='B',
         mult='1048576', tg=ntg),
    item('Cluster: memory allocated', d + 'mem.used', m, '$.summary.memory_mb_used', units='B',
         mult='1048576', tg=ntg),
    item('Cluster: running VMs', d + 'vms', m, '$.summary.running_vms', tg=ntg),
    item('Placement: root resource providers', d + 'rp_roots', m, '$.summary.placement_rp_roots',
         tg=tags(service='placement', layer='L3', component='resource_providers'),
         desc='-1 = not collected.'),
    item('Cinder: pools', d + 'pools_total', m, '$.summary.pools_total',
         tg=tags(service='cinder', layer='L3', component='pools')),
    item('OpenSDN: processes', d + 'sdn.total', m, '$.summary.opensdn_processes',
         tg=tags(service='opensdn', layer='L3', component='process')),
    item('OpenSDN: processes down', d + 'sdn.down', m, '$.summary.opensdn_down',
         tg=tags(service='opensdn', layer='L3', component='process')),
]
[i for i in items if i['key'] == d + 'hv.schedulable'][0]['triggers'] = [trig(
    'OpenStack Nova: no schedulable hypervisor left',
    '%s=0 and %s>0' % (fn('last', d + 'hv.schedulable'), fn('last', d + 'hv.total')),
    'DISASTER', tags(service='nova', layer='L3', impact='availability', component='hypervisors'),
    'Every hypervisor is down or disabled - new VMs cannot be scheduled anywhere in this AZ.',
    deps=[FAIL['mid']])]
[i for i in items if i['key'] == d + 'rp_roots'][0]['triggers'] = [trig(
    'OpenStack Placement: root providers ({ITEM.LASTVALUE1}) differ from hypervisors ({ITEM.LASTVALUE2})',
    '%s>=0 and %s>=0 and %s<>%s' % (fn('last', d + 'rp_roots'), fn('last', d + 'hv.total'),
                                    fn('last', d + 'rp_roots'), fn('last', d + 'hv.total')),
    'WARNING', tags(service='placement', layer='L3', impact='stability', component='resource_providers'),
    'A compute node is missing from placement (cannot be scheduled) or a stale provider is left '
    'after a node was removed.', deps=[FAIL['mid']])]

# hypervisor LLD
hk = 'os.hv.state["{#HV}"]'
htg = tags(service='nova', layer='L3', component='nova-compute', node='{#HOST}')


def cap_pair(key, what, warn, high):
    hi = trig('OpenStack Nova: {#HV} %s allocation above {$%s}%%' % (what, high),
              '%s>{$%s}' % (fn('last', key), high), 'HIGH',
              tags(service='nova', layer='L3', impact='capacity', component='nova-compute',
                   node='{#HOST}'), opdata='{ITEM.LASTVALUE}')
    wa = trig('OpenStack Nova: {#HV} %s allocation above {$%s}%%' % (what, warn),
              '%s>{$%s}' % (fn('last', key), warn), 'WARNING',
              tags(service='nova', layer='L3', impact='capacity', component='nova-compute',
                   node='{#HOST}'), deps=[hi], opdata='{ITEM.LASTVALUE}')
    return [hi, wa]


hv_trigs = [trig('OpenStack Nova: hypervisor {#HV} is down', '%s>=2' % fn('count', hk, '#2', '"eq"', '"0"'),
                 'HIGH', tags(service='nova', layer='L3', impact='stability', component='nova-compute',
                              node='{#HOST}'),
                 'Hypervisor state down 2 rounds in a row (nova_libvirt or nova-compute on the host). '
                 'Disable it with --disable-reason maintenance for planned work.',
                 deps=[FAIL['mid']], recovery='%s<>0' % fn('last', hk))]
hv_trigs += cap_pair('os.hv.vcpu.pct["{#HV}"]', 'vCPU', 'OS_HV.CPU.WARN', 'OS_HV.CPU.HIGH')
hv_trigs += cap_pair('os.hv.mem.pct["{#HV}"]', 'memory', 'OS_HV.MEM.WARN', 'OS_HV.MEM.HIGH')
llds.append(lld(
    'Hypervisors', 'os.mid.discovery.hv', M['mid'], 'hypervisors', ['id', 'host'],
    [item('Hypervisor {#HV}: state', hk, M['mid'], sel('hypervisors', 'state', '{#HV}'),
          vt='UNSIGNED', vmap='Service state', tg=htg, proto=True),
     item('Hypervisor {#HV}: vCPU allocated', 'os.hv.vcpu.pct["{#HV}"]', M['mid'],
          sel('hypervisors', 'vcpus_pct', '{#HV}'), units='%', tg=htg, proto=True,
          desc='vcpus_used / vcpus. Can pass 100% with cpu_allocation_ratio > 1.'),
     item('Hypervisor {#HV}: memory allocated', 'os.hv.mem.pct["{#HV}"]', M['mid'],
          sel('hypervisors', 'memory_pct', '{#HV}'), units='%', tg=htg, proto=True),
     item('Hypervisor {#HV}: disk allocated', 'os.hv.disk.pct["{#HV}"]', M['mid'],
          sel('hypervisors', 'disk_pct', '{#HV}'), units='%', tg=htg, proto=True),
     item('Hypervisor {#HV}: running VMs', 'os.hv.vms["{#HV}"]', M['mid'],
          sel('hypervisors', 'running_vms', '{#HV}'), tg=htg, proto=True)],
    hv_trigs))
llds[-1]['lld_macro_paths'] = [{'lld_macro': '{#HV}', 'path': '$.id'},
                               {'lld_macro': '{#HOST}', 'path': '$.host'}]
llds[-1]['filter'] = {'evaltype': 'AND', 'conditions': [
    {'macro': '{#HV}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'A'},
    {'macro': '{#HOST}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'}]}

# Cinder pools LLD
pk = 'os.pool.free.pct["{#POOL}"]'
ptg = tags(service='cinder', layer='L3', component='{#BACKEND}', pool='{#POOL}')
pf_hi = trig('OpenStack Cinder: pool {#POOL} free below {$OS_POOL.FREE.HIGH}%',
             '%s>=0 and %s<{$OS_POOL.FREE.HIGH}' % (fn('last', pk), fn('last', pk)), 'HIGH',
             tags(service='cinder', layer='L3', impact='capacity', component='{#BACKEND}'),
             opdata='{ITEM.LASTVALUE}')
llds.append(lld(
    'Cinder pools', 'os.mid.discovery.pools', M['mid'], 'pools', ['id', 'backend'],
    [item('Cinder pool {#POOL}: backend state', 'os.pool.state["{#POOL}"]', M['mid'],
          sel('pools', 'state', '{#POOL}'), vt='UNSIGNED', vmap='Service state', tg=ptg, proto=True),
     item('Cinder pool {#POOL}: free', pk, M['mid'], sel('pools', 'free_pct', '{#POOL}'),
          units='%', tg=ptg, proto=True, desc='-1 = driver reports infinite / unknown.'),
     item('Cinder pool {#POOL}: total capacity', 'os.pool.total["{#POOL}"]', M['mid'],
          sel('pools', 'total_gb', '{#POOL}'), units='B', mult='1073741824', tg=ptg, proto=True),
     item('Cinder pool {#POOL}: free capacity', 'os.pool.free["{#POOL}"]', M['mid'],
          sel('pools', 'free_gb', '{#POOL}'), units='B', mult='1073741824', tg=ptg, proto=True)],
    [trig('OpenStack Cinder: pool {#POOL} backend is down',
          '%s>=2' % fn('count', 'os.pool.state["{#POOL}"]', '#2', '"eq"', '"0"'), 'HIGH',
          tags(service='cinder', layer='L3', impact='availability', component='{#BACKEND}'),
          deps=[FAIL['mid']], recovery='%s<>0' % fn('last', 'os.pool.state["{#POOL}"]')),
     pf_hi,
     trig('OpenStack Cinder: pool {#POOL} free below {$OS_POOL.FREE.WARN}%',
          '%s>=0 and %s<{$OS_POOL.FREE.WARN}' % (fn('last', pk), fn('last', pk)), 'WARNING',
          tags(service='cinder', layer='L3', impact='capacity', component='{#BACKEND}'),
          deps=[pf_hi], opdata='{ITEM.LASTVALUE}')]))
llds[-1]['lld_macro_paths'] = [{'lld_macro': '{#POOL}', 'path': '$.id'},
                               {'lld_macro': '{#BACKEND}', 'path': '$.backend'}]
llds[-1]['filter'] = {'evaltype': 'AND', 'conditions': [
    {'macro': '{#POOL}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'A'},
    {'macro': '{#BACKEND}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'}]}

# Neutron network IP availability LLD: removed (capacity only)

# ------------------------------------------------------------------ OpenSDN (mid)
sk = 'os.sdn.state["{#ID}"]'
stg = tags(service='opensdn', layer='L3', component='{#MODULE}', node='{#HOST}')
sdn_down = trig('OpenStack OpenSDN: {#MODULE} on {#HOST} is not functional',
                '%s>=2' % fn('count', sk, '#2', '"eq"', '"0"'), 'WARNING',
                tags(service='opensdn', layer='L3', impact='stability', component='{#MODULE}',
                     node='{#HOST}'),
                'One process instance is down; the other nodes still serve. The module-level '
                'trigger fires (availability) when no instance is left. See the description and '
                'down-connections items.', deps=[FAIL['mid']], recovery='%s<>0' % fn('last', sk))
llds.append(lld(
    'OpenSDN processes', 'os.mid.discovery.sdn', M['mid'], 'opensdn',
    ['id', 'type', 'host', 'module'],
    [item('OpenSDN {#MODULE} on {#HOST}: state', sk, M['mid'], sel('opensdn', 'state'),
          vt='UNSIGNED', vmap='Service state', tg=stg, proto=True,
          desc='From analytics UVE NodeStatus.process_status. Backup instances of active/backup '
               'modules count as up.'),
     item('OpenSDN {#MODULE} on {#HOST}: connections down', 'os.sdn.down_count["{#ID}"]', M['mid'],
          sel('opensdn', 'down_count'), vt='UNSIGNED', tg=stg, proto=True,
          desc='Backend connections not Up: Cassandra, Zookeeper, Kafka, Redis, RabbitMQ, collector.'),
     item('OpenSDN {#MODULE} on {#HOST}: down connections', 'os.sdn.down["{#ID}"]', M['mid'],
          sel('opensdn', 'down'), vt='CHAR', history='7d', discard='1h', tg=stg, proto=True),
     item('OpenSDN {#MODULE} on {#HOST}: description', 'os.sdn.desc["{#ID}"]', M['mid'],
          sel('opensdn', 'description'), vt='CHAR', history='7d', discard='1h', tg=stg, proto=True)],
    [sdn_down,
     trig('OpenStack OpenSDN: {#MODULE} on {#HOST} lost a backend connection',
          '%s>0' % fn('min', 'os.sdn.down_count["{#ID}"]', '#2'), 'WARNING',
          tags(service='opensdn', layer='L3', impact='stability', component='{#MODULE}',
               node='{#HOST}'), deps=[sdn_down], opdata='{ITEM.LASTVALUE}'),
      trig('OpenStack OpenSDN: {#MODULE} on {#HOST} stopped reporting',
           '%s=1' % fn('nodata', sk, '{$OS_MID.NODATA}'), 'WARNING',
           tags(service='opensdn', layer='L3', impact='stability', component='{#MODULE}',
                node='{#HOST}'),
           'The UVE disappeared - analytics lost the node, or the node was removed (the row then '
           'goes away after {$OS_LLD.LIFETIME}).', deps=[FAIL['mid']])],
    desc='Only when opensdn_analytics_url is set in the ini.'))
llds[-1]['filter'] = {'evaltype': 'AND', 'conditions': [
    {'macro': '{#HOST}', 'value': '{$OS_SKIP.HOST.NOT_MATCHES}',
     'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'A'}]}
mk = 'os.sdn.module.up["{#ID}"]'
llds.append(lld(
    'OpenSDN modules', 'os.mid.discovery.sdnmod', M['mid'], 'opensdn_modules',
    ['id', 'type', 'module'],
    [item('OpenSDN {#MODULE} ({#TYPE}): instances up', mk, M['mid'], sel('opensdn_modules', 'up'),
          vt='UNSIGNED', tg=tags(service='opensdn', layer='L3', component='{#MODULE}'), proto=True),
     item('OpenSDN {#MODULE} ({#TYPE}): instances total', 'os.sdn.module.total["{#ID}"]', M['mid'],
          sel('opensdn_modules', 'total'), vt='UNSIGNED',
          tg=tags(service='opensdn', layer='L3', component='{#MODULE}'), proto=True)],
    [trig('OpenStack OpenSDN: no functional {#MODULE} left ({#TYPE})',
          '%s>=2' % fn('count', mk, '#2', '"eq"', '"0"'), 'HIGH',
          tags(service='opensdn', layer='L3', impact='availability', component='{#MODULE}'),
          'Every instance of this module is down - the SDN function it provides is gone.',
          deps=[FAIL['mid']], recovery='%s>0' % fn('last', mk))]))

# ------------------------------------------------------------------ slow fixed
s, m = 'os.slow.', M['slow']
otg = tags(service='octavia', layer='L3', component='amphora')
cert_hi = trig('OpenStack Octavia: amphora certificate expires in {ITEM.VALUE} days',
               '%s>=0 and %s<{$OS_CERT.HIGH}' % (fn('last', s + 'amp.cert_days'), fn('last', s + 'amp.cert_days')),
               'HIGH', tags(service='octavia', layer='L3', impact='stability', component='amphora'),
               'The soonest amphora cert expiry. Rotate before it lapses or the controller loses '
               'contact with the amphora.', deps=[FAIL['slow']])
items += [
    item('Probe credential: days until expiry', s + 'cred.expire_days', m, '$.summary.cred_expires_days',
         units='d', tg=tags(service='monitoring', layer='L3', component='app_credential'),
         desc='-1 = never expires or not readable.',
         ),
    item('Octavia: spare amphorae', s + 'amp.spare', m, '$.summary.octavia_amp_spare', tg=otg,
         desc='READY amphorae with no load balancer. -1 = not collected.'),
    item('Octavia: soonest amphora cert expiry', s + 'amp.cert_days', m,
         '$.summary.octavia_cert_min_days', units='d', tg=otg, desc='-1 = none / not collected.'),
    item('TLS endpoints: soonest certificate expiry', s + 'tls.cert_days', m,
         '$.summary.tls_cert_min_days', units='d',
         tg=tags(service='keystone', layer='L2', component='tls_cert'),
         desc='Soonest TLS certificate expiry across HTTPS endpoints (auth_url, horizon_url, '
              'novnc_url, catalog roots). -1 = plain HTTP only or not reachable.'),
]
items[-4]['triggers'] = [trig(
    'OpenStack probe credential expires in {ITEM.VALUE} days',
    '%s>=0 and %s<{$OS_CRED.EXPIRE.WARN}' % (fn('last', s + 'cred.expire_days'), fn('last', s + 'cred.expire_days')),
    'WARNING', tags(service='monitoring', layer='L3', impact='monitoring', component='app_credential'),
    'Create a new application credential with the monitoring role and update the ini on every '
    'proxy host before this one expires.', deps=[FAIL['slow']])]
items[-3]['triggers'] = [trig(
    'OpenStack Octavia: spare amphorae below {$OS_OCTAVIA.SPARE.MIN}',
    '%s>=0 and %s<{$OS_OCTAVIA.SPARE.MIN}' % (fn('last', s + 'amp.spare'), fn('last', s + 'amp.spare')),
    'WARNING', tags(service='octavia', layer='L3', impact='capacity', component='amphora'),
    'Off by default ({$OS_OCTAVIA.SPARE.MIN} = 0). Set it if you run a spare pool.',
    deps=[FAIL['slow']])]
items[-2]['triggers'] = [cert_hi, trig(
    'OpenStack Octavia: amphora certificate expires in {ITEM.VALUE} days (warning)',
    '%s>=0 and %s<{$OS_CERT.WARN}' % (fn('last', s + 'amp.cert_days'), fn('last', s + 'amp.cert_days')),
    'WARNING', tags(service='octavia', layer='L3', impact='capacity', component='amphora'),
    deps=[cert_hi])]
# Aggregate TLS item stays (dashboards / history); alerting moved to the
# per-endpoint "TLS certificates" LLD below.
items[-1]['description'] += ' Alerts come from the TLS certificates discovery (one row per endpoint).'

# ------------------------------------------------------------------ slow LLD: TLS certificates
TLS_FIELDS = ['id', 'service', 'label', 'host', 'port']
tk, te = 'os.tls.days["{#ID}"]', 'os.tls.error["{#ID}"]'
ttg = tags(service='{#SERVICE}', layer='L2', component='tls_cert', endpoint='{#ID}')


def tls_trig(level, macro, prio, deps=()):
    return trig('TLS certificate {#ID} ({#LABEL}) expires in {ITEM.VALUE} days' +
                ('' if level == 'high' else ' (%s)' % level),
                # -1 = not readable (that has its own trigger), not "expired 1 day ago"
                '%s<>-1 and %s<{%s:"{#ID}"}' % (fn('last', tk), fn('last', tk), macro), prio,
                tags(service='{#SERVICE}', layer='L2', impact='stability', component='tls_cert',
                     endpoint='{#ID}'),
                'Renew the certificate of {#HOST}:{#PORT} ({#LABEL}). A negative value = already '
                'expired. Thresholds: {$OS_TLS.WARN} / {$OS_TLS.AVERAGE} / {$OS_TLS.HIGH} days, '
                'context = endpoint id.', deps=deps, opdata='{ITEM.LASTVALUE}')


tls_high = tls_trig('high', '$OS_TLS.HIGH', 'HIGH', [FAIL['slow']])
tls_avg = tls_trig('average', '$OS_TLS.AVERAGE', 'AVERAGE', [tls_high])
tls_warn = tls_trig('warning', '$OS_TLS.WARN', 'WARNING', [tls_avg])
tls_fail = trig('TLS certificate {#ID} ({#LABEL}) cannot be checked',
                '%s=-1 and %s=-1' % (fn('last', tk), fn('last', tk, '#2')), 'WARNING',
                tags(service='{#SERVICE}', layer='L2', impact='monitoring', component='tls_cert',
                     endpoint='{#ID}'),
                'No certificate could be read 2 rounds in a row: endpoint unreachable from the '
                'probe host, TLS handshake failure, or not https. See the error item.',
                deps=[FAIL['slow']], opdata='{ITEM.LASTVALUE}')
llds.append(lld(
    'TLS certificates', 'os.slow.discovery.tls', M['slow'], 'tls_certs', TLS_FIELDS,
    [item('TLS cert {#ID} ({#LABEL}): days until expiry', tk, M['slow'], sel('tls_certs', 'days'),
          units='d', tg=ttg, proto=True,
          desc='Read even when verification fails (expired / unknown CA). -1 = not readable.'),
     item('TLS cert {#ID} ({#LABEL}): error', te, M['slow'], sel('tls_certs', 'error'),
          vt='CHAR', history='7d', discard='1h', tg=ttg, proto=True,
          desc='Empty = verified OK. "verify failed: ..." = cert read but not trusted.')],
    [tls_high, tls_avg, tls_warn, tls_fail],
    desc='One row per https host:port: catalog endpoints of tls_interfaces (default public) + '
         'auth_url, horizon_url, novnc_url, s3_url, s3_static_url, tls_extra. One TLS handshake '
         'per row, no API call.'))

# ------------------------------------------------------------------ S3 / Ceph RGW fixed
# The S3 checks themselves (root, static_web, canary, list_buckets, head_bucket,
# repl_write, repl_read) are rows of the API checks / Mid calls LLDs (service=s3).
s3tg = tags(service='s3', layer='L3', component='canary')
items += [
    item('S3: canary content mismatch', 'os.fast.s3.canary_mismatch', M['fast'],
         '$.summary.s3_canary_mismatch', vmap='Yes/No', tg=s3tg,
         desc='1 = the object read back differs from the one just written. -1 = canary not run.'),
    item('S3: replication lag', 'os.mid.s3.repl_lag', M['mid'], '$.summary.s3_repl_lag_s',
         units='s', tg=tags(service='s3', layer='L3', component='replication'),
         desc='Age of the peer zone marker read through this zone (multisite sync lag + up to '
              'one mid interval). -1 = not collected (see the S3 mid repl_read call).'),
]
items[-2]['triggers'] = [trig(
    'OpenStack S3: canary read back different content',
    '%s=1' % fn('last', 'os.fast.s3.canary_mismatch'), 'HIGH',
    tags(service='s3', layer='L3', impact='availability', component='canary', target='vip'),
    'RGW returned other data than was just written: data integrity problem in RGW / Ceph.',
    deps=[FAIL['fast']])]
items[-1]['description'] += ' Worst direction; alerts come from the S3 replication discovery.'

rk = 'os.s3.repl.lag["{#ID}"]'
llds.append(lld(
    'S3 replication', 'os.mid.discovery.s3repl', M['mid'], 's3_repl', ['id', 'from', 'to'],
    [item('S3 replication {#FROM} -> {#TO}: lag', rk, M['mid'], sel('s3_repl', 'lag_s'), units='s',
          tg=tags(service='s3', layer='L3', component='replication', target='{#TO}'), proto=True,
          desc='Age of the {#FROM} marker read through the {#TO} endpoint (sync lag + up to one '
               'mid interval). A row appears once the marker was read successfully.')],
    [trig('OpenStack S3: replication {#FROM} -> {#TO} lag {ITEM.VALUE}',
          '%s>{$OS_S3.REPL.LAG.WARN}' % fn('min', rk, '#2'), 'WARNING',
          tags(service='s3', layer='L3', impact='stability', component='replication',
               zone_from='{#FROM}', zone_to='{#TO}'),
          'Objects written in {#FROM} are not visible in {#TO} yet. Check '
          '`radosgw-admin sync status` on both zones. If the marker cannot be read at all, the '
          'S3 mid repl_from_{#FROM} call item shows why.',
          deps=[FAIL['mid']], opdata='{ITEM.LASTVALUE}')],
    desc='One row per replication direction (s3_zones, or s3_peer_bucket with a single endpoint).'))

# ------------------------------------------------------------------ macros / valuemaps
MACROS = [
    ('{$OS_API.ACCEPT}', '0', 'Extra 4xx code that is normal for one service, so it does not raise the '
     '"rejects the probe" trigger. Context = service tag, e.g. {$OS_API.ACCEPT:"barbican"} = 401. 0 = none.'),
    ('{$OS_AUTH.WARN}', '5000', 'Token issue time warning / SLA threshold, ms (3 rounds > 5000ms).'),
    ('{$OS_CERT.HIGH}', '7', 'Octavia amphora cert expiry, days - High.'),
    ('{$OS_CERT.WARN}', '30', 'Octavia amphora cert expiry, days - Warning.'),
    ('{$OS_CRED.EXPIRE.WARN}', '14', 'Probe application credential expiry warning, days.'),
    ('{$OS_FAST.INTERVAL}', '2m', 'Documentation only: must match os-probe-fast.timer.'),
    ('{$OS_FAST.NODATA}', '10m', 'nodata() window for the fast master.'),
    ('{$OS_FAST.RUNTIME.WARN}', '30000', 'Warn when the fast run takes longer, ms. Keep below fast_deadline_ms (40000).'),
    ('{$OS_HV.CPU.HIGH}', '95', 'Hypervisor vCPU allocation, % - High.'),
    ('{$OS_HV.CPU.WARN}', '85', 'Hypervisor vCPU allocation, % - Warning.'),
    ('{$OS_HV.MEM.HIGH}', '95', 'Hypervisor memory allocation, % - High.'),
    ('{$OS_HV.MEM.WARN}', '85', 'Hypervisor memory allocation, % - Warning.'),
    ('{$OS_LATENCY.WARN}', '5000', 'API response time warning / SLA threshold, ms (3 rounds > 5000ms). Context = service tag, e.g. {$OS_LATENCY.WARN:"swift"} = 8000.'),
    ('{$OS_LLD.LIFETIME}', '7d', 'How long an undiscovered row survives (removed node, service).'),
    ('{$OS_MAINT.STALE}', '7d', 'Window for the "in maintenance longer than" trigger.'),
    ('{$OS_MID.INTERVAL}', '5m', 'Documentation only: must match os-probe-mid.timer.'),
    ('{$OS_MID.NODATA}', '20m', 'nodata() window for the mid master.'),
    ('{$OS_MID.RUNTIME.WARN}', '90000', 'Warn when the mid run takes longer, ms. Keep below mid_deadline_ms (120000).'),
    ('{$OS_OCTAVIA.SPARE.MIN}', '0', 'Minimum spare amphorae. 0 = trigger off.'),
    ('{$OS_POOL.FREE.HIGH}', '10', 'Cinder pool free space, % - High.'),
    ('{$OS_POOL.FREE.WARN}', '20', 'Cinder pool free space, % - Warning.'),
    ('{$OS_PROBE.RAW.HISTORY}', '1h', 'History of the 3 raw JSON masters. Short on purpose (DB writes); raise to 1d while debugging.'),
    ('{$OS_S3.REPL.LAG.WARN}', '900', 'S3 multisite replication lag warning, seconds (2 rounds). Includes up to one mid interval.'),
    ('{$OS_RES.MAX}', '0', 'Accepted bad / stuck resource count. Context = resource id, e.g. {$OS_RES.MAX:"heat:stacks_failed"} = 5.'),
    ('{$OS_SKIP.AZ.NOT_MATCHES}', '(?i)(^kkn$|[-_.]kkn([-_.]|$))', 'Regex of availability zones / cells to skip in discovery (e.g. unprovisioned AZs).'),
    ('{$OS_SKIP.HOST.NOT_MATCHES}', '(?i)(^kkn[-_.]|[-_.@]kkn([-_.0-9]|$))', 'Regex of hostnames / pools / targets to skip in discovery (e.g. unprovisioned AZs/nodes).'),
    ('{$OS_SLOW.INTERVAL}', '30m', 'Documentation only: must match os-probe-slow.timer.'),
    ('{$OS_SLOW.NODATA}', '75m', 'nodata() window for the slow master.'),
    ('{$OS_TLS.AVERAGE}', '7', 'TLS certificate expiry, days - Average. Context = endpoint id host:port.'),
    ('{$OS_TLS.HIGH}', '3', 'TLS certificate expiry, days - High. Context = endpoint id host:port.'),
    ('{$OS_TLS.WARN}', '30', 'TLS certificate expiry, days - Warning. Context = endpoint id host:port.'),
    ('{$OS_SLOW.RUNTIME.WARN}', '240000', 'Warn when the slow run takes longer, ms. Keep below slow_deadline_ms (300000).'),
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
    'Service state': [('0', 'Down'), ('1', 'Up'), ('2', 'Maintenance')],
    'Collection status': [('0', 'Failed'), ('1', 'OK')],
    'Probe network': [('0', 'OK'), ('1', 'Cut off')],
    'Yes/No': [('0', 'No'), ('1', 'Yes')],
}

DESC = """OpenStack (Kolla-Ansible) API monitoring for Zabbix 6.0, layers L2 + L3.

DATA: os_probe.py v2 runs on the Zabbix proxy host of the AZ from 3 systemd timers and pushes one
JSON per tier to a trapper master. Every other item is a dependent item or LLD row - no extra
API calls, no host interface, no External check.
  os.probe.fast  every 2m   L2 root per VIP and node, Horizon, token, L3 probes, service records
  os.probe.mid   every 5m   hypervisors, placement, Cinder pools, error counts, OpenSDN
  os.probe.slow  every 30m  Glance canary, stuck resources, Octavia amphorae, credential expiry

STATUS: every check stores the real HTTP code. 0 = no response, -1 = skipped.
  0 or >= 500, 2 rounds -> impact=availability (counts for SLA)
  400-499, 2 rounds     -> impact=monitoring (probe credential / policy / path)

TAGS: service / layer / impact / component / node / target (vip or node IP).
Add host tag az=<AZ> on the host.
Only impact=availability should be mapped to Services / SLA.

HOST SETUP: one host per AZ, monitored by that AZ's proxy, name = zabbix_host in
/etc/zabbix/os_probe.ini. No interface needed. Credentials live in the ini (chmod 600),
not in macros.

S3 (Ceph RGW): os_probe.py s3_* keys. service=s3 rows: root and static_web (L2, anonymous),
canary PUT/GET/DELETE (L3, availability), ListBuckets / HEAD / replication marker (mid,
monitoring), replication lag and canary content mismatch (fixed items).

TLS: one LLD row per https endpoint (public catalog endpoints + ini URLs), Warning < 30d,
Average < 7d, High < 3d, Warning when the cert cannot be read 2 rounds in a row.

Neutron agents are not checked: OpenSDN (contrail-neutron-plugin) has no agent extension.
"""

root_triggers = []
for it in items:
    root_triggers += it.pop('triggers', [])
root_triggers.sort(key=lambda t: t['name'])

tpl = {
    'uuid': uid('template:' + T), 'template': T, 'name': T, 'description': DESC,
    'groups': [{'name': 'Templates/OpenStack'}],
    'items': items, 'discovery_rules': llds,
    'macros': [{'macro': k, 'value': v, 'description': dsc} for k, v, dsc in MACROS],
    'valuemaps': [{'uuid': uid('vmap:' + n), 'name': n,
                   'mappings': [{'value': a, 'newvalue': b} for a, b in mp]}
                  for n, mp in VMAPS.items()],
}
doc = {'zabbix_export': {'version': '6.0', 'date': '2026-10-21T00:00:00Z',
                         'groups': [{'uuid': uid('group:Templates/OpenStack'),
                                     'name': 'Templates/OpenStack'}],
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
