# S3 (Ceph RGW) Monitoring: การแก้ไขและการทดสอบ

Oct 26, 2026 · ไฟล์ก่อนแก้อยู่ที่ `Backupcomplete1/`

## สิ่งที่เช็ค

| Tier | Check (`service=s3`) | ทำอะไร | impact | นับ SLA |
|---|---|---|---|---|
| fast 2m | `root` (L2) | `GET /` แบบ anonymous | availability | ✅ (VIP down = Disaster) |
| fast 2m | `static_web` (L2) | `GET s3_static_url` (ถ้าตั้งไว้) | availability | ✅ |
| fast 2m | `canary` (L3) | PUT → GET (เทียบเนื้อหา) → DELETE `zbx-canary/rw` | availability | ✅ |
| mid 5m | `list_buckets` | ListBuckets (SigV4) | monitoring | ❌ |
| mid 5m | `head_bucket` | HEAD `s3_bucket` | monitoring | ❌ |
| mid 5m | `repl_write` | PUT `zbx-canary/repl-marker` = epoch ลง bucket ของ zone ตัวเอง | monitoring | ❌ |
| mid 5m | `repl_read` | GET marker จาก `s3_peer_bucket` ผ่าน endpoint ของ zone ตัวเอง → lag | monitoring | ❌ |
| slow 30m | TLS | cert ของ `s3_url` / `s3_static_url` เข้าไปอยู่ใน "TLS endpoints: soonest certificate expiry" | stability | ❌ |

- ทุก S3 check **ไม่ต้องใช้ Keystone token** ถ้า Keystone ล่ม S3 ก็ยังถูกเช็คอยู่
- ทุกตัวใช้ `http_status` แบบเดียวกับ check อื่น: 0 = ไม่ตอบ, 5xx = ล่ม, 4xx = ปัญหาที่ฝั่ง probe (key, policy)

## จุดที่แก้

### `os_probe.py`
- docstring: เพิ่มคำอธิบายส่วน S3
- import: `binascii`, `hashlib`, `hmac`, `urllib.parse` (ยังเป็น standard library ทั้งหมด)
- `LABELS`: เพิ่ม `'s3': 'S3'`
- `DEFAULTS`: เพิ่ม `s3_url`, `s3_region`, `s3_access_key`, `s3_secret_key`, `s3_bucket`, `s3_peer_bucket`, `s3_static_url` และค่าคงที่ `S3_CANARY_KEY`, `S3_MARKER_KEY`
- ฟังก์ชันใหม่: `s3_err()` (อ่าน `<Code>` จาก XML error), `s3_sign()` (AWS SigV4 ผ่าน test vector ทางการของ AWS)
- `Probe.__init__`: อ่านค่า `s3_*`
- method ใหม่: `s3_call`, `s3_check`, `s3_steps`, `s3_root`, `s3_static_web`, `s3_canary`, `s3_list_buckets`, `s3_head_bucket`, `s3_repl_write`, `s3_repl_read`, `s3_jobs`
- `run_fast` / `run_mid`: ส่งงาน S3 เข้า pool ก่อนเช็ค token
- `run_slow`: `slow_tls` ทำงานแม้ไม่มี token และรวม URL ของ S3 ด้วย
- `summary`: fast เพิ่ม `s3_canary_mismatch`, mid เพิ่ม `s3_repl_lag_s` (-1 = ไม่ได้เก็บ)

### `tools/build_api_probe.py` → `zbx-template-os-api-probe.yaml`
- item ใหม่ `S3: canary content mismatch` (`os.fast.s3.canary_mismatch`) พร้อม trigger High (`impact=availability`, `target=vip`)
- item ใหม่ `S3: replication lag` (`os.mid.s3.repl_lag`) พร้อม trigger Warning เมื่อ `min(#2) > {$OS_S3.REPL.LAG.WARN}`
- macro ใหม่ `{$OS_S3.REPL.LAG.WARN}` = 900 วินาที
- เพิ่มส่วน S3 ใน template description
- S3 check ทุกตัวถูกค้นหาโดย LLD เดิม ("API checks" และ "Mid calls") ได้ item, trigger และ override เดิมโดยอัตโนมัติ
- แก้ builder 2 จุด (ไม่เปลี่ยนเนื้อหาของเดิม):
  - `KEEP_UUID` คง UUID ของ trigger prototype 2 ตัวให้ตรงกับไฟล์เดิม
  - `ignore_aliases` ปิด YAML anchor (`&id001`) เพราะ Zabbix import ไม่รองรับ
