# สรุป API ที่ os_probe.py เรียกในโหมด project scope

อ้างอิง: `os_probe.py` ที่ตั้ง `scope = project` ใน `os_probe_project.ini`
ทุก call ใช้ token ของ application credential ที่มี role `member` หรือ `reader` ใน project เดียว ไม่ใช้สิทธิ์ admin

| Tier | รอบ | ทำอะไร |
|---|---|---|
| fast | ทุก 2 นาที | ขอ token, functional probe ทุก service (availability) |
| mid | ทุก 5 นาที | ไม่มี check ของ project (quota และนับ resource ปิดไว้) |
| slow | ทุก 30 นาที | Glance canary, วันหมดอายุ credential |

---

## 0. หลักการที่ใช้กับทุก call

### 0.1 วิธีเรียก
- เป็น `GET` ทั้งหมด ยกเว้นตอนขอ token (`POST`) และ**ไม่มีการสร้าง แก้ หรือลบ resource ของลูกค้า**
- แนบ header `X-Auth-Token: <token>` (token ขอครั้งเดียวต่อ 1 tier ต่อ 1 รอบ)
- **base URL** มาจาก Keystone catalog ของ project นั้น ใช้ interface ตาม `iface` ใน ini (ค่าเริ่มต้น `internal`) เช่น
  - Nova `http://VIP:8774/v2.1/<project_id>`
  - Cinder `http://VIP:8776/v3/<project_id>`
  - Neutron `http://VIP:9696`
  - Glance `http://VIP:9292` (ต่อท้าย `/v2` ให้อัตโนมัติ)
- **service ที่ไม่อยู่ใน catalog จะไม่ถูกเรียกเลย** ไม่นับว่า down
- บาง check มี path สำรองหลายแบบ เช่น `/v2.0/networks` กับ `/networks` จะลองตามลำดับ **path แรกที่ไม่ได้ 404 ถือเป็นผล** เพราะแต่ละ cloud ลงทะเบียน URL ใน catalog ไม่เหมือนกัน (บางที่มี `/v2.0` ต่อท้าย บางที่ไม่มี)

### 0.2 สิ่งที่ต่างจากโหมด cloud
- **ตัด filter ข้าม project ออกอัตโนมัติ:** `&all_tenants=1` และ `&global_tenant=true` ถูกลบออกจากทุก path (ทำใน `path()`) path ชุดเดียวกับ cloud จึงได้เฉพาะ resource ของ project นี้
- **ไม่เรียก admin API:** `os-services`, `/services`, `/mservices`, conductors, hypervisors, placement, Cinder pools, IP availability, OpenSDN analytics, Octavia amphorae และ Glance stores

### 0.3 ผลที่ส่งเข้า Zabbix (1 call = 1 record ใน `checks[]`)
| `http_status` | ความหมาย | trigger |
|---|---|---|
| 2xx | ปกติ | – |
| 0 | ไม่มีการตอบกลับ (timeout, refused, DNS, TLS) | down (availability) |
| ≥ 500 | server error | down (availability) |
| 400–499 | สิทธิ์หรือ policy ไม่ให้เรียก, path ไม่รองรับ | monitoring (ไม่นับ SLA) |
| -1 | ถูก skip (`skip_checks`, deadline, ไม่มี token) | ไม่ alert |

ใน fast tier ถ้า **3 รอบติดกัน (~6 นาที)** ได้ 0 หรือ 5xx จะเกิด trigger "is down" ขึ้นใน Zabbix และช่อง honeycomb ของ service นั้นจะเป็นสีแดง

---

## 1. Keystone (fast + slow)

| Tier | Method / Path | ใช้เช็คอะไร |
|---|---|---|
| fast | `POST {auth_url}/v3/auth/tokens` | ขอ token ด้วย application credential: 201 = ได้ token, เก็บเวลาที่ใช้ และอ่าน project id/name กับ catalog จากผลที่ได้ |
| fast | `GET /v3/auth/catalog` | catalog ยังตอบได้ไหม และนับจำนวน service (ถ้าลดลงจากเมื่อชั่วโมงก่อนจะ alert) |
| slow | `GET /v3/users/{user_id}/application_credentials/{cred_id}` | อ่าน `expires_at` แล้วแปลงเป็นจำนวนวันที่เหลือ (`cred_expires_days`) |

