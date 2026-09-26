"""Resolve what the experiment must NOT be able to reach, on the HOST side.

Run as root OUTSIDE the private network namespace, before any load:

    python3 vps_forbidden_targets.py --root <root> --prod-pid <mecha-school MainPID> \
        --prod-repo /var/www/mecha-school --public-ip <ip> --owner attlt

It reads the production DATABASE_URL / REDIS_URL / SUPABASE_URL (process
environment first, the production .env as a fallback — both READ-ONLY), takes
only their host names and ports, resolves them — plus the Firebase / Google
OAuth endpoints and the host's public address — to IP addresses, and writes
<root>/secrets/forbidden_targets.json (mode 0600, owned by the test account).

netns_proof.py (--targets-file) and aiface_load.py (ATTLT_FORBIDDEN_TARGETS)
then TCP-probe every address from INSIDE the namespace and require every probe
to fail. That turns "the namespace has only lo" into a direct, per-target proof
that production PostgreSQL/Supabase, Redis, Firebase and port 7788 are
unreachable from the experiment.

Nothing is printed except labels and counts: never a URL, user, password,
host name or address. The file itself holds host names and addresses only —
no credential is ever written.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import socket
import sys
from urllib.parse import urlsplit

FIREBASE_NAMES = ('fcm.googleapis.com', 'oauth2.googleapis.com',
                  'firebaseinstallations.googleapis.com', 'www.googleapis.com')
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


def proc_env(pid: int) -> dict:
    raw = open(f'/proc/{pid}/environ', 'rb').read().decode('utf-8', 'replace')
    return dict(kv.split('=', 1) for kv in raw.split('\0') if '=' in kv)


def dotenv(path: str) -> dict:
    out = {}
    try:
        for line in open(path, encoding='utf-8', errors='replace'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k = k.strip()
            if k.startswith('export '):
                k = k[7:].strip()
            out[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def resolve(name: str) -> list:
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--prod-pid', type=int, default=0)
    ap.add_argument('--prod-repo', default='/var/www/mecha-school')
    ap.add_argument('--public-ip', default='')
    ap.add_argument('--owner', default='attlt')
    a = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('run as root (reads the production process environment read-only)')

    env, source = {}, None
    if a.prod_pid:
        try:
            env = proc_env(a.prod_pid)
            source = 'process environment'
        except OSError:
            env = {}
    file_env = dotenv(os.path.join(a.prod_repo, '.env'))
    from_file = []
    for k in ('DATABASE_URL', 'REDIS_URL', 'SUPABASE_URL'):
        if not env.get(k) and file_env.get(k):
            env[k] = file_env[k]
            from_file.append(k)
    if from_file:
        source = f"{source + ' + ' if source else ''}.env ({', '.join(from_file)})"

    targets, names, summary = [], [], {}

    def add(label, host, ports):
        if not host:
            summary[label] = 'not configured'
            return
        ips = [host] if host in LOOPBACK else resolve(host)
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if host not in LOOPBACK and not literal:
            names.append(host)      # a DNS NAME must not resolve inside; IPs are TCP-probed
        for ip in ips:
            for p in ports:
                targets.append({'label': label, 'ip': ip, 'port': int(p)})
        summary[label] = {'addresses': len(ips), 'ports': sorted({int(p) for p in ports}),
                          'loopback': host in LOOPBACK}

    db_host = None
    if env.get('DATABASE_URL'):
        u = urlsplit(env['DATABASE_URL'])
        db_host = u.hostname
        # the configured port, plus both Supabase ports (direct 5432, pooler 6543)
        add('production PostgreSQL/Supabase', db_host, {u.port or 5432, 5432, 6543})
    else:
        summary['production PostgreSQL/Supabase'] = 'DATABASE_URL NOT FOUND'
    if env.get('REDIS_URL'):
        u = urlsplit(env['REDIS_URL'])
        add('production Redis', u.hostname, {u.port or 6379})
    else:
        summary['production Redis'] = 'not configured'
    if env.get('SUPABASE_URL'):
        add('production Supabase API', urlsplit(env['SUPABASE_URL']).hostname, {443})
    for n in FIREBASE_NAMES:
        add('Firebase / Google', n, {443})
    if a.public_ip:
        add('this host public address (prod HTTPS / AI Face 7788)', a.public_ip, {80, 443, 7788})

    firebase_addrs = len({t['ip'] for t in targets if t['label'] == 'Firebase / Google'})
    doc = {
        'note': 'host names and addresses only; no credential. Never printed.',
        'source': source,
        'production_db_hostname': db_host or '',
        'production_db_host_sha256_12': hashlib.sha256((db_host or '').encode()).hexdigest()[:12],
        'targets': targets,
        'names_to_resolve': sorted(set(names)),
    }
    path = os.path.join(os.path.abspath(a.root), 'secrets', 'forbidden_targets.json')
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, indent=2)
    pw = pwd.getpwnam(a.owner)
    os.chown(tmp, pw.pw_uid, pw.pw_gid)
    os.replace(tmp, path)

    report = {'source': source, 'targets': len(targets), 'by_label': summary,
              'production_db_host_sha256_12': doc['production_db_host_sha256_12'],
              'firebase_addresses': firebase_addrs}
    with open(os.path.join(os.path.abspath(a.root), 'results', 'forbidden_targets_summary.json'),
              'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    problems = []
    if not db_host:
        problems.append('production DATABASE_URL host not found')
    elif not any(t['label'] == 'production PostgreSQL/Supabase' for t in targets):
        problems.append('production database host did not resolve on the host')
    if not firebase_addrs:
        problems.append('Firebase endpoints did not resolve on the host')
    if problems:
        print('FORBIDDEN TARGETS INCOMPLETE: ' + '; '.join(problems))
        raise SystemExit(1)
    print('forbidden targets written (addresses withheld)')


if __name__ == '__main__':
    sys.exit(main())
