# สรุปการออกแบบ Dashboard สำหรับ OpenStack Monitoring บน Zabbix 6.0
*(OpenStack Zabbix 6.0 Dashboard Design Specification — Overview & Per-Service Drill-Down)*

เอกสารฉบับนี้สรุปรายละเอียดการออกแบบ **Zabbix 6.0 Dashboard** สำหรับโปรเจกต์ OpenStack Monitoring ([`zbx-template-os-api-probe.yaml`](./zbx-template-os-api-probe.yaml), [`zbx-template-os-deps.yaml`](./zbx-template-os-deps.yaml), [`os_probe.py`](./os_probe.py), และ [`os_zabbix_setup.py`](./os_zabbix_setup.py)) โดยสอดคล้องกับแนวทางล่าสุดที่ **ไม่มีการคิด SLA รวมทั้ง OpenStack (No OpenStack Overall SLA)** แต่เน้นการดู **สถานะแยกตามเซอร์วิส, แยกส่วนที่เกิดปัญหา (HAProxy / VIP / Controller Node / Worker / Backend), และดูสถานะการสร้าง Resource เป็นตัวเลขจริง**

---

## 1. แนวคิดหลักของโครงสร้าง Dashboard (Single Pane of Glass → Drill-Down)

ออกแบบให้ตอบคำถามของทีมดูแลระบบ (NOC / SRE / On-call) และผู้บริหารเป็น **2 ระดับ**:

1. **หน้าหลัก (Master Overview Page — 1 หน้าดูภาพรวมทุกเซอร์วิส)**:
   * ตอบคำถาม: *"ตอนนี้เซอร์วิสไหน Up หรือ Down? เริ่มมีปัญหาตั้งแต่กี่โมง (ดูกราฟสถานะย้อนหลัง)? และกระทบ SLA หรือยัง?"*
2. **หน้าย่อยรายเซอร์วิส (Per-Service Sub-Pages — แยก Tab ตามเซอร์วิส)**:
   * ตอบคำถาม: *"เมื่อเซอร์วิสนี้มีปัญหา มันพังจากส่วนไหน (HAProxy, VIP, โหนด Controller, Worker Daemon, หรือ Storage/Hypervisor)? และตอนนี้มี Resource กำลังสร้างอยู่กี่อัน ค้างกี่อัน พังกี่อัน?"*

> [!IMPORTANT]
> **ไม่ต้องแก้ไข [`os_probe.py`](./os_probe.py) หรือไฟล์ Template `.yaml`**:
> Item ทุกตัวที่ใช้ในเอกสารนี้ (ทั้ง HTTP status รายเส้น API, เวลาตอบสนอง `ms`, สถานะ Daemon รายโหนด, สถานะ HAProxy backend, และตัวเลขนับจำนวน Resource ที่กำลังสร้าง/ค้าง/พัง) **มีอยู่ใน Template ปัจจุบันครบ 100% แล้ว**

---

## 2. การออกแบบหน้าหลัก: Master Overview Dashboard

จัดเรียงจากบนลงล่างตามลำดับความสำคัญ (Tier 1 Shared Critical → Tier 2 Core → Tier 3 Non-Core) เพื่อใช้จัดลำดับการมองเห็นโดยไม่มีการนำมาคิด SLA รวม:

