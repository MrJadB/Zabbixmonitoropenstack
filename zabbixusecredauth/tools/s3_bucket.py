#!/usr/bin/env python3
"""Create / check / list S3 buckets on Ceph RGW without aws cli (uses os_probe's SigV4).

    export S3_ACCESS_KEY=... S3_SECRET_KEY=... S3_REGION=NCP-TH
    python3 tools/s3_bucket.py https://s3-bkk.stg.thaiopenstack.org create zbx-monitor-bkk
    python3 tools/s3_bucket.py https://s3-bkk.stg.thaiopenstack.org head   zbx-monitor-bkk
    python3 tools/s3_bucket.py https://s3-bkk.stg.thaiopenstack.org list
"""
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from os_probe import s3_sign  # noqa: E402

ACTIONS = {'create': 'PUT', 'head': 'HEAD', 'list': 'GET'}


def main():
    if len(sys.argv) < 3 or sys.argv[2] not in ACTIONS or (sys.argv[2] != 'list' and len(sys.argv) < 4):
        raise SystemExit(__doc__)
    endpoint, action = sys.argv[1].rstrip('/'), sys.argv[2]
    bucket = sys.argv[3] if len(sys.argv) > 3 else ''
    ak, sk = os.environ.get('S3_ACCESS_KEY'), os.environ.get('S3_SECRET_KEY')
    region = os.environ.get('S3_REGION', 'us-east-1')
    if not ak or not sk:
        raise SystemExit('set S3_ACCESS_KEY and S3_SECRET_KEY')
    url = endpoint + '/' + (bucket if action != 'list' else '')
    method = ACTIONS[action]
    req = urllib.request.Request(url, method=method, data=b'' if method == 'PUT' else None,
                                 headers=s3_sign(method, url, ak, sk, region))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code, body = r.getcode(), r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read().decode('utf-8', 'replace')
    err = re.search(r'<Code>([^<]+)</Code>', body)
    print('%s %s -> HTTP %s%s' % (method, url, code, (' ' + err.group(1)) if err else ''))
    if action == 'list' and code == 200:
        for name in re.findall(r'<Name>([^<]+)</Name>', body):
            print('  ' + name)
    return 0 if code < 300 else 1


if __name__ == '__main__':
    sys.exit(main())
