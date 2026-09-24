"""Drive the REAL institute attendance API and record an immutable ledger.

Endpoint (the application's own authenticated mobile interface, unmodified):

    POST /api/mobile/v1/teacher/institute/sessions/<session_id>/attendance
    Authorization: Bearer <instructor access token>
    {"records": [{"student_id": …, "status": "absent"}, …]}

Nothing is bypassed. The request goes through @jwt_required, @role_required
('teacher'), the institute context resolution, the instructor-assignment check
and the full submit_attendance() transaction — which is the code under test.
Raising throughput by skipping authorization would measure a system that does
not exist.

The ledger (results/<round>/institute_events.jsonl) is append-only, one JSON
object per line, with exactly the fields declared in
institute_common.LEDGER_FIELDS. There is no field that can hold a credential,
and ledger_record() raises on an undeclared key, so a token cannot be written
even by mistake.

This module NEVER runs as part of harness preparation. `plan` is pure and
offline; `run` requires an explicit --i-am-running-a-round flag.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import institute_common as ic  # noqa: E402

TOKENS_FILE = os.path.join('secrets', 'institute_tokens.json')


# ── Planning (pure, offline, no credentials, no I/O beyond reading fixtures) ──

def build_plan(cfg: dict, inst_fx: dict, *, waves: int, rate: float,
               absent_fraction: float = 0.5) -> list:
    """The full submission schedule. Deterministic; computes no side effects.

    One plan entry == one HTTP request == one logical transition. Entries are
    ordered by their scheduled offset so the driver only has to pace them.
    """
    entries = []
    seq = 0
    for wave in range(waves):
        for s_str, school in sorted(inst_fx['schools'].items(),
                                    key=lambda kv: int(kv[0])):
            s = int(s_str)
            for gidx, group in enumerate(school['groups']):
                local = ic.enrolled_local_indices(
                    s, gidx, cfg['students_per_school'])
                if not local:
                    continue
                absent_local = set(ic.absent_local_indices(
                    s, gidx, wave, cfg['students_per_school'], absent_fraction))
                by_local = dict(zip(local, group['student_db_ids']))
                status_map = {
                    by_local[j]: (ic.STATUS_ABSENT if j in absent_local
                                  else ic.STATUS_PRESENT)
                    for j in local if j in by_local
                }
                absent_ids = sorted(sid for sid, st in status_map.items()
                                    if st == ic.STATUS_ABSENT)
                entries.append({
                    'seq': seq,
                    'school_idx': s,
                    'school_id': school['school_id'],
                    'group_id': group['group_id'],
                    'session_id': group['session_id'],
                    'instructor_username': school['instructor_username'],
                    'wave': wave,
                    'student_ids': sorted(status_map),
                    'absent_student_ids': absent_ids,
                    'intended_status_map': status_map,
                    'transition_id': ic.transition_id(
                        group['session_id'], wave, sorted(status_map)),
                    'due_offset_s': round(seq / rate, 4) if rate > 0 else 0.0,
                })
                seq += 1
    return entries


def expected_outbox_jobs(plan: list, inst_fx: dict, cfg: dict) -> dict:
    """What the application MUST create, computed from the plan alone.

    Only a genuine transition into absent creates work, so the previous wave's
    absent set is carried forward per session, exactly as
    submit_attendance() does.
    """
    tokens_per_parent = ic.TOKENS_PER_PARENT
    if ic.INACTIVE_TOKEN_INDEX is not None:
        tokens_per_parent -= 1
    previous: dict = {}
    transitions = 0
    per_session: dict = {}
    for e in sorted(plan, key=lambda x: (x['wave'], x['seq'])):
        sid = e['session_id']
        before = previous.get(sid, set())
        now = set(e['absent_student_ids'])
        new = ic.newly_absent(before, now)
        previous[sid] = now
        transitions += len(new)
        per_session[sid] = per_session.get(sid, 0) + len(new)
    # One parent per student in this fixture set (seed.py links 1:1).
    return {
        'newly_absent_transitions': transitions,
        'expected_notifications': transitions * 1,
        'expected_jobs': transitions * 1 * tokens_per_parent,
        'active_tokens_per_parent': tokens_per_parent,
        'parents_per_student': 1,
        'per_session_transitions': per_session,
    }


# ── Instructor tokens (issued outside the timed round, never reported) ────────

def issue_tokens(root: str):
    """Re-exec inside the target venv and mint instructor access tokens.

    Uses the application's own encode_token() with the EXPERIMENT JWT secret,
    exactly as preauth.py does for parents. The production signing key is never
    involved. Output goes to secrets/, which reports never include.
    """
    import target
    cfg = common.load_config(root)
    env = target.build_env(cfg, common.load_secrets(root), ws_enabled=False)
    r = subprocess.run([target.venv_bin(root, 'python'),
                        os.path.abspath(__file__), '--root', root,
                        '--issue-tokens-inside'],
                       cwd=os.path.join(root, 'app_src'), env=env)
    raise SystemExit(r.returncode)


def _issue_tokens_inside(root: str):
    import datetime as dt
    from app import create_app
    from app.models import User
    from app.blueprints.mobile_api.utils import encode_token

    inst = json.load(open(os.path.join(root, 'run', 'institute_fixtures.json'),
                          encoding='utf-8'))
    usernames = [s['instructor_username'] for s in inst['schools'].values()]
    app = create_app('production')
    with app.app_context():
        users = {u.username: u for u in
                 User.query.execution_options(bypass_tenant_scope=True)
                 .filter(User.username.in_(usernames)).all()}
        missing = [u for u in usernames if u not in users]
        if missing:
            raise SystemExit(f'{len(missing)} instructor user(s) missing')
        tokens = {u: encode_token(users[u], 'access') for u in usernames}
    issued = dt.datetime.now(dt.timezone.utc)
    path = os.path.join(root, TOKENS_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'issued_at': issued.isoformat(),
                   'expires_at': (issued + dt.timedelta(hours=24)).isoformat(),
                   'method': 'app encode_token() with experiment-only '
                             'JWT_SECRET_KEY',
                   'tokens': tokens}, fh)
    print(f'issued {len(tokens)} instructor access tokens')


# ── Execution (never invoked during harness preparation) ─────────────────────

def run(root: str, *, out_dir: str, waves: int, rate: float,
        absent_fraction: float, timeout: float = 30.0):
    import urllib.error
    import urllib.request

    cfg = common.load_config(root)
    inst = json.load(open(os.path.join(root, 'run', 'institute_fixtures.json'),
                          encoding='utf-8'))
    with open(os.path.join(root, TOKENS_FILE), encoding='utf-8') as fh:
        tokens = json.load(fh)['tokens']

    plan = build_plan(cfg, inst, waves=waves, rate=rate,
                      absent_fraction=absent_fraction)
    os.makedirs(out_dir, exist_ok=True)
    ledger_path = os.path.join(out_dir, 'institute_events.jsonl')
    base = f"http://{cfg.get('target_host', '127.0.0.1')}:{cfg['http_port']}"

    expected = expected_outbox_jobs(plan, inst, cfg)
    with open(os.path.join(out_dir, 'institute_plan.json'), 'w',
              encoding='utf-8') as fh:
        json.dump({'entries': len(plan), 'rate_per_s': rate, 'waves': waves,
                   'expected': expected}, fh, indent=2)

    t0 = time.time()
    with open(ledger_path, 'a', encoding='utf-8') as ledger:
        for e in plan:
            due = t0 + e['due_offset_s']
            delay = due - time.time()
            if delay > 0:
                time.sleep(delay)
            body = json.dumps({'records': [
                {'student_id': sid, 'status': st}
                for sid, st in sorted(e['intended_status_map'].items())]}
            ).encode('utf-8')
            url = (f"{base}/api/mobile/v1/teacher/institute/sessions/"
                   f"{e['session_id']}/attendance")
            req = urllib.request.Request(url, data=body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('Authorization',
                           'Bearer ' + tokens[e['instructor_username']])
            started = time.time()
            status_code, keys, error = None, None, None
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status_code = resp.status
                    payload = json.loads(resp.read().decode('utf-8') or '{}')
                    keys = sorted(payload)
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                error = f'HTTP {exc.code}'
            except Exception as exc:
                error = type(exc).__name__
            rec = ic.ledger_record(
                experiment_id=cfg['experiment_id'], seq=e['seq'],
                school_idx=e['school_idx'], school_id=e['school_id'],
                group_id=e['group_id'], session_id=e['session_id'],
                wave=e['wave'], student_ids=e['student_ids'],
                absent_student_ids=e['absent_student_ids'],
                intended_status_map=e['intended_status_map'],
                transition_id=e['transition_id'],
                submitted_at=round(started - t0, 4),
                response_status=status_code, response_body_keys=keys,
                latency_ms=round((time.time() - started) * 1000, 2),
                attempt=1, retried=False, error=error)
            ledger.write(json.dumps(rec, ensure_ascii=False) + '\n')
            ledger.flush()
    print(json.dumps({'submitted': len(plan), 'ledger': ledger_path,
                      'expected': expected}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['plan', 'issue-tokens', 'run'])
    ap.add_argument('--root', required=True)
    ap.add_argument('--out', default=None)
    ap.add_argument('--waves', type=int, default=2)
    ap.add_argument('--rate', type=float, default=5.0)
    ap.add_argument('--absent-fraction', type=float, default=0.5)
    ap.add_argument('--issue-tokens-inside', action='store_true')
    ap.add_argument('--i-am-running-a-round', action='store_true',
                    help='required for `run`; prevents accidental execution')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.issue_tokens_inside:
        return _issue_tokens_inside(root)
    if a.action == 'issue-tokens':
        return issue_tokens(root)
    cfg = common.load_config(root)
    if a.action == 'plan':
        inst = json.load(open(os.path.join(root, 'run',
                                           'institute_fixtures.json'),
                              encoding='utf-8'))
        plan = build_plan(cfg, inst, waves=a.waves, rate=a.rate,
                          absent_fraction=a.absent_fraction)
        print(json.dumps({'entries': len(plan),
                          'expected': expected_outbox_jobs(plan, inst, cfg)},
                         indent=2))
        return
    if not a.i_am_running_a_round:
        raise SystemExit('refusing to generate load without '
                         '--i-am-running-a-round')
    run(root, out_dir=a.out or os.path.join(root, 'results', 'institute'),
        waves=a.waves, rate=a.rate, absent_fraction=a.absent_fraction)


if __name__ == '__main__':
    main()
