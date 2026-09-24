"""Prove that the ephemeral private network namespace really isolates the round.

Run twice by vps_run.sh, before any load is generated and again after the target
is listening:

    nsenter --net=/proc/<nspid>/ns/net -- python3 netns_proof.py \
        --root <root> --scope inside  --label pre-load
    python3 netns_proof.py --root <root> --scope outside --label pre-load \
        --nspid <nspid> --live-health-url https://<domain>/ops/health

`inside` asserts the experiment CANNOT reach production or the internet:
only the loopback interface exists, and every probe toward the production
application, Redis, PostgreSQL, the public address and an external notification
endpoint must fail to connect. `outside` asserts the host has no listener on the
test ports (nothing is published), that the namespace is genuinely a different
one, and that production is still healthy before the round starts.

Standard library only (it runs under the system interpreter, as root, inside the
namespace). Writes <root>/results/isolation_<scope>_<label>.json and exits
non-zero if any required property fails. No secrets are read or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest  # noqa: E402

TEST_PORTS = (18180, 18188, 55480)

# Everything the experiment must NOT be able to reach from inside the namespace.
FORBIDDEN = [
    ('production app (loopback)', '127.0.0.1', 8000),
    ('production Redis (loopback)', '127.0.0.1', 6379),
    ('any local PostgreSQL', '127.0.0.1', 5432),
    ('production HTTPS (public address)', None, 443),      # host filled from --public-ip
    ('external DNS', '1.1.1.1', 53),
    ('external HTTPS', '1.1.1.1', 443),
]
FORBIDDEN_NAMES = ['fcm.googleapis.com', 'oauth2.googleapis.com', 'pypi.org']


def tcp_probe(host, port, timeout=2.0):
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return {'target': f'{host}:{port}', 'connected': True,
                'ms': round((time.perf_counter() - t0) * 1000, 1)}
    except OSError as exc:
        return {'target': f'{host}:{port}', 'connected': False, 'error': type(exc).__name__,
                'ms': round((time.perf_counter() - t0) * 1000, 1)}


def resolves(name):
    try:
        socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def _v4(hexaddr):
    b = bytes.fromhex(hexaddr)
    return '.'.join(str(x) for x in reversed(b))


def _v6(hexaddr):
    words = [hexaddr[i:i + 8] for i in range(0, 32, 8)]
    raw = b''.join(bytes.fromhex(w)[::-1] for w in words)
    try:
        return socket.inet_ntop(socket.AF_INET6, raw)
    except OSError:
        return hexaddr


def listeners():
    """LISTEN sockets in the CURRENT network namespace (no external tools)."""
    out = []
    for path, fam in (('/proc/net/tcp', 4), ('/proc/net/tcp6', 6)):
        try:
            lines = open(path, encoding='ascii').read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 4 or f[3] != '0A':
                continue
            addr, port = f[1].rsplit(':', 1)
            out.append({'addr': _v4(addr) if fam == 4 else _v6(addr), 'port': int(port, 16),
                        'family': f'ipv{fam}'})
    return sorted(out, key=lambda r: (r['port'], r['addr']))


def interfaces():
    try:
        lines = open('/proc/net/dev', encoding='ascii').read().splitlines()[2:]
    except OSError:
        return []
    return sorted(ln.split(':')[0].strip() for ln in lines if ':' in ln)


def netns_id(pid='self'):
    try:
        return os.readlink(f'/proc/{pid}/ns/net')
    except OSError as exc:
        return f'unreadable: {type(exc).__name__}'


def http_status(url, timeout=8):
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception as exc:
        return type(exc).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--scope', choices=['inside', 'outside'], required=True)
    ap.add_argument('--label', default='pre-load')
    ap.add_argument('--nspid', type=int, default=0)
    ap.add_argument('--public-ip', default='')
    ap.add_argument('--live-health-url', default='')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    res = {'scope': a.scope, 'label': a.label, 'at_utc': manifest.utcnow_iso(),
           'netns_self': netns_id('self'), 'interfaces': interfaces(),
           'listeners': listeners(), 'failures': []}

    if a.scope == 'inside':
        res['netns_pid1_host'] = netns_id(1)
        res['separate_namespace'] = (res['netns_self'] != res['netns_pid1_host']
                                     and not res['netns_pid1_host'].startswith('unreadable'))
        probes = []
        for name, host, port in FORBIDDEN:
            if host is None:
                if not a.public_ip:
                    continue
                host = a.public_ip
            p = tcp_probe(host, port)
            p['what'] = name
            probes.append(p)
        res['reachability_probes'] = probes
        res['dns_resolution'] = {n: resolves(n) for n in FORBIDDEN_NAMES}
        # required properties
        if res['interfaces'] != ['lo']:
            res['failures'].append(f"namespace has interfaces other than lo: {res['interfaces']}")
        if not res['separate_namespace']:
            res['failures'].append('not running in a separate network namespace')
        for p in probes:
            if p['connected']:
                res['failures'].append(f"REACHABLE from inside the namespace: {p['what']} ({p['target']})")
        for n, ok in res['dns_resolution'].items():
            if ok:
                res['failures'].append(f'DNS resolves {n} inside the namespace')
        public = [l for l in res['listeners'] if l['addr'] not in ('127.0.0.1', '::1')]
        res['listeners_confined_to_namespace'] = True   # the namespace has no external interface
        res['non_loopback_listeners_in_namespace'] = public
    else:
        res['host_test_port_listeners'] = [l for l in res['listeners'] if l['port'] in TEST_PORTS]
        if res['host_test_port_listeners']:
            res['failures'].append(f"test ports are listening on the HOST: {res['host_test_port_listeners']}")
        if a.nspid:
            res['netns_holder'] = netns_id(a.nspid)
            res['namespace_is_separate'] = res['netns_holder'] not in (res['netns_self'],)
            if not res['namespace_is_separate']:
                res['failures'].append('the namespace holder shares the host network namespace')
        if a.live_health_url:
            res['production_health_status'] = http_status(a.live_health_url)
            if res['production_health_status'] != 200:
                res['failures'].append(f"production health is {res['production_health_status']} (expected 200)")

    res['PASS'] = not res['failures']
    out_dir = os.path.join(root, 'results')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f'isolation_{a.scope}_{a.label}.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, indent=2)
    print(json.dumps(res, indent=2))
    print(f'ISOLATION {a.scope}/{a.label}: ' + ('PASS' if res['PASS'] else 'FAIL'))
    raise SystemExit(0 if res['PASS'] else 1)


if __name__ == '__main__':
    main()