body ที่ใช้ขอ token:
```json
{"auth": {"identity": {"methods": ["application_credential"],
  "application_credential": {"id": "<cred_id>", "secret": "<cred_secret>"}}}}
```
ถ้าขอ token ไม่ได้ ทุก check ที่ต้องใช้ token ในรอบนั้นจะได้ `-1` (skipped: no token) จึงไม่เกิด alert ซ้ำซ้อน และ trigger ของ Keystone จะเป็นตัวที่ alert แทน

---

## 2. Functional probe (fast ทุก 2 นาที, impact = availability)

ทุก service ใช้**รูปแบบเดียวกัน** คือ **list resource ของ project แบบ `limit=1`** (ขอแค่ 1 รายการ)
- เป็น call ที่ผ่าน API → policy → database จริง จึงยืนยันได้ว่าลูกค้า "ใช้งาน service ได้" ไม่ใช่แค่ว่า process ยังเปิดอยู่
- ใช้ทรัพยากรน้อยและคงที่ ไม่ว่า project จะมี resource มากแค่ไหน
- ไม่มี resource เลยก็ไม่เป็นไร เพราะได้ `200` พร้อม list ว่าง ซึ่งถือว่าปกติ

| Service | Check | Path (ต่อจาก base URL) | หมายเหตุ |
|---|---|---|---|
| Nova | servers | `/servers?limit=1` | |
| | flavors | `/flavors?limit=1` | |
| | keypairs | `/os-keypairs?limit=1` | |
| | availability_zones | `/os-availability-zone` | อ่าน `zoneState.available` ของแต่ละ AZ ดูข้อ 2.1 |
| Cinder | volumes | `/volumes?limit=1` | |
| | volume_types | `/types` | |
| | snapshots | `/snapshots?limit=1` | |
| | backups | `/backups?limit=1` | |
| | availability_zones | `/os-availability-zone` | ดูข้อ 2.1 |
| Neutron | networks | `/v2.0/networks?limit=1&fields=id` | |
| | subnets, ports, routers, floatingips, security_groups | `/v2.0/<resource>?limit=1&fields=id` | resource ละ 1 check ดูข้อ 2.2 |
| | extensions | `/v2.0/extensions` | neutron-server ตอบเองโดยไม่ผ่าน backend |
| Glance | images | `/v2/images?limit=1` | |
| Heat | stacks | `/stacks?limit=1` | |
| Octavia | providers | `/v2/lbaas/providers` | |
| | loadbalancers | `/v2/lbaas/loadbalancers?limit=1&fields=id` | |
| Trove | datastores | `/datastores` | |
| | instances | `/instances?limit=1` | |
| Magnum | clusters | `/clusters?limit=1` | |
| | clustertemplates | `/clustertemplates?limit=1` | |
| Barbican | secrets | `/v1/secrets?limit=1` | |
| | containers | `/v1/containers?limit=1` | |
| Swift | account | `{account URL}?format=json&limit=1` | base URL คือ `.../v1/AUTH_<project>` เอง จึง list container ได้ 1 ตัว |
| Manila | shares | `/shares?limit=1` | |
| Designate | zones | `/v2/zones?limit=1` | |
| Ironic | nodes | `/v1/nodes?limit=1&fields=uuid` | header `X-OpenStack-Ironic-API-Version: 1.8` |
| Aodh | alarms | `/v2/alarms?limit=1` | |
| Gnocchi | resources | `/v1/resource/generic?limit=1` | |
| Placement | – | – | ไม่มี API ที่ member เรียกได้ จึงไม่มี check |

ตัวเลือก `fields=id` / `fields=uuid` ใช้ลดขนาดของ response ที่ได้กลับมา

