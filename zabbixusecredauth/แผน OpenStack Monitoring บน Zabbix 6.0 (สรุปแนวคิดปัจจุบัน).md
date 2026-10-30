# แผน OpenStack Monitoring บน Zabbix 6.0 (สรุปแนวคิดปัจจุบัน)

Oct 21, 2026 · @Nipa Cloud

## ภาพรวม

ข้อสรุป: ต่อยอดแกนของ `os_probe.py` ได้ แต่ใช้เป็นเพียงชั้น L3 ของระบบใหม่ เพราะของเดิมเป็น API health probe ที่ครอบคลุมน้อยกว่าแนวคิด 4 ชั้น (L1–L4) ที่ตั้งไว้ จึงต้องวางสถาปัตยกรรมใหม่เป็นหลาย template ตามชั้น แล้วเก็บเฉพาะส่วนที่ใช้ได้ไว้

**เป้าหมาย**

- monitor ทุก service และ service ย่อยของ OpenStack ที่ deploy ด้วย Kolla-Ansible โดยรู้ว่าอะไรล่ม ล่มเพราะอะไร และ API ตอบหรือไม่
- วัด SLA ต่อ product ที่ลูกค้าเห็น (Compute, Block Storage, Network ฯลฯ) ไม่ใช่ต่อ container

**ข้อกำหนดที่ล็อกไว้**

- Zabbix 6.0.x เท่านั้น ห้ามใช้ฟีเจอร์ 6.2 ขึ้นไป (ไม่มี Gauge, Pie, Honeycomb, global filter)
- multi-AZ มี Zabbix proxy ต่อ AZ
- SDN เป็น OpenSDN (Tungsten Fabric) จึงไม่มี Neutron agent ให้เช็ค
- credential แบบ least-privilege และต้องรอดจาก `kolla-ansible reconfigure` กับการ re-import template

**การตัดสินใจที่ได้ตกลงกันแล้ว**

1. ตัด L1 (Docker container) ออกจากขอบเขตรอบแรก
2. รัน probe แบบ A: timer + trapper บน proxy host ต่อ AZ (วัดจากนอก node เหมาะกับ SLA)
3. script ไฟล์เดียว รับ `--tier fast|mid|slow` และ master item แยก 3 ตัวต่อ AZ
4. ทุก check ส่งสถานะเป็น code ตรง ๆ (`http_status` เช่น 200, 404, 503) ไม่แปลงเป็นหมวด เพื่อดูสาเหตุล่มได้ตรง ๆ

## สิ่งที่มีอยู่แล้ว

ของเดิมคือ `os_probe.py` กับ template `STG-ORG OpenStack-api` ซึ่งเช็คได้แค่ความพร้อมของ API (Layer 1 แบบไม่ใช้ token และ Layer 2 แบบใช้ token) และเก็บข้อมูลบางส่วนไว้แต่ยังไม่มี trigger รองรับ

### ใช้ต่อได้

| ส่วน | สิ่งที่มี | ทำไมใช้ต่อได้ |
| --- | --- | --- |
| `os_probe.py` แกนหลัก | auth ด้วย application credential, เดิน service catalog, รันขนาน (thread pool), ตาม pagination, แปลง error เป็นข้อความ `HTTP 403: <policy reason>`, canary image แบบ stream | ครบกว่าเวอร์ชัน JS เดิม และไม่ต้องจำกัด 60 วินาทีแบบ Script item |
| โหมด trapper | `--send` ส่ง JSON เข้า Zabbix ด้วย sender protocol เอง (ไม่ต้องมี `zabbix_sender`), อ่าน ini ต่อ section/AZ | ตรงกับแผน A (timer + trapper) อยู่แล้ว |
| รูปแบบ template | master item → dependent item → LLD, ค่า `DISCARD_UNCHANGED_HEARTBEAT`, valuemap, macro พร้อม override ต่อ service | ใช้ซ้ำกับ template ใหม่ได้เลย |
| ข้อมูลใน JSON ที่ยังไม่มี item/trigger | `service_agents` (host, binary, state, disabled\_reason), `hypervisors` (vCPU/RAM/disk, state) | เพิ่ม LLD ได้โดยไม่เพิ่ม API call |
| template อื่นที่ทำไว้ | OpenSDN vRouter (KDropStats), Ceph (Prometheus), `validate_zbx_export.py`, บทเรียน Zabbix 6.0 | ใช้ต่อ แต่ต้องปรับ tag ให้ตรง schema ใหม่ |

### ช่องโหว่ที่พบจากการอ่านไฟล์