- ตรวจแล้ว: เมื่อเทียบกับไฟล์เดิม มีแค่ส่วนที่เพิ่ม (items 54→56, triggers 26→28, macros 30→31) และไม่มี UUID ซ้ำ

### `os_zabbix_setup.py`
- `LABELS`: เพิ่ม `'s3': 'S3'`
- `KINDS`: เพิ่ม `'s3': 'vip'`
- `DEFAULT_SERVICES`: เพิ่ม `s3`
- ผลที่ได้ต่อ AZ:
  - Services: `<AZ> · S3`, `· Availability`, `· VIP`, `· Response Time`
  - SLA: `SLA - <AZ> - S3 - Availability` และ `SLA - <AZ> - S3 - Response Time`
  - Dashboard: หน้า S3 ใน "Per service"

### `deploy/`
- `os_zabbix_setup.json` และ `os_zabbix_setup.example.json`: เพิ่ม `"s3"` ใน `services`
- `os_probe.ini.example`: เพิ่มกลุ่มค่า `s3_*` ใน `[bkk]`

## เตรียมฝั่ง Ceph (ครั้งเดียว)

ทำบน **metadata master zone** (`radosgw-admin zonegroup get | grep master_zone`):

```
radosgw-admin user create --uid=zbx-monitor --display-name="Zabbix monitor"
radosgw-admin quota set --quota-scope=user --uid=zbx-monitor --max-size=10M --max-objects=100
radosgw-admin quota enable --quota-scope=user --uid=zbx-monitor
```

สร้าง bucket ด้วย key ของ `zbx-monitor` (ใช้ s3cmd หรือ aws cli ก็ได้):

```
aws --endpoint-url https://s3-bkk.stg.thaiopenstack.org s3 mb s3://zbx-monitor-bkk
aws --endpoint-url https://s3-non.stg.thaiopenstack.org s3 mb s3://zbx-monitor-non
```

## ตั้งค่า ini

| | `[bkk]` | `[non]` |
|---|---|---|
| `s3_url` | `https://s3-bkk.stg.thaiopenstack.org` | `https://s3-non.stg.thaiopenstack.org` |
| `s3_region` | `NCP-TH` | `NCP-TH` |
| `s3_access_key` / `s3_secret_key` | key ของ zbx-monitor | key เดียวกัน (sync แล้ว) |
| `s3_bucket` | `zbx-monitor-bkk` | `zbx-monitor-non` |
| `s3_peer_bucket` | `zbx-monitor-non` | `zbx-monitor-bkk` |

**ข้อควรระวัง:** S3 check ทุกตัวใช้ id `s3:<check>@vip` จึงต้องมี 1 S3 endpoint ต่อ 1 section (1 Zabbix host) ถ้า 2 section ส่งไปที่ Zabbix host เดียวกัน ค่าจะทับกัน

## วิธีทดสอบ

1. **ไม่ต้องส่งเข้า Zabbix ก่อน**: ดูผล S3 ที่ได้
   ```
   python3 os_probe.py --tier fast -c /etc/zabbix/os_probe.ini -s bkk --pretty | grep -A12 '"s3:'
   python3 os_probe.py --tier mid  -c /etc/zabbix/os_probe.ini -s bkk --pretty | grep -E '"s3:|s3_repl_lag_s' -A10
   ```
   ผลที่ควรได้:

   | check | ค่าที่ควรได้ |
   |---|---|
   | `root` | 200 |
   | `canary` | 204 (สถานะของ DELETE) และ `s3_canary_mismatch: 0` |
   | `list_buckets` / `head_bucket` / `repl_write` | 200 |
   | `repl_read` | ครั้งแรกจะได้ 404 จนกว่า probe ของอีก zone จะเขียน marker หลังจากนั้นได้ 200 และ `s3_repl_lag_s` ควรน้อยกว่า 300–400 |

2. **ความหมายของ error ที่อาจเจอ**

   | Error | สาเหตุ |
   |---|---|
   | `403 InvalidAccessKeyId` | key ผิด หรือ user ยังไม่ sync มาที่ zone นี้ |
   | `403 SignatureDoesNotMatch` | secret ผิด หรือ `s3_region` ไม่ตรง |
   | `404 NoSuchBucket` | ยังไม่ได้สร้าง bucket |
   | `403 QuotaExceeded` | quota ไม่พอ |
   | `0` | proxy ออกไปยัง URL ภายนอกไม่ได้ (DNS, firewall, TLS) |