### 2.1 Availability zone ของ Nova และ Cinder
`GET /os-availability-zone` ให้ผลเป็น
```json
{"availabilityZoneInfo": [{"zoneName": "BKK-1", "zoneState": {"available": true}}]}
```
- script แยกออกเป็น 1 แถวต่อ 1 AZ (`available` = 1/0) ส่งใน `azs[]`
- ถ้า AZ ไหนมีค่า `available = false` (nova-compute หรือ cinder-volume ทั้ง AZ down) จะเกิด trigger **High** ขึ้น ข้อมูลนี้ member เห็นได้โดยไม่ต้องใช้ admin API
- AZ ที่อยู่ใน `skip_azs` จะถูกข้าม

### 2.2 Neutron ทำไมต้องแยกหลาย check
เมื่อใช้ OpenSDN แต่ละประเภท resource จะวิ่งผ่าน contrail plugin ไปยัง config API คนละ path จึงต้องเช็คทีละตัว ตัวที่พังจะได้ไม่ถูกตัวอื่นกลบไว้
ส่วน `extensions` neutron-server ตอบเองโดยไม่ผ่าน OpenSDN จึงยังใช้ได้แม้ RBAC ของ OpenSDN จะไม่ให้ role reader list network ได้
ถ้า policy ไม่ให้ list resource บางตัว ให้ปิดด้วย `skip_checks` เช่น `neutron:subnets` (ใน ini ตัวอย่างปิด resource หลักไว้ เหลือแค่ `extensions`)

---

## 3. Quota (mid ทุก 5 นาที) — ปิดไว้เป็นค่าเริ่มต้น

**ไม่เรียก API ใดในหัวข้อนี้แล้ว** (`project_quota = 0`) และ template ไม่มี item/trigger ของ quota ตารางด้านล่างเก็บไว้อ้างอิงกรณีเปิดกลับด้วย `project_quota = 1`

ทุก service ให้ผลเป็นแถว `{resource, used, limit, pct}` ใน `quotas[]`
`pct` = used × 100 / limit (ถ้า limit = -1 แปลว่าไม่จำกัด และถ้า limit = 0 แปลว่าไม่อนุญาต ทั้งสองกรณีให้ pct = -1)

| Service | Path | อ่านจาก | resource |
|---|---|---|---|
| Nova | `GET /limits` | `limits.absolute`: `maxTotal*` / `total*Used` | instances, cores, ram_mb, server_groups |
| Cinder | `GET /limits` | เหมือน Nova | volumes, gigabytes, snapshots, backups, backup_gigabytes |
| Neutron | `GET /v2.0/quotas/{project_id}/details` | `quota.<res>.used / limit` | ทุก resource ที่ได้มา (network, port, router, floatingip ...) ถ้าได้ 404 แปลว่าไม่ได้เปิด extension ไว้ จึงถูก skip |
| Octavia | `GET /v2/lbaas/quotas/{project_id}` + `GET /v2/lbaas/loadbalancers?fields=id&limit=<cap>` | Octavia ไม่มีข้อมูลการใช้งาน (usage) ให้: limit จาก quota, used = จำนวน LB ที่นับเอง | load_balancers |
| Manila | `GET /quota-sets/{project_id}/detail` (header `X-OpenStack-Manila-API-Version: 2.25`) | `quota_set.<res>.in_use / limit` | ทุก resource ที่ได้มา |

trigger: `{$OS_QUOTA.WARN}` = 80% (Warning), `{$OS_QUOTA.HIGH}` = 95% (High)

---

## 4. นับ resource ที่ error, ค้าง หรือไม่ได้รัน (mid / slow) — ปิดไว้เป็นค่าเริ่มต้น

**ไม่เรียก API ใดในหัวข้อนี้แล้ว** (`project_resources = 0`) เพราะชุด cloud นับของทุก project อยู่แล้ว template ไม่มี item/trigger ของส่วนนี้ ตารางด้านล่างเก็บไว้อ้างอิงกรณีเปิดกลับด้วย `project_resources = 1`

### 4.1 รูปแบบที่ใช้ร่วมกัน
ทุก service ใช้ **list resource ของ project แบบกรองตาม status** (`?status=<STATE>&limit=<list_cap>`) แล้วนับจำนวน แบ่งเป็น 3 แบบ