| ประเด็น | สิ่งที่พบ | ผล |
| --- | --- | --- |
| ความครอบคลุม | เช็คแค่ API health ไม่มี L1, L2 รายต่อ node, L4 (Galera, RabbitMQ, HAProxy, memcached), tag/Services/SLA | ครอบคลุมไม่ถึงครึ่งของแนวคิด |
| trigger | มี trigger เฉพาะ `api.http`, `api.ok`, `api.ms`, `res.bad`, `collect_ok`, `total_ms`, `checks_skipped` ส่วน `os.nova.down`, `os.cinder.down`, `os.hv.down`, `os.hv.error` ไม่มี trigger | ข้อมูลเก็บอยู่แต่ไม่แจ้งเตือน |
| LLD | description บอก 4 ตัว (API, hypervisor, Nova, Cinder) แต่ใน YAML มี 2 ตัว (API, Resource) | ไม่รู้ว่า compute หรือ cinder-volume ตัวไหนล่ม มีแต่ตัวเลขรวม |
| timeout | master item เป็น External check (เพดานของ proxy ไม่เกิน 30 วินาที) แต่ macro ตั้ง `deadline_ms`=45000, `http_timeout_ms`=50000, `PROBE.TIMEOUT`=90s ซึ่งเป็นค่าของเวอร์ชัน Script item | ชนเพดานแล้วเสียข้อมูลทั้งก้อน |
| หน้าต่างเวลาของ trigger | poll ทุก 5 นาที แต่ใช้ `min(...,5m)` และ recovery `min(...,3m)` พร้อม `DISCARD_UNCHANGED_HEARTBEAT` | หน้าต่างอาจไม่มีค่า recovery ไม่แน่นอน ควรใช้ `last(#N)` |
| impact ผิด | `collect_ok=0` ติด `scope=availability` และ `res.bad` ก็ติด availability | credential หมดอายุจะถูกนับเป็น downtime ใน SLA |
| แยก error | `ok=0` เหมือนกันทุกกรณี (401/403/404/5xx/timeout) มีแต่ข้อความ | แยก monitoring ออกจาก availability ไม่ได้ |
| จุดที่ถูกข้าม | `network` และ `object-store` อยู่ใน `admin_only_types`; `orchestration`, `placement`, `baremetal`, `metric`, `key-manager` เป็น L1\_ONLY คือ `ok=1` เสมอ | ในทางปฏิบัติ Neutron, Heat, Placement ไม่ได้ถูกทดสอบด้วย token |
| ขอบเขตข้อมูล | resource sweep ไม่ใช้ `all_tenants` จึงเห็นเฉพาะ project ของ credential และใช้การ list เต็มแทนการนับ | ตัวเลขไม่ใช่ของลูกค้า และโตตามขนาด cloud |
| ค่าค้างจาก JS และ STG | description ยังบอก Script item/Duktape/ไม่มี interface; macro ไม่ถูกใช้ (`OS_HV.CPU.WARN`, `OS_HV.MEM.WARN`, `OS_MAINT.STALE`, `OS_PROBE.INTERVAL`); item Neutron agents ไม่มีความหมายบน OpenSDN; ฝัง cred id, canary UUID, Keystone URL ของ STG | ต้องเคลียร์ก่อนใช้ multi-AZ |
| tag | ใช้ `scope`, `layer=l1-blackbox`, `service`=ชื่อใน catalog (เช่น `cinderv3`) | ไม่ตรง schema `service/layer/impact/component/node/az` ผูก Services ไม่ได้ |

## สิ่งที่ปรับเพิ่ม

งานทั้งหมดแบ่งเป็นปรับ (แก้ของเดิม) เพิ่ม (สร้างใหม่) ลบ และตัดออกจากขอบเขต

| # | งาน | ประเภท | อยู่ที่ |
| --- | --- | --- | --- |
| 1 | ปรับ `os_probe.py` ให้รับ `--tier fast/mid/slow`, ใช้ token ร่วมต่อรอบ, นับ `requests`/`bytes`/`ms` ต่อ tier | ปรับ | script |
| 2 | ส่งสถานะเป็น code: ทุก check ส่ง `http_status` ตรง ๆ (200, 404, 503 …) ไม่แปลงเป็นหมวด (ดูหัวข้อ Status เป็น code) | ปรับ | script + template |
| 3 | เปลี่ยนการนับ resource เป็น filter ฝั่ง server (`with_count`, `status=error`, `all_tenants`) แทนการ list เต็ม | ปรับ | script |
| 4 | แยกสาเหตุที่ trigger จาก `http_status`: 401/403/404 → monitoring, 5xx หรือ 0 → availability | ปรับ | template |
| 5 | เปลี่ยนหน้าต่าง trigger เป็น `last(#N)` และผูก dependency กับ `collect_ok` ของ tier ตัวเอง | ปรับ | template |
| 6 | collector ใหม่: Cinder pools, Placement RP count, Heat/Magnum/Ironic service state, Octavia providers/amphora/cert, Glance stores, Keystone catalog count, L2 รายต่อ node | เพิ่ม | script |
| 7 | LLD `{binary}@{host}` (Nova/Cinder/Heat/Magnum), LLD hypervisor, LLD pools พร้อม trigger ตามหัวข้อ 5.3–5.6 ของเอกสารแนวคิด | เพิ่ม | template |
| 8 | tag schema `service/layer/impact/component/node/az` แทน `scope` (normalize `cinderv3`→`cinder`, `compute`→`nova`) | เปลี่ยน | template |
| 9 | template `OS-Deps`: Galera, RabbitMQ, HAProxy, memcached พร้อม tag | เพิ่ม | template |
| 10 | template `OS-Host`: chrony, disk, agent unavailable, proxy ไม่ส่งข้อมูล | เพิ่ม | template |
| 11 | role `monitoring` และ application credential ใหม่ + policy override | เพิ่ม | Keystone / `/etc/kolla/config/*/policy.yaml` |
| 12 | systemd unit/timer ต่อ AZ × tier (deploy ด้วย Ansible) | เพิ่ม | proxy host |
| 13 | script สร้าง Services/SLA ผ่าน Zabbix API (รันซ้ำได้) | เพิ่ม | Python |
| 14 | Dashboard 4 ชุด + Scheduled report (PDF) | เพิ่ม | Zabbix |
| 15 | ปรับ tag ของ template OpenSDN และ Ceph ให้ตรง schema | ปรับ | template |
| 16 | ลบ External check, item Neutron agents, macro ที่ไม่ถูกใช้, description เก่าของ JS และค่า STG ที่ฝังไว้ | ลบ | template |
| 17 | L1 คือ Docker container (state, health, RestartCount) | ตัดออกก่อน | — |

