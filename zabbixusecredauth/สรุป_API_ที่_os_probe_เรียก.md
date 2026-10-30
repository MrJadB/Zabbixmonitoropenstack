# สรุป API ที่ os_probe.py เรียก

อ้างอิง: `os_probe.py` (v2) · tier: fast ทุก 2 นาที, mid ทุก 5 นาที, slow ทุก 30 นาที

## 1. Root ของ endpoint (ทุก service, fast ทุก 2 นาที)

- **Path:** `GET http://<VIP หรือ IP ของ controller>:<port>/` โดยไม่ใช้ token
- **ใช้กับ:** ทุก service ที่อยู่ใน catalog เช่น Keystone, Nova, Neutron, Glance, Cinder, Heat, Octavia, Placement, Barbican, Trove, Magnum
- **เช็ค:** process ของ API ยังตอบไหม ทั้งตอนผ่าน HAProxy (VIP) และตอนยิงตรงเข้าแต่ละ controller เป็นเช็คด้าน **availability** (L2) และไม่ต้องใช้ token จึงยังเช็คได้แม้ Keystone ล่ม
- เช็คเดียวกันนี้ยังใช้กับ Horizon (`GET /auth/login/`), noVNC และ `ref_url` (ปลายทางนอก OpenStack ใช้แยกว่าเครื่อง probe หลุดเน็ตหรือ cloud ล่ม)

## 2. Keystone: token และสิทธิ์

| Path | เช็ค | Tier |
|---|---|---|
| `POST /v3/auth/tokens` | ขอ token ด้วย application credential ได้ไหม, ใช้เวลาเท่าไร | fast |
| `GET /v3/auth/catalog` | catalog มีครบทุก service ไหม | fast |
| `GET /v3/users/{id}/application_credentials/{id}` | อีกกี่วันจะหมดอายุ | slow |

## 3. API ตอบได้จริงเมื่อใช้ token (fast)

เรียก list สั้น ๆ แบบ `limit=1` เพื่อดูว่า API ทำงานครบถึงฐานข้อมูล ไม่ใช่แค่ process ยังเปิดอยู่

| Service | Path |
|---|---|
| Neutron | `GET /v2.0/networks?limit=1` |
| Glance | `GET /v2/images?limit=1`, `GET /v2/info/stores` |
| Trove | `GET /datastores` |
| Octavia | `GET /v2/lbaas/providers` |

## 4. สถานะ service/agent ราย host (fast)

- **Path:** `GET /os-services` (Nova, Cinder), `GET /services` (Heat, Manila), `GET /mservices` (Magnum), `GET /v1/conductors` (Ironic)
- **เช็ค:** service ภายในแต่ละตัวบนแต่ละ host เช่น nova-scheduler, nova-conductor, nova-compute, cinder-volume, cinder-scheduler, heat-engine ว่ายัง up อยู่ไหม และมีตัวไหนถูก disable, forced down หรืออยู่ใน maintenance

## 5. ภาพรวม cluster และ capacity (mid ทุก 5 นาที)

| Service | Path | เช็ค |
|---|---|---|
| Nova | `GET /os-hypervisors/detail` | hypervisor up/down, vCPU/RAM ทั้งหมดและที่จองไป, จำนวน VM |
| Placement | `GET /resource_providers` | จำนวน compute ที่ลงทะเบียน |
| Cinder | `GET /scheduler-stats/get_pools?detail=true` | backend pool และพื้นที่ storage |
| Neutron | `GET /v2.0/network-ip-availabilities` | IP ใน subnet ใกล้หมดไหม |
| OpenSDN | `GET /analytics/uves/<node-type>/*` | process ของ config, control, analytics และ database node |

## 6. นับ resource ที่ error หรือค้าง (mid/slow)

ทุก service ใช้รูปแบบเดียวกัน คือ **list resource แบบกรองตาม status จากทุก project** (`all_tenants=1` หรือ `global_tenant=true`) แล้วนับ 2 ค่า:

- จำนวนที่เป็น **error**
- จำนวนที่อยู่ในสถานะชั่วคราว (creating, deleting, pending) ตอนนี้ (**busy**) และจำนวนที่ค้างนานเกิน `stuck_minutes` (ค่าเริ่มต้น 60 นาที) (**stuck**)

| Service | Path | สถานะที่ดู | Tier |
|---|---|---|---|
| Nova | `GET /servers/detail?status=...&all_tenants=1` | ERROR, BUILD, deleting, MIGRATING, RESIZE | mid |
| Cinder | `GET /volumes`, `/snapshots`, `/backups` `?status=...&all_tenants=1` | error, creating, deleting, attaching, extending, restoring | mid |
| Octavia | `GET /v2/lbaas/loadbalancers?provisioning_status=...` | ERROR, PENDING_CREATE/UPDATE/DELETE | mid |
| Octavia | `GET /v2/octavia/amphorae?status=...` | ERROR, BOOTING, PENDING_DELETE, spare amphora | slow |
| Heat | `GET /stacks?status=...&global_tenant=true` | FAILED, CREATE/UPDATE/DELETE_IN_PROGRESS | slow |
| Glance | `GET /v2/images?status=...` | queued, saving, importing, pending_delete | slow |
| Trove / Magnum | `GET /instances`, `GET /clusters/detail` | ERROR/FAILED และสถานะที่ค้าง | mid/slow |
| Ironic / Manila | `GET /v1/nodes?provision_state=...`, `GET /shares?status=...` | error, failed, deploying, cleaning | mid |

กลุ่มนี้ต้องอ่านข้อมูลของทุก project จึงเป็นกลุ่มที่ติด 403 policy มากที่สุด

## 7. ทดสอบการใช้งานจริง (slow)

- **Glance canary:** `GET /v2/images/<canary_id>/file` แบบ Range 0-0 เพื่อดูว่าดึงข้อมูล image จาก storage ได้จริง

## สรุปตามประเภทปัญหา

- กลุ่ม 1–4 ใช้บอกว่าระบบ **ล่ม** หรือไม่ ซึ่งนับเข้า SLA
- กลุ่ม 5–6 ใช้ดู **capacity** และ resource ที่ค้างหรือ error ซึ่งไม่นับเข้า SLA

## API ที่ติด 403 (policy) ณ ตอนนี้

- Octavia: `GET /v2/lbaas/providers`, `GET /v2/lbaas/loadbalancers` (ทุก project), `GET /v2/octavia/amphorae`
- Cinder: `GET /v3/{project}/scheduler-stats/get_pools`, `GET /v3/{project}/os-services`
- Neutron: `GET /v2.0/networks`
- Heat: `GET /v1/{project}/stacks?global_tenant=true`
