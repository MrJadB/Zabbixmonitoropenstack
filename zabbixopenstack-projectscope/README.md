# OpenStack monitoring แบบ project scope (Zabbix 6.0)

ชุดนี้ monitor OpenStack จากมุมของ **project เดียว** โดยใช้ credential ที่มีแค่ role `member` หรือ `reader` ใน project นั้น
ไม่ต้องใช้ admin, ไม่ต้องใช้ role `monitoring` และไม่ต้องแก้ policy
ชุดนี้รันคู่กับชุด cloud scope (`OS-API-Probe`) ได้บน Zabbix proxy เครื่องเดียวกัน โดยไม่ใช้อะไรร่วมกัน

## ไฟล์ในชุดนี้

| ไฟล์ | หน้าที่ |
|---|---|
| `os_probe.py` | script ตัวเดียวกับ `../os_probe.py` (ต้องเหมือนกันเสมอ) โหมดตั้งด้วย `scope = project` ใน ini |
| `tools/build_api_probe_project.py` | ใช้สร้าง `zbx-template-os-api-probe-project.yaml` |
| `zbx-template-os-api-probe-project.yaml` | template `OS-API-Probe-Project` ไว้ import เข้า Zabbix |
| `os_zabbix_setup_project.py` | สร้าง Services, SLA และ dashboard `OpenStack · ProjectScope` |
| `deploy/os_probe_project.ini.example` | ini ตัวอย่าง (หนึ่ง section ต่อ project) |
| `deploy/os-probe-project@.service`, `os-probe-project-{fast,mid,slow}.timer` | systemd unit และ timer |
| `deploy/os_zabbix_setup_project.example.json` | config ของ `os_zabbix_setup_project.py` |

## สิ่งที่เช็ค

ทุก service ที่อยู่ใน catalog มี functional probe อย่างน้อย 1 ตัว (fast ทุก 2 นาที) ใช้เป็นเช็ค availability ของ service นั้นจากมุมของลูกค้า

> **quota: ปิดแล้ว** ชุด project ไม่เช็ค quota (ไม่มี API call, item, trigger หรือกราฟ quota) คอลัมน์ quota ด้านล่างเก็บไว้อ้างอิงเท่านั้น ถ้าจะเปิดกลับ ให้ตั้ง `project_quota = 1` ใน ini และเอาส่วน quota กลับเข้า builder/dashboard
>
> **นับ resource error / ค้าง: ปิดแล้ว** (`project_resources = 0`) ชุด cloud นับของทุก project อยู่แล้ว คอลัมน์ "mid / slow" ด้านล่างเก็บไว้อ้างอิงเท่านั้น ชุด project เหลือแค่ functional probe, AZ, Glance canary และวันหมดอายุ credential

| Service | fast: functional probe (`limit=1`) | mid / slow: error, ค้าง | quota (ปิด) |
|---|---|---|---|
| Keystone | token, catalog | วันหมดอายุของ credential | – |
| Nova | servers, flavors, keypairs, availability zones (**AZ ไหน available = false → High**) | ERROR, BUILD/deleting/migrating/resize ค้าง, จำนวน SHUTOFF/PAUSED/SUSPENDED (กราฟ ไม่มี trigger) | instances, cores, RAM, server groups |
| Cinder | volumes, volume types, snapshots, backups, availability zones (**AZ ไหน available = false → High**) | error, error_deleting/extending, creating/deleting/attaching ค้าง | volumes, GB, snapshots, backups |
| Neutron | networks, subnets, ports, routers, floating IPs, security groups | routers / floating IPs / ports ที่ ERROR | ทุก resource จาก `/quotas/{project}/details` |
| Glance | images | queued/saving/importing/pending_delete ค้าง, canary | – |
| Heat | stacks | FAILED, *_IN_PROGRESS ค้าง | – |
| Octavia | load balancers, providers | LB provisioning/operating ERROR, **DEGRADED, OFFLINE**, **pool ERROR/down, listener ERROR**, PENDING_* ค้าง | load balancers |
| Magnum | clusters, cluster templates | FAILED, *_IN_PROGRESS ค้าง | – |
| Trove | datastores, instances | ERROR, BUILD/resize ค้าง | – |
| Manila | shares | error, creating/deleting/extending ค้าง | ทุก resource จาก `/quota-sets/{project}/detail` |
| Designate | zones | zones ERROR, PENDING ค้าง | – |
| Barbican | secrets, containers | – | – |
| Swift | account (list container) | – | – |
| Ironic | nodes | error, deploy/clean failed, maintenance, deploying/cleaning ค้าง | – |
| Aodh / Gnocchi | alarms / resources | – | – |
| Placement | – (ไม่มี API ที่ member เรียกได้) | – | – |

เช็คของ service ที่ไม่มีใน catalog จะไม่ถูกยิง ถ้า policy ของ cloud ไม่ให้ role member หรือ reader เรียก check ไหน (ได้ 403 ตลอด) ให้ปิดด้วย `skip_checks`

**สิ่งที่ตัดออก** เพราะเป็น API ของ admin: service records (os-services), hypervisor, placement, Cinder pool, IP availability, OpenSDN, Octavia amphora และ Glance stores

**สิ่งที่ปิดไว้เป็นค่าเริ่มต้น** เพราะ host ของ cloud เช็คอยู่แล้ว ถ้าเปิดจะแจ้งเตือนซ้ำ:
- L2 root, Horizon และ noVNC (เปิดด้วย `project_l2 = 1`)
- TLS (เปิดด้วย `project_tls = 1`)
- S3 (ไม่ต้องใส่ `s3_*` ใน ini)

## การแยกจากชุด cloud ให้ขาดกัน