```text
┌───────────────────────────────────────────────────────────────────────────────────┐
│ แถวที่ 1: สถานะปัจจุบันรายเซอร์วิส (Current Status Tiles — เรียงตาม Tier)           │
│ • Tier 1 (Shared): [Keystone] [MariaDB] [RabbitMQ] [HAProxy] [keepalived] [memc]  │
│ • Tier 2 (Core)  : [Nova] [Placement] [Glance] [Cinder] [Neutron] [OpenSDN]       │
│ • Tier 3 (Add-on): [Octavia] [Horizon] [Heat] [Magnum] [Barbican] [Trove]         │
│   (แสดง HTTP Status @ VIP: 200/300 = สีเขียว, 0/5xx = สีแดง + ค่า Latency ms)      │
├─────────────────────────────────────────┬─────────────────────────────────────────┤
│ แถวที่ 2A: กราฟสถานะย้อนหลังทุกเซอร์วิส    │ แถวที่ 2B: กราฟความเร็วย้อนหลังทุกเซอร์วิส │
│ (Historical Status Timeline)            │ (Historical Latency Timeline)           │
│ • SVG Graph (Stepped / ขั้นบันได):       │ • SVG Graph:                            │
│   `* root @ vip: HTTP status`           │   `* @ vip: response time`              │
│   + `Keystone: token request status`    │   + `Keystone: token request time`      │
│ • ปกติเกาะเส้น 200/300; ถ้าร่วงลง 0 หรือ   │ • แสดงเส้นเกณฑ์ SLA ที่ 5000 ms          │
│   ดีดขึ้น 500/502/503/504 เห็นเวลาที่ล่มทันที │                                         │
├─────────────────────────────────────────┴─────────────────────────────────────────┤
│ แถวที่ 3: รายการปัญหาปัจจุบัน (Active Problems — กรอง `az=<AZ>`)                    │
│ • แสดง Tag ครบ: `service`, `layer` (L2/L3/L4), `target` (vip/IP), `node`, `impact`│
├───────────────────────────────────────────────────────────────────────────────────┤
│ แถวที่ 4: สรุป SLA Report (เฉพาะ Product SLA และ/หรือ Core Services ที่ต้องการ)      │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. การออกแบบหน้าย่อยรายเซอร์วิส (Per-Service Sub-Page Layout)

เมื่อคลิกเข้ามาดูหน้าย่อยของเซอร์วิสใดเซอร์วิสหนึ่ง (เช่น **Nova**, **Cinder**, **Keystone**, **Glance**, **Neutron**, **Octavia** ฯลฯ) จะแบ่งหน้าจอเป็น **5 แถว (จากบนลงล่าง)** เพื่อให้ไล่สาเหตุได้ครบทุกชั้นในหน้าเดียว:

```text
┌─────────────────────────────────────────┬─────────────────────────────────────────┐
│ แถวที่ 1A: สถานะ HAProxy ของเซอร์วิสนี้    │ แถวที่ 1B: HTTP Status เส้น API สำคัญ     │
│ (L4 Load Balancer Layer)                │ (L2/L3 VIP เทียบกับ Controller รายโหนด)  │
│ • `HAProxy backend *<svc>*: state`      │ • `<Label> root @ vip: HTTP status`     │
│   (1 = Up, 0 = Down)                    │ • `<Label> root @ <ctl1..3>: HTTP status`│
│ • `HAProxy backend *<svc>*: active`     │ • เส้น API เฉพาะของเซอร์วิสนั้น (เช่น      │
│   (จำนวนเครื่องหลังบ้านที่ออนไลน์ 3→2→0)   │   volume/backup/pool/token/novnc/canary)│
├─────────────────────────────────────────┼─────────────────────────────────────────┤
│ แถวที่ 2A: สถานะ Node / Worker / Backend │ แถวที่ 2B: เวลาตอบสนอง API (Latency ms) │
│ (L3 Internal Daemons & Storage/Compute) │ (VIP เทียบกับ Controller รายโหนด)        │
│ • `<Label> * on *: state`               │ • `<Label> * @ *: response time`        │
│   (1 = Up, 0 = Down, 2 = Maintenance)   │ • เทียบกับเส้นเกณฑ์ SLA 5000 ms           │
│   เช่น scheduler, conductor, volume      │ • ช่วยชี้ชัดว่าโหนดไหนช้าแล้วดึง VIP ช้าตาม │
│ • สถานะ Pool (Cinder) / Hypervisor (Nova)│                                         │
├─────────────────────────────────────────┼─────────────────────────────────────────┤
│ แถวที่ 3A: จำนวน Resource ที่กำลังทำงานอยู่ │ แถวที่ 3B: จำนวน Resource ที่ค้าง / พัง    │
│ (Active / Busy Count — แสดงเป็นตัวเลข)   │ (Stuck & Error Count — แสดงเป็นตัวเลข)   │
│ • กราฟแสดงจำนวนชิ้นที่กำลังสร้าง/ลบ/ย้าย   │ • `*_stuck` (ค้างเกิน `stuck_minutes`    │
│   ณ ตอนนั้นจริง ๆ (`kind=busy`)          │   ค่าเริ่มต้น 30 นาที)                     │
│   เช่น `servers_build`, `volumes_creating`│ • `*_error` / `*_failed` (สร้างพัง)     │
│   `backups_creating`, `lb_pending_create`│   (ยามปกติต้องเป็น 0; ถ้า > 0 แปลว่าติดขัด)│
├─────────────────────────────────────────┴─────────────────────────────────────────┤
│ แถวที่ 4: ตาราง Problems ของเซอร์วิสนี้ (`service=<svc>`)                           │
│ • แสดงรายการแจ้งเตือนทั้งหมดของเซอร์วิสนี้ (รวมถึงถ้าเส้น API ดึง Resource ได้ 4xx/5xx)│
├─────────────────────────────────────────┬─────────────────────────────────────────┤
│ แถวที่ 5A: SLA Availability ของเซอร์วิสนี้ │ แถวที่ 5B: SLA Response Time ของเซอร์วิสนี้│
│ • `SLA - <AZ> - <Service> - Availability`│ • `SLA - <AZ> - <Service> - Response`  │
└─────────────────────────────────────────┴─────────────────────────────────────────┘
```

---

## 4. ตารางการอ่านกราฟหน้าย่อยเพื่อชี้จุดเสีย (Root-Cause Matrix)

ด้วยการวางกราฟเปรียบเทียบกันทั้ง 3 แถวบน ผู้ดูแลระบบจะแยกสาเหตุของปัญหาออกได้ทันทีดังนี้:

| อาการที่เห็นบนหน้า Dashboard ย่อยของเซอร์วิส | สาเหตุของปัญหา (Root Cause) |
| :--- | :--- |
| **แถว 1A** (`HAProxy active servers`) เหลือ `0` หรือ **แถว 1B** เส้น `root @ vip` เป็น `0`/`503` แต่เส้น `root @ ctl1..3` ยังเป็น `200` | ปัญหาอยู่ที่ **VIP / Keepalived / HAProxy** (ตัว API บนโหนด Controller ยังดีอยู่) |
| **แถว 1B** เส้น `root @ ctl2` ตกเป็น `0` หรือ `502` อยู่เส้นเดียว ส่วน `vip`, `ctl1`, `ctl3` ยังเป็น `200` | **API Container บนโหนด `ctl2` พังโหนดเดียว** ระบบยังทำงานต่อได้ผ่าน HA (`ALG_ALL`) ไม่เสีย SLA |
| **แถว 1B** เส้น API ทุกเส้นเป็น `200` ปกติ แต่ **แถว 2A** (`<Label> * on *: state`) ตกเป็น `0` (Down) | **API หน้าบ้านยังตอบได้ แต่ Worker ข้างหลังตาย** (เช่น `nova-scheduler`, `nova-conductor`, `cinder-volume`, `cinder-backup`, `heat-engine` ตาย) |
| **แถว 1B** และ Worker ปกติ แต่ **แถว 2A** มี `Cinder pool *: backend state = 0` หรือ `Hypervisors: schedulable = 0` | **Control Plane ปกติ แต่ Storage Backend (Ceph/SAN) หรือ Compute Hypervisor พัง** |
| **แถว 3A** (`กำลังสร้าง`) พุ่งขึ้น แล้วตามด้วย **แถว 3B** (`*_stuck` หรือ `*_error`) ดีดขึ้นจาก `0` เป็น `1, 2, 3...` | **การสร้าง Resource ของลูกค้าค้างหรือพัง** (เช่น สร้าง VM ค้าง `BUILD` หรือสร้าง Volume/Backup ค้าง) |
| **แถว 1B** ทั้ง `vip` และ `ctl1..3` ร่วงเป็น `500` หรือ `504` พร้อมกันทุกเส้นในเวลาเดียวกัน | **ปัญหาที่ L4 Shared Dependency (MariaDB Galera หรือ RabbitMQ)** → เปิดดูหน้าย่อย L4 ได้ทันที |

---

## 5. รายละเอียดสิ่งที่ต้องดึงมาแสดงในหน้าย่อยของแต่ละเซอร์วิส (ครบทุกเซอร์วิส)

ข้อมูลด้านล่างอ้างอิงจากชื่อ Item จริงใน [`zbx-template-os-api-probe.yaml`](./zbx-template-os-api-probe.yaml) และ [`zbx-template-os-deps.yaml`](./zbx-template-os-deps.yaml) โดยทุกค่าสถานะ Resource (`os.mid.res["{#ID}"]` และ `os.slow.res["{#ID}"]`) **เก็บเป็นตัวเลขจำนวนชิ้น (Count)** เช่น `0`, `1`, `5` อัน:

### 5.1 Cinder (Block Storage)
* **1) เส้น API สำคัญ (HTTP Status & Response Time)**:
  * **API หลัก**: `Cinder root @ vip`, `Cinder root @ <ctl1..3>`, `Cinder os-services @ vip`
  * **Pool / Volume / Snapshot / Backup API**:
    * `Cinder mid pools: HTTP status` (`/scheduler-stats/get_pools`)
    * `Cinder mid volumes_creating: HTTP status` และ `Cinder mid volumes_error: HTTP status` (`/volumes`)
    * `Cinder mid snapshots_creating: HTTP status` และ `Cinder mid snapshots_error: HTTP status` (`/snapshots`)
    * `Cinder mid backups_creating: HTTP status` และ `Cinder mid backups_error: HTTP status` (`/backups`)
* **2) สถานะ HAProxy / Node / Daemon / Storage Pool (`state`)**:
  * **HAProxy**: `HAProxy backend *cinder*: state` (`1`=Up, `0`=Down) & `active servers`
  * **Cinder Daemons รายโหนด**: `Cinder cinder-scheduler on *: state`, `Cinder cinder-volume on *: state`, `Cinder cinder-backup on *: state`
  * **Storage Pools**: `Cinder pool *: backend state` (`1`=Up, `0`=Down) และ `Cinder pool *: free` (`%`)
* **3) สถานะ Resource เป็นตัวเลขจำนวนชิ้น (กำลังทำ vs ค้าง/พัง)**:
  * **กำลังทำตอนนี้ (Busy Count)**:
    * Volume: `Cinder: volumes_creating`, `Cinder: volumes_attaching`, `Cinder: volumes_detaching`, `Cinder: volumes_deleting`, `Cinder: volumes_extending`
    * Snapshot: `Cinder: snapshots_creating`, `Cinder: snapshots_deleting`
    * Backup: `Cinder: backups_creating`, `Cinder: backups_restoring`, `Cinder: backups_deleting`
  * **ค้างเกินเวลา (Stuck Count)**:
    * `Cinder: volumes_creating_stuck`, `volumes_attaching_stuck`, `volumes_detaching_stuck`, `volumes_deleting_stuck`, `volumes_extending_stuck`
    * `Cinder: snapshots_creating_stuck`, `snapshots_deleting_stuck`
    * `Cinder: backups_creating_stuck`, `backups_restoring_stuck`, `backups_deleting_stuck`
  * **พัง / Error (Error Count)**:
    * `Cinder: volumes_error`, `Cinder: volumes_error_deleting`, `Cinder: volumes_error_extending`, `Cinder: snapshots_error`, `Cinder: snapshots_error_deleting`, `Cinder: backups_error`

---

### 5.2 Nova (Compute)
* **1) เส้น API สำคัญ (HTTP Status & Response Time)**:
  * **API & Console หลัก**: `Nova root @ vip`, `Nova root @ <ctl1..3>`, `Nova novnc @ vip`, `Nova novnc @ <ctl1..3>`, `Nova os-services @ vip`
  * **Hypervisor & Server API**: `Nova mid hypervisors: HTTP status`, `Nova mid servers_build: HTTP status`, `Nova mid servers_error: HTTP status`
* **2) สถานะ HAProxy / Node / Daemon / Hypervisor (`state`)**:
  * **HAProxy**: `HAProxy backend *nova*: state` & `active servers`
  * **Nova Daemons รายโหนด**: `Nova nova-scheduler on *: state`, `Nova nova-conductor on *: state`, `Nova nova-compute on *: state`
  * **Hypervisors**: `Hypervisors: schedulable`, `Hypervisors: down`, `Hypervisors: in maintenance`, และ `Hypervisor *: state`
* **3) สถานะ Resource เป็นตัวเลขจำนวนเครื่อง (กำลังทำ vs ค้าง/พัง)**:
  * **กำลังทำตอนนี้ (Busy Count)**: `Nova: servers_build` (กำลังสร้าง), `Nova: servers_deleting` (กำลังลบ), `Nova: servers_migrating` (กำลัง Migrate), `Nova: servers_resize` (กำลัง Resize), และ `Cluster: running VMs`
  * **ค้างเกินเวลา (Stuck Count)**: `Nova: servers_build_stuck`, `Nova: servers_deleting_stuck`, `Nova: servers_migrating_stuck`, `Nova: servers_resize_stuck`
  * **พัง / Error (Error Count)**: `Nova: servers_error`

---

### 5.3 Keystone (Identity)
* **1) เส้น API สำคัญ (HTTP Status & Response Time)**:
  * `Keystone: token request status` & `Keystone: token request time` (`POST /v3/auth/tokens`)
  * `Keystone root @ vip` & `Keystone root @ <ctl1..3>` (`GET /v3`)
  * `Keystone catalog @ vip: HTTP status` (`GET /v3/auth/catalog`)
  * `Keystone slow app_credential: HTTP status`
* **2) สถานะ HAProxy**:
  * `HAProxy backend *keystone*: state` & `active servers`
* **3) ตัวเลขสุขภาพ Resource / Credential**:
  * `Keystone: catalog services` (จำนวนเซอร์วิสใน Catalog)
  * `Probe credential: days until expiry` และ `TLS endpoints: soonest certificate expiry`

---

### 5.4 Glance (Image Service)
* **1) เส้น API สำคัญ (HTTP Status & Response Time)**:
  * `Glance root @ vip` & `Glance root @ <ctl1..3>`
  * `Glance images @ vip: HTTP status` (`/v2/images?limit=1`)
  * `Glance stores @ vip: HTTP status` (`/v2/info/stores`)
  * `Glance slow canary: HTTP status` & `response time` (ดาวน์โหลดอิมเมจจริง byte 0-0)
* **2) สถานะ HAProxy & Stores**:
  * `HAProxy backend *glance*: state` & `active servers`
  * `Glance: stores` (จำนวน Store ที่พร้อมใช้งาน)
* **3) สถานะ Resource เป็นตัวเลขจำนวน Image (กำลังทำ vs ค้าง)**:
  * **กำลังทำตอนนี้ (Busy Count)**: `Glance: images_queued`, `Glance: images_saving`, `Glance: images_importing`, `Glance: images_deleting`
  * **ค้างเกินเวลา (Stuck Count)**: `Glance: images_queued_stuck`, `Glance: images_saving_stuck`, `Glance: images_importing_stuck`, `Glance: images_deleting_stuck`

---

### 5.5 Neutron & OpenSDN (Networking)
* **1) เส้น API สำคัญ (HTTP Status & Response Time)**:
  * **Neutron**: `Neutron root @ vip`, `Neutron root @ <ctl1..3>`, `Neutron networks @ vip` (`/v2.0/networks?limit=1`), `Neutron mid ip_avail: HTTP status` (`/v2.0/network-ip-availabilities`)
  * **OpenSDN**: `OpenSDN config-api @ *: HTTP status`, `OpenSDN analytics-api @ *: HTTP status`, `OpenSDN control @ *: HTTP status`, `OpenSDN mid uve-*: HTTP status`
* **2) สถานะ HAProxy & SDN Modules/Processes**:
  * `HAProxy backend *neutron*: state` & `active servers`
  * `OpenSDN * (*): instances up` เทียบกับ `instances total`
  * `OpenSDN * on *: state` และ `OpenSDN: processes down`
* **3) สถานะ Resource (Network IP Availability)**:
  * `Network * (*): free IPs %`, `Network * (*): free IPs`, `Network * (*): used IPs`

---

### 5.6 Octavia (Load Balancer as a Service)
* **1) เส้น API สำคัญ (HTTP Status & Response Time)**:
  * `Octavia root @ vip`, `Octavia root @ <ctl1..3>`, `Octavia providers @ vip` (`/v2/lbaas/providers`)
  * `Octavia slow amphorae: HTTP status`, `Octavia mid lb_pending_create: HTTP status`, `Octavia mid lb_provisioning_error: HTTP status`
* **2) สถานะ HAProxy & Health Manager**:
  * `HAProxy backend *octavia*: state` & `active servers`
  * สถานะพอร์ต `Octavia health-manager UDP 5555` รายโหนด (จาก `OS-Deps-Node`)
* **3) สถานะ Resource เป็นตัวเลขจำนวน LB / Amphora (กำลังทำ vs ค้าง/พัง)**:
  * **กำลังทำตอนนี้ (Busy Count)**: `Octavia: lb_pending_create`, `Octavia: lb_pending_update`, `Octavia: lb_pending_delete`, `Octavia: amphorae_booting`, `Octavia: amphorae_deleting`, และ `Octavia: spare amphorae`
  * **ค้างเกินเวลา (Stuck Count)**: `Octavia: lb_pending_create_stuck`, `Octavia: lb_pending_update_stuck`, `Octavia: lb_pending_delete_stuck`, `Octavia: amphorae_booting_stuck`, `Octavia: amphorae_deleting_stuck`
  * **พัง / Error (Error Count)**: `Octavia: lb_provisioning_error`, `Octavia: lb_operating_error`, `Octavia: amphorae_error`

---

### 5.7 Heat, Magnum, Trove, Manila, Ironic, Barbican, Horizon
แต่ละเซอร์วิสใช้โครงสร้าง 5 แถวเดียวกัน:

| เซอร์วิส | เส้น API สำคัญ (HTTP Status & ms) | สถานะ HAProxy & Daemon รายโหนด | ตัวเลขสถานะ Resource (กำลังทำ / ค้าง / พัง) |
| :--- | :--- | :--- | :--- |
| **Heat** | `root @ vip/ctl`, `services @ vip`, `slow stacks_*` | `HAProxy *heat*`, `Heat heat-engine on *: state` | • **Busy**: `stacks_creating`, `stacks_updating`, `stacks_deleting`<br>• **Stuck**: `stacks_*_stuck`<br>• **Failed**: `stacks_failed` |
| **Magnum** | `root @ vip/ctl`, `mservices @ vip`, `slow clusters_states`, `mid clusters_failed` | `HAProxy *magnum*`, `Magnum magnum-conductor on *: state` | • **Busy**: `clusters_creating`, `clusters_updating`, `clusters_deleting`<br>• **Stuck**: `clusters_*_stuck`<br>• **Failed**: `clusters_failed` |
| **Trove** | `root @ vip/ctl`, `datastores @ vip`, `slow instances_states`, `mid instances_error` | `HAProxy *trove*` | • **Busy**: `instances_build`, `instances_deleting`, `instances_resize`<br>• **Stuck**: `instances_*_stuck`<br>• **Error**: `instances_error` |
| **Manila** | `root @ vip/ctl`, `services @ vip`, `mid shares_*` | `HAProxy *manila*`, `Manila manila-* on *: state` | • **Busy**: `shares_creating`, `shares_deleting`, `shares_extending`<br>• **Stuck**: `shares_*_stuck`<br>• **Error**: `shares_error` |
| **Ironic** | `root @ vip/ctl`, `conductors @ vip`, `mid nodes_*` | `HAProxy *ironic*`, `Ironic ironic-conductor on *: state` | • **Busy**: `nodes_deploying`, `nodes_cleaning`, `nodes_wait_callback`<br>• **Stuck**: `nodes_*_stuck`<br>• **Error**: `nodes_error`, `nodes_deploy_failed`, `nodes_clean_failed` |
| **Placement** | `root @ vip/ctl`, `mid resource_providers` | `HAProxy *placement*` | • `Placement: root resource providers` (จำนวน RP) |
| **Barbican** | `root @ vip/ctl` | `HAProxy *barbican*` | — |
| **Horizon** | `Horizon login @ vip` (`/auth/login/`) | `HAProxy *horizon*` | — |

---

### 5.8 หน้าย่อย L4 Core Infrastructure (`HAProxy`, `RabbitMQ`, `MariaDB`, `keepalived`, `memcached`)
เพื่อให้ไล่ปัญหาชั้น L4 ได้ครบ จึงมีหน้าย่อยสำหรับโครงสร้างพื้นฐาน:
1. **HAProxy**:
   * กราฟ `HAProxy backend *: state` (`1`/`0`), `HAProxy backend *: active servers`, `HAProxy backend *: session usage (%)`
   * ตาราง Problems กรอง `service=haproxy` + SLA `SLA - <AZ> - HAProxy - Availability`
2. **RabbitMQ**:
   * กราฟ `RabbitMQ: nodes running` เทียบกับ `nodes total`, `RabbitMQ: partitions`, `RabbitMQ: memory alarm` / `disk alarm`
   * กราฟ `RabbitMQ: messages ready`, `messages unacknowledged`, และ `queues with 0 consumers`
   * ตาราง Problems กรอง `service=rabbitmq` + SLA `SLA - <AZ> - RabbitMQ - Availability`
3. **MariaDB Galera**:
   * กราฟรายโหนด `mysql.ping`, `Galera: cluster size`, `Galera: ready / primary / synced`, `Galera: flow control paused ratio`
   * ตาราง Problems กรอง `service=mariadb` + SLA `SLA - <AZ> - MariaDB - Availability`
4. **keepalived & memcached**:
   * กราฟ `VIP *: holders` (ปกติ `= 1`, หาย `= 0`, split-brain `> 1`) และ `memcached.ping` รายโหนด
   * ตาราง Problems กรอง `service=keepalived` / `service=memcached` + SLA ประจำตัว

---

## 6. สรุปขั้นตอนการนำไปสร้างจริงด้วย [`os_zabbix_setup.py`](./os_zabbix_setup.py)

เมื่อต้องการสร้าง Dashboard ชุดนี้ลงใน Zabbix 6.0 อัตโนมัติ จะดำเนินการแก้ไขเฉพาะฟังก์ชัน `Builder.build_dashboards` ใน [`os_zabbix_setup.py`](./os_zabbix_setup.py) โดย:
1. สร้าง **หน้าหลัก (Master Overview)** แสดงสถานะ VIP, กราฟขั้นบันได HTTP status ย้อนหลัง, กราฟ Response time ย้อนหลัง, ตาราง Problems, และ SLA Summary
2. สร้าง **หน้าย่อยรายเซอร์วิส (`OpenStack · Per service` + `L4 Infrastructure`)** ครบทั้ง 5 แถว (HAProxy + เส้น API สำคัญ, สถานะ Daemon/Node + Latency, กราฟตัวเลข Resource กำลังสร้าง vs ค้าง/Error, ตาราง Problems, และ SLA Report ของเซอร์วิสนั้น)
3. รันคำสั่ง `python3 os_zabbix_setup.py -c deploy/os_zabbix_setup.example.json --only dashboards` เพื่ออัปเดต Dashboard บน Zabbix Server ได้แบบ Idempotent (รันซ้ำได้ตลอดโดยไม่ซ้ำซ้อน)