| แบบ | วิธีนับ | ผลใน Zabbix |
|---|---|---|
| **error** (`kind=bad`) | นับรายการที่ได้กลับมา | Warning ถ้ามากกว่า `{$OS_RES.MAX}` (ค่าเริ่มต้น 0) |
| **ค้าง** (state scan) | ได้ 2 ค่า: `<what>` = จำนวนที่อยู่ในสถานะนั้นตอนนี้ (busy) กับ `<what>_stuck` = จำนวนที่ timestamp (`updated_at`/`created_at`) เก่ากว่า `stuck_minutes` (ค่าเริ่มต้น 60 นาที) | busy ใช้ดูกราฟ, stuck เป็น Warning |
| **trend** (`kind=busy`) | นับอย่างเดียว | ใช้ดูกราฟ ไม่มี trigger |

- `limit=<list_cap>` (ค่าเริ่มต้น 1000) ถ้าได้ครบ cap จะติดธง `capped=1` แปลว่าจริง ๆ อาจมีมากกว่านี้
- **ป้องกัน filter ถูกเพิกเฉย:** ถ้า API ไม่ทำตาม filter แล้วคืนทุกรายการกลับมา (เช่น Nova ไม่ยอมกรองด้วย `task_state` ถ้าไม่มีสิทธิ์) จะ**ไม่นับ** และให้ค่า -1 พร้อมบอกข้อผิดพลาด เพื่อไม่ให้ได้ตัวเลขผิด
- Cinder ใช้ `with_count=true&limit=1` พร้อม header `OpenStack-API-Version: volume 3.45` ให้ server นับให้เลยโดยไม่ต้องดึง list มาทั้งหมด

### 4.2 mid (ทุก 5 นาที)

| Service | แบบ | Path | สถานะ |
|---|---|---|---|
| Nova | error | `/servers?status=ERROR&limit=<cap>` | ERROR |
| | ค้าง | `/servers/detail?status=<S>&limit=<cap>` | BUILD, MIGRATING, RESIZE |
| | ค้าง | `/servers/detail?task_state=deleting&limit=<cap>` | deleting |
| | trend | `/servers?status=<S>&limit=<cap>` | SHUTOFF, PAUSED, SUSPENDED |
| Cinder | error | `/volumes`, `/snapshots`, `/backups` `?status=<S>&with_count=true&limit=1` | error, error_deleting, error_extending (volume), error_deleting (snapshot) |
| | ค้าง | `/<volumes,snapshots,backups>/detail?status=<S>&limit=<cap>` | volume: creating, deleting, attaching, detaching, extending · snapshot: creating, deleting · backup: creating, restoring, deleting |
| Neutron | error | `/v2.0/<routers,floatingips,ports>?status=ERROR&fields=id&limit=<cap>` | ERROR |
| Octavia | error | `/v2/lbaas/loadbalancers?provisioning_status=ERROR` และ `?operating_status=ERROR` | ERROR |
| | error | `/v2/lbaas/loadbalancers?operating_status=DEGRADED` / `OFFLINE` | DEGRADED, OFFLINE (LB ที่เจ้าของ disable เองจะ OFFLINE ด้วย ยอมรับได้ด้วย `{$OS_RES.MAX:"octavia:lb_offline"}`) |
| | error | `/v2/lbaas/pools?provisioning_status=ERROR`, `?operating_status=ERROR` | pool ERROR, pool down |
| | error | `/v2/lbaas/listeners?provisioning_status=ERROR` | listener ERROR |
| | ค้าง | `/v2/lbaas/loadbalancers?provisioning_status=<S>` | PENDING_CREATE/UPDATE/DELETE |
| Trove | error | `/instances?limit=<cap>` แล้วกรองเองที่ฝั่ง script | status = ERROR |
| Magnum | error | `/clusters?limit=<cap>` แล้วกรองเองที่ฝั่ง script | status มีคำว่า FAILED |
| Manila | error | `/shares?status=error&limit=<cap>` | error |
| | ค้าง | `/shares/detail?status=<S>&limit=<cap>` | creating, deleting, extending |
| Designate | error | `/v2/zones?status=ERROR&limit=<cap>` | ERROR |
| | ค้าง | `/v2/zones?status=PENDING&limit=<cap>` | PENDING |
| Ironic | error | `/v1/nodes?provision_state=<S>&fields=uuid` หรือ `?maintenance=true` | error, deploy failed, clean failed, maintenance |
| | ค้าง | `/v1/nodes?provision_state=<S>&fields=uuid,provision_state,...` | deploying, cleaning, wait call-back |