## สถาปัตยกรรมสุดท้าย

probe รันที่ proxy host ของแต่ละ AZ ผ่าน systemd timer 3 ชุด (fast, mid, slow) ยิง API ด้วย token เดียวต่อรอบ แล้ว push JSON เข้า Zabbix ผ่าน trapper ส่วน L4 และ host ใช้ template สำเร็จรูปผ่าน agent/HTTP ของ proxy เดิม

&#91;embedded content: สถาปัตยกรรมสุดท้าย · 3 โซน (OpenStack, proxy host, Zabbix)\]

เลือกแบบ A (trapper บน proxy host) แทนแบบ B (cache file + UserParameter บน controller) เพราะวัดจากนอก node ซึ่งตรงกับมุมมองลูกค้าและ SLA และ probe ไม่พึ่ง controller ที่กำลังถูกตรวจ ถ้า script หยุดทำงานจะไม่มีข้อมูลเข้า ทำให้ `nodata()` ยิงเองตามธรรมชาติ ต่างจากแบบ B ที่ agent ยังอ่านไฟล์เก่ามาส่งเหมือนปกติ

| Template | ชั้น | วิธีเก็บ |
| --- | --- | --- |
| `OS-API-Probe` | L2, L3 | trapper จาก proxy host ต่อ AZ แยก 3 master (fast/mid/slow) แล้ว dependent item และ LLD |
| `OS-Deps` | L4 | clone template สำเร็จรูป (MySQL by agent2, RabbitMQ HTTP, HAProxy HTTP, Memcached) แล้วเติม tag |
| `OS-Host` | host | Linux agent: chrony, disk, agent unavailable |
| OpenSDN, Ceph | L3/L4 | ใช้ของที่ทำไว้ ปรับ tag ให้ตรง schema |
| Services/SLA | — | script Python ผ่าน Zabbix API |

## Status เป็น code

ทุก check ส่ง `http_status` เป็นเลข HTTP จริงตรง ๆ (200, 404, 503 …) ไม่แปลงเป็นหมวดหรือรหัสอื่น เพื่อให้ดูสาเหตุล่มได้จากเลขเดียว การตัดสินว่าอะไรนับ SLA ทำที่ trigger ตามช่วงของ code ไม่ได้ทำใน script

### ค่าที่ script ส่ง

- `http_status`: เลข HTTP จริงจาก service และ `0` = ไม่มี response (timeout, connection refused, DNS, TLS ผิด)
- `error`: ข้อความเดิม (เช่น policy reason) เก็บไว้เป็นรายละเอียดประกอบ
- `ms`: เวลาตอบ
- `skipped`: เป็น 1 เมื่อไม่ได้ยิง (ชน deadline, ต้องใช้ admin, ยังไม่ตั้งค่า) และไม่ส่ง `http_status` เพื่อกัน false DOWN เหมือนที่ของเดิมทำ
- ฟิลด์ `ok` 0/1 เดิมเลิกใช้ trigger ใหม่อ่าน `http_status` โดยตรง

### การอ่านค่า `http_status`

| http\_status | ความหมายที่พบบ่อย | impact |
| --- | --- | --- |
| 200–399 | ปกติ (บาง service ตอบ 300 ที่ root ถือว่าปกติ) | — |
| 401, 403 | credential / role / policy ของ probe ไม่ใช่ service ล่ม | monitoring |
| 404 | path ที่ probe ใช้ไม่ตรงกับ service เวอร์ชันนี้ | monitoring |
| 400–499 อื่น ๆ (400, 409, 415, 429) | probe ยิงผิดรูปแบบ หรือถูก rate limit | monitoring |
| 500–599 | service หรือ backend พัง (DB, RabbitMQ, worker); 502/503/504 มักมาจาก HAProxy หรือ WSGI | availability |
| 0 | ไม่มี response: VIP, HAProxy, keepalived, เครือข่าย, ใบรับรอง | availability (ต้องกันกรณีฝั่ง probe เอง) |

ถ้า service ใดตอบ code นอกช่วงปกตินี้เป็นประจำ ให้กำหนด code ที่ยอมรับต่อ service ผ่าน macro context เช่น `{$OS_API.OK:"identity"}`

### จุดที่ต้องส่ง code

- `api_checks[]` (L2/L3 ของแต่ละ service ทั้ง VIP และราย node)
- แต่ละแหล่งของ `service_agents` (Nova/Cinder/Heat/Magnum `os-services`) แทน `agent_errors` ที่เป็นข้อความรวมก้อนเดียว
- `hypervisors` (`hypervisors_http_status` แทน `hypervisors_error`) และ `resources[]` (แทน `resource_errors`)
- การขอ token (`auth_http_status`): 401/403 = credential หมดอายุหรือถูกถอด role, 5xx หรือ 0 = Keystone ล่ม
- L2 ราย node ให้ script ยิงเองแทน HTTP agent item เดิม (เดิมคืนแค่ 0/1) เพื่อให้ได้รูปแบบข้อมูลเดียวกันทั้งระบบ

ตัวอย่างสัญญา JSON (บางส่วน):

```json
{
  "tier": "fast",
  "auth": {"http_status": 201, "ms": 310},
  "api_checks": [
    {"service_type": "block-storage", "target": "vip", "http_status": 503,
     "ms": 1204, "error": "HTTP 503: Service Unavailable"},
    {"service_type": "compute", "target": "10.0.0.11", "http_status": 0,
     "ms": 5001, "error": "cannot get URL: timed out"},
    {"service_type": "network", "target": "vip", "skipped": 1}
  ],
  "sources": [
    {"name": "cinder-os-services", "http_status": 403}
  ],
  "summary": {"collect_ok": 1, "total_ms": 4120, "requests": 38, "bytes": 91234}
}
```

