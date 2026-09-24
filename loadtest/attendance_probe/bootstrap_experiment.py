"""Create a NEW, disposable institute/outbox experiment root on the host.

Everything this writes lives under one directory named after a freshly
generated experiment id, and every subdirectory carries the ownership marker
that cleanup.py verifies before it removes anything. Nothing outside that
directory is touched.

What it produces
────────────────
    <root>/.attlt_owner            ownership marker (manifest.create)
    <root>/manifest.json           resource inventory for cleanup
    <root>/experiment.json         non-secret configuration
    <root>/secrets/secrets.json    experiment-only secrets, never reported
    <root>/app_src/                git archive of the tested revision
    <root>/guard/firebase_admin/   import blocker for the TARGET process
    <root>/run/environment_identity.json   printable identity card

The container never builds any of this: the compose project bind-mounts the
root read-write at /exp and the runner works inside it. Keeping creation on
the host is what lets `docker compose down -v` be the only destructive step
that matters for the database.

No database is contacted here, no load is generated, and no application code
is imported.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import environment_identity as ident  # noqa: E402
import institute_common as ic  # noqa: E402
import manifest  # noqa: E402

DB_NAME = 'core_school_attendance_load_test'
FORBIDDEN_IN_ARCHIVE = ('.env', 'firebase-key.json', 'github_actions_mecha',
                        'github_actions_mecha.pub')


def build_config(root: str, exp_id: str, a) -> dict:
    """The experiment's non-secret configuration.

    pg_host is the compose service name, which environment_identity classifies
    as 'container' — isolated by construction, because that name only resolves
    inside the private network.
    """
    return {
        'experiment_id': exp_id,
        'target_host': '127.0.0.1',
        'http_port': a.http_port,
        'ws_port': a.ws_port,
        'pg_mode': 'container',
        'pg_host': 'db',
        'pg_port': 5432,
        'db_name': DB_NAME,
        'app_revision': a.revision,
        'num_schools': a.schools,
        'students_per_school': a.students,
        'devices_per_school': 2,
        # The institute round never reads AI Face history, and seeding it would
        # add hundreds of thousands of rows that prove nothing here.
        'history_calendar_days': 0,
        'history_end_date': None,          # set by seed.py
        'school_timezone': 'Asia/Baghdad',
        'att_start_time': '07:00:00',
        'att_late_threshold': '07:45:00',
        'att_absence_threshold': '09:00:00',
        'att_departure_time': '13:00:00',
        'target_server': 'gunicorn',
        'gunicorn_env': {
            'WEB_CONCURRENCY': '1', 'GUNICORN_THREADS': '4',
            'GUNICORN_TIMEOUT': '120',
            'SQLALCHEMY_POOL_SIZE': '5', 'SQLALCHEMY_MAX_OVERFLOW': '10',
            'SQLALCHEMY_POOL_TIMEOUT': '30',
        },
    }


def archive_source(root: str, repo: str, revision: str) -> str:
    """git archive of the tested revision: no .env, no untracked file."""
    dest = os.path.join(root, 'app_src')
    rev = subprocess.run(['git', '-C', repo, 'rev-parse', revision],
                         capture_output=True, text=True,
                         check=True).stdout.strip()
    blob = subprocess.run(['git', '-C', repo, 'archive', '--format=tar', rev],
                          capture_output=True, check=True).stdout
    os.makedirs(dest)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest, filter='data')
    for forbidden in FORBIDDEN_IN_ARCHIVE:
        if os.path.exists(os.path.join(dest, forbidden)):
            raise SystemExit(f'archived source contains {forbidden} — aborting')
    manifest.write_owner_marker(dest, json.load(
        open(os.path.join(root, 'manifest.json'), encoding='utf-8'))['experiment_id'])
    return rev


def write_guard(root: str, exp_id: str) -> None:
    """The TARGET process must never import firebase_admin at all.

    Only the worker gets the fake; the web process gets an import error, which
    is the same control the AI Face rounds have always used.
    """
    guard = os.path.join(root, 'guard', 'firebase_admin')
    os.makedirs(guard)
    with open(os.path.join(guard, '__init__.py'), 'w', encoding='utf-8') as fh:
        fh.write('raise ImportError("firebase_admin is blocked inside the '
                 'attendance load-test target (isolation guard: no real push '
                 'notifications)")\n')
    manifest.write_owner_marker(os.path.join(root, 'guard'), exp_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=None,
                    help='experiments directory (default: ../../_loadtest)')
    ap.add_argument('--repo', default=manifest.REPO_ROOT)
    ap.add_argument('--revision', default='HEAD')
    ap.add_argument('--schools', type=int, default=60)
    ap.add_argument('--students', type=int, default=60)
    ap.add_argument('--http-port', type=int, default=18180)
    ap.add_argument('--ws-port', type=int, default=18188)
    a = ap.parse_args()

    if a.schools >= 90:
        # seed.py reserves 90/91 for its precheck schools.
        raise SystemExit('--schools must stay below 90')
    if a.students < ic.GROUPS_PER_SCHOOL * ic.STUDENTS_PER_GROUP:
        raise SystemExit(
            f'--students must be at least '
            f'{ic.GROUPS_PER_SCHOOL * ic.STUDENTS_PER_GROUP} so every group '
            f'fills')

    root = manifest.create(base=a.base)
    exp_id = manifest.load(root)['experiment_id']
    tag = exp_id.rsplit('-', 1)[-1]

    cfg = build_config(root, exp_id, a)
    rev = archive_source(root, a.repo, a.revision)
    cfg['app_revision_full'] = rev
    with open(os.path.join(root, 'experiment.json'), 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2)

    sec = {
        'pg_user': f'attlt_{tag}',
        'pg_password': secrets.token_urlsafe(24),
        'app_secret_key': secrets.token_urlsafe(48),
        'jwt_secret_key': secrets.token_urlsafe(48),
        'ops_metrics_token': secrets.token_urlsafe(24),
        'parent_password': 'Lt-' + secrets.token_urlsafe(12),
    }
    sec_path = os.path.join(root, 'secrets', 'secrets.json')
    with open(sec_path, 'w', encoding='utf-8') as fh:
        json.dump(sec, fh, indent=2)

    write_guard(root, exp_id)

    identity = ident.build(
        dict(cfg, root=root), experiment_id=exp_id, tag=tag, app_commit=rev,
        worker_enabled=True, prefixes=ic.experiment_prefixes(tag),
        production_network_reachable=False,
        cleanup_manifest_path=os.path.join(root, 'manifest.json'))
    ident.write(root, identity)

    manifest.add_resource(root, 'file', path=os.path.join(root, 'experiment.json'),
                          note='experiment config (non-secret)')
    manifest.add_resource(root, 'file', path=sec_path,
                          note='test-only secrets; never include in reports')
    manifest.add_resource(root, 'directory', path=os.path.join(root, 'app_src'),
                          revision=rev, note='git archive of the tested revision')
    manifest.add_resource(root, 'directory', path=os.path.join(root, 'guard'),
                          note='import guard, first on the target PYTHONPATH')
    manifest.add_resource(root, 'ports', http=a.http_port, ws=a.ws_port,
                          pg=None,
                          bind='container loopback only; no published ports')
    manifest.add_resource(root, 'docker-compose', project=f'attlt_{tag}',
                          note='destroy with: docker compose -p attlt_<tag> '
                               '-f compose.institute-load.yml down -v')

    print(json.dumps({
        'experiment_id': exp_id,
        'experiment_tag': tag,
        'root': root,
        'compose_project': f'attlt_{tag}',
        'app_revision': rev,
        'num_schools': cfg['num_schools'],
        'students_per_school': cfg['students_per_school'],
        'sessions': cfg['num_schools'] * ic.GROUPS_PER_SCHOOL,
        'transition_budget': cfg['num_schools'] * ic.GROUPS_PER_SCHOOL
        * ic.STUDENTS_PER_GROUP,
        'job_budget': cfg['num_schools'] * ic.GROUPS_PER_SCHOOL
        * ic.STUDENTS_PER_GROUP * ic.TOKENS_PER_PARENT,
        'database_host_classification': identity['database_host_classification'],
        'database_isolated': identity['database_isolated'],
    }, indent=2))


if __name__ == '__main__':
    main()