Trove กับ Magnum กรองเองที่ฝั่ง script เพราะ API ของทั้งสองไม่รองรับ `?status=`

### 4.3 slow (ทุก 30 นาที)

| Service | แบบ | Path | สถานะ |
|---|---|---|---|
| Heat | error | `/stacks?status=FAILED&limit=<cap>` | FAILED |
| | ค้าง | `/stacks?status=<S>&limit=<cap>` | CREATE/UPDATE/DELETE_IN_PROGRESS (timestamp: `updated_time`, `creation_time`) |
| Glance | ค้าง | `/v2/images?status=<S>&limit=<cap>` | queued, saving, importing, pending_delete |
| Trove | ค้าง | `/instances?limit=<cap>` (call เดียว แยกนับหลายสถานะ) | BUILD, SHUTDOWN/DELETING, RESIZE |
| Magnum | ค้าง | `/clusters/detail?limit=<cap>` (call เดียว แยกนับหลายสถานะ) | CREATE/UPDATE/DELETE_IN_PROGRESS |

Heat, Glance, Trove และ Magnum อยู่ใน slow tier เพราะต้นทุนของ call เพิ่มตามจำนวน resource ของลูกค้า

---

## 5. Glance canary (slow, impact = availability)

- **Path:** `GET /v2/images/{canary_id}/file` พร้อม header `Range: bytes=0-0`
- ดาวน์โหลดแค่ 1 byte จาก image เล็ก ๆ ที่ project มองเห็นได้ เพื่อยืนยันว่า backend ของ Glance (เช่น Ceph) อ่านข้อมูลได้จริง
- ถ้าได้ 404 แปลว่าไม่พบ canary image ให้ตรวจ `canary_id`
- ถ้าไม่ได้ตั้ง `canary_id` ใน ini จะไม่มีการเรียก

---

## 6. Check ที่ปิดไว้เป็นค่าเริ่มต้น

host ของชุด cloud เช็คสิ่งเหล่านี้อยู่แล้ว ถ้าเปิดซ้ำที่ project จะ alert 2 ครั้ง

| เปิดด้วย | Path | เช็ค |
|---|---|---|
| `project_l2 = 1` | `GET <root ของทุก endpoint>/` (ไม่ใช้ token), `GET {horizon_url}/auth/login/`, `GET {novnc_url}/vnc_lite.html` | process ของ API, Horizon และ noVNC ยังตอบไหม |
| `project_tls = 1` | TLS handshake ไปยัง https host:port ของ catalog | จำนวนวันก่อน certificate หมดอายุ |
| `s3_*` ใน ini | S3 / Ceph RGW | ไม่ใช้ token ของ Keystone |

**`ref_url`** (fast): `GET` ไปยังปลายทางนอก OpenStack ใช้แยกว่า "เครื่อง probe หลุดเน็ต" หรือ "cloud ล่ม" ควรตั้งไว้เสมอ เพราะเมื่อ `project_l2 = 0` นี่คือวิธีเดียวที่แยกสองกรณีนี้ได้

---

## 7. ปิด check ที่ policy ไม่อนุญาต

ถ้า check ใดได้ 401/403 ตลอด (policy ของ cloud ไม่ให้ member/reader เรียก) ให้ปิดใน ini

```ini
# ชื่อ check เดียว / ชื่อ service:check / wildcard
skip_checks = trove:datastores, neutron:subnets, heat:stacks_*
```

- `heat:stacks_*` ปิด FAILED และสถานะค้าง แต่ยังเก็บ `stacks` (functional probe) ไว้ ช่อง Heat ใน honeycomb จึงยังทำงานได้
- `heat:stacks*` ปิดทั้งหมด รวม `stacks` ด้วย ช่อง Heat จะไม่มี trigger และเป็นสีเขียวตลอด
- `skip_types = <service>` ปิดทั้ง service
- check ที่ถูก skip ส่งค่า `-1` จึงไม่มี alert
