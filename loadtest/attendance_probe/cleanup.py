"""Remove ONLY resources whose ownership is proven by the experiment manifest.

TWO LEVELS. Level 1 is the default and removes nothing.

    # Level 1 — dry run: enumerate exactly what would be removed
    python cleanup.py --root <experiment root>

    # Level 2 — destructive: requires the exact tag AND explicit confirmation
    python cleanup.py --root <experiment root> --execute \
        --tag <experiment tag> --confirm --delete-db

Level 2 passes through destructive_gate() first, which REFUSES unless the
experiment tag matches, --confirm is present, the root carries this
experiment's ownership marker, run/environment_identity.json verifies (fake
Firebase, non-production WS port, production unreachable), the database host
and name are provably isolated, and every manifest directory is owned. A
refusal removes nothing. There is no override flag.
Options (all opt-in):
    --delete-db          drop the experiment database (existing-server mode) or delete the
                         experiment-owned PG data directory (local-cluster mode)
    --delete-reports     also delete results/ (reports are preserved by default)
    --delete-tooling     also delete the tooling directory in the repository, only if every
                         file still matches the hash recorded at cleanup-plan time of the round
Safety rules:
  * a process is stopped only if pid + create_time + cmdline match the manifest
  * a database is dropped only if attlt_owner contains exactly this experiment id AND the
    database COMMENT is attlt:<experiment id>
  * a directory is deleted only if it carries the .attlt_owner marker for this experiment
  * never uses broad kills, git clean, or docker prune
  * afterwards: verifies the repository status/diff hash is unchanged since the round
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import environment_identity as ident  # noqa: E402
import manifest  # noqa: E402


class RefusedUnsafeCleanup(SystemExit):
    """Raised instead of deleting anything when isolation cannot be proven."""


def destructive_gate(root: str, m: dict, cfg: dict, *, tag: str | None,
                     confirm: bool) -> dict:
    """Everything that must be true before a single row or file is removed.

    Fails closed and refuses loudly. There is no flag that skips it, and no
    branch that downgrades a refusal to a warning.
    """
    exp = m['experiment_id']
    reasons = []

    if not confirm:
        reasons.append('--confirm was not given')
    if not tag:
        reasons.append('--tag <experiment tag> is required for destructive '
                       'cleanup and was not given')
    else:
        expected_tag = exp.rsplit('-', 1)[-1]
        if tag != expected_tag:
            reasons.append('--tag does not match this experiment')

    if not manifest.owner_marker_matches(root, exp):
        reasons.append('experiment root ownership marker missing or different')

    # The identity card is the second, independent proof. It raises on its own
    # if the database is production-shaped, the WS port is 7788, Firebase is
    # not the local fake, or production is reachable.
    try:
        identity = ident.verify(root, experiment_id=exp,
                                tag=tag or exp.rsplit('-', 1)[-1])
    except SystemExit as exc:
        reasons.append(f'environment identity: {exc}')
        identity = None

    isolated, why = ident.is_isolated_db(cfg.get('pg_host', ''),
                                         cfg.get('db_name', ''))
    if not isolated:
        reasons.extend('database is not provably isolated: ' + r for r in why)

    unowned = [r for r in m.get('resources', [])
               if r.get('kind') == 'directory'
               and r.get('path')
               and os.path.exists(r['path'])
               and r.get('owner_marker')
               and not manifest.owner_marker_matches(r['path'], exp)]
    for r in unowned:
        reasons.append(f"resource not owned by this manifest: {r['path']}")

    if reasons:
        raise RefusedUnsafeCleanup(
            'DESTRUCTIVE CLEANUP REFUSED — nothing was removed:\n  - '
            + '\n  - '.join(reasons))
    return {'gate': 'PASS', 'experiment_id': exp,
            'database': cfg.get('db_name'),
            'database_host_classification':
                ident.classify_db_host(cfg.get('pg_host', '')),
            'identity_verified': bool(identity)}


def proc_matches(res):
    try:
        import psutil
        p = psutil.Process(res['pid'])
        return (abs(p.create_time() - res['create_time']) < 1.0 and p.cmdline() == res['cmdline']), p
    except Exception:
        return False, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--delete-db', action='store_true')
    ap.add_argument('--delete-reports', action='store_true')
    ap.add_argument('--delete-tooling', action='store_true')
    ap.add_argument('--delete-docker-image', action='store_true')
    ap.add_argument('--tag', default=None,
                    help='the experiment tag; REQUIRED with --execute')
    ap.add_argument('--confirm', action='store_true',
                    help='explicit confirmation; REQUIRED with --execute')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    m = manifest.load(root)
    exp = m['experiment_id']
    if not manifest.owner_marker_matches(root, exp):
        raise SystemExit('experiment root ownership marker missing or different — refusing')
    cfg = json.load(open(os.path.join(root, 'experiment.json')))
    report = {'experiment_id': exp, 'executed': a.execute, 'deleted': [], 'stopped': [], 'preserved': [], 'skipped': []}
    # Level 1 (--dry-run, the default) enumerates and removes nothing, so it
    # runs without the gate. Level 2 must prove isolation before anything goes.
    if a.execute:
        report['gate'] = destructive_gate(root, m, cfg, tag=a.tag,
                                          confirm=a.confirm)
    else:
        report['mode'] = ('DRY RUN — nothing will be removed. Re-run with '
                          '--execute --tag <tag> --confirm to act.')

    def act(kind, what, fn):
        if a.execute:
            try:
                fn()
                report['deleted' if kind == 'delete' else 'stopped'].append(what)
            except Exception as exc:
                report['skipped'].append(f'{what}: {type(exc).__name__}: {exc}')
        else:
            report['deleted' if kind == 'delete' else 'stopped'].append('[plan] ' + what)

    # processes
    for res in m['resources']:
        if res['kind'] != 'process':
            continue
        ok, p = proc_matches(res)
        if not ok:
            report['skipped'].append(f"process {res['role']} pid {res['pid']}: not running or identity mismatch")
            continue
        def _stop(p=p):
            import psutil
            procs = [p] + p.children(recursive=True)
            for q in procs:
                q.terminate()
            _, alive = psutil.wait_procs(procs, timeout=30)
            for q in alive:
                q.kill()
        act('stop', f"process {res['role']} pid {res['pid']}", _stop)

    # database
    sec_path = os.path.join(root, 'secrets', 'secrets.json')
    sec = json.load(open(sec_path)) if os.path.exists(sec_path) else None
    pg = next((r for r in m['resources'] if r['kind'] == 'pg_cluster'), None)
    if pg and manifest.owner_marker_matches(pg['data_dir'], exp):
        pg_ctl = os.path.join(os.environ.get('ATTLT_PG_BIN', r'C:\Program Files\PostgreSQL\18\bin' if os.name == 'nt' else ''),
                              'pg_ctl' + ('.exe' if os.name == 'nt' else ''))
        act('stop', f"experiment PostgreSQL cluster {pg['data_dir']}",
            lambda: subprocess.run([pg_ctl, '-D', pg['data_dir'], '-m', 'fast', 'stop'], check=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if a.delete_db:
            act('delete', f"PG data directory {pg['data_dir']}", lambda: shutil.rmtree(pg['data_dir']))
        else:
            report['preserved'].append(f"PG data directory {pg['data_dir']} (pass --delete-db)")
    elif cfg.get('pg_mode') == 'existing' and sec:
        if a.delete_db:
            import psycopg2
            dsn = os.environ.get('ATTLT_PG_ADMIN_DSN')
            if not dsn:
                report['skipped'].append('database drop: ATTLT_PG_ADMIN_DSN not set')
            else:
                c = psycopg2.connect(host=cfg['pg_host'], port=cfg['pg_port'], user=sec['pg_user'],
                                     password=sec['pg_password'], dbname=cfg['db_name'])
                cur = c.cursor()
                cur.execute('SELECT experiment_id FROM attlt_owner')
                owners = [r[0] for r in cur.fetchall()]
                cur.execute('SELECT shobj_description(oid, %s) FROM pg_database WHERE datname=%s', ('pg_database', cfg['db_name']))
                comment = cur.fetchone()[0]
                c.close()
                if owners == [exp] and comment == f'attlt:{exp}':
                    def _drop():
                        ac = psycopg2.connect(dsn)
                        ac.autocommit = True
                        k = ac.cursor()
                        k.execute(f'DROP DATABASE "{cfg["db_name"]}"')
                        k.execute(f'DROP ROLE "{sec["pg_user"]}"')
                        ac.close()
                    act('delete', f"database {cfg['db_name']} and role {sec['pg_user']}", _drop)
                else:
                    report['skipped'].append('database ownership marker/comment mismatch — not dropped')
        else:
            report['preserved'].append(f"database {cfg['db_name']} (pass --delete-db)")

    # directories inside the experiment root
    keep = {'results'} if not a.delete_reports else set()
    for name in ('venv-target', 'venv-gen', 'app_src', 'guard', 'run', 'logs', 'secrets', 'results'):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        if name in keep:
            report['preserved'].append(f'{path} (reports)')
            continue
        if name == 'pgdata':
            continue
        if manifest.owner_marker_matches(path, exp):
            act('delete', f'directory {path}', lambda p=path: shutil.rmtree(p))
        else:
            report['skipped'].append(f'{path}: owner marker missing — preserved')
    # docker image built for the Linux driver check
    for res in m['resources']:
        if res.get('kind') == 'docker_image':
            if a.execute and a.delete_docker_image:
                import shutil as _sh
                dk = _sh.which('docker')
                if dk:
                    try:
                        subprocess.run([dk, 'rmi', res['name']], check=False,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        report['deleted'].append(f"docker image {res['name']}")
                    except Exception as exc:
                        report['skipped'].append(f"docker image {res['name']}: {exc}")
            else:
                report['preserved'].append(f"docker image {res['name']}" + ('' if a.delete_docker_image else ' (pass --delete-docker-image)'))
    report['preserved'].append(f"tooling {m['tool_dir']}" + ('' if a.delete_tooling else ' (pass --delete-tooling)'))
    report['preserved'].append(f'manifest {manifest.manifest_path(root)}')

    # repository unchanged check (against the last round's snapshot)
    try:
        import run_round
        snaps = [json.load(open(os.path.join(root, 'results', d, 'run_meta.json')))
                 for d in os.listdir(os.path.join(root, 'results'))
                 if os.path.exists(os.path.join(root, 'results', d, 'run_meta.json'))]
        if snaps:
            now = run_round.repo_snapshot(manifest.REPO_ROOT)
            last = snaps[-1].get('repo_snapshot_after') or snaps[-1]['repo_snapshot_before']
            report['repo_unchanged_since_round'] = (now['status_sha256'] == last['status_sha256']
                                                    and now['diff_sha256'] == last['diff_sha256'])
    except Exception as exc:
        report['repo_check_error'] = str(exc)
    print(json.dumps(report, indent=2))
    if a.execute:
        with open(os.path.join(root, 'cleanup_report.json'), 'w') as fh:
            json.dump(report, fh, indent=2)


if __name__ == '__main__':
    main()
