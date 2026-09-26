"""Package ONLY what the VPS AI Face outbox round needs, as paste-ready parts.

    python make_aifx_package.py --out <dir>

Produces in <dir>:
  attlt_aifx_<date>.tgz (+ .sha256)   tooling + the tested-source bundle
  paste_partNN.txt                    base64 heredoc chunks for the Hostinger
                                      web console (paste in order)
  paste_final.txt                     decode, verify sha256, unpack

Text files are normalised to LF (a CRLF shell script does not run on Linux).
No experiment data, secrets, venvs, results or local databases are included.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import tarfile
import time

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
DEST = '/srv/attlt-pkg-aifx'
CHUNK = 18000

TOP_LEVEL = [
    'common.py', 'manifest.py', 'environment_identity.py', 'guard_rules.py', 'institute_common.py',
    'outbox_monitor.py', 'safety_gates.py', 'target.py', 'worker_control.py', 'watchdog.py',
    'health_sentinel.py', 'netns_proof.py', 'setup_env.py', 'aiface_client.py',
    'vps_aiface_run.sh', 'vps_aiface_root.py', 'vps_prepare_db.py', 'vps_forbidden_targets.py',
    'vps_prod_snapshot.py', 'vps_aiface_digest.py', 'vps_prod_constraints.py',
]
EXTRA = [
    'docker/aiface_load.py',
    'fake_firebase/firebase_admin/__init__.py',
    'fake_firebase/firebase_admin/credentials.py',
    'fake_firebase/firebase_admin/messaging.py',
    'aifx/aifx.bundle',
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    stamp = time.strftime('%Y%m%d')
    name = f'attlt_aifx_{stamp}.tgz'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for rel in TOP_LEVEL + EXTRA:
            src = os.path.join(TOOL_DIR, rel)
            data = open(src, 'rb').read()
            if not rel.endswith('.bundle'):
                data = data.replace(b'\r\n', b'\n')
            info = tarfile.TarInfo('attendance_probe/' + rel)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o755 if rel.endswith('.sh') else 0o644
            tf.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    sha = hashlib.sha256(blob).hexdigest()
    with open(os.path.join(out, name), 'wb') as fh:
        fh.write(blob)
    with open(os.path.join(out, name + '.sha256'), 'w', newline='\n') as fh:
        fh.write(f'{sha}  {name}\n')
    b64 = base64.b64encode(blob).decode('ascii')
    lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]
    parts, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > CHUNK and cur:
            parts.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        parts.append(cur)
    for old in os.listdir(out):
        if old.startswith('paste_part') or old == 'paste_final.txt':
            os.remove(os.path.join(out, old))
    for i, chunk in enumerate(parts, 1):
        op = '>' if i == 1 else '>>'
        pre = f'mkdir -p {DEST} && ' if i == 1 else ''
        body = '\n'.join(chunk)
        with open(os.path.join(out, f'paste_part{i:02d}.txt'), 'w', newline='\n') as fh:
            fh.write(f"{pre}cat {op} {DEST}/pkg.b64 <<'B64EOF'\n{body}\nB64EOF\n")
    with open(os.path.join(out, 'paste_final.txt'), 'w', newline='\n') as fh:
        fh.write(f"cd {DEST} && base64 -d pkg.b64 > {name} && echo '{sha}  {name}' | sha256sum -c - "
                 f"&& tar xzf {name} && ls -la attendance_probe && "
                 f"bash attendance_probe/vps_aiface_run.sh --preflight\n")
    print(f'package: {os.path.join(out, name)}  ({len(blob)} bytes)')
    print(f'sha256:  {sha}')
    print(f'paste parts: {len(parts)} (+ paste_final.txt) in {out}')


if __name__ == '__main__':
    main()