3. **ส่งเข้า Zabbix จริง**
   - import `zbx-template-os-api-probe.yaml` ใหม่ (Update existing)
   - รัน `os_probe.py --tier fast --send ...` แล้วดู Latest data ของ host กรองด้วย tag `service=s3`
   - LLD จะสร้าง item ภายในรอบถัดไป
4. **Services/SLA**
   ```
   python3 os_zabbix_setup.py -c deploy/os_zabbix_setup.json --dry-run --only services,sla
   python3 os_zabbix_setup.py -c deploy/os_zabbix_setup.json --only services,sla
   ```
5. **ทดสอบให้ trigger ทำงาน (ทำในช่วงที่ปลอดภัย)**

   | ทดสอบ | วิธี | ผลที่คาด |
   |---|---|---|
   | canary | ใส่ `s3_bucket` ผิดชั่วคราว | 404 → trigger "rejects the probe" ไม่นับ SLA |
   | replication lag | หยุด probe ฝั่ง peer นานกว่า 15 นาที | trigger replication lag Warning |
   | root down | ใส่ `s3_url` เป็น host ที่ไม่มีอยู่ | 0 → Disaster ภายใน 3 รอบ แล้วเปลี่ยนค่ากลับ |

## ข้อสังเกตจากการทดสอบ (Oct 26, 2026)
- `s3-bkk` และ `s3-non` ตอบ 200 ใน 0.13–0.15 วินาที (`Ceph Object Gateway (squid)`)
- **cert TLS ของ `s3-bkk` และ `s3-non` หมดอายุ 1 ธ.ค. 2026** (เหลือประมาณ 36 วัน)
- `static-s3-bkk` / `static-s3-non` ไม่มี DNS ที่ชื่อหลัก ต้องใช้รูปแบบ `<bucket>.static-s3-...`

---

# TLS certificate ราย endpoint (เพิ่มรอบ 2)

ไฟล์ก่อนแก้รอบนี้อยู่ที่ `Backupcomplete2/`

## ทำงานอย่างไร
- ทำงานใน tier slow (ทุก 30 นาที) และสร้าง **1 แถวต่อ https host:port**
- **แหล่งของ endpoint:**
  - endpoint ใน catalog ตาม `tls_interfaces` (ค่าเริ่มต้นคือ `public`) ดึงได้อัตโนมัติจาก catalog ที่มากับ token
  - URL ใน ini: `auth_url`, `horizon_url`, `novnc_url`, `s3_url`, `s3_static_url`
  - `tls_extra = name=host:port,...` สำหรับ endpoint นอก catalog
  - endpoint ที่เป็น http จะถูกข้ามไป
- ถ้าหลาย service ใช้ host:port เดียวกัน จะรวมเป็นแถวเดียว และ label แสดงทุก service เช่น `Swift, S3`
- **cert ที่ verify ไม่ผ่าน** (หมดอายุแล้ว หรือ CA ไม่รู้จัก) ยังอ่านจำนวนวันได้ ส่วน error จะแสดงเป็น `verify failed: ...`

## Trigger (ราย endpoint)
| เงื่อนไข | Priority |
|---|---|
| เหลือน้อยกว่า `{$OS_TLS.WARN}` = 30 วัน | Warning |
| เหลือน้อยกว่า `{$OS_TLS.AVERAGE}` = 7 วัน | Average |
| เหลือน้อยกว่า `{$OS_TLS.HIGH}` = 3 วัน (หรือหมดอายุแล้ว) | High |
| อ่าน cert ไม่ได้ 2 รอบติดกัน | Warning (`impact=monitoring`) |

- trigger ทั้ง 3 ระดับมี dependency ต่อกัน จึงแสดงเฉพาะระดับที่สูงที่สุด
- ปรับค่าราย endpoint ได้ด้วย context เช่น `{$OS_TLS.WARN:"s3-bkk.stg.thaiopenstack.org:443"}` = 45
- trigger ใช้ tag `service=<ชื่อจริง>`, `impact=stability`, `endpoint=<host:port>` และไม่มี `target=vip` จึง**ไม่นับเข้า SLA**

## จุดที่แก้
- **`os_probe.py`:**
  - ค่าใหม่ใน ini: `tls_interfaces`, `tls_extra`
  - `walk_catalog` เก็บ https endpoint ของ interface ที่เลือกไว้
  - method ใหม่ `tls_add`, `tls_one`, `tls_unverified` และเขียน `slow_tls` ใหม่ให้ทำงานแบบขนาน
  - JSON slow มี `tls_certs[]` และ summary เพิ่ม `tls_certs_total`, `tls_certs_failed`
  - ค่า `tls_cert_min_days` เดิมยังอยู่
