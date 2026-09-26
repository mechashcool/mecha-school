"""Read-only production state snapshot, taken before and after the round.

    python3 vps_prod_snapshot.py --root <root> --label pre  --health-url <url>
    python3 vps_prod_snapshot.py --root <root> --label post --health-url <url> --compare pre

Run as root on the HOST (outside the namespace). It only READS:
  * every mecha* systemd unit: ActiveState, MainPID, NRestarts,
    ActiveEnterTimestamp, and a sha256 of `systemctl cat` (unit + drop-ins)
  * sha256 of the production .env (never its content) and the production
    checkout's HEAD commit
  * the listener on production AI Face port 7788 (owning pids) and whether
    anything listens on the test ports on the host
  * production /ops/health status and latency (3 samples)
With --compare, it fails (exit 1) if any production unit restarted or changed
MainPID, any unit/drop-in/.env hash changed, the production HEAD moved, the
7788 listener changed, or a test port is listening on the host.
Standard library only. Writes <root>/results/prod_<label>.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request

TEST_PORTS = (18180, 18188, 55480)


def sh(*cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8', 'replace')
    return hashlib.sha256(data).hexdigest()[:16]


def units():
    out = sh('systemctl', 'list-units', '--type=service', '--all', '--no-legend', '--plain', 'mecha*')
    names = sorted({ln.split()[0] for ln in out.splitlines() if ln.strip()})
    res = {}
    for u in names:
        props = dict(ln.split('=', 1) for ln in sh(
            'systemctl', 'show', u, '-p', 'ActiveState', '-p', 'MainPID', '-p', 'NRestarts',
            '-p', 'ActiveEnterTimestamp').splitlines() if '=' in ln)
        res[u] = {**props, 'unit_and_dropins_sha': sha(sh('systemctl', 'cat', u))}
    return res


def listeners(port):
    out = sh('ss', '-ltnpH', f'sport = :{port}')
    pids = sorted({int(p) for p in re.findall(r'pid=(\d+)', out)})
    return {'listening': bool(out.strip()), 'pids': pids}


def health(url):
    rows = []
    for _ in range(3):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                st = r.status
        except Exception as exc:
            st = type(exc).__name__
        rows.append({'status': st, 'ms': round((time.perf_counter() - t0) * 1000, 1)})
        time.sleep(0.5)
    return rows


def snapshot(a):
    env_path = os.path.join(a.prod_repo, '.env')
    try:
        env_sha = sha(open(env_path, 'rb').read())
    except OSError:
        env_sha = None
    head = sh('env', 'GIT_CONFIG_COUNT=1', 'GIT_CONFIG_KEY_0=safe.directory', 'GIT_CONFIG_VALUE_0=*',
              'GIT_OPTIONAL_LOCKS=0', 'git', '-C', a.prod_repo, 'rev-parse', 'HEAD').strip()
    return {'label': a.label, 'at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'units': units(), 'prod_env_sha': env_sha, 'prod_head': head,
            'port_7788': listeners(7788),
            'host_test_ports': {p: listeners(p)['listening'] for p in TEST_PORTS},
            'health': health(a.health_url) if a.health_url else None}


def compare(pre, post):
    diffs = []
    for u, p in pre['units'].items():
        q = post['units'].get(u)
        if q is None:
            diffs.append(f'unit {u} disappeared')
            continue
        for k in ('MainPID', 'NRestarts', 'ActiveEnterTimestamp', 'ActiveState', 'unit_and_dropins_sha'):
            if p.get(k) != q.get(k):
                diffs.append(f'unit {u}: {k} changed')
    for u in post['units']:
        if u not in pre['units']:
            diffs.append(f'new unit {u} appeared')
    if pre['prod_env_sha'] != post['prod_env_sha']:
        diffs.append('production .env hash changed')
    if pre['prod_head'] != post['prod_head']:
        diffs.append('production checkout HEAD moved')
    if pre['port_7788'] != post['port_7788']:
        diffs.append('port 7788 listener changed')
    if any(post['host_test_ports'].values()):
        diffs.append(f"test port listening on the host: {post['host_test_ports']}")
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--health-url', default='')
    ap.add_argument('--prod-repo', default='/var/www/mecha-school')
    ap.add_argument('--compare', default='')
    a = ap.parse_args()
    snap = snapshot(a)
    if a.compare:
        pre = json.load(open(os.path.join(a.root, 'results', f'prod_{a.compare}.json'), encoding='utf-8'))
        snap['compared_with'] = a.compare
        snap['differences'] = compare(pre, snap)
        snap['production_unchanged'] = not snap['differences']
    with open(os.path.join(a.root, 'results', f'prod_{a.label}.json'), 'w', encoding='utf-8') as fh:
        json.dump(snap, fh, indent=2)
    print(json.dumps(snap, indent=2))
    if a.compare and snap['differences']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
