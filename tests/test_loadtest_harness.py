# -*- coding: utf-8 -*-
"""Safety tests for the institute-outbox load-test harness.

These test the HARNESS, not the system under load. Nothing here starts a
target, a worker or a generator; nothing connects to a database; nothing
touches a network. Every module under test was written so its safety-critical
logic is pure, which is what makes that possible.

What is proven here
───────────────────
  * synthetic identifiers are deterministic and carry the experiment tag
  * institute fixtures can only attach to experiment-owned schools, and
    enrollment never crosses a tenant boundary
  * fake device tokens belong only to experiment parents and schools
  * the fake Firebase REFUSES to import without the experiment markers, and
    performs no network I/O when it does
  * the expected-outbox-job arithmetic matches stage_absence_deliveries()
  * reconciliation catches loss, duplication, cross-school leakage and a
    non-empty queue
  * the worker's environment declares the outbox role, the local fake and the
    isolated database, and starts no shared background service
  * cleanup enumerates in dry run, and REFUSES against a production-shaped
    database, a wrong tag, a missing confirmation and an unowned resource
  * the new outbox guardrails fire, and the existing ones are unchanged
"""
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_DIR = os.path.join(REPO_ROOT, 'loadtest', 'attendance_probe')

pytestmark = pytest.mark.skipif(
    not os.path.isdir(TOOL_DIR),
    reason='loadtest/attendance_probe/ is not present in this checkout')

if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

import common                      # noqa: E402
import environment_identity as ident   # noqa: E402
import guard_rules                 # noqa: E402
import institute_common as ic      # noqa: E402
import institute_generator as igen  # noqa: E402
import outbox_reconcile as orec    # noqa: E402
import safety_gates                # noqa: E402

TAG = 'abc123'
EXPERIMENT_ID = f'attlt-20260924-{TAG}'


# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures — a complete, disposable experiment root on disk. No database.
# ═════════════════════════════════════════════════════════════════════════════

NUM_SCHOOLS = 3
STUDENTS_PER_SCHOOL = 40


def _cfg(root):
    return {
        'experiment_id': EXPERIMENT_ID,
        'root': root,
        'target_host': '127.0.0.1',
        'http_port': 18180,
        'ws_port': 18188,
        'pg_mode': 'existing',
        'pg_host': '127.0.0.1',
        'pg_port': 55480,
        'db_name': 'core_school_attendance_load_test',
        'app_revision': 'test',
        'num_schools': NUM_SCHOOLS,
        'students_per_school': STUDENTS_PER_SCHOOL,
        'devices_per_school': 2,
        'history_calendar_days': 5,
        'history_end_date': '2026-09-23',
        'school_timezone': 'Asia/Baghdad',
        'att_start_time': '07:00:00',
        'att_late_threshold': '07:45:00',
        'att_absence_threshold': '09:00:00',
        'att_departure_time': '13:00:00',
        'target_server': 'gunicorn',
        'gunicorn_env': {'WEB_CONCURRENCY': '1', 'GUNICORN_THREADS': '4'},
    }


def _institute_fixtures(cfg):
    """The shape institute_fixtures.py writes, built without a database."""
    out = {'experiment_id': EXPERIMENT_ID, 'test_date': '2026-09-24',
           'schools': {}, 'totals': {}}
    sid_seq, gid_seq, sess_seq, uid_seq = 1000, 2000, 3000, 4000
    for s in range(cfg['num_schools']):
        school_id = 100 + s
        groups = []
        for gidx in range(ic.GROUPS_PER_SCHOOL):
            locals_ = ic.enrolled_local_indices(s, gidx,
                                                cfg['students_per_school'])
            student_ids, parent_ids, ks = [], [], []
            for j in locals_:
                k = s + j * cfg['num_schools']
                sid_seq += 1
                uid_seq += 1
                student_ids.append(sid_seq)
                parent_ids.append(uid_seq)
                ks.append(k)
            gid_seq += 1
            sess_seq += 1
            groups.append({'group_id': gid_seq, 'session_id': sess_seq,
                           'group_name': ic.group_name(TAG, s, gidx),
                           'student_db_ids': student_ids,
                           'parent_user_ids': parent_ids,
                           'student_ks': ks})
        out['schools'][str(s)] = {
            'school_id': school_id, 'year_id': 200 + s,
            'instructor_username': ic.instructor_username(TAG, s),
            'instructor_user_id': 900 + s, 'instructor_employee_id': 950 + s,
            'subject_id': 300 + s, 'groups': groups,
            'token_rows': len({k for g in groups for k in g['student_ks']})
            * ic.TOKENS_PER_PARENT,
        }
    return out