### การนำไปใช้ใน Zabbix

- item prototype `os.api.http_status[...]` เก็บ code จริง พร้อม valuemap ชื่อ code (200 OK, 403 Forbidden, 503 Service Unavailable …)
- trigger `availability` ยิงเมื่อ `http_status` เป็น 0 หรือ ≥ 500 ติดกัน 2 รอบขึ้นไป; trigger `monitoring` ยิงเมื่ออยู่ในช่วง 400–499 ติดกัน 2 รอบ
- ชื่อและ description ของ trigger แสดง `{ITEM.LASTVALUE}` ให้เห็น "503" ใน problem ทันที (ต้องทดสอบว่าใช้ใน tag ของ event ได้หรือไม่ ยังไม่ได้ยืนยันบน 6.0)
- Dashboard Per-service: กราฟ/ตาราง `http_status` ต่อ service เพื่อดูว่าเริ่มได้ 503 บ่อยขึ้นก่อนล่มจริง

## ระดับ เบา / กลาง / หนัก

แบ่งระดับตาม "ต้นทุนโตตามอะไร" ไม่ใช่ความช้าของ call และแต่ละระดับเท่ากับ 1 tier ของ script (ไฟล์เดียว รับ `--tier`) และ 1 master item (trapper) ต่อ AZ

| Tier | ต้นทุนโตตาม | ความถี่ | Master item | อยู่ใน tier |
| --- | --- | --- | --- | --- |
| เบา (fast) | คงที่ ไม่ขึ้นกับขนาด cloud | 1–2 นาที | `os.probe.fast` | L2 รายต่อ node + VIP (`GET /`), ออก token, `os-services` (Nova/Cinder/Heat/Magnum/Ironic conductors), probe เชิงฟังก์ชัน (`networks?limit=1`, `images?limit=1`, `/v2/info/stores`, Trove datastores, Octavia providers), Horizon login |
| กลาง (mid) | ขนาดโครงสร้าง (จำนวน node/backend) หรือ count แบบ filter | 5 นาที | `os.probe.mid` | `os-hypervisors/detail` (capacity), Placement RP count, Cinder `get_pools`, นับ resource ผิดปกติแบบ filter (volume/instance error, LB ERROR), RabbitMQ consumers เฉพาะ queue ที่กำหนด, OpenSDN backend |
| หนัก (slow) | ข้อมูลลูกค้า หรือดาวน์โหลดไฟล์ | 15–30 นาที | `os.probe.slow` | Glance canary download, image `queued`/`saving` ค้าง, Heat stack FAILED/ค้าง, Trove/Magnum ค้าง, Octavia amphora/spare/cert expiry |

**สิ่งที่ห้ามทำ** (โตตามข้อมูลลูกค้าหรือกดทับตัวกลาง): list `servers`/`volumes`/`images` เต็มแบบ all\_tenants ไม่ filter, ไล่ `placement usages` ต่อ RP, ดึง RabbitMQ `/api/queues` ทั้งหมด, ใช้ `docker.container_stats`

### วิธีคุมโหลด

1. **ถามนับ ไม่ list**: filter ฝั่ง server และอ่านแค่ผลเล็ก เช่น Cinder `?status=error&all_tenants=1&with_count=true&limit=1` (microversion ≥ 3.45), Nova `?status=ERROR`, Octavia `?provisioning_status=ERROR`; สถานะชั่วคราวค้างนานให้ filter สถานะก่อน แล้วค่อยเช็คอายุจากผลที่เหลือ
2. **token ร่วมต่อรอบ** ของแต่ละ tier (\~1–2 token/นาที/AZ) ไม่แคช token ลงไฟล์ในเฟสแรก
3. **จำกัด concurrency** `workers` 4–8 และใช้ systemd `Type=oneshot` จึงไม่เริ่มรอบใหม่ถ้ารอบเก่ายังไม่จบ ใส่ `TimeoutStartSec` = deadline + margin และเหลื่อมเวลาเริ่มระหว่าง AZ
4. **Glance canary** ใช้ Range request (`bytes=0-0`) ถ้า backend รองรับ หรือใช้ image เล็กมาก
5. **ลดการเขียน DB**: `DISCARD_UNCHANGED_HEARTBEAT` กับค่าที่ไม่เปลี่ยน และ history สั้นกับ item diagnostic
6. **วัดโหลดในตัว**: JSON แต่ละ tier ส่ง `requests`, `bytes`, `total_ms` เป็น item และตั้ง trigger เตือนก่อนชน deadline

การจัดระดับมาจากพฤติกรรม API ตามที่เข้าใจ ยังไม่ได้วัดบน cloud จริง รอบแรกจะใช้ตัวนับข้างบนปรับ tier ให้เหมาะสม

## Coverage matrix

แต่ละ service ถูกเช็คที่ชั้น L2 (ไม่ใช้ token ทั้งรายต่อ node และผ่าน VIP), L3 (ใช้ token ดู state และ resource) และ L4 (dependency) ช่องว่างใน L3 คือ API ไม่มีของให้เช็คแบบ read-only ไม่ใช่ข้อบกพร่อง