| ส่วน | ชุด cloud | ชุด project |
|---|---|---|
| Template | `OS-API-Probe` | `OS-API-Probe-Project` (UUID namespace ของตัวเอง ไม่มี UUID ซ้ำกันเลย) |
| Tag ของ item และ trigger | `service`, `impact`, ... | ชุดเดียวกัน **+ `scope=project` ทุกตัว** |
| Host tag | `az=<AZ>` | `scope=project`, `project=<ชื่อ>` และ **ห้ามมี `az`** |
| Services | `<root>` → `AZ-<AZ>` → ... จับด้วย `az=<AZ>` | `OpenStack Projects` → `Project <ชื่อ>` → ... จับด้วย `scope=project` + `project=<ชื่อ>` |
| SLA | `SLA - <AZ> - ...` ใช้ service tag `az=<AZ>` | `SLA - Project <ชื่อ> - ...` ใช้ service tag `scope=project` + `project=<ชื่อ>` |
| Dashboard | widget ทุกตัวกรอง `scope` ≠ `project` | `OpenStack · ProjectScope` (ทุกหน้ากรอง `scope=project`): หน้า **Overview** (service honeycomb ของ project admin, กราฟ HTTP status และ response time ของทุก service, ตัวนับ 0/5xx และ 4xx, Keystone token time และปัญหา outages/problems), หน้า **Available SLA** และ **Response SLA** (รวม SLA card ของทุก service) และ **หน้าละ service** (กราฟ HTTP status แบ่งครึ่งกับ API response time ด้านบน และ outages/problems ด้านล่าง) |
| ini / systemd | `os_probe.ini`, `os-probe@*` | `os_probe_project.ini`, `os-probe-project@*` |

Item key ของทั้งสองชุดเหมือนกัน (`os.probe.fast` ฯลฯ) ซึ่งไม่เป็นปัญหาเพราะอยู่คนละ host
**ห้ามผูกทั้งสอง template ไว้กับ host เดียวกัน**

## ขั้นตอน deploy

### 1. ฝั่ง OpenStack (ทำต่อ project)

```bash
# user ที่มี role member หรือ reader ใน project นี้
openstack --os-project-name demo application credential create zbx-probe-demo
```

ถ้าจะใช้ Glance canary ให้มี image เล็ก ๆ ที่ project นี้มองเห็น แล้วใส่ id ใน `canary_id`

### 2. บน Zabbix proxy

```bash
# script ตัวเดียวกับของ cloud (แทนตัวเดิมได้เลย เพราะโหมด cloud ทำงานเหมือนเดิมทุกอย่าง)
install -m 755 os_probe.py /usr/local/bin/os_probe.py
install -m 600 -o zabbix -g zabbix deploy/os_probe_project.ini.example /etc/zabbix/os_probe_project.ini
# แก้ไฟล์ ini: zabbix_host, auth_url, cred_id, cred_secret และ ref_url

# ทดสอบก่อนส่ง (แค่พิมพ์ JSON ออกมา)
sudo -u zabbix /usr/local/bin/os_probe.py --tier fast -c /etc/zabbix/os_probe_project.ini --pretty
sudo -u zabbix /usr/local/bin/os_probe.py --tier mid  -c /etc/zabbix/os_probe_project.ini --pretty

cp deploy/os-probe-project@.service deploy/os-probe-project-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now os-probe-project-fast.timer os-probe-project-mid.timer os-probe-project-slow.timer
```

### 3. บน Zabbix

1. Import `zbx-template-os-api-probe-project.yaml`
2. สร้าง **host ใหม่** ต่อ project
   - ชื่อ host ตรงกับ `zabbix_host` ใน ini
   - monitored by proxy ของ AZ นั้น
   - ไม่ต้องมี interface
   - ผูก template `OS-API-Probe-Project` ตัวเดียว
   - host tag: `scope=project` และ `project=<ชื่อ>` โดย**ไม่ใส่ `az`**
3. สร้าง Services, SLA และ dashboard

   ```bash
   export ZBX_TOKEN=...
   python3 os_zabbix_setup_project.py -c deploy/os_zabbix_setup_project.example.json --dry-run
   python3 os_zabbix_setup_project.py -c /etc/zabbix/os_zabbix_setup_project.json
   ```

4. รัน `../os_zabbix_setup.py` ของ cloud อีกครั้ง เพื่อเพิ่มเงื่อนไข `scope` ≠ `project` ให้ widget ของ dashboard cloud

### ไม่ต้อง unlink ของเดิม

- host ของ cloud และ template `OS-API-Probe` ไม่ต้องแตะเลย ถ้ากด "Unlink and clear" จะลบ history และข้อมูล SLA ย้อนหลังทิ้ง
- host ของ project เป็น host ใหม่ จึงไม่มีอะไรต้อง unlink
- ต้อง unlink and clear เฉพาะ host ทดสอบที่เคยผูก template อื่นไว้

## ปรับค่าผ่าน macro (ระดับ host หรือ template)

| Macro | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `{$OS_LATENCY.WARN}` | 5000 | ms ของ API ที่ถือว่าช้า (context = service) |
| `{$OS_API.ACCEPT}` | 0 | รหัส 4xx ที่ถือว่าปกติของ service นั้น |

ถ้า policy ของ project ไม่ให้เรียก check ใด (ได้ 403 ตลอด) ให้ปิดใน ini ด้วย `skip_checks = <service>:<check>` เช่น `trove:datastores`

## สร้าง template ใหม่หลังแก้ builder

```bash
python3 tools/build_api_probe_project.py zbx-template-os-api-probe-project.yaml
```

UUID สร้างจากชื่อ จึงคงเดิมทุกครั้งที่ build และเมื่อ import ซ้ำ Zabbix จะ update ของเดิมแทนการสร้างใหม่