- **template:**
  - LLD ใหม่ "TLS certificates" (`os.slow.discovery.tls`) มี item `os.tls.days[...]`, `os.tls.error[...]` และ trigger 4 ตัวตามตารางข้างบน
  - macro ใหม่ `{$OS_TLS.WARN/AVERAGE/HIGH}`
  - **เอา trigger แบบรวม 2 ตัวเดิมออก** ("OpenStack TLS endpoint certificate expires in ...") ส่วน item แบบรวมยังเก็บไว้ใช้กับ dashboard และดูย้อนหลัง
- **`os_probe.ini.example`:** เพิ่ม `tls_interfaces` และ `tls_extra`

## ⚠️ ตอน import template
ถ้า import โดยไม่ติ๊ก "Delete missing" ระบบจะ**ไม่ลบ** trigger TLS แบบรวม 2 ตัวเดิมออก และจะได้แจ้งเตือนซ้ำ ให้เลือกทำอย่างใดอย่างหนึ่ง:
- ติ๊ก **Delete missing** เฉพาะ Triggers ตอน import
- หรือลบเองที่ Template OS-API-Probe → Triggers → ค้นหาคำว่า "TLS endpoint"

## วิธีทดสอบ
```
sudo -u zabbix /usr/local/bin/os_probe.py --tier slow -c /etc/zabbix/os_probe.ini --pretty \
  | python3 -c "import json,sys; [print(r['id'], r['label'], r['days'], r['error']) for r in json.load(sys.stdin)['tls_certs']]"
```
- ควรเห็นทุก public endpoint ที่เป็น https พร้อมจำนวนวัน
- ถ้าอยากเห็นแต่ละ trigger ทำงาน ให้ตั้ง `{$OS_TLS.WARN:"<host:port>"}` = 400 บน host ชั่วคราว แล้วลบออกหลังทดสอบ
- **ข้อควรระวัง:** Python บน proxy ต้องใช้ OpenSSL 1.1.1 ขึ้นไป (รองรับ TLS 1.3) เพราะ `stg.thaiopenstack.org` รับเฉพาะ TLS 1.3 ตรวจได้ด้วย `python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"`

## ค่าที่วัดได้ตอนทดสอบ (Oct 26, 2026)
| Endpoint | หมดอายุ |
|---|---|
| `s3-bkk` / `s3-non` :443 | 1 ธ.ค. 2026 (ประมาณ 36 วัน, Warning) |
| `stg.thaiopenstack.org:5000` (Keystone public) | 21 ธ.ค. 2026 (ประมาณ 56 วัน) |

---

# S3 หลาย zone ใน section เดียว (เพิ่มรอบ 3)

ไฟล์ก่อนแก้รอบนี้อยู่ที่ `Backupcomplete3/` โค้ดชุดเดียวรองรับได้ 2 แบบ:

| | แบบ A: 1 endpoint ต่อ section | แบบ B: หลาย zone ใน section เดียว |
|---|---|---|
| ใช้เมื่อ | 1 Zabbix host ต่อ AZ | proxy เดียวและ Zabbix host เดียว ดูแลหลาย zone (เช่น ระบบ STG ที่มี BKK + NON) |
| ini | `s3_url`, `s3_bucket`, `s3_peer_bucket`, `s3_static_url` | `s3_zones = bkk,non` + `s3_<zone>_url`, `s3_<zone>_bucket`, `s3_<zone>_static_url` |
| record | `s3:<check>@vip` (`target=vip`) | `s3:<check>@bkk`, `s3:<check>@non` (`target=<zone>`) |
| replication | อ่าน `s3_peer_bucket` | ทุก zone อ่าน marker ของ zone อื่นอัตโนมัติ: `repl_from_non@bkk`, `repl_from_bkk@non` |
| `os_zabbix_setup.json` | ไม่ต้องใส่ `s3_zones` | ใส่ `"s3_zones": ["BKK", "NON"]` ใน AZ นั้น |
| Services | `S3 · VIP`, `S3 · Response Time` | `S3 · Availability` มี service ลูก `S3 · BKK` และ `S3 · NON`, `S3 · Response Time` มี service ลูกราย zone |
| SLA | `S3 - Availability` / `Response Time` | SLA ระดับ AZ เดิม (zone ใดล่มก็นับ) + `S3 BKK - ...` และ `S3 NON - ...` |

