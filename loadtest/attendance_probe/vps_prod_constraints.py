"""Pin the experiment's target venv to the package versions PRODUCTION runs.

    python3 vps_prod_constraints.py --root <root> --prod-pid <MainPID> \
        --prod-repo /var/www/mecha-school --owner attlt

Why: requirements.txt does not pin every transitive dependency. A fresh
`pip install -r requirements.txt` on 2026-09-25 resolves SQLAlchemy 2.1.0, whose
default `postgresql://` driver is psycopg 3 — not installed, so the app cannot
even create its engine. Production runs an older, working set. Testing a
different set than production would make the round meaningless either way.

Read-only: it locates the production interpreter's site-packages (from the
production process's command line, or <prod-repo>/venv|.venv), LISTS the
*.dist-info directories and reads their METADATA Name/Version. Nothing is
executed, imported or modified in production. Writes <root>/run/
prod_constraints.txt (pip constraints: Name==Version) and a summary.
If production's environment cannot be found, it writes the minimal safe pin
`SQLAlchemy<2.1` and says so.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pwd


def site_packages_candidates(prod_pid: int, prod_repo: str) -> list:
    cands = []
    if prod_pid:
        try:
            argv = open(f'/proc/{prod_pid}/cmdline', 'rb').read().split(b'\0')
            for arg in argv[:2]:
                a = arg.decode('utf-8', 'replace')
                if '/bin/' in a:
                    cands.append(os.path.dirname(os.path.dirname(a)))
        except OSError:
            pass
    for name in ('venv', '.venv', 'env'):
        cands.append(os.path.join(prod_repo, name))
    out = []
    for venv in cands:
        out += sorted(glob.glob(os.path.join(venv, 'lib', 'python3*', 'site-packages')))
    seen, uniq = set(), []
    for p in out:
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            uniq.append(p)
    return uniq


def dist_versions(site: str) -> dict:
    pins = {}
    for d in glob.glob(os.path.join(site, '*.dist-info')):
        name = ver = None
        try:
            for line in open(os.path.join(d, 'METADATA'), encoding='utf-8', errors='replace'):
                if line.startswith('Name:') and name is None:
                    name = line.split(':', 1)[1].strip()
                elif line.startswith('Version:') and ver is None:
                    ver = line.split(':', 1)[1].strip()
                if name and ver:
                    break
        except OSError:
            continue
        if name and ver:
            pins[name] = ver
    return pins


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--prod-pid', type=int, default=0)
    ap.add_argument('--prod-repo', default='/var/www/mecha-school')
    ap.add_argument('--owner', default='attlt')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    pins, source = {}, None
    for site in site_packages_candidates(a.prod_pid, a.prod_repo):
        pins = dist_versions(site)
        if any(k.lower() == 'sqlalchemy' for k in pins):
            source = 'production site-packages (dist-info listing, read-only)'
            break
    if not pins or not source:
        pins, source = {}, 'FALLBACK: production environment not found'
        lines = ['SQLAlchemy<2.1']
    else:
        # pip/setuptools/wheel are the venv's own tooling, not the app's.
        skip = {'pip', 'setuptools', 'wheel', 'distribute'}
        lines = [f'{n}=={v}' for n, v in sorted(pins.items(), key=lambda kv: kv[0].lower())
                 if n.lower() not in skip]
    path = os.path.join(root, 'run', 'prod_constraints.txt')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    pw = pwd.getpwnam(a.owner)
    os.chown(path, pw.pw_uid, pw.pw_gid)
    sqla = next((v for n, v in pins.items() if n.lower() == 'sqlalchemy'), None)
    summary = {'source': source, 'pins': len(lines), 'production_sqlalchemy': sqla,
               'production_flask': next((v for n, v in pins.items() if n.lower() == 'flask'), None),
               'production_psycopg2': next((v for n, v in pins.items()
                                            if n.lower() in ('psycopg2-binary', 'psycopg2')), None)}
    with open(os.path.join(root, 'results', 'prod_constraints_summary.json'), 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
