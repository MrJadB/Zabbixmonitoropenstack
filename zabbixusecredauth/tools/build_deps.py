#!/usr/bin/env python3
"""Build zbx-template-os-deps.yaml (Zabbix 6.0): OS-Deps-Node, OS-Deps-Cluster,
OS-Host, OS-Proxy-Watch.

    python3 tools/build_deps.py zbx-template-os-deps.yaml

UUIDs are derived from names, so a rebuild keeps them stable and a re-import
updates the templates instead of duplicating them.
"""
import sys
import uuid
import yaml

NS = uuid.UUID('9d0b7f2e-6a51-4f0e-8c7e-1f4f0c2b7d33')
GROUP = 'Templates/OpenStack'


def uid(name):
    return uuid.UUID(bytes=uuid.uuid5(NS, name).bytes, version=4).hex


def tags(**kw):
    return [{'tag': k, 'value': str(v)} for k, v in kw.items()]


class Tpl(object):
    def __init__(self, name, desc):
        self.name, self.desc = name, desc
        self.items, self.llds, self.macros, self.triggers = [], [], [], []

    def fn(self, f, key, *args):
        return '%s(/%s/%s%s)' % (f, self.name, key, ''.join(',' + a for a in args))

    def item(self, name, key, typ='DEPENDENT', vt='FLOAT', master=None, pp=None, delay=None,
             history='31d', units='', vmap='', desc='', tg=None, proto=False, **extra):
        it = {'uuid': uid('%s:%s:%s' % (self.name, 'proto' if proto else 'item', key)),
              'name': name}
        if typ != 'ZABBIX_PASSIVE':
            it['type'] = typ
        it['key'] = key
        if typ == 'DEPENDENT':
            it['delay'] = '0'
        elif delay:
            it['delay'] = delay
        it['history'] = history
        if vt in ('CHAR', 'TEXT'):
            it['trends'] = '0'
        if vt != 'UNSIGNED':
            it['value_type'] = vt
        if units:
            it['units'] = units
        it.update(extra)
        if desc:
            it['description'] = desc
        if vmap:
            it['valuemap'] = {'name': vmap}
        if pp:
            it['preprocessing'] = pp
        if master:
            it['master_item'] = {'key': master}
        it['tags'] = tg or []
        return it

    def add(self, *items):
        self.items.extend(items)
        return items[0]

    def trig(self, name, expr, prio, tg, desc='', deps=(), recovery=None, opdata='', manual=False):
        t = {'uuid': uid('%s:trig:%s|%s' % (self.name, name, expr)), 'expression': expr, 'name': name,
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
            t['dependencies'] = [dict((k, d[k]) for k in ('name', 'expression', 'recovery_expression')
                                      if k in d) for d in deps]
        t['tags'] = tg
        return t

    def root_trig(self, *a, **kw):
        t = self.trig(*a, **kw)
        self.triggers.append(t)
        return t

    def macro(self, name, value, desc, secret=False):
        m = {'macro': name}
        if secret:
            m['type'] = 'SECRET_TEXT'
        if value is not None:
            m['value'] = value
        m['description'] = desc
        self.macros.append(m)

    def export(self, vmaps):
        return {'uuid': uid('template:' + self.name), 'template': self.name, 'name': self.name,
                'description': self.desc, 'groups': [{'name': GROUP}], 'items': self.items,
                'discovery_rules': self.llds,
                'macros': sorted(self.macros, key=lambda m: m['macro']),
                'valuemaps': [{'uuid': uid('%s:vmap:%s' % (self.name, n)), 'name': n,
                               'mappings': [{'value': a, 'newvalue': b} for a, b in VMAPS[n]]}
                              for n in vmaps]}


def jp(path):
    return {'type': 'JSONPATH', 'parameters': [path]}


def js(code):
    return {'type': 'JAVASCRIPT', 'parameters': [code]}


def discard(hb='1h'):
    return {'type': 'DISCARD_UNCHANGED_HEARTBEAT', 'parameters': [hb]}


def lld_map(fields):
    return js('\n'.join([
        'var a = JSON.parse(value), out = [];',
        'for (var i = 0; i < a.length; i++) {',
        '  out.push({' + ', '.join('%s: a[i].%s' % (f, f) for f in fields) + '});',
        '}',
        'return JSON.stringify(out);']))


def count_down(t, key, n=5):
    return '%s>=%d' % (t.fn('count', key, '#%d' % n, '"eq"', '"0"'), n)


VMAPS = {
    'Service state': [('0', 'Down'), ('1', 'Up')],
    'Yes/No': [('0', 'No'), ('1', 'Yes')],
}
BOOL_JS = "return (value === 'true' || value === true || value === 'ON' || value === '1') ? 1 : 0;"

# ======================================================================= OS-Deps-Node
n = Tpl('OS-Deps-Node', """L4 dependencies seen from each controller. Link to every controller host (Zabbix agent 2).

  MariaDB Galera   agent 2 MySQL plugin: wsrep_* status, connections
  memcached        agent 2 Memcached plugin
  keepalived VIP   /proc/net/fib_trie: is the VIP local on this node (OS-Deps-Cluster sums it)
  Octavia HM       net.udp.listen on the health-manager port

Kolla binds MariaDB and memcached to the API interface, not localhost: the host interface IP
({HOST.CONN}) must be the node's API IP.

DB user (read-only is enough):
  CREATE USER 'zbx_monitor'@'%' IDENTIFIED BY '<pw>';
  GRANT USAGE, REPLICATION CLIENT, PROCESS, SHOW DATABASES, SHOW VIEW ON *.* TO 'zbx_monitor'@'%';

Tags: service / layer=L4 / impact / component / node. Set host tag az=<AZ>.""")
DSN = '"tcp://{HOST.CONN}:{$MYSQL.PORT}","{$MYSQL.USER}","{$MYSQL.PASSWORD}"'
G = 'mysql.get_status_variables[%s]' % DSN
PING = 'mysql.ping[%s]' % DSN
mtg = tags(service='mariadb', layer='L4', component='galera')
n.add(n.item('MariaDB: ping', PING, typ='ZABBIX_PASSIVE', vt='UNSIGNED', delay='1m',
             vmap='Service state', tg=mtg),
      n.item('MariaDB: status variables (raw)', G, typ='ZABBIX_PASSIVE', vt='TEXT', delay='1m',
             history='0', tg=mtg, desc='SHOW GLOBAL STATUS as JSON. Not stored; dependents read it.'),
      n.item('Galera: ready', 'galera.ready', vt='UNSIGNED', master=G, vmap='Yes/No', tg=mtg,
             pp=[jp('$.wsrep_ready'), js(BOOL_JS)]),
      n.item('Galera: cluster status', 'galera.cluster_status', vt='CHAR', master=G, history='7d',
             tg=mtg, pp=[jp('$.wsrep_cluster_status'), discard()]),
      n.item('Galera: primary component', 'galera.primary', vt='UNSIGNED', master=G, vmap='Yes/No',
             tg=mtg, pp=[jp('$.wsrep_cluster_status'), js("return value === 'Primary' ? 1 : 0;")]),
      n.item('Galera: cluster size', 'galera.size', vt='UNSIGNED', master=G, tg=mtg,
             pp=[jp('$.wsrep_cluster_size')]),
      n.item('Galera: local state', 'galera.state', vt='CHAR', master=G, history='7d', tg=mtg,
             pp=[jp('$.wsrep_local_state_comment'), discard()]),
      n.item('Galera: synced', 'galera.synced', vt='UNSIGNED', master=G, vmap='Yes/No', tg=mtg,
             pp=[jp('$.wsrep_local_state_comment'), js("return value === 'Synced' ? 1 : 0;")]),
      n.item('Galera: flow control paused (cumulative)', 'galera.flow_paused_ns', master=G,
             units='ns', tg=mtg, pp=[jp('$.wsrep_flow_control_paused_ns')],
             desc='Stored cumulative on purpose (CHANGE_PER_SECOND bug ZBX-24890); the ratio is '
                  'a calculated item.'),
      n.item('Galera: flow control paused ratio', 'galera.flow_paused.ratio', typ='CALCULATED',
             delay='1m', tg=mtg,
             params='(last(//galera.flow_paused_ns)-last(//galera.flow_paused_ns,#2))/60000000000',
             desc='Share of the last minute replication was paused. 0.1 = paused 10% of the time.'),
      n.item('Galera: local receive queue', 'galera.recv_queue', master=G, tg=mtg,
             pp=[jp('$.wsrep_local_recv_queue')]),
      n.item('MariaDB: threads connected', 'mysql.threads_connected', master=G, tg=mtg,
             pp=[jp('$.Threads_connected')]),
      n.item('MariaDB: max used connections', 'mysql.max_used_connections', master=G, tg=mtg,
             pp=[jp('$.Max_used_connections')]))
NT = dict(node='{HOST.NAME}')
m_down = n.root_trig('MariaDB on {HOST.NAME} is down', count_down(n, PING), 'HIGH',
                     tags(service='mariadb', layer='L4', impact='availability', component='mysqld', **NT),
                     'mysql.ping failed 5 rounds in a row (5m). One node down keeps the cluster up; the '
                     'Services tree only degrades when 2 of 3 nodes have problems.',
                     recovery='%s=1' % n.fn('last', PING))
m_np = n.root_trig('Galera on {HOST.NAME}: not in the Primary component',
                   '%s=0' % n.fn('max', 'galera.primary', '#5'), 'DISASTER',
                   tags(service='mariadb', layer='L4', impact='availability', component='galera', **NT),
                   'Non-Primary = this node refuses writes (split brain or quorum lost). On every '
                   'node at once = the whole cloud cannot write to its DB.', deps=[m_down])
n.root_trig('Galera on {HOST.NAME}: cluster size {ITEM.VALUE}',
            '%s<=1' % n.fn('max', 'galera.size', '#5'), 'DISASTER',
            tags(service='mariadb', layer='L4', impact='availability', component='galera', **NT),
            deps=[m_down])
n.root_trig('Galera on {HOST.NAME}: wsrep not ready', '%s=0' % n.fn('max', 'galera.ready', '#5'), 'HIGH',
            tags(service='mariadb', layer='L4', impact='availability', component='galera', **NT),
            deps=[m_down, m_np])
n.root_trig('Galera on {HOST.NAME}: cluster size {ITEM.VALUE} of {$GALERA.SIZE}',
            '%s>1 and %s<{$GALERA.SIZE}' % (n.fn('last', 'galera.size'), n.fn('last', 'galera.size')),
            'HIGH', tags(service='mariadb', layer='L4', impact='stability', component='galera', **NT),
            'A node left the cluster - one more failure loses quorum.', deps=[m_down])
n.root_trig('Galera on {HOST.NAME}: not synced ({ITEM.VALUE})',
            '%s=0' % n.fn('max', 'galera.synced', '#3'), 'WARNING',
            tags(service='mariadb', layer='L4', impact='stability', component='galera', **NT),
            deps=[m_down])
n.root_trig('Galera on {HOST.NAME}: flow control paused above {$GALERA.FLOW.WARN}',
            '%s>{$GALERA.FLOW.WARN}' % n.fn('min', 'galera.flow_paused.ratio', '#5'), 'WARNING',
            tags(service='mariadb', layer='L4', impact='stability', component='galera', **NT),
            'A slow node makes the whole cluster wait. Check disk latency on the slowest node.',
            deps=[m_down], opdata='{ITEM.LASTVALUE}')
n.root_trig('Galera on {HOST.NAME}: receive queue above {$GALERA.RECVQ.WARN}',
            '%s>{$GALERA.RECVQ.WARN}' % n.fn('min', 'galera.recv_queue', '#5'), 'WARNING',
            tags(service='mariadb', layer='L4', impact='stability', component='galera', **NT),
            deps=[m_down], opdata='{ITEM.LASTVALUE}')
n.root_trig('MariaDB on {HOST.NAME}: connections above {$MYSQL.CONN.WARN}% of {$MYSQL.MAX_CONN}',
            '%s>{$MYSQL.MAX_CONN}*{$MYSQL.CONN.WARN}/100' % n.fn('last', 'mysql.threads_connected'),
            'WARNING', tags(service='mariadb', layer='L4', impact='capacity', component='mysqld', **NT),
            deps=[m_down], opdata='{ITEM.LASTVALUE}')

MC = '"tcp://{HOST.CONN}:{$MEMCACHED.PORT}"'
MS = 'memcached.stats[%s]' % MC
MP = 'memcached.ping[%s]' % MC
ctg = tags(service='memcached', layer='L4', component='memcached')
n.add(n.item('memcached: ping', MP, typ='ZABBIX_PASSIVE', vt='UNSIGNED', delay='1m',
             vmap='Service state', tg=ctg),
      n.item('memcached: stats (raw)', MS, typ='ZABBIX_PASSIVE', vt='TEXT', delay='1m',
             history='0', tg=ctg),
      n.item('memcached: get hits (cumulative)', 'memcached.get_hits', master=MS, tg=ctg,
             pp=[jp('$.get_hits')]),
      n.item('memcached: get misses (cumulative)', 'memcached.get_misses', master=MS, tg=ctg,
             pp=[jp('$.get_misses')]),
      n.item('memcached: evictions (cumulative)', 'memcached.evictions', master=MS, tg=ctg,
             pp=[jp('$.evictions')]),
      n.item('memcached: current connections', 'memcached.curr_connections', master=MS, tg=ctg,
             pp=[jp('$.curr_connections')]),
      n.item('memcached: max connections', 'memcached.max_connections', master=MS, tg=ctg,
             pp=[jp('$.max_connections')]),
      n.item('memcached: hit ratio', 'memcached.hit_ratio', typ='CALCULATED', delay='1m',
             units='%', tg=ctg,
             params='100*(last(//memcached.get_hits)-last(//memcached.get_hits,#2))/'
                    '(last(//memcached.get_hits)-last(//memcached.get_hits,#2)+'
                    'last(//memcached.get_misses)-last(//memcached.get_misses,#2)+1)',
             desc='Over the last minute. +1 in the divisor keeps an idle minute from dividing by 0.'))
c_down = n.root_trig('memcached on {HOST.NAME} is down', count_down(n, MP), 'HIGH',
                     tags(service='memcached', layer='L4', impact='availability',
                          component='memcached', **NT),
                     'Keystone token validation and Horizon sessions lose this cache node.',
                     recovery='%s=1' % n.fn('last', MP))
n.root_trig('memcached on {HOST.NAME}: evicting items',
            '%s-%s>{$MEMCACHED.EVICT.MAX}' % (n.fn('last', 'memcached.evictions'),
                                              n.fn('last', 'memcached.evictions', '#2')),
            'WARNING', tags(service='memcached', layer='L4', impact='stability',
                            component='memcached', **NT),
            'Cache full - raise memcached_memory or expect slower token validation.', deps=[c_down])
n.root_trig('memcached on {HOST.NAME}: connections above {$MEMCACHED.CONN.WARN}%',
            '%s>%s*{$MEMCACHED.CONN.WARN}/100' % (n.fn('last', 'memcached.curr_connections'),
                                                  n.fn('last', 'memcached.max_connections')),
            'WARNING', tags(service='memcached', layer='L4', impact='capacity',
                            component='memcached', **NT), deps=[c_down])

FIB = 'vfs.file.contents[/proc/net/fib_trie]'
ktg = tags(service='keepalived', layer='L4', component='vip')


def vip_js(macro):
    return js('\n'.join([
        "var vip = '%s';" % macro,
        "if (!vip) { throw 'VIP macro is empty - item disabled on purpose'; }",
        "var lines = value.split('\\n');",
        "for (var i = 0; i < lines.length - 1; i++) {",
        "  if (lines[i].replace(/^[\\s|+`-]+/, '') === vip && /\\/32 host LOCAL/.test(lines[i + 1])) {",
        "    return 1;",
        "  }",
        "}",
        "return 0;"]))


n.add(n.item('keepalived: routing table (raw)', FIB, typ='ZABBIX_PASSIVE', vt='TEXT', delay='1m',
             history='0', tg=ktg, desc='World-readable, no sudo. Local addresses show as "/32 host LOCAL".'),
      n.item('keepalived: internal VIP on this node', 'os.vip.present[internal]', vt='UNSIGNED',
             master=FIB, vmap='Yes/No', tg=ktg, pp=[vip_js('{$OS_VIP.INTERNAL}')],
             desc='Summed across controllers by OS-Deps-Cluster. Empty macro = not supported '
                  '(skipped by the sum).'),
      n.item('keepalived: external VIP on this node', 'os.vip.present[external]', vt='UNSIGNED',
             master=FIB, vmap='Yes/No', tg=ktg, pp=[vip_js('{$OS_VIP.EXTERNAL}')]))

OHM = 'net.udp.listen[{$OCTAVIA.HM.PORT}]'
n.add(n.item('Octavia health manager: UDP listening', OHM, typ='ZABBIX_PASSIVE', vt='UNSIGNED',
             delay='1m', vmap='Yes/No',
             tg=tags(service='octavia', layer='L4', component='health-manager'),
             desc='Amphorae send heartbeats here. Disable this item on nodes without '
                  'octavia_health_manager.'))
n.root_trig('Octavia health manager on {HOST.NAME} is not listening on UDP {$OCTAVIA.HM.PORT}',
            count_down(n, OHM), 'HIGH',
            tags(service='octavia', layer='L4', impact='availability', component='health-manager', **NT),
            'No heartbeats are received here: amphorae look dead and get failed over.',
            recovery='%s=1' % n.fn('last', OHM))
for k, v, d, *sec in [
        ('{$MYSQL.PORT}', '3306', 'MariaDB port on the API interface.'),
        ('{$MYSQL.USER}', 'zbx_monitor', 'Read-only monitoring user (see template description).'),
        ('{$MYSQL.PASSWORD}', None, 'Set on the host or a host group. Secret text.', True),
        ('{$MYSQL.MAX_CONN}', '10000', 'MariaDB max_connections (Kolla default 10000).'),
        ('{$MYSQL.CONN.WARN}', '80', 'Connections warning, % of {$MYSQL.MAX_CONN}.'),
        ('{$GALERA.SIZE}', '3', 'Expected Galera cluster size.'),
        ('{$GALERA.FLOW.WARN}', '0.1', 'Flow control paused ratio warning (0-1).'),
        ('{$GALERA.RECVQ.WARN}', '100', 'Receive queue warning.'),
        ('{$MEMCACHED.PORT}', '11211', 'memcached port on the API interface.'),
        ('{$MEMCACHED.EVICT.MAX}', '0', 'Evictions per minute allowed.'),
        ('{$MEMCACHED.CONN.WARN}', '90', 'Connections warning, % of max_connections.'),
        ('{$OS_VIP.INTERNAL}', '', 'kolla_internal_vip_address of this AZ. Set on the host group.'),
        ('{$OS_VIP.EXTERNAL}', '', 'kolla_external_vip_address. Empty = not checked.'),
        ('{$OCTAVIA.HM.PORT}', '5555', 'octavia health manager UDP port.')]:
    n.macro(k, v, d, secret=bool(sec))

# ======================================================================= OS-Deps-Cluster
c = Tpl('OS-Deps-Cluster', """L4 cluster-wide checks. Link to the AZ host (the same host as OS-API-Probe).

  RabbitMQ   management API via the internal VIP (Kolla publishes rabbitmq_management on 15672):
             nodes, partitions, alarms, fd/sockets, queues with no consumers (regex-filtered
             server-side, never the full /api/queues)
  keepalived how many controllers hold the VIP (sum of OS-Deps-Node items in {$OS_CTL.GROUP})

RabbitMQ user: rabbitmqctl add_user zbx_monitor <pw>; set_user_tags zbx_monitor monitoring;
set_permissions -p / zbx_monitor '' '' '.*'""")
RU = '{$RABBITMQ.API.URL}'
http = dict(url=None, authtype='BASIC', username='{$RABBITMQ.USERNAME}',
            password='{$RABBITMQ.PASSWORD}', timeout='10s')
rtg = tags(service='rabbitmq', layer='L4', component='rabbitmq')


def rq(name, key, path, qf):
    h = dict(http, url=RU + path)
    h['query_fields'] = [{'name': a, 'value': b} for a, b in qf]
    return c.item(name, key, typ='HTTP_AGENT', vt='TEXT', delay='{$RABBITMQ.INTERVAL}',
                  history='0', tg=rtg, **h)


RN, RO, RQ = 'rabbitmq.nodes.raw', 'rabbitmq.overview.raw', 'rabbitmq.queues.raw'
c.add(rq('RabbitMQ: nodes (raw)', RN, '/api/nodes',
         [('columns', 'name,running,mem_alarm,disk_free_alarm,fd_used,fd_total,'
                      'sockets_used,sockets_total,partitions')]),
      rq('RabbitMQ: overview (raw)', RO, '/api/overview',
         [('columns', 'cluster_name,queue_totals,object_totals')]),
      rq('RabbitMQ: OpenStack queues (raw)', RQ, '/api/queues/{$RABBITMQ.VHOST}',
         [('page', '1'), ('page_size', '500'), ('name', '{$RABBITMQ.QUEUE.MATCH}'),
          ('use_regex', 'true'), ('columns', 'name,consumers,messages')]),
      c.item('RabbitMQ: nodes total', 'rabbitmq.nodes.total', vt='UNSIGNED', master=RN, tg=rtg,
             pp=[js('return JSON.parse(value).length;')]),
      c.item('RabbitMQ: nodes running', 'rabbitmq.nodes.running', vt='UNSIGNED', master=RN, tg=rtg,
             pp=[js('\n'.join(['var a = JSON.parse(value), n = 0;',
                               'for (var i = 0; i < a.length; i++) { if (a[i].running) { n++; } }',
                               'return n;']))]),
      c.item('RabbitMQ: network partitions', 'rabbitmq.partitions', vt='UNSIGNED', master=RN, tg=rtg,
             pp=[js('\n'.join(['var a = JSON.parse(value), n = 0;',
                               'for (var i = 0; i < a.length; i++) { n += (a[i].partitions || []).length; }',
                               'return n;']))]),
      c.item('RabbitMQ: messages', 'rabbitmq.messages', master=RO, tg=rtg,
             pp=[jp('$.queue_totals.messages')]),
      c.item('RabbitMQ: messages unacknowledged', 'rabbitmq.messages_unacked', master=RO, tg=rtg,
             pp=[jp('$.queue_totals.messages_unacknowledged')]),
      c.item('RabbitMQ: connections', 'rabbitmq.connections', master=RO, tg=rtg,
             pp=[jp('$.object_totals.connections')]),
      c.item('RabbitMQ: consumers', 'rabbitmq.consumers', master=RO, tg=rtg,
             pp=[jp('$.object_totals.consumers')]))
r_api = c.root_trig('RabbitMQ management API not answering via the VIP',
                    '%s=1' % c.fn('nodata', 'rabbitmq.nodes.total', '{$RABBITMQ.NODATA}'), 'HIGH',
                    tags(service='rabbitmq', layer='L4', impact='availability', component='rabbitmq'),
                    'HAProxy routes 15672 to any live node, so no answer usually means no RabbitMQ '
                    'node is up. Rule out a wrong {$RABBITMQ.PASSWORD} first (item error = 401).')
c.root_trig('RabbitMQ: network partition detected', '%s>0' % c.fn('min', 'rabbitmq.partitions', '#5'),
            'DISASTER', tags(service='rabbitmq', layer='L4', impact='availability', component='rabbitmq'),
            'Split brain: RPC between services is unreliable until the minority side restarts.',
            deps=[r_api])
c.root_trig('RabbitMQ: only {ITEM.LASTVALUE1} of {ITEM.LASTVALUE2} nodes running',
            '%s*2<=%s' % (c.fn('max', 'rabbitmq.nodes.running', '#5'), c.fn('last', 'rabbitmq.nodes.total')),
            'DISASTER', tags(service='rabbitmq', layer='L4', impact='availability', component='rabbitmq'),
            'Half or fewer nodes running for 5m - quorum queues stop.', deps=[r_api])
c.root_trig('RabbitMQ: {ITEM.VALUE} unacknowledged messages across the cluster',
            '%s>{$RABBITMQ.UNACKED.WARN}' % c.fn('min', 'rabbitmq.messages_unacked', '#3'),
            'WARNING', tags(service='rabbitmq', layer='L4', impact='stability', component='rabbitmq'),
            'Workers are holding RPC messages without acknowledging them (stuck consumers or '
            'leaked reply/fanout queues).', deps=[r_api], opdata='{ITEM.LASTVALUE}')

NODE_FIELDS = ['name']
nsel = "$[?(@.name=='{#NODE}')]"
ntg = tags(service='rabbitmq', layer='L4', component='rabbitmq', node='{#NODE}')
pct_js = lambda a, b: js('var o = JSON.parse(value);\nreturn o.%s ? 100 * o.%s / o.%s : 0;' % (b, a, b))
rnode = 'rabbitmq.node.running["{#NODE}"]'
rn_down = c.trig('RabbitMQ node {#NODE} is not running', count_down(c, rnode), 'HIGH',
                 tags(service='rabbitmq', layer='L4', impact='stability', component='rabbitmq',
                      node='{#NODE}'), deps=[r_api], recovery='%s=1' % c.fn('last', rnode))
c.llds.append({
    'uuid': uid('OS-Deps-Cluster:lld:nodes'), 'name': 'RabbitMQ nodes', 'type': 'DEPENDENT',
    'key': 'rabbitmq.nodes.discovery', 'delay': '0', 'lifetime': '7d',
    'item_prototypes': [
        c.item('RabbitMQ {#NODE}: running', rnode, vt='UNSIGNED', master=RN, vmap='Service state',
               tg=ntg, proto=True, pp=[jp(nsel + '.running.first()'), js(BOOL_JS)]),
        c.item('RabbitMQ {#NODE}: memory alarm', 'rabbitmq.node.mem_alarm["{#NODE}"]', vt='UNSIGNED',
               master=RN, vmap='Yes/No', tg=ntg, proto=True,
               pp=[jp(nsel + '.mem_alarm.first()'), js(BOOL_JS)]),
        c.item('RabbitMQ {#NODE}: disk alarm', 'rabbitmq.node.disk_alarm["{#NODE}"]', vt='UNSIGNED',
               master=RN, vmap='Yes/No', tg=ntg, proto=True,
               pp=[jp(nsel + '.disk_free_alarm.first()'), js(BOOL_JS)]),
        c.item('RabbitMQ {#NODE}: file descriptors used', 'rabbitmq.node.fd_pct["{#NODE}"]',
               master=RN, units='%', tg=ntg, proto=True,
               pp=[jp(nsel + '.first()'), pct_js('fd_used', 'fd_total')]),
        c.item('RabbitMQ {#NODE}: sockets used', 'rabbitmq.node.sockets_pct["{#NODE}"]',
               master=RN, units='%', tg=ntg, proto=True,
               pp=[jp(nsel + '.first()'), pct_js('sockets_used', 'sockets_total')])],
    'trigger_prototypes': [
        rn_down,
        c.trig('RabbitMQ node {#NODE}: memory or disk alarm - publishers blocked',
               '%s=1 or %s=1' % (c.fn('min', 'rabbitmq.node.mem_alarm["{#NODE}"]', '#5'),
                                 c.fn('min', 'rabbitmq.node.disk_alarm["{#NODE}"]', '#5')), 'HIGH',
               tags(service='rabbitmq', layer='L4', impact='availability', component='rabbitmq',
                    node='{#NODE}'),
               'RabbitMQ blocks every publisher connected to this node for 5m: RPC calls hang.', deps=[r_api]),
        c.trig('RabbitMQ node {#NODE}: file descriptors above {$RABBITMQ.FD.WARN}%',
               '%s>{$RABBITMQ.FD.WARN}' % c.fn('last', 'rabbitmq.node.fd_pct["{#NODE}"]'), 'WARNING',
               tags(service='rabbitmq', layer='L4', impact='capacity', component='rabbitmq',
                    node='{#NODE}'), deps=[rn_down]),
        c.trig('RabbitMQ node {#NODE}: sockets above {$RABBITMQ.FD.WARN}%',
               '%s>{$RABBITMQ.FD.WARN}' % c.fn('last', 'rabbitmq.node.sockets_pct["{#NODE}"]'),
               'WARNING', tags(service='rabbitmq', layer='L4', impact='capacity',
                               component='rabbitmq', node='{#NODE}'), deps=[rn_down])],
    'master_item': {'key': RN},
    'lld_macro_paths': [{'lld_macro': '{#NODE}', 'path': '$.name'}],
    'preprocessing': [lld_map(['name']), discard()]})

qsel = "$.items[?(@.name=='{#QUEUE}')]"
qtg = tags(service='rabbitmq', layer='L4', component='{#QUEUE}')
qc = 'rabbitmq.queue.consumers["{#QUEUE}"]'
c.llds.append({
    'uuid': uid('OS-Deps-Cluster:lld:queues'), 'name': 'RabbitMQ OpenStack queues', 'type': 'DEPENDENT',
    'key': 'rabbitmq.queues.discovery', 'delay': '0', 'lifetime': '7d',
    'description': 'Only queues matching {$RABBITMQ.QUEUE.MATCH} (filtered by the API).',
    'item_prototypes': [
        c.item('RabbitMQ queue {#QUEUE}: consumers', qc, vt='UNSIGNED', master=RQ, tg=qtg, proto=True,
               pp=[jp(qsel + '.consumers.first()')]),
        c.item('RabbitMQ queue {#QUEUE}: messages', 'rabbitmq.queue.messages["{#QUEUE}"]',
               vt='UNSIGNED', master=RQ, tg=qtg, proto=True, pp=[jp(qsel + '.messages.first()')])],
    'trigger_prototypes': [
        c.trig('RabbitMQ queue {#QUEUE} has no consumers', '%s=0' % c.fn('max', qc, '#5'), 'HIGH',
               tags(service='rabbitmq', layer='L4', impact='availability', component='{#QUEUE}'),
               'Nothing reads this RPC queue for 5m: every worker of that service is down or disconnected '
               '(e.g. all nova-conductor, cinder-scheduler, heat-engine).', deps=[r_api],
               recovery='%s>0' % c.fn('last', qc)),
        c.trig('RabbitMQ queue {#QUEUE}: {ITEM.VALUE} messages waiting',
               '%s>{$RABBITMQ.QUEUE.MSG.WARN:"{#QUEUE}"}' % c.fn('min', 'rabbitmq.queue.messages["{#QUEUE}"]', '#3'),
               'WARNING', tags(service='rabbitmq', layer='L4', impact='stability', component='{#QUEUE}'),
               'Consumers are slower than producers.', deps=[r_api])],
    'master_item': {'key': RQ},
    'lld_macro_paths': [{'lld_macro': '{#QUEUE}', 'path': '$.name'}],
    'preprocessing': [jp('$.items'), lld_map(['name']), discard()]})

for vip in ('internal', 'external'):
    key = 'os.vip.holders[%s]' % vip
    c.add(c.item('keepalived: controllers holding the %s VIP' % vip, key, typ='CALCULATED',
                 vt='UNSIGNED', delay='1m', tg=tags(service='keepalived', layer='L4', component='vip'),
                 params='sum(last_foreach(/*/os.vip.present[%s]?[group="{$OS_CTL.GROUP}"]))' % vip,
                 desc='Not supported when no controller has the VIP macro set (then not checked).'))
    c.root_trig('keepalived: %s VIP is on no controller' % vip, '%s=0' % c.fn('max', key, '#5'), 'DISASTER',
                tags(service='keepalived', layer='L4', impact='availability', component='vip-' + vip),
                'Every API behind this VIP is unreachable for 5m.')
    c.root_trig('keepalived: %s VIP is on {ITEM.VALUE} controllers (split brain)' % vip,
                '%s>1' % c.fn('min', key, '#5'), 'HIGH',
                tags(service='keepalived', layer='L4', impact='availability', component='vip-' + vip),
                'Two nodes answer ARP for the VIP for 5m: clients flap between them. Usually VRRP '
                'traffic blocked between controllers.')

# HAProxy stats CSV (Kolla default port 1984)
HP_RAW, HP_JSON = 'haproxy.stats.raw', 'haproxy.stats.json'
htg_hp = tags(service='haproxy', layer='L4', component='haproxy')
HP_PARSE_JS = '\n'.join([
    "var lines = value.split('\\n'), hdr = [], backends = [], servers = [];",
    "for (var i = 0; i < lines.length; i++) {",
    "  var ln = lines[i].replace(/\\r$/, '');",
    "  if (!ln) { continue; }",
    "  if (ln.charAt(0) === '#') { hdr = ln.slice(2).split(','); continue; }",
    "  var cols = ln.split(','), o = {};",
    "  for (var j = 0; j < hdr.length; j++) { if (hdr[j]) { o[hdr[j]] = cols[j] || ''; } }",
    "  if (!o.pxname || o.pxname === 'stats' || o.pxname === 'haproxy_stats') { continue; }",
    "  var st = (o.status || '').toUpperCase();",
    "  if (o.svname === 'BACKEND') {",
    "    var slim = parseInt(o.slim || '0', 10), scur = parseInt(o.scur || '0', 10);",
    "    backends.push({pxname: o.pxname, state: st === 'UP' ? 1 : 0,",
    "      act: parseInt(o.act || '0', 10), scur: scur,",
    "      spct: slim > 0 ? Math.round(scur * 10000 / slim) / 100 : 0});",
    "  } else if (o.svname && o.svname !== 'FRONTEND') {",
    "    var up = (st.indexOf('UP') === 0 || st === 'NO CHECK' || st.indexOf('MAINT') === 0) ? 1 : 0;",
    "    servers.push({id: o.pxname + '/' + o.svname, pxname: o.pxname, svname: o.svname,",
    "      state: up, status: o.status || ''});",
    "  }",
    "}",
    "return JSON.stringify({backends: backends, servers: servers});",
])
c.add(c.item('HAProxy: stats CSV (raw)', HP_RAW, typ='HTTP_AGENT', vt='TEXT',
             delay='{$HAPROXY.INTERVAL}', history='0', tg=htg_hp,
             url='{$HAPROXY.STATS.URL}', authtype='BASIC',
             username='{$HAPROXY.USERNAME}', password='{$HAPROXY.PASSWORD}', timeout='10s'),
      c.item('HAProxy: parsed stats (JSON)', HP_JSON, vt='TEXT', master=HP_RAW,
             history='0', tg=htg_hp, pp=[js(HP_PARSE_JS)]))
bsel = "$.backends[?(@.pxname=='{#PXNAME}')]"
btg_hp = tags(service='haproxy', layer='L4', component='{#PXNAME}')
bstate = 'haproxy.backend.state["{#PXNAME}"]'
bdown = c.trig('HAProxy backend {#PXNAME} is DOWN (no active servers)',
               count_down(c, bstate), 'DISASTER',
               tags(service='haproxy', layer='L4', impact='availability', component='{#PXNAME}'),
               'Every server in this HAProxy backend is down: requests to the VIP fail with 503.',
               recovery='%s=1' % c.fn('last', bstate))
c.llds.append({
    'uuid': uid('OS-Deps-Cluster:lld:haproxy.backends'), 'name': 'HAProxy backends',
    'type': 'DEPENDENT', 'key': 'haproxy.backends.discovery', 'delay': '0', 'lifetime': '7d',
    'filter': {'evaltype': 'AND', 'conditions': [
        {'macro': '{#PXNAME}', 'value': '{$HAPROXY.BACKEND.MATCHES}', 'formulaid': 'A'},
        {'macro': '{#PXNAME}', 'value': '{$HAPROXY.BACKEND.NOT_MATCHES}',
         'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'}]},
    'item_prototypes': [
        c.item('HAProxy backend {#PXNAME}: state', bstate, vt='UNSIGNED', master=HP_JSON,
               vmap='Service state', tg=btg_hp, proto=True, pp=[jp(bsel + '.state.first()')]),
        c.item('HAProxy backend {#PXNAME}: active servers', 'haproxy.backend.act["{#PXNAME}"]',
               vt='UNSIGNED', master=HP_JSON, tg=btg_hp, proto=True, pp=[jp(bsel + '.act.first()')]),
        c.item('HAProxy backend {#PXNAME}: session usage', 'haproxy.backend.spct["{#PXNAME}"]',
               master=HP_JSON, units='%', tg=btg_hp, proto=True, pp=[jp(bsel + '.spct.first()')])],
    'trigger_prototypes': [
        bdown,
        c.trig('HAProxy backend {#PXNAME}: session usage above {$HAPROXY.SESS.WARN}%',
               '%s>{$HAPROXY.SESS.WARN}' % c.fn('min', 'haproxy.backend.spct["{#PXNAME}"]', '#3'),
               'WARNING', tags(service='haproxy', layer='L4', impact='capacity', component='{#PXNAME}'),
               deps=[bdown], opdata='{ITEM.LASTVALUE}')],
    'master_item': {'key': HP_JSON},
    'lld_macro_paths': [{'lld_macro': '{#PXNAME}', 'path': '$.pxname'}],
    'preprocessing': [jp('$.backends'), lld_map(['pxname']), discard()]})
ssel = "$.servers[?(@.id=='{#ID}')]"
stg_hp = tags(service='haproxy', layer='L4', component='{#PXNAME}', node='{#SVNAME}')
sstate = 'haproxy.server.state["{#ID}"]'
c.llds.append({
    'uuid': uid('OS-Deps-Cluster:lld:haproxy.servers'), 'name': 'HAProxy backend servers',
    'type': 'DEPENDENT', 'key': 'haproxy.servers.discovery', 'delay': '0', 'lifetime': '7d',
    'filter': {'evaltype': 'AND', 'conditions': [
        {'macro': '{#PXNAME}', 'value': '{$HAPROXY.BACKEND.MATCHES}', 'formulaid': 'A'},
        {'macro': '{#PXNAME}', 'value': '{$HAPROXY.BACKEND.NOT_MATCHES}',
         'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'},
        {'macro': '{#SVNAME}', 'value': '{$HAPROXY.SERVER.NOT_MATCHES}',
         'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'C'}]},
    'item_prototypes': [
        c.item('HAProxy {#PXNAME}/{#SVNAME}: state', sstate, vt='UNSIGNED', master=HP_JSON,
               vmap='Service state', tg=stg_hp, proto=True, pp=[jp(ssel + '.state.first()')])],
    'trigger_prototypes': [
        c.trig('HAProxy {#PXNAME}: server {#SVNAME} is DOWN',
               count_down(c, sstate), 'WARNING',
               tags(service='haproxy', layer='L4', impact='stability', component='{#PXNAME}',
                    node='{#SVNAME}'),
               'One backend server failed its HAProxy health check; other servers still take traffic.',
               recovery='%s=1' % c.fn('last', sstate))],
    'master_item': {'key': HP_JSON},
    'lld_macro_paths': [{'lld_macro': '{#ID}', 'path': '$.id'},
                        {'lld_macro': '{#PXNAME}', 'path': '$.pxname'},
                        {'lld_macro': '{#SVNAME}', 'path': '$.svname'}],
    'preprocessing': [jp('$.servers'), lld_map(['id', 'pxname', 'svname']), discard()]})
for k, v, d, *sec in [
        ('{$RABBITMQ.API.URL}', 'http://INTERNAL-VIP:15672', 'RabbitMQ management API via the internal VIP.'),
        ('{$RABBITMQ.USERNAME}', 'zbx_monitor', 'User with the monitoring tag.'),
        ('{$RABBITMQ.PASSWORD}', None, 'Secret text.', True),
        ('{$RABBITMQ.VHOST}', '%2F', 'URL-encoded vhost. Kolla uses /.'),
        ('{$RABBITMQ.INTERVAL}', '1m', 'Poll interval of the 3 API calls.'),
        ('{$RABBITMQ.NODATA}', '5m', 'nodata() window for the management API.'),
        ('{$RABBITMQ.FD.WARN}', '80', 'File descriptor / socket warning, %.'),
        ('{$RABBITMQ.QUEUE.MATCH}',
         '^(conductor|scheduler|cinder-scheduler|cinder-volume|cinder-backup|engine|'
         'magnum-conductor|octavia_prov|trove-conductor|trove-taskmanager|manila-scheduler|manila-share)$',
         'Regex of OpenStack RPC queues to watch (server-side filter).'),
        ('{$RABBITMQ.QUEUE.MSG.WARN}', '1000', 'Messages waiting warning. Context = queue name.'),
        ('{$RABBITMQ.UNACKED.WARN}', '500', 'Cluster-wide unacknowledged messages warning.'),
        ('{$HAPROXY.STATS.URL}', 'http://INTERNAL-VIP:1984/;csv', 'HAProxy stats CSV URL (Kolla uses port 1984).'),
        ('{$HAPROXY.USERNAME}', 'stats', 'HAProxy stats basic-auth username (Kolla default: stats).'),
        ('{$HAPROXY.PASSWORD}', None, 'HAProxy stats password (haproxy_stats_password in passwords.yml). Secret text.', True),
        ('{$HAPROXY.INTERVAL}', '1m', 'HAProxy stats poll interval.'),
        ('{$HAPROXY.SESS.WARN}', '80', 'HAProxy backend session usage warning, %.'),
        ('{$HAPROXY.BACKEND.MATCHES}', '.*', 'Regex of HAProxy backends to discover.'),
        ('{$HAPROXY.BACKEND.NOT_MATCHES}', '(?i)(^$|_cell_kkn_|_kkn_back$|^kkn_)',
         'Regex of HAProxy backends to skip (e.g. unprovisioned AZs/cells like kkn).'),
        ('{$HAPROXY.SERVER.NOT_MATCHES}', '(?i)(^$|^kkn-|_kkn_)',
         'Regex of HAProxy backend servers to skip (e.g. unprovisioned AZs/nodes).'),
        ('{$OS_CTL.GROUP}', 'OpenStack/CHANGE-ME/Controllers',
         'Host group of this AZ\'s controllers (linked to OS-Deps-Node). Used by the VIP sum.')]:
    c.macro(k, v, d, secret=bool(sec))

# ======================================================================= OS-Host
h = Tpl('OS-Host', """Minimal host checks for OpenStack controllers and computes (Zabbix agent, passive).
Clock, file systems, agent availability. Docker containers (L1) are out of scope by decision.

Disk uses vfs.fs.discovery instead of fixed paths: /var/lib/docker is mode 710 root, so the agent
cannot stat paths below it. MariaDB / RabbitMQ data live in docker volumes on whichever file
system holds /var/lib/docker - that file system is discovered and alarmed.""")
htg = tags(service='host', component='agent')
MOUNTS = 'vfs.file.contents[/proc/mounts]'
h.add(h.item('Zabbix agent: ping', 'agent.ping', typ='ZABBIX_PASSIVE', vt='UNSIGNED', delay='1m',
             vmap='Yes/No', tg=htg),
      h.item('System: local time', 'system.localtime', typ='ZABBIX_PASSIVE', vt='UNSIGNED',
             delay='1m', units='unixtime', tg=tags(service='chrony', component='clock')),
      h.item('File systems: /proc/mounts (raw)', MOUNTS, typ='ZABBIX_PASSIVE', vt='TEXT',
             delay='1m', history='0', tg=tags(service='host', component='disk')))
h.root_trig('Zabbix agent on {HOST.NAME} is unavailable',
            '%s=1' % h.fn('nodata', 'agent.ping', '{$AGENT.NODATA}'), 'HIGH',
            tags(service='monitoring', impact='monitoring', component='agent', node='{HOST.NAME}'),
            'Host down, agent stopped, or the proxy cannot reach it. Every agent-based check on '
            'this host is blind.')
h.root_trig('{HOST.NAME}: clock off by more than {$TIME.MAX.DIFF}',
            '%s=0' % h.fn('fuzzytime', 'system.localtime', '{$TIME.MAX.DIFF}'), 'WARNING',
            tags(service='chrony', impact='stability', component='clock', node='{HOST.NAME}'),
            'Compared with the Zabbix server clock. Galera, RabbitMQ and Keystone tokens break '
            'with clock skew - check chronyc tracking.')
FS = '{#FSNAME}'
fk = 'vfs.fs.size[{#FSNAME},pused]'
fro = 'vfs.fs.readonly[{#FSNAME}]'
RO_JS = '\n'.join([
    "var mp = '{#FSNAME}', lines = value.split('\\n');",
    "for (var i = 0; i < lines.length; i++) {",
    "  var p = lines[i].split(' ');",
    "  if (p[1] === mp && p[3]) {",
    "    var opts = p[3].split(',');",
    "    for (var j = 0; j < opts.length; j++) { if (opts[j] === 'ro') { return 1; } }",
    "    return 0;",
    "  }",
    "}",
    "return 0;",
])
fs_hi = h.trig('{HOST.NAME}: {#FSNAME} above {$VFS.PUSED.HIGH}% used',
               '%s>{$VFS.PUSED.HIGH}' % h.fn('last', fk), 'HIGH',
               tags(service='host', impact='capacity', component='{#FSNAME}', node='{HOST.NAME}'),
               opdata='{ITEM.LASTVALUE}')
h.llds.append({
    'uuid': uid('OS-Host:lld:fs'), 'name': 'File systems', 'key': 'vfs.fs.discovery',
    'delay': '1h', 'lifetime': '7d',
    'filter': {'evaltype': 'AND', 'conditions': [
        {'macro': '{#FSTYPE}', 'value': '{$VFS.FSTYPE.MATCHES}', 'formulaid': 'A'},
        {'macro': '{#FSNAME}', 'value': '{$VFS.FSNAME.NOT_MATCHES}',
         'operator': 'NOT_MATCHES_REGEX', 'formulaid': 'B'}]},
    'item_prototypes': [
        h.item(FS + ': space used', fk, typ='ZABBIX_PASSIVE', delay='5m', units='%', proto=True,
               tg=tags(service='host', component='disk', fs=FS)),
        h.item(FS + ': total space', 'vfs.fs.size[{#FSNAME},total]', typ='ZABBIX_PASSIVE',
               vt='UNSIGNED', delay='1h', units='B', proto=True,
               tg=tags(service='host', component='disk', fs=FS)),
        h.item(FS + ': read-only', fro, vt='UNSIGNED', master=MOUNTS, vmap='Yes/No', proto=True,
               tg=tags(service='host', component='disk', fs=FS), pp=[js(RO_JS)],
               desc='1 = mounted read-only (ro in /proc/mounts), usually after an I/O error.')],
    'trigger_prototypes': [
        fs_hi,
        h.trig('{HOST.NAME}: {#FSNAME} above {$VFS.PUSED.WARN}% used',
               '%s>{$VFS.PUSED.WARN}' % h.fn('last', fk), 'WARNING',
               tags(service='host', impact='capacity', component='{#FSNAME}', node='{HOST.NAME}'),
               deps=[fs_hi], opdata='{ITEM.LASTVALUE}'),
        h.trig('{HOST.NAME}: {#FSNAME} is mounted read-only',
               '%s=1' % h.fn('last', fro), 'HIGH',
               tags(service='host', impact='stability', component='{#FSNAME}', node='{HOST.NAME}'),
               'Filesystem remounted read-only (disk or storage I/O error): containers and VMs '
               'on this mount cannot write.')]})
for k, v, d in [
        ('{$AGENT.NODATA}', '5m', 'agent.ping nodata window.'),
        ('{$TIME.MAX.DIFF}', '60', 'Allowed clock difference with the Zabbix server, seconds.'),
        ('{$VFS.PUSED.WARN}', '80', 'File system used, % - Warning.'),
        ('{$VFS.PUSED.HIGH}', '90', 'File system used, % - High.'),
        ('{$VFS.FSTYPE.MATCHES}', '^(ext4|xfs|btrfs|zfs)$', 'File system types to discover.'),
        ('{$VFS.FSNAME.NOT_MATCHES}', '^(/dev|/sys|/run|/proc|/boot/efi|/var/lib/docker/.+|/snap/.+)',
         'Mount points to skip (container overlays, pseudo file systems).')]:
    h.macro(k, v, d)

# ======================================================================= OS-Proxy-Watch
p = Tpl('OS-Proxy-Watch', """Is the AZ proxy still sending data? Internal check - the host this
template is linked to MUST be monitored by the Zabbix server, not by the proxy it watches.
One host per proxy, or link it once per proxy with {$PROXY.NAME} set on each host.""")
pk = 'zabbix[proxy,{$PROXY.NAME},lastaccess]'
p.add(p.item('Proxy {$PROXY.NAME}: last access', pk, typ='INTERNAL', vt='UNSIGNED', delay='1m',
             units='unixtime', tg=tags(service='monitoring', component='proxy')))
p.root_trig('Zabbix proxy {$PROXY.NAME} is not sending data',
            '%s=0' % p.fn('fuzzytime', pk, '{$PROXY.LASTACCESS.MAX}'), 'HIGH',
            tags(service='monitoring', impact='monitoring', component='proxy'),
            'Every check of this AZ is stale (agent items and the os_probe trapper data). '
            'Not counted as downtime - the cloud may be fine.')
p.macro('{$PROXY.NAME}', 'CHANGE-ME', 'Proxy name exactly as in Administration > Proxies.')
p.macro('{$PROXY.LASTACCESS.MAX}', '180', 'Seconds without contact before alarming.')

# ======================================================================= export
TPLS = [(n, ['Service state', 'Yes/No']), (c, ['Service state', 'Yes/No']),
        (h, ['Yes/No']), (p, [])]
doc = {'zabbix_export': {
    'version': '6.0', 'date': '2026-10-21T00:00:00Z',
    'groups': [{'uuid': uid('group:' + GROUP), 'name': GROUP}],
    'templates': [t.export(v) for t, v in TPLS],
    'triggers': sorted(sum((t.triggers for t, _ in TPLS), []), key=lambda t: t['name'])}}
for t in doc['zabbix_export']['templates']:
    if not t['valuemaps']:
        del t['valuemaps']
    if not t['discovery_rules']:
        del t['discovery_rules']


class Dumper(yaml.SafeDumper):
    pass


Dumper.add_representer(str, lambda d, s: d.represent_scalar(
    'tag:yaml.org,2002:str', s, style='|' if '\n' in s else None))
open(sys.argv[1], 'w').write(yaml.dump(doc, Dumper=Dumper, sort_keys=False, allow_unicode=True,
                                       width=4096, default_flow_style=False, indent=2))
print('templates', len(TPLS), 'items', sum(len(t.items) for t, _ in TPLS),
      'triggers', len(doc['zabbix_export']['triggers']))