| Service | L2 (fast, ราย node + VIP) | L3 fast | L3 mid | L3 slow |
| --- | --- | --- | --- | --- |
| Keystone | `GET /v3` | เวลาออก token, จำนวน endpoint ใน catalog | — | — |
| Nova | `GET /`, novnc `/vnc_auto.html` | `os-services` (compute/scheduler/conductor: state, status, forced\_down) | `os-hypervisors/detail` (capacity), instance ERROR/BUILD ค้าง | — |
| Placement | `GET /` | — | จำนวน `resource_providers` เทียบกับ hypervisor | — |
| Neutron + OpenSDN | `GET /`; OpenSDN config-api 8082, analytics 8081, control 8083, vRouter 8085 | `networks?limit=1` | backend (Cassandra, ZooKeeper, Kafka, Redis, RabbitMQ ของ config) | — |
| Glance | `GET /versions` | `images?limit=1`, `/v2/info/stores` | — | canary download, image `queued`/`saving` ค้าง |
| Cinder | `GET /` | `os-services` (`host@backend`) | `get_pools`, volume/backup/snapshot error | volume สถานะชั่วคราวค้าง |
| Heat | `GET /` | `services` (heat-engine) | — | stack FAILED/IN\_PROGRESS ค้าง |
| Octavia | `GET /`, UDP 5555 | `lbaas/providers` | LB `provisioning_status`/`operating_status` ERROR | amphora, spare pool, cert expiry |
| Barbican | `GET /` | — (policy ปัจจุบันไม่เปิด read-only) | — | — |
| Trove | `GET /` | `datastores` | instance ERROR | instance BUILD ค้าง |
| Magnum | `GET /` | `mservices` | cluster FAILED | — |
| Ironic (ถ้าเปิด) | `GET /v1` | `conductors` | node error/maintenance | — |
| Horizon | `GET /auth/login/` ต้องได้ 200 + เวลาตอบ | — | — | — |

### L4 ของทุก service (template `OS-Deps`)

| Component | เช็คอะไร | เครื่องมือ |
| --- | --- | --- |
| MariaDB Galera | `wsrep_ready`, `wsrep_cluster_status`=Primary, `wsrep_cluster_size`=3, `wsrep_local_state_comment`=Synced, flow control, recv queue, connections เทียบ `max_connections` | template MySQL by Zabbix agent 2 + user monitor |
| RabbitMQ | `/api/nodes` (running, `mem_alarm`, `disk_free_alarm`, fd/socket, partitions), `/api/overview`, queue `consumers=0` ของ service | template RabbitMQ cluster by HTTP (15672) |
| HAProxy | backend/server DOWN จาก `/stats;csv` (Kolla ใช้พอร์ต 1984), session usage | template HAProxy by HTTP |
| keepalived | VIP อยู่บน controller เพียง 1 ตัว | calculated item ทั้ง host group |
| memcached | up, hit ratio, evictions | template Memcached by Zabbix agent 2 |

### Host (template `OS-Host`)

ใช้ agent บน controller/compute เฉพาะ chrony offset/sync, disk (`/var/lib/docker`, `/var/lib/mysql`, `/var/lib/rabbitmq`), และ agent unavailable ไม่ใช้ Docker plugin เพราะตัด L1 ออกแล้ว

Ceph แยกอยู่ใน template ของ Ceph ไม่รวมในชุดนี้

## Tag schema และ trigger หลัก

ทุก trigger ต้องมี tag ครบตั้งแต่แรก เพราะ SLA อ่านจาก tag ไม่ใช่จาก item เฉพาะ `impact=availability` เท่านั้นที่ผูกเข้า Services

| Tag | ค่า | หมายเหตุ |
| --- | --- | --- |
| `service` | keystone, nova, placement, neutron, opensdn, glance, cinder, heat, octavia, barbican, trove, magnum, ironic, horizon, mariadb, rabbitmq, haproxy, keepalived, memcached, chrony, host, monitoring | normalize จาก catalog type (`volumev3`→cinder, `compute`→nova) ใน script |
| `layer` | L2, L3, L4 | L1 ไม่ใช้ในรอบแรก |
| `impact` | availability, stability, capacity, resource, monitoring | เฉพาะ availability เข้า SLA |
| `component` | binary / queue / backend จาก LLD macro | ใช้แยก node ในต้นไม้ Services |
| `node` | `{HOST.NAME}` หรือ host จาก LLD | ใส่เฉพาะ trigger ระดับ node ไม่ใส่ใน trigger VIP/cluster |
| `az` | BKK, NON … | host tag ต่อ AZ |

### ความหมายของ `impact`

- **availability**: ฟังก์ชันที่ลูกค้าใช้ล่ม หรือเสีย quorum
- **stability**: เสีย redundancy เสื่อมสภาพ หรือ flapping แต่ลูกค้ายังใช้ได้
- **capacity**: ใกล้เต็ม
- **resource**: resource ของลูกค้าผิดปกติ (volume, LB, stack) ไม่ใช่ตัวแพลตฟอร์ม
- **monitoring**: ตัว monitor เองเสีย (probe, proxy, credential) ไม่นับเป็น downtime

### trigger หลัก (ความรุนแรงใช้ชื่อ Zabbix 6.0; ตัวเลขเป็น default ผ่าน macro)

