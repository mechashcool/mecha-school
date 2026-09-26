"""Create the run root for the VPS AI Face outbox round on the PRESERVED dataset.

    python3 vps_aiface_root.py create   --root <new root> --preserved-root <round-1 root> \
        --tested-sha <sha> --run-id <id>
    python3 vps_aiface_root.py identity --root <new root>

The preserved round-1 experiment (10 schools × 1,000 students, its PostgreSQL
cluster, its ownership marker) is REUSED, not recreated, so this round keeps
the preserved experiment_id: every synthetic identifier, the database
ownership marker and the fake-Firebase salt derive from it. The run itself
lives in a NEW directory so none of round 1's files, logs or results are
touched:

  * experiment.json   copy of round 1's, with the tested revision, the
                      production gunicorn defaults (no GUNICORN_MAX_REQUESTS
                      pin — gunicorn.conf.py decides, as in production) and
                      pg_data_dir pointing at the preserved cluster
  * secrets.json      copy of round 1's test-only secrets (mode 0600)
  * run/fixtures.json copy of seed.py's output for the preserved population
  * manifest.json     records the preserved cluster as REUSED, NOT OWNED —
                      there is deliberately no `database`/`pg_cluster` entry,
                      so cleanup.py run against this root can never drop or
                      delete the preserved database or its data directory
Standard library only. Prints no secret.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest  # noqa: E402

GUNICORN_ENV = {'WEB_CONCURRENCY': '1', 'GUNICORN_THREADS': '4', 'GUNICORN_TIMEOUT': '120',
                'SQLALCHEMY_POOL_SIZE': '5', 'SQLALCHEMY_MAX_OVERFLOW': '10',
                'SQLALCHEMY_POOL_TIMEOUT': '30'}


def create(a):
    root, pre = os.path.abspath(a.root), os.path.abspath(a.preserved_root)
    if os.path.exists(root):
        raise SystemExit(f'refusing to reuse existing run root: {root}')
    cfg = json.load(open(os.path.join(pre, 'experiment.json'), encoding='utf-8'))
    sec = json.load(open(os.path.join(pre, 'secrets', 'secrets.json'), encoding='utf-8'))
    fx = json.load(open(os.path.join(pre, 'run', 'fixtures.json'), encoding='utf-8'))
    exp_id = cfg['experiment_id']
    tag = exp_id.rsplit('-', 1)[-1]
    problems = []
    if not manifest.owner_marker_matches(pre, exp_id):
        problems.append('preserved root ownership marker does not match its experiment_id')
    if sec.get('pg_user') != f'attlt_{tag}':
        problems.append('preserved secrets name an unexpected database role')
    if (cfg.get('pg_mode'), cfg.get('pg_host'), cfg.get('db_name')) != \
            ('local-cluster', '127.0.0.1', 'core_school_attendance_load_test'):
        problems.append('preserved experiment.json is not a local loopback cluster of the test DB')
    if (cfg.get('num_schools'), cfg.get('students_per_school'), cfg.get('devices_per_school')) != (10, 1000, 2):
        problems.append('preserved experiment.json is not the 10 × 1,000 × 2 dataset')
    if fx.get('experiment_id') != exp_id:
        problems.append('fixtures.json belongs to a different experiment')
    if problems:
        raise SystemExit('preserved experiment REFUSED: ' + '; '.join(problems))

    os.makedirs(root)
    os.chmod(root, 0o750)
    manifest.write_owner_marker(root, exp_id)
    for sub in ('secrets', 'logs', 'run', 'results'):
        os.makedirs(os.path.join(root, sub))
        manifest.write_owner_marker(os.path.join(root, sub), exp_id)
    os.chmod(os.path.join(root, 'secrets'), 0o700)

    new = {k: v for k, v in cfg.items() if k not in ('root', 'app_revision_full')}
    new.update({'app_revision': a.tested_sha, 'target_server': 'gunicorn',
                'gunicorn_env': dict(GUNICORN_ENV),
                'pg_data_dir': os.path.join(pre, 'pgdata'),
                'preserved_root': pre, 'run_id': a.run_id,
                'http_port': 18180, 'ws_port': 18188})
    with open(os.path.join(root, 'experiment.json'), 'w', encoding='utf-8') as fh:
        json.dump(new, fh, indent=2)
    sec_path = os.path.join(root, 'secrets', 'secrets.json')
    fd = os.open(sec_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(sec, fh, indent=2)
    shutil.copyfile(os.path.join(pre, 'run', 'fixtures.json'), os.path.join(root, 'run', 'fixtures.json'))

    manifest.save(root, {
        'experiment_id': exp_id, 'run_id': a.run_id,
        'purpose': 'VPS AI Face attendance + durable outbox load validation on the PRESERVED dataset',
        'created_at': manifest.utcnow_iso(), 'root': root, 'tool_dir': manifest.TOOL_DIR,
        'preserved_root': pre,
        'resources': [
            {'kind': 'directory', 'path': root, 'owner_marker': manifest.OWNERSHIP_MARKER_FILE,
             'recorded_at': manifest.utcnow_iso(), 'note': 'run root'},
            {'kind': 'reused-not-owned', 'path': os.path.join(pre, 'pgdata'),
             'recorded_at': manifest.utcnow_iso(),
             'note': 'PRESERVED 10k cluster of the round-1 experiment: reused, NEVER deleted by this run'},
            {'kind': 'file', 'path': sec_path, 'note': 'copy of round-1 test-only secrets; never reported'},
        ],
    })
    print(json.dumps({'root': root, 'experiment_id': exp_id, 'experiment_tag': tag,
                      'run_id': a.run_id, 'pg_port': new['pg_port'], 'db_name': new['db_name'],
                      'pg_data_dir': new['pg_data_dir'], 'tested_sha': a.tested_sha}, indent=2))


def identity(a):
    import environment_identity as ident
    import institute_common as ic
    root = os.path.abspath(a.root)
    cfg = json.load(open(os.path.join(root, 'experiment.json'), encoding='utf-8'))
    tag = cfg['experiment_id'].rsplit('-', 1)[-1]
    if not cfg.get('app_revision_full'):
        raise SystemExit('app_revision_full missing — run setup_env.py --steps src first')
    card = ident.build(dict(cfg, root=root), experiment_id=cfg['experiment_id'], tag=tag,
                       app_commit=cfg['app_revision_full'], worker_enabled=True,
                       prefixes=ic.experiment_prefixes(tag), production_network_reachable=False,
                       cleanup_manifest_path=os.path.join(root, 'manifest.json'))
    ident.write(root, card)
    if not card['database_isolated']:
        raise SystemExit(f"identity card says the database is not isolated: {card['database_isolation_reasons']}")
    print(json.dumps({k: card[k] for k in ('experiment_id', 'experiment_tag', 'app_source_commit',
                                          'database_host', 'database_name',
                                          'database_host_classification', 'database_isolated',
                                          'target_ws_port', 'firebase_mode')}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['create', 'identity'])
    ap.add_argument('--root', required=True)
    ap.add_argument('--preserved-root')
    ap.add_argument('--tested-sha')
    ap.add_argument('--run-id')
    a = ap.parse_args()
    if a.action == 'create':
        if not (a.preserved_root and a.tested_sha and a.run_id):
            raise SystemExit('create needs --preserved-root, --tested-sha and --run-id')
        create(a)
    else:
        identity(a)


if __name__ == '__main__':
    main()