@pytest.fixture
def experiment(tmp_path):
    """A fully-formed experiment root: manifest, identity, owner markers."""
    root = str(tmp_path / 'exp')
    os.makedirs(root)
    for sub in ('secrets', 'logs', 'run', 'results'):
        os.makedirs(os.path.join(root, sub))
        with open(os.path.join(root, sub, '.attlt_owner'), 'w') as fh:
            fh.write(EXPERIMENT_ID + '\n')
    with open(os.path.join(root, '.attlt_owner'), 'w') as fh:
        fh.write(EXPERIMENT_ID + '\n')

    cfg = _cfg(root)
    with open(os.path.join(root, 'experiment.json'), 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh)
    with open(os.path.join(root, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump({'experiment_id': EXPERIMENT_ID, 'root': root,
                   'tool_dir': TOOL_DIR, 'created_at': '2026-09-24T00:00:00+00:00',
                   'resources': [
                       {'kind': 'directory', 'path': root,
                        'owner_marker': '.attlt_owner'},
                       {'kind': 'directory', 'path': os.path.join(root, 'run'),
                        'owner_marker': '.attlt_owner'}]}, fh)
    with open(os.path.join(root, 'secrets', 'secrets.json'), 'w') as fh:
        json.dump({'pg_user': 'attlt_u', 'pg_password': 'x',
                   'app_secret_key': 'x' * 40, 'jwt_secret_key': 'y' * 40,
                   'ops_metrics_token': 'z' * 20, 'parent_password': 'p'}, fh)

    identity = ident.build(cfg, experiment_id=EXPERIMENT_ID, tag=TAG,
                           app_commit='deadbeef', worker_enabled=True,
                           prefixes=ic.experiment_prefixes(TAG),
                           cleanup_manifest_path=os.path.join(root, 'manifest.json'))
    ident.write(root, identity)
    inst = _institute_fixtures(cfg)
    with open(os.path.join(root, 'run', 'institute_fixtures.json'), 'w',
              encoding='utf-8') as fh:
        json.dump(inst, fh)
    return {'root': root, 'cfg': cfg, 'inst': inst, 'identity': identity}


# ═════════════════════════════════════════════════════════════════════════════
#  1. Deterministic experiment naming
# ═════════════════════════════════════════════════════════════════════════════

def test_identifiers_are_deterministic_and_tagged():
    for fn, args in ((ic.subject_code, (TAG, 4)),
                     (ic.group_name, (TAG, 4, 1)),
                     (ic.instructor_employee_id, (TAG, 4)),
                     (ic.instructor_username, (TAG, 4)),
                     (ic.device_token, (TAG, 77, 1))):
        first, second = fn(*args), fn(*args)
        assert first == second, f'{fn.__name__} is not deterministic'
        assert TAG in first, f'{fn.__name__} does not carry the experiment tag'


def test_every_prefix_carries_the_tag():
    for name, prefix in ic.experiment_prefixes(TAG).items():
        assert TAG in prefix, f'prefix {name}={prefix!r} lacks the tag'


def test_identifiers_of_two_experiments_never_collide():
    a, b = ic.experiment_prefixes('aaa111'), ic.experiment_prefixes('bbb222')
    for key in a:
        assert a[key] != b[key]
    assert ic.device_token('aaa111', 1, 0) != ic.device_token('bbb222', 1, 0)


# ═════════════════════════════════════════════════════════════════════════════
#  2 & 3. Synthetic-only fixtures, multi-school tenant isolation
# ═════════════════════════════════════════════════════════════════════════════

def test_group_enrollment_is_disjoint_within_a_school():
    seen = set()
    for gidx in range(ic.GROUPS_PER_SCHOOL):
        members = ic.enrolled_local_indices(0, gidx, STUDENTS_PER_SCHOOL)
        assert not (seen & set(members)), 'a student is enrolled twice'
        seen |= set(members)


def test_no_group_reaches_beyond_its_school_population():
    for gidx in range(ic.GROUPS_PER_SCHOOL):
        for j in ic.enrolled_local_indices(0, gidx, STUDENTS_PER_SCHOOL):
            assert 0 <= j < STUDENTS_PER_SCHOOL


def test_fixture_students_belong_to_exactly_one_school(experiment):
    """The student id of one school may never appear in another's group."""
    by_school = {}
    for s_str, school in experiment['inst']['schools'].items():
        ids = {sid for g in school['groups'] for sid in g['student_db_ids']}
        by_school[s_str] = ids
    all_ids = [sid for ids in by_school.values() for sid in ids]
    assert len(all_ids) == len(set(all_ids)), 'a student id spans two schools'
    for a in by_school:
        for b in by_school:
            if a != b:
                assert not (by_school[a] & by_school[b])


def test_school_student_partitioning_never_crosses_tenants():
    """institute_fixtures._school_students must return only that school's rows."""
    import institute_fixtures as ifx
    cfg = _cfg('/nonexistent')
    fx = {'students': [None] * (NUM_SCHOOLS * STUDENTS_PER_SCHOOL)}
    for k in range(len(fx['students'])):
        fx['students'][k] = {'student_db_id': 10_000 + k,
                             'parent_user_id': 20_000 + k}
    for s in range(NUM_SCHOOLS):
        got = ifx._school_students(fx, cfg, s)
        for j, rec in got.items():
            assert common.layout(cfg, rec['k'])['school_idx'] == s
            assert common.layout(cfg, rec['k'])['local_idx'] == j


def test_generator_plan_never_mixes_schools(experiment):
    plan = igen.build_plan(experiment['cfg'], experiment['inst'],
                           waves=2, rate=5.0)
    by_school = {}
    for e in plan:
        by_school.setdefault(e['school_id'], set()).update(e['student_ids'])
    schools = list(by_school)
    for i, a in enumerate(schools):
        for b in schools[i + 1:]:
            assert not (by_school[a] & by_school[b]), \
                'one submission batch spans two schools'


def test_generator_submits_only_to_experiment_sessions(experiment):
    known = {g['session_id'] for s in experiment['inst']['schools'].values()
             for g in s['groups']}
    plan = igen.build_plan(experiment['cfg'], experiment['inst'],
                           waves=3, rate=10.0)
    assert plan
    assert {e['session_id'] for e in plan} <= known


# ═════════════════════════════════════════════════════════════════════════════
#  4. Fake token isolation
# ═════════════════════════════════════════════════════════════════════════════

def test_fake_tokens_are_recognisable_and_never_real():
    tok = ic.device_token(TAG, 12345, 1)
    assert ic.is_experiment_token(TAG, tok)
    assert not ic.is_experiment_token('other1', tok)
    # A real FCM registration token is a long base64url string with a colon in
    # it and no 'LT<tag>-TOK-' marker. Structurally distinguishable.
    assert ':' not in tok
    assert tok.startswith(f'LT{TAG}-TOK-')


def test_every_fixture_token_belongs_to_an_experiment_parent(experiment):
    inst = experiment['inst']
    parents = {p for s in inst['schools'].values() for g in s['groups']
               for p in g['parent_user_ids']}
    ks = {k for s in inst['schools'].values() for g in s['groups']
          for k in g['student_ks']}
    assert parents and ks
    for k in ks:
        for d in range(ic.TOKENS_PER_PARENT):
            assert ic.is_experiment_token(TAG, ic.device_token(TAG, k, d))


def test_ownership_verification_rejects_a_foreign_row():
    """_verify_ownership must abort when any row sits outside the experiment."""
    import institute_fixtures as ifx

    class Cur:
        def __init__(self, counts):
            self.counts = list(counts)

        def scalar(self):
            return self.counts.pop(0)

    class Session:
        def __init__(self, counts):
            self.cur = Cur(counts)

        def execute(self, *a, **k):
            return self.cur

    class DB:
        def __init__(self, counts):
            self.session = Session(counts)

    def text(x):
        return x

    # All clean: four table checks then the token-prefix check.
    ifx._verify_ownership(DB([0, 0, 0, 0, 0]), text, {'x': 1}, TAG, {1, 2})
    # One institute group in a school we do not own.
    with pytest.raises(SystemExit) as exc:
        ifx._verify_ownership(DB([1, 0, 0, 0, 0]), text, {'x': 1}, TAG, {1, 2})
    assert 'outside experiment schools' in str(exc.value)
    # A device token that is not ours.
    with pytest.raises(SystemExit) as exc:
        ifx._verify_ownership(DB([0, 0, 0, 0, 3]), text, {'x': 1}, TAG, {1, 2})
    assert 'not tagged' in str(exc.value)


# ═════════════════════════════════════════════════════════════════════════════
#  5. Fake Firebase: refuses without markers, and never touches the network
# ═════════════════════════════════════════════════════════════════════════════

FAKE_DIR = os.path.join(TOOL_DIR, 'fake_firebase')


def _run_fake(code, env_extra, root=None):
    """Import the fake in a SUBPROCESS so it can never shadow firebase_admin
    for the rest of this test session."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() in ('PATH', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR',
                            'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'HOME',
                            'LANG', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA')}
    env['PYTHONPATH'] = FAKE_DIR
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    if root:
        env['ATTLT_EXPERIMENT_ROOT'] = root
    env.update(env_extra)
    return subprocess.run([sys.executable, '-c', code], env=env,
                          capture_output=True, text=True, timeout=120)


def test_fake_firebase_refuses_to_import_without_markers():
    r = _run_fake('import firebase_admin', {})
    assert r.returncode != 0
    assert 'ATTLT_FAKE_FIREBASE=1 is required' in (r.stdout + r.stderr)


def test_fake_firebase_refuses_on_an_ownership_mismatch(experiment):
    r = _run_fake('import firebase_admin',
                  {'ATTLT_FAKE_FIREBASE': '1',
                   'ATTLT_EXPERIMENT_ID': 'attlt-someone-else'},
                  root=experiment['root'])
    assert r.returncode != 0
    assert 'ownership marker does not match' in (r.stdout + r.stderr)


def test_fake_firebase_refuses_without_an_experiment_root():
    r = _run_fake('import firebase_admin',
                  {'ATTLT_FAKE_FIREBASE': '1',
                   'ATTLT_EXPERIMENT_ID': EXPERIMENT_ID})
    assert r.returncode != 0
    assert 'ATTLT_EXPERIMENT_ROOT is required' in (r.stdout + r.stderr)


FAKE_SEND_CODE = r'''
import json, socket, sys

class NoNetwork(RuntimeError):
    pass

def _blocked(*a, **k):
    raise NoNetwork('the fake firebase attempted network I/O')

# Every outbound path is armed BEFORE the fake is imported or used.
socket.socket = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

import firebase_admin
from firebase_admin import credentials, messaging

assert firebase_admin.ATTLT_FAKE is True
app = firebase_admin.initialize_app(credentials.Certificate('/does/not/exist'),
                                    options={'httpTimeout': 10.0})
msg = messaging.Message(
    token='LTabc123-TOK-00001-0',
    notification=messaging.Notification(title='t', body='b'),
    android=messaging.AndroidConfig(priority='high'),
    apns=messaging.APNSConfig(payload=messaging.APNSPayload(
        aps=messaging.Aps(sound='default'))),
    data={'a': '1'})
ids = [messaging.send(msg) for _ in range(3)]
led = firebase_admin.ledger()
print(json.dumps({
    'ids_are_strings': all(isinstance(i, str) and i for i in ids),
    'attempts': led.attempts,
    'successes': led.successes,
    'distinct': len(led.fingerprints),
    'token_in_output': any('LTabc123-TOK-00001-0' in i for i in ids),
    'repr_leaks_token': 'LTabc123-TOK-00001-0' in repr(msg),
}))
'''


def test_fake_firebase_sends_without_any_network_io(experiment):
    r = _run_fake(FAKE_SEND_CODE,
                  {'ATTLT_FAKE_FIREBASE': '1',
                   'ATTLT_EXPERIMENT_ID': EXPERIMENT_ID},
                  root=experiment['root'])
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'NoNetwork' not in (r.stdout + r.stderr)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out['ids_are_strings']
    assert out['attempts'] == 3 and out['successes'] == 3
    assert out['distinct'] == 1, 'the same token must fingerprint identically'
    assert not out['token_in_output'], 'a token leaked into the message id'
    assert not out['repr_leaks_token'], 'a token leaked into repr()'


def test_fake_firebase_ledger_stores_no_token(experiment):
    _run_fake(FAKE_SEND_CODE,
              {'ATTLT_FAKE_FIREBASE': '1',
               'ATTLT_EXPERIMENT_ID': EXPERIMENT_ID},
              root=experiment['root'])
    path = os.path.join(experiment['root'], 'run', 'fake_fcm_sends.json')
    assert os.path.exists(path)
    blob = open(path, encoding='utf-8').read()
    assert 'LTabc123-TOK-' not in blob, 'the send ledger persisted a token'
    data = json.loads(blob)
    assert data['attempts'] == 3
    assert data['duplicate_sends'] == 2


# ═════════════════════════════════════════════════════════════════════════════
#  6. Expected outbox job calculation
# ═════════════════════════════════════════════════════════════════════════════

def test_expected_jobs_is_one_per_active_token_per_parent():
    parents = {1: [10, 11], 2: [12]}
    tokens = {10: ['a', 'b'], 11: ['c'], 12: []}
    assert ic.expected_jobs_for_transitions([1], parents, tokens) == 3
    assert ic.expected_jobs_for_transitions([2], parents, tokens) == 0
    assert ic.expected_jobs_for_transitions([1, 2], parents, tokens) == 3


def test_a_parent_without_tokens_still_gets_an_in_app_notification():
    parents = {1: [10]}
    assert ic.expected_jobs_for_transitions([1], parents, {10: []}) == 0
    assert ic.expected_notifications_for_transitions([1], parents) == 1


def test_only_new_absences_count_as_transitions():
    assert ic.newly_absent({1, 2}, {1, 2}) == set()          # replay
    assert ic.newly_absent({1, 2}, {1, 2, 3}) == {3}         # one new
    assert ic.newly_absent({1, 2}, {1}) == set()             # absent → present
    assert ic.newly_absent(set(), {5}) == {5}                # first record


def test_generator_expectation_matches_the_transition_rule(experiment):
    plan = igen.build_plan(experiment['cfg'], experiment['inst'],
                           waves=2, rate=5.0, absent_fraction=0.5)
    exp = igen.expected_outbox_jobs(plan, experiment['inst'], experiment['cfg'])
    # Recompute independently from the plan, the way the reconciler will.
    previous, transitions = {}, 0
    for e in sorted(plan, key=lambda x: (x['wave'], x['seq'])):
        s = e['session_id']
        new = set(e['absent_student_ids']) - previous.get(s, set())
        transitions += len(new)
        previous[s] = set(e['absent_student_ids'])
    assert exp['newly_absent_transitions'] == transitions
    assert exp['expected_jobs'] == transitions * ic.TOKENS_PER_PARENT
    assert exp['expected_notifications'] == transitions


def test_a_pure_replay_wave_creates_no_new_jobs(experiment):
    """Submitting the same wave twice must add nothing."""
    plan = igen.build_plan(experiment['cfg'], experiment['inst'],
                           waves=1, rate=5.0)
    doubled = plan + [dict(e, seq=e['seq'] + len(plan)) for e in plan]
    once = igen.expected_outbox_jobs(plan, experiment['inst'], experiment['cfg'])
    twice = igen.expected_outbox_jobs(doubled, experiment['inst'],
                                      experiment['cfg'])
    assert twice['expected_jobs'] == once['expected_jobs']


def test_the_ledger_cannot_hold_a_credential():
    with pytest.raises(ValueError):
        ic.ledger_record(experiment_id='x', seq=1, authorization='Bearer abc')
    rec = ic.ledger_record(experiment_id='x', seq=1)
    assert set(rec) == set(ic.LEDGER_FIELDS)
    assert not any('token' in k or 'auth' in k for k in rec)


# ═════════════════════════════════════════════════════════════════════════════
#  7 & 8. Reconciliation: dedup, state machine, loss, leakage
# ═════════════════════════════════════════════════════════════════════════════

def _observed(**over):
    base = {
        'attendance_records_total': 20, 'attendance_absent_records': 10,
        'attendance_unexpected': 0, 'attendance_duplicate_logical': 0,
        'attendance_cross_school': 0, 'sessions_recorded': 3,
        'notification_rows': 10, 'notification_cross_school': 0,
        'outbox_status_counts': {'sent': 20},
        'outbox_distinct_dedup_keys': 20, 'outbox_cross_school': 0,
        'transitions_missing_job': 0, 'jobs_without_transition': 0,
        'fake_attempts': 20, 'fake_successes': 20, 'fake_failures': 0,
        'fake_distinct': 20, 'fake_duplicate_sends': 0, 'worker_reclaims': 0,
    }
    base.update(over)
    return base


def _expected(transitions=10, tokens=2):
    return {'intended_submissions': 6, 'accepted_submissions': 6,
            'rejected_submissions': 0, 'replay_submissions': 0,
            'newly_absent_transitions': transitions,
            'expected_notifications': transitions,
            'expected_jobs': transitions * tokens}


def test_a_perfect_round_reports_no_violations():
    r = orec.verdict(orec.compute(_expected(), _observed()), drain_complete=True)
    assert r['violations'] == [], r['violations']
    assert r['correct'] is True


def test_reconciliation_detects_attendance_loss():
    r = orec.verdict(orec.compute(_expected(),
                                  _observed(attendance_absent_records=9)),
                     drain_complete=True)
    assert any('committed_expected != committed_actual' in v
               for v in r['violations'])
    assert any('missing_attendance' in v for v in r['violations'])


def test_reconciliation_detects_duplicate_attendance():
    r = orec.verdict(orec.compute(_expected(),
                                  _observed(attendance_duplicate_logical=1)),
                     drain_complete=True)
    assert any('unexpected_duplicates' in v for v in r['violations'])


def test_reconciliation_detects_cross_school_leakage():
    for key, needle in (('attendance_cross_school', 'cross_school_attendance'),
                        ('notification_cross_school', 'cross_school_recipients'),
                        ('outbox_cross_school', 'cross_school_jobs')):
        r = orec.verdict(orec.compute(_expected(), _observed(**{key: 1})),
                         drain_complete=True)
        assert any(needle in v for v in r['violations']), key


def test_reconciliation_detects_a_dedup_key_collision():
    """Fewer distinct keys than rows means the unique index did not hold."""
    r = orec.verdict(orec.compute(_expected(),
                                  _observed(outbox_distinct_dedup_keys=19)),
                     drain_complete=True)
    assert any('distinct dedup keys' in v for v in r['violations'])


def test_reconciliation_detects_a_missing_job_for_a_real_transition():
    r = orec.verdict(orec.compute(_expected(),
                                  _observed(transitions_missing_job=2)),
                     drain_complete=True)
    assert any('transitions_missing_job' in v for v in r['violations'])


def test_state_machine_total_is_the_sum_of_every_state():
    obs = _observed(outbox_status_counts={'pending': 4, 'processing': 1,
                                          'retry': 2, 'sent': 13})
    rep = orec.compute(_expected(), obs)
    o = rep['outbox']
    assert o['actual_jobs'] == 20
    assert o['backlog'] == 7
    assert sum(o[s] for s in orec.ALL_STATUSES) == o['actual_jobs']


def test_a_backlog_is_fine_mid_round_but_not_after_drain():
    obs = _observed(outbox_status_counts={'pending': 7, 'sent': 13},
                    outbox_distinct_dedup_keys=20)
    mid = orec.verdict(orec.compute(_expected(), obs), drain_complete=False)
    assert mid['violations'] == [], mid['violations']
    after = orec.verdict(orec.compute(_expected(), obs), drain_complete=True)
    assert any('pending' in v for v in after['violations'])
    assert any('sent' in v for v in after['violations'])


def test_a_dead_job_fails_the_drain():
    obs = _observed(outbox_status_counts={'sent': 19, 'dead': 1})
    r = orec.verdict(orec.compute(_expected(), obs), drain_complete=True)
    assert any('dead' in v for v in r['violations'])


def test_duplicate_sends_are_explained_by_reclaims_not_assumed_impossible():
    obs = _observed(fake_attempts=22, fake_duplicate_sends=2, worker_reclaims=2)
    r = orec.verdict(orec.compute(_expected(), obs), drain_complete=True)
    assert r['fake_firebase']['unexplained_duplicate_sends'] == 0
    assert r['violations'] == []
    obs = _observed(fake_attempts=22, fake_duplicate_sends=2, worker_reclaims=0)
    r = orec.verdict(orec.compute(_expected(), obs), drain_complete=True)
    assert any('unexplained_duplicate_sends' in v for v in r['violations'])


def test_the_report_never_claims_exactly_once():
    r = orec.verdict(orec.compute(_expected(), _observed()), drain_complete=True)
    assert 'at-least-once' in r['delivery_guarantee']
    assert 'NOT exactly-once' in r['delivery_guarantee']


def test_expected_from_ledger_ignores_rejected_submissions():
    rows = [
        {'seq': 0, 'wave': 0, 'session_id': 1, 'absent_student_ids': [1, 2],
         'response_status': 200, 'error': None},
        {'seq': 1, 'wave': 0, 'session_id': 2, 'absent_student_ids': [3],
         'response_status': 500, 'error': 'HTTP 500'},
        {'seq': 2, 'wave': 1, 'session_id': 1, 'absent_student_ids': [1, 2, 4],
         'response_status': 200, 'error': None},
    ]
    exp = orec.expected_from_ledger(rows, tokens_per_parent=2)
    assert exp['accepted_submissions'] == 2
    assert exp['rejected_submissions'] == 1
    assert exp['newly_absent_transitions'] == 3      # 1,2 then 4
    assert exp['expected_jobs'] == 6


# ═════════════════════════════════════════════════════════════════════════════
#  9 & 14. Worker environment construction and lifecycle isolation
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def worker_env(experiment):
    import worker_control as wc
    sec = json.load(open(os.path.join(experiment['root'], 'secrets',
                                      'secrets.json')))
    return wc.build_worker_env(experiment['cfg'], sec, batch_size=1,
                               poll_seconds=5.0, lease_seconds=30)


def test_worker_env_declares_the_outbox_role_and_enables_the_feature(worker_env):
    assert worker_env['MECHA_PROCESS_ROLE'] == 'outbox-worker'
    assert worker_env['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] == 'true'
    assert worker_env['FLASK_ENV'] == 'production'


def test_worker_env_selects_the_local_fake_firebase(worker_env, experiment):
    assert worker_env['ATTLT_FAKE_FIREBASE'] == '1'
    assert worker_env['ATTLT_EXPERIMENT_ID'] == EXPERIMENT_ID
    assert worker_env['PYTHONPATH'].split(os.pathsep)[0].endswith('fake_firebase')
    gac = worker_env['GOOGLE_APPLICATION_CREDENTIALS']
    assert gac.startswith(experiment['root']), \
        'the credential path must stay inside the experiment root'


def test_worker_env_carries_no_production_credential(worker_env):
    for var in safety_gates.PRODUCTION_CREDENTIAL_VARS:
        assert not (worker_env.get(var) or '').strip(), f'{var} is set'


def test_worker_env_disables_every_shared_background_service(worker_env):
    for var, want in safety_gates.REQUIRED_OFF.items():
        assert worker_env[var].lower() == want, var
    assert worker_env['AIFACE_WS_ENABLED'] == 'false'


def test_worker_env_uses_only_existing_throughput_knobs(worker_env):
    """Mode B must be configuration, never a sleep patched into the app."""
    assert worker_env['OUTBOX_BATCH_SIZE'] == '1'
    assert worker_env['OUTBOX_POLL_SECONDS'] == '5.0'
    assert worker_env['OUTBOX_LEASE_SECONDS'] == '30'


def test_experiment_lease_never_touches_the_production_default():
    import worker_control as wc
    unit = os.path.join(REPO_ROOT, 'deploy',
                        'mecha-school-outbox-worker.service')
    text = open(unit, encoding='utf-8').read()
    assert 'OUTBOX_LEASE_SECONDS=300' in text, \
        'the production unit default changed'
    assert wc.EXPERIMENT_LEASE_SECONDS != 300


def test_the_worker_role_may_not_start_background_services():
    """The lifecycle gate the harness relies on, asserted against real code."""
    from app import lifecycle
    assert lifecycle.ROLE_OUTBOX_WORKER not in lifecycle._BACKGROUND_SERVICE_ROLES
    assert lifecycle._BACKGROUND_SERVICE_ROLES == frozenset({lifecycle.ROLE_WEB})


def test_worker_command_line_runs_only_the_outbox_module():
    src = open(os.path.join(TOOL_DIR, 'worker_control.py'), encoding='utf-8').read()
    assert "'-m',\n           'app.services.outbox_worker', 'run'" in src \
        or "'app.services.outbox_worker', 'run'" in src
    for forbidden in ('gunicorn', 'wsgi:application', 'ai_face', '7788'):
        assert forbidden not in src, f'{forbidden!r} in the worker supervisor'


# ═════════════════════════════════════════════════════════════════════════════
#  Startup gates
# ═════════════════════════════════════════════════════════════════════════════

def test_gates_pass_for_a_correct_worker_environment(experiment, worker_env):
    out = safety_gates.run_all(
        env=worker_env, cfg=experiment['cfg'], identity=experiment['identity'],
        prefixes=ic.experiment_prefixes(TAG), role='worker',
        netns_proof_ok=True, exists=lambda p: False)
    assert out['ok'] is True


def test_gates_refuse_without_a_network_isolation_proof(experiment, worker_env):
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(
            env=worker_env, cfg=experiment['cfg'],
            identity=experiment['identity'],
            prefixes=ic.experiment_prefixes(TAG), role='worker',
            netns_proof_ok=None, exists=lambda p: False)
    assert 'network isolation has not been proven' in str(exc.value)


def test_gates_refuse_a_production_database(experiment, worker_env):
    cfg = dict(experiment['cfg'], pg_host='aws-1.pooler.supabase.com',
               db_name='postgres')
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(env=worker_env, cfg=cfg,
                             identity=experiment['identity'],
                             prefixes=ic.experiment_prefixes(TAG),
                             role='worker', netns_proof_ok=True,
                             exists=lambda p: False)
    assert 'production-like' in str(exc.value)


def test_gates_refuse_the_production_ws_port(experiment, worker_env):
    cfg = dict(experiment['cfg'], ws_port=7788)
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(env=worker_env, cfg=cfg,
                             identity=experiment['identity'],
                             prefixes=ic.experiment_prefixes(TAG),
                             role='worker', netns_proof_ok=True,
                             exists=lambda p: False)
    assert '7788' in str(exc.value)


def test_gates_refuse_a_reachable_dotenv(experiment, worker_env):
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(env=worker_env, cfg=experiment['cfg'],
                             identity=experiment['identity'],
                             prefixes=ic.experiment_prefixes(TAG),
                             role='worker', netns_proof_ok=True,
                             exists=lambda p: p.endswith('.env'))
    assert 'load_dotenv' in str(exc.value)


def test_gates_refuse_a_real_firebase_credential(experiment, worker_env):
    env = dict(worker_env, FIREBASE_SERVICE_ACCOUNT_JSON='{"private_key":"x"}')
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(env=env, cfg=experiment['cfg'],
                             identity=experiment['identity'],
                             prefixes=ic.experiment_prefixes(TAG),
                             role='worker', netns_proof_ok=True,
                             exists=lambda p: False)
    assert 'FIREBASE_SERVICE_ACCOUNT_JSON' in str(exc.value)


def test_gates_refuse_a_credential_outside_the_experiment_root(experiment,
                                                               worker_env):
    env = dict(worker_env,
               GOOGLE_APPLICATION_CREDENTIALS='/etc/mecha-school/firebase-key.json')
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(env=env, cfg=experiment['cfg'],
                             identity=experiment['identity'],
                             prefixes=ic.experiment_prefixes(TAG),
                             role='worker', netns_proof_ok=True,
                             exists=lambda p: False)
    assert 'outside the experiment root' in str(exc.value)


def test_gates_refuse_on_the_vps_without_a_proven_sentinel(experiment,
                                                           worker_env):
    with pytest.raises(SystemExit) as exc:
        safety_gates.run_all(env=worker_env, cfg=experiment['cfg'],
                             identity=experiment['identity'],
                             prefixes=ic.experiment_prefixes(TAG),
                             role='worker', netns_proof_ok=True, on_vps=True,
                             sentinel_running=None, exists=lambda p: False)
    assert 'sentinel' in str(exc.value)


# ═════════════════════════════════════════════════════════════════════════════
#  Environment identity
# ═════════════════════════════════════════════════════════════════════════════

def test_identity_classifies_hosts_correctly():
    assert ident.classify_db_host('127.0.0.1') == 'loopback'
    assert ident.classify_db_host('db') == 'container'
    assert ident.classify_db_host('x.pooler.supabase.com') == 'production-like'
    assert ident.classify_db_host('') == 'unknown'
    assert ident.classify_db_host('some.random.host') == 'unknown'


def test_only_loopback_or_container_with_a_test_name_is_isolated():
    assert ident.is_isolated_db('127.0.0.1', 'core_school_attendance_load_test')[0]
    assert ident.is_isolated_db('db', 'anything_test')[0]
    assert not ident.is_isolated_db('127.0.0.1', 'mecha_school')[0]
    assert not ident.is_isolated_db('some.random.host', 'x_test')[0]


def test_identity_file_contains_no_credential(experiment):
    blob = open(ident.identity_path(experiment['root']), encoding='utf-8').read()
    for bad in ('password', 'secret', 'private_key', 'jwt'):
        assert bad not in blob.lower()
    assert experiment['identity']['firebase_mode'] == 'fake-local'
    assert experiment['identity']['production_network_reachable'] is False


def test_identity_write_refuses_to_persist_a_credential(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, 'run'), exist_ok=True)
    with pytest.raises(SystemExit):
        ident.write(root, {'experiment_id': 'x', 'pg_password': 'hunter2'})


def test_identity_verify_rejects_a_tag_mismatch(experiment):
    with pytest.raises(SystemExit):
        ident.verify(experiment['root'], experiment_id=EXPERIMENT_ID,
                     tag='wrong1')


# ═════════════════════════════════════════════════════════════════════════════
#  10–13. Cleanup
# ═════════════════════════════════════════════════════════════════════════════

def _cleanup(root, *args):
    return subprocess.run(
        [sys.executable, os.path.join(TOOL_DIR, 'cleanup.py'),
         '--root', root, *args],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})


def test_cleanup_dry_run_removes_nothing_and_enumerates(experiment):
    root = experiment['root']
    r = _cleanup(root)
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout[r.stdout.index('{'):])
    assert payload['executed'] is False
    assert 'DRY RUN' in payload['mode']
    assert any(item.startswith('[plan] ') for item in payload['deleted'])
    assert os.path.isdir(os.path.join(root, 'run')), 'dry run deleted a directory'
    assert os.path.exists(os.path.join(root, 'manifest.json'))


def test_cleanup_refuses_without_the_confirm_flag(experiment):
    r = _cleanup(experiment['root'], '--execute', '--tag', TAG)
    assert r.returncode != 0
    assert '--confirm was not given' in (r.stdout + r.stderr)
    assert os.path.isdir(os.path.join(experiment['root'], 'run'))


def test_cleanup_refuses_without_the_exact_tag(experiment):
    r = _cleanup(experiment['root'], '--execute', '--confirm')
    assert r.returncode != 0
    assert '--tag' in (r.stdout + r.stderr)
    r = _cleanup(experiment['root'], '--execute', '--confirm',
                 '--tag', 'not-the-tag')
    assert r.returncode != 0
    assert 'does not match this experiment' in (r.stdout + r.stderr)
    assert os.path.isdir(os.path.join(experiment['root'], 'run'))


def test_cleanup_refuses_against_a_production_like_database(experiment):
    root = experiment['root']
    cfg = dict(experiment['cfg'], pg_host='db.abcdefgh.supabase.co',
               db_name='postgres')
    with open(os.path.join(root, 'experiment.json'), 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh)
    identity = dict(experiment['identity'],
                    database_host=cfg['pg_host'], database_name=cfg['db_name'])
    ident.write(root, identity)
    r = _cleanup(root, '--execute', '--confirm', '--tag', TAG, '--delete-db')
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert 'REFUSED' in out and 'not provably isolated' in out
    assert os.path.isdir(os.path.join(root, 'run')), 'a refusal deleted something'


def test_cleanup_refuses_when_the_identity_file_is_missing(experiment):
    os.remove(ident.identity_path(experiment['root']))
    r = _cleanup(experiment['root'], '--execute', '--confirm', '--tag', TAG)
    assert r.returncode != 0
    assert 'environment identity' in (r.stdout + r.stderr)


def test_cleanup_refuses_an_unowned_manifest_resource(experiment, tmp_path):
    root = experiment['root']
    foreign = str(tmp_path / 'not-ours')
    os.makedirs(foreign)
    m = json.load(open(os.path.join(root, 'manifest.json')))
    m['resources'].append({'kind': 'directory', 'path': foreign,
                           'owner_marker': '.attlt_owner'})
    with open(os.path.join(root, 'manifest.json'), 'w') as fh:
        json.dump(m, fh)
    r = _cleanup(root, '--execute', '--confirm', '--tag', TAG)
    assert r.returncode != 0
    assert 'not owned by this manifest' in (r.stdout + r.stderr)
    assert os.path.isdir(foreign), 'a refusal touched a foreign directory'


def test_cleanup_deletes_only_manifest_owned_directories(experiment, tmp_path):
    root = experiment['root']
    # A directory inside the root that the experiment did NOT mark as its own.
    unmarked = os.path.join(root, 'logs')
    os.remove(os.path.join(unmarked, '.attlt_owner'))
    # And one entirely outside it.
    outside = str(tmp_path / 'untouched')
    os.makedirs(outside)
    open(os.path.join(outside, 'keep.txt'), 'w').close()

    r = _cleanup(root, '--execute', '--confirm', '--tag', TAG)
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout[r.stdout.index('{'):])
    assert payload['gate']['gate'] == 'PASS'

    assert os.path.isdir(outside) and os.path.exists(
        os.path.join(outside, 'keep.txt')), 'cleanup left its own root'
    assert os.path.isdir(unmarked), 'an unmarked directory was deleted'
    assert any('owner marker missing' in s for s in payload['skipped'])
    # run/ carried the marker, so it is gone; results/ is preserved by default.
    assert not os.path.exists(os.path.join(root, 'run'))
    assert os.path.isdir(os.path.join(root, 'results'))


def _executable_strings(path):
    """Every string literal the module can actually execute.

    Docstrings are excluded deliberately: cleanup.py's documentation names the
    dangerous operations in order to say it does not use them, and a naive
    substring scan would read that as the operation itself.
    """
    import ast
    tree = ast.parse(open(path, encoding='utf-8').read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


def test_cleanup_never_contains_a_wildcard_row_delete():
    path = os.path.join(TOOL_DIR, 'cleanup.py')
    strings = _executable_strings(path)
    assert strings, 'no string literals parsed — the scan would be vacuous'
    for s in strings:
        low = s.lower()
        for forbidden in ('delete from', 'truncate', 'like %', "like '%",
                          'drop schema', 'git clean', 'docker prune',
                          'drop table'):
            assert forbidden not in low, \
                f'{forbidden!r} in an executable string of cleanup.py: {s[:80]!r}'
    # DROP DATABASE is permitted, but only against the manifest-verified name,
    # never a pattern.
    drops = [s for s in strings if 'drop database' in s.lower()]
    for s in drops:
        assert '%' not in s and 'like' not in s.lower(), s
    src = open(path, encoding='utf-8').read()
    assert 'DROP DATABASE' in src, 'the database drop path vanished'


# ═════════════════════════════════════════════════════════════════════════════
#  15. Watchdog / guardrails
# ═════════════════════════════════════════════════════════════════════════════

def test_existing_guardrails_are_unchanged():
    th = guard_rules.build_thresholds(min_mem_pct=20.0)
    assert th['host_cpu_pct'] == 85.0
    assert th['mem_available_floor_pct'] == 20.0
    assert th['db_connections_frac_of_max'] == 0.9
    assert th['error_rate'] == 0.01
    assert th['ack_p95_ms'] == 5000
    assert th['parent_read_p95_ms'] == 2000
    assert th['compliance_mode'] == guard_rules.COMPLIANCE_ENFORCED


def test_outbox_thresholds_are_additive():
    th = guard_rules.build_thresholds()
    for key in guard_rules.OUTBOX_THRESHOLDS:
        assert key in th
    assert th['outbox_dead_jobs_allowed'] == 0
    assert th['outbox_backlog_ceiling'] > 0


def test_a_healthy_sample_produces_no_outbox_stop_reason():
    th = guard_rules.build_thresholds()
    sample = {'outbox_backlog': 120, 'outbox_dead': 0, 'outbox_cancelled': 0,
              'outbox_backlog_falling': True}
    assert guard_rules.outbox_breaches(sample, th, draining=True,
                                       sustained=None) == []


def test_outbox_guardrails_fire():
    th = guard_rules.build_thresholds()
    cases = (
        ({'outbox_backlog': th['outbox_backlog_ceiling'] + 1}, 'backlog'),
        ({'outbox_dead': 1}, 'dead'),
        ({'outbox_cancelled': 1}, 'cancelled'),
        ({'unplanned_worker_restarts': 1}, 'restarted'),
        ({'db_operational_errors': 1}, 'OperationalError'),
        ({'db_pool_timeouts': 1}, 'pool timeout'),
        ({'worker_tracebacks': 1}, 'traceback'),
        ({'isolation_violations': 1}, 'ISOLATION VIOLATION'),
    )
    for sample, needle in cases:
        reasons = guard_rules.outbox_breaches(sample, th, draining=False,
                                              sustained=None)
        assert any(needle in r for r in reasons), (sample, reasons)


def test_a_stuck_backlog_halts_only_after_the_window():
    th = guard_rules.build_thresholds()
    now = [0.0]
    sustained = guard_rules.SustainedBreach(lambda: now[0])
    sample = {'outbox_backlog': 500, 'outbox_backlog_falling': False}
    assert guard_rules.outbox_breaches(sample, th, draining=True,
                                       sustained=sustained) == []
    now[0] = th['outbox_no_drain_window_s'] + 1
    reasons = guard_rules.outbox_breaches(sample, th, draining=True,
                                          sustained=sustained)
    assert any('stuck' in r for r in reasons)
    # Once it starts falling again the breach resets.
    now[0] += 1
    assert guard_rules.outbox_breaches(
        {'outbox_backlog': 400, 'outbox_backlog_falling': True}, th,
        draining=True, sustained=sustained) == []


# ═════════════════════════════════════════════════════════════════════════════
#  Repository hygiene: nothing generated or secret may become trackable
# ═════════════════════════════════════════════════════════════════════════════

def test_no_secret_or_result_artifact_is_tracked():
    out = subprocess.run(['git', 'ls-files', 'loadtest/'], cwd=REPO_ROOT,
                         capture_output=True, text=True, timeout=120)
    tracked = [p for p in out.stdout.splitlines() if p.strip()]
    forbidden = ('secrets.json', 'tokens.json', '.env', 'firebase-key.json',
                 'fake_fcm_sends.json', 'environment_identity.json',
                 'manifest.json', 'experiment.json', 'fixtures.json')
    for path in tracked:
        base = os.path.basename(path)
        assert base not in forbidden, f'{path} must never be tracked'
        assert not path.endswith(('.log', '.jsonl', '.bundle', '.tgz')), path
        assert '/out/' not in path and '/round2/' not in path, path