| กลุ่ม | trigger | Sev | impact |
| --- | --- | --- | --- |
| API ทุก service | endpoint ที่ node ได้ `http_status` = 0 หรือ ≥ 500 ติดหลายรอบ | High | availability |
| API ทุก service | endpoint ผ่าน VIP ได้ `http_status` = 0 หรือ ≥ 500 | Disaster | availability |
| API ทุก service | `http_status` 400–499 ติดหลายรอบ (401/403 credential/policy, 404 path ผิด, อื่น ๆ probe ยิงผิด) | Average | monitoring |
| API ทุก service | response time > 2 วินาที นาน 5 นาที | Warning | stability |
| Keystone | ขอ token ไม่ได้ (3/4) | Disaster | availability |
| Keystone | token > 3 วินาที; endpoint ใน catalog ลดลง | Warning / High | stability / availability |
| Nova | scheduler/conductor ต่อ host down | High | availability |
| Nova | nova-compute / hypervisor down, service disabled/forced\_down | High / Warning | stability |
| Nova | RP count ≠ hypervisor; vCPU/RAM > 85% / > 95% | Warning / High | stability / capacity |
| Neutron/OpenSDN | functional probe ล้มเหลว; backend (Cassandra, ZK, Kafka, Redis) down | High | availability |
| Glance | `images?limit=1` ล้มเหลว; store หาย | High | availability |
| Cinder | cinder-volume (`host@backend`) / scheduler / backup down; pool `backend_state` ≠ up | High | availability |
| Cinder | pool free < 20% / < 10% | Warning / High | capacity |
| Cinder, Heat, Octavia, Trove, Magnum | resource ERROR/FAILED/ค้าง | Warning | resource |
| Heat / Magnum / Ironic | engine / conductor down | High | availability |
| Octavia | provider amphora หาย; UDP 5555 ไม่ listen | High | availability |
| Octavia | spare amphora ต่ำ; cert < 30 / < 7 วัน | Warning / High | capacity / stability |
| Galera | status ≠ Primary, size ≤ 1 | Disaster | availability |
| Galera | `wsrep_ready`=OFF; size = 2 | High | availability / stability |
| RabbitMQ | partition, node running ≤ ครึ่งหนึ่ง | Disaster | availability |
| RabbitMQ | memory/disk alarm | High | availability |
| HAProxy | backend ทุก server DOWN | Disaster | availability |
| keepalived | VIP ไม่อยู่บน node ใดเลย / อยู่ > 1 node | Disaster / High | availability |
| monitoring | agent unavailable, proxy ไม่ส่งข้อมูล, probe nodata/ชน deadline, credential < 14 วัน | High / Warning | monitoring |

พารามิเตอร์ครบทุก trigger (Barbican/Trove/Magnum/Ironic/Horizon, RabbitMQ queue consumers, chrony, host disk) อยู่ในตาราง 5.1–5.13 ของเอกสารแนวคิดที่อัพโหลดมา แต่ปรับให้อ่าน `http_status` แทน ok 0/1 และตัดแถว L1 ออก

## Services, SLA และ Dashboard

ใช่ Dashboard ต้องมีข้อมูลดิบก่อน แต่ยังไม่พอ เพราะ SLA ไม่ได้อ่านจาก item แต่อ่านจากสถานะ problem ผ่าน Services จึงต้องมี trigger + tag คั่นกลาง

1. ข้อมูลดิบ (item: `http_status`, state, capacity)
2. trigger + tag (`service`, `az`, `impact`)
3. Services (ต้นไม้ต่อ AZ แยกตาม product)
4. SLA (ต่อ product ผูกด้วย service tag)
5. Dashboard / SLA report / Scheduled report (PDF)

### หลักการ

วัด SLA จากมุมที่ลูกค้าเห็น (product) ไม่ใช่สถานะ container ข้อมูลระดับ queue/การเช็ครายตัวใช้อธิบายสาเหตุ เฉพาะ trigger `impact=availability` ผูกเข้า Service ส่วน capacity, resource, monitoring แจ้งเตือนอย่างเดียว ไม่งั้นตัวเลข SLA จะเพี้ยน

### โครงสร้าง Services (ทำซ้ำต่อ AZ)

- NIPA Cloud (root)
  - AZ-BKK
    - Compute (VM create/boot) ← Nova API + Placement + Keystone + Glance
    - Block Storage ← Cinder + Ceph
    - Network ← Neutron + OpenSDN
    - Load Balancer / DBaaS / Kubernetes ← Octavia / Trove / Magnum
    - Shared platform ← MariaDB, RabbitMQ, HAProxy/VIP
- service ย่อยมีหลาย parent ได้ (MariaDB เป็น child ของ Compute, Block Storage, Identity)
- Problem tags ของแต่ละ node: `service=...`, `az=...`, `impact=availability`
- กฎสถานะสำหรับ HA 3 controller: API ต่อ node ใช้ "Most critical if all children have problems" เพื่อให้ node เดียวล่มแล้ว parent ยัง OK (ไม่กิน SLA แต่ problem ยังแจ้ง); Galera/RabbitMQ ที่ต้องมี quorum เพิ่ม additional rule: ลูกอย่างน้อย 2 ใน 3 ตัวสถานะ ≥ Average → ตั้งเป็น High/Disaster
- propagation: "as is" เป็นหลัก component ที่ไม่ critical ใช้ "decrease by 1" หรือ "ignore"

### SLA และรายงาน

- SLA ต่อ product (Compute, Block Storage, Network ฯลฯ) ไม่ใช่ต่อ service ย่อย SLO ตรงกับเอกสารมาตรฐานการให้บริการ Public Cloud
- schedule 24x7 รอบรายงานรายเดือน (ทำ quarterly ได้ด้วย) มี effective date
- excluded downtime: ใส่ช่วง planned maintenance (เช่น kolla upgrade)
- ผูก SLA กับ Service ด้วย service tag เช่น `sla=compute-99.9`
- ดูที่ Reports → SLA report (SLI, uptime, downtime, error budget); ส่ง PDF ทางอีเมลด้วย Scheduled report ซึ่งต้องมี Zabbix web service; ทำรายงานเองใช้ API `sla.getsli`
- Services/SLA ไม่อยู่ใน YAML template จึงทำเป็นสคริปต์ Python เรียก `service.create` / `sla.create` แบบ idempotent ครบทุก AZ