ถ้าตั้งทั้ง 2 แบบไว้ใน section เดียวกัน `s3_zones` จะมีผลก่อน

## จุดที่แก้
- **`os_probe.py`:**
  - ค่าใหม่ `s3_zones`
  - method ใหม่ `s3_zone_list()` และ S3 method ทุกตัวรับ zone เป็นพารามิเตอร์ (`s3_call`, `s3_check`, `s3_step`, `s3_anon`, `s3_canary`, `s3_repl_read`, `s3_jobs`)
  - JSON mid มี `s3_repl[]` (`id = <from>><to>`, `lag_s`)
  - `s3_repl_lag_s` = ค่าที่แย่ที่สุดในทุกทิศ และ `s3_canary_mismatch` = จำนวน zone ที่เนื้อหาไม่ตรง
- **template:**
  - override ใหม่ "S3 zone root check is Disaster" สำหรับ `service=s3` และ `check=root` ทุก target
  - LLD ใหม่ "S3 replication" (`os.mid.discovery.s3repl`) มี item ราย direction และ trigger Warning เมื่อ `min(#2) > {$OS_S3.REPL.LAG.WARN}`
  - item `S3: replication lag` แบบรวมยังเก็บไว้ แต่ไม่มี trigger แล้ว (ย้ายการแจ้งเตือนไปที่ LLD)
- **`os_zabbix_setup.py`:**
  - method ใหม่ `s3_zones()` และสร้าง SLA ราย zone
  - เมื่อเปลี่ยนไปใช้แบบ B จะลบ leaf `S3 · VIP` เดิมให้อัตโนมัติ
- **`deploy/`:**
  - `os_zabbix_setup*.json` เพิ่ม `"s3_zones": ["BKK", "NON"]`
  - `os_probe.ini.example` แสดงทั้งแบบ A และแบบ B

## ทดสอบที่ทำแล้ว
- ยิงจริงไปที่ `s3-bkk` และ `s3-non` ทั้ง 2 แบบ: root ได้ 200 ส่วน request ที่ใช้ key ปลอมได้ 403 `InvalidAccessKeyId` ถูกต้อง
- จำลอง S3 server ในเครื่อง:
  - canary ได้ 204 และเทียบเนื้อหาตรงกัน
  - วัด lag ได้ทั้ง 2 ทิศ (`non>bkk` = 44 วินาที ตามที่ตั้งไว้)
- ini แบบเดิมที่ไม่มีค่า S3 ทำงานเหมือนเดิม
- `os_zabbix_setup.py --dry-run` ผ่าน

---

# สรุปขั้นตอน Deploy (ฉบับล่าสุด)

1. **Ceph (ครั้งเดียว):** สร้าง user `zbx-monitor` บน metadata master zone, ตั้ง quota, สร้าง bucket `zbx-monitor-bkk` และ `zbx-monitor-non`
2. **Proxy:**
   - เช็ค `python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"` ต้องเป็น OpenSSL 1.1.1 ขึ้นไป
   - backup แล้ววาง script: `install -m 755 os_probe.py /usr/local/bin/os_probe.py`
3. **ini:**
   - ใส่ `tls_interfaces = public`, `s3_region = NCP-TH` และ key
   - ระบบ STG (proxy และ host เดียว): ใช้**แบบ B** (`s3_zones = bkk,non` + `s3_bkk_*`, `s3_non_*`)
   - ระบบที่มี host แยกราย AZ: ใช้**แบบ A** ใน section ของแต่ละ AZ
4. **ทดสอบ:** `sudo -u zabbix /usr/local/bin/os_probe.py --tier fast|mid|slow -c /etc/zabbix/os_probe.ini --pretty`
5. **Import template:** Create new + Update existing และติ๊ก **Delete missing เฉพาะ Triggers** (เพื่อลบ trigger TLS แบบรวมเดิม)
6. **Services/SLA:**
   - ระบบแบบ B: ใส่ `"s3_zones"` ใน AZ ของ `os_zabbix_setup.json`
   - ระบบแบบ A: ไม่ต้องใส่
   - รัน `--dry-run` ก่อน แล้วจึงรันจริง
7. **ตรวจ:** Latest data กรอง `service=s3` และ `component=tls_cert` ส่วน Services ดูที่ `S3 · BKK` / `S3 · NON`