### Dashboard (Zabbix 6.0) และข้อมูลดิบที่ต้องมีก่อน

| Dashboard | widget หลัก | ต้องมีก่อน |
| --- | --- | --- |
| Executive / SLA | SLA report ต่อ product, Problems กรอง `impact=availability` | trigger availability + tag ถูกต้อง, Services/SLA |
| Per-AZ control plane | item value (endpoint up, Galera size, token time), graph (API ms, RabbitMQ msgs, Galera flow control), Problems กรอง `az` | item L2/L4 ต่อ AZ |
| Capacity | Top hosts (vCPU/RAM ต่อ hypervisor), Cinder pool free%, Ceph, graph prototype | LLD hypervisor/pools |
| Per-service troubleshooting | หนึ่งหน้าต่อ service ไล่ L2→L4 แสดง `http_status` | item `http_status` ราย service |

6.0 ไม่มี global filter ใช้ tag filter ในแต่ละ widget แทน ไม่มี Gauge/Pie จึงใช้ item value, graph, top hosts ตัวเลขสรุปข้าม AZ (เช่น hypervisor down ทั้งระบบ) ให้ทำเป็น calculated item แยก ไม่คำนวณใน script

## Credential, policy และข้อจำกัด Zabbix 6.0

credential เปลี่ยนได้ จึงออกแบบใหม่เป็น role `monitoring` เฉพาะ ที่เปิดเฉพาะ GET ที่ใช้ แทนการใช้ admin เต็ม (least-privilege)

### Credential และ policy

- application credential รับ role ตอนสร้าง ถ้าเพิ่ม role หลังต้องสร้าง credential ใหม่
- policy override ต้องวางที่ `/etc/kolla/config/<service>/policy.yaml` เท่านั้น การแก้ใน `/etc/kolla/<service>/` หายหลัง `kolla-ansible deploy/reconfigure`
- endpoint ที่ต้องเปิดให้ role monitoring: list แบบ `all_tenants` (servers, volumes, backups, snapshots, LB, stacks, clusters, instances), Nova `os-services` และ `os-hypervisors/detail`, Cinder `os-services` และ `get_pools`, Heat `services`, Magnum `mservices`, Ironic `conductors`, Octavia amphorae, Placement `resource_providers`, Neutron `networks`
- ข้อควรทำ: ทดสอบ policy แต่ละ service จริงแทนอ่านจากเอกสาร เพราะ role ในสภาพแวดล้อมนี้ไม่มาตรฐาน
- Barbican เช็ค L3 แบบ read-only ไม่ได้ตาม policy ปัจจุบัน ใช้แค่ L2 กับ consumer ของ queue
- Neutron: `contrail-neutron-plugin` ไม่มี agent extension (`/v2.0/agents` ได้ 404) ใช้ functional probe `networks?limit=1` แทน และต้องให้ role monitoring อ่านได้ (ของเดิมข้ามไปเพราะ 403)

### การเก็บ secret

แบบ A ใช้ ini บน proxy host (`/etc/zabbix/os_probe.ini`, chmod 600, owner = user ที่รัน timer) แทน macro Secret text ใน Zabbix และใช้ `ca_file` สำหรับ Kolla internal CA แทนการปิด `verify_tls` ข้อแลกเปลี่ยนคือมีไฟล์บน proxy host ซึ่งขัดกับความต้องการเดิมที่ไม่อยากมีไฟล์บน proxy แต่ยอมรับได้เพราะเลือกแบบ A แล้ว

### ข้อจำกัดสำหรับเขียน template บน Zabbix 6.0

- YAML: JavaScript preprocessing ใช้ block literal (`|`) เสมอ ไม่ใช้ double-quoted
- ใช้ `groups:` ไม่ใช้ `template_groups:`; root `groups` ต้องมี `uuid` แบบ UUIDv4 จริง; item trapper คือ `type: TRAP` ไม่ใช่ `TRAPPER`
- ห้าม `enabled_lifetime_type: DISABLE_NEVER`, widget `honeycomb`, dashboard 72 คอลัมน์
- `CHANGE_PER_SECOND` มีบั๊ก ZBX-24890 ให้เก็บค่าสะสมแล้วคำนวณฝั่ง Zabbix
- อย่าใช้ `CHECK_NOT_SUPPORTED` พร้อม `CUSTOM_VALUE` บน item หลัก จะซ่อน error จริง; แต่ผล `http_status` แก้ปัญหานี้ได้เมื่อ script เป็นผู้ยิงเอง
- หลีกเลี่ยง `DISCARD_UNCHANGED_HEARTBEAT` กับ item ที่ต้องแสดงสถานะทันทีหลัง re-import
- ใช้ template แบบแบน (flat) เพื่อหลีกเลี่ยง diamond linkage และรัน `validate_zbx_export.py` ทุกครั้งก่อน import

## ขอบเขตที่ตัดออก ช่องโหว่ที่ยอมรับ และความเสี่ยง

ตัด L1 (Docker container) ออกจากรอบแรกได้ เพราะ Kolla รันทุก service เป็น container แต่ `os-services` และ L2 รายต่อ node บอกได้ส่วนใหญ่อยู่แล้ว ผลคือช่องโหว่ด้านล่าง

### ช่องโหว่ที่ยอมรับ (เห็นทางอ้อมเท่านั้น)

| เหตุการณ์ | เห็นได้อย่างไร |
| --- | --- |
| `nova_libvirt` ล้ม | ผ่าน nova-compute/hypervisor state=down |
| `octavia_health_manager` ล้ม | ผ่านสถานะ amphora/LB และ UDP 5555 |
| container restart วนที่ node เดียว แต่ service ยังตอบผ่าน node อื่น | ไม่เห็นจนกว่าจะล่มจริง (L2 ราย node จะจับได้เมื่อ node นั้นตอบผิด) |
| อายุ fernet key | ไม่เห็น เพราะเป็นไฟล์บน node ต้องใช้ agent item แยก |
| keepalived split-brain | ต้องใช้ agent item ตรวจว่า VIP อยู่บน node ไหน (ยังไม่ได้ตัดสินใจว่าจะใส่ใน `OS-Host`) |
| Barbican | เห็นแค่ L2 เพราะ policy ไม่เปิด read-only |

### ความเสี่ยงและสิ่งที่ต้องระวัง

- **ไม่ได้ทดสอบบน cloud จริง**: ระดับเบา/กลาง/หนัก และเวลารันประมาณจากพฤติกรรม API รอบแรกต้องวัด `requests`/`bytes`/`total_ms` แล้วปรับ tier
- **ขนาดค่าต่อ trapper item**: Zabbix 6.0 จำกัด text ราว 64 KB ตามที่จำได้ (ต้องทดสอบ) การแยก 3 tier ช่วยลดขนาด แต่ tier mid ที่มี hypervisor มากอาจต้องแยกอีก
- **ไฟล์บน proxy host**: แบบ A มี script + ini บน proxy host ต่อ AZ ต้อง deploy/อัปเดตผ่าน Ansible
- **false alarm จากฝั่ง probe**: `http_status=0 (ไม่มี response`) อาจเกิดจากเครือข่ายของ proxy host เอง หรือ CA/ใบรับรองผิด จึงต้องใส่ trigger "ทุก service ใน AZ ได้ http\_status=0 พร้อมกัน" เป็น monitoring และให้เป็น dependency ของ trigger availability, ยิงเมื่อผิดติดหลายรอบเท่านั้น
- **maintenance**: ช่วง kolla upgrade ต้องใส่ใน excluded downtime ของ SLA มิฉะนั้นตัวเลขตก
- **code บางตัวอาจไม่ตรงแผนที่**: บาง service ตอบ 300 หรือ 401 ที่ root เป็นปกติ ต้องกำหนด "code ที่ยอมรับ" ต่อ service ใน script แทนการถือว่า 2xx คือปกติ

## ลำดับงาน ข้อมูลที่ต้องการ และข้อควรทดสอบ

ขั้นต่อไปคือปรับ `os_probe.py` ตามแผน (ข้อ 1) แต่ยังเขียนไม่ได้จนกว่าจะได้ข้อมูลด้านล่าง

### ลำดับงาน

1. ปรับ `os_probe.py`: 3 tier, token ร่วม, `http_status` `(ส่ง code ตรง ๆ)`, ตัวนับ `requests`/`bytes`/`ms`, collector ใหม่ตาม coverage matrix
2. template `OS-API-Probe`: LLD `{binary}@{host}`, hypervisor, pools, L2 ราย node พร้อม tag schema และ trigger ที่อ่าน http\_statu`s` (รัน `validate_zbx_export.py` ก่อน import)
3. template `OS-Deps` และ `OS-Host`
4. ปรับ tag ของ template OpenSDN และ Ceph ให้ตรง schema
5. script สร้าง Services/SLA ผ่าน Zabbix API และ dashboard 4 ชุด
6. ทดสอบจำลองเหตุการณ์ (ดูด้านล่าง)

### ข้อมูลที่ต้องการก่อนเริ่มข้อ 1

- [ ] รายชื่อ AZ และ IP ของ controller ต่อ AZ (catalog ให้แค่ VIP จึงต้องมี macro `{$OS_NODES}` สำหรับ L2 ราย node และต้องยิงด้วย IP ซึ่งกระทบกับ TLS)
- [ ] ผล `openstack catalog list` ของแต่ละ AZ (ใช้แทน `docker ps` เพื่อรู้ว่า service ไหนเปิดอยู่)
- [ ] ยืนยันว่ายังใช้ agent บน controller/compute สำหรับ chrony และ disk
- [ ] SLO ของแต่ละ product และขอบเขต "nova-compute ล่ม" นับใน SLA Compute หรือแยกเป็นระดับ VM
- [ ] รายการ policy ที่ role `monitoring` ต้องใช้ (ผมจะทำรายการให้คุณไปวางที่ `/etc/kolla/config/<service>/policy.yaml`)

### ข้อควรทดสอบก่อนใช้จริง

1. Services/SLA อยู่นอก YAML ต้องสร้างผ่าน UI หรือ API (`service.create`, `sla.create`) แบบรันซ้ำได้ครบทุก AZ
2. trigger ต้องมี tag ก่อน Services จะมีสถานะ; ตรวจว่า problem รับ host tag (`az`) กับ trigger tag ครบ และมาโครเช่น `{HOST.NAME}` แปลงเป็นค่าจริง
3. สถานะ Service ที่ไม่ใช่ OK ถูกนับเป็น downtime ใน SLA report จริง
4. จำลองเหตุ: หยุด `cinder_volume` ที่ node เดียว (parent ต้องยัง OK) แล้วหยุดทุก node (ต้องเป็น High และนับ downtime)
5. จำลองแต่ละช่วง http\_statu`s`: เปลี่ยน secret ให้ผิด (ได้ 401/403 และไม่นับ SLA), ปิด HAProxy backend (ได้ 503 หรือ 0 และนับ SLA)
6. ทดสอบขนาด JSON ต่อค่าใน trapper และ `total_ms` ของแต่ละ tier บน cloud จริง
