"""Fail-closed startup gates. Nothing starts until every one of these passes.

Each gate is a pure function over an environment dict, an identity record and
a configuration dict, so the whole safety boundary can be unit-tested without
building an environment or touching a network.

Two rules govern this module:
  * A gate never auto-corrects. It reports the violation and the caller stops.
  * A gate that cannot evaluate its condition FAILS. Absence of proof is not
    proof of isolation.
"""
from __future__ import annotations

import os

import environment_identity as ident

# Variables that must be empty/absent in any experiment process. A non-empty
# value means a real external service is reachable.
PRODUCTION_CREDENTIAL_VARS = (
    'FIREBASE_SERVICE_ACCOUNT_JSON',
    'FCM_SERVICE_ACCOUNT_JSON',
    'SUPABASE_URL',
    'SUPABASE_SERVICE_ROLE_KEY',
    'SUPABASE_SERVICE_KEY',
    'SUPABASE_BUCKET',
    'REDIS_URL',
    'SENTRY_DSN',
    'HIKVISION_HOST',
    'HIKVISION_USER',
    'HIKVISION_PASSWORD',
)

# Background services that must be switched off in every experiment process.
REQUIRED_OFF = {
    'ATTENDANCE_SCHEDULER_DISABLED': 'true',
    'FEE_REMINDER_SCHEDULER_DISABLED': 'true',
    'HIKVISION_AUTO_SYNC': 'false',
    'DURABLE_PUSH_QUEUE_ENABLED': 'false',
    'SYNC_JOURNAL_ENABLED': 'false',
    'SYNC_SIGNAL_ENABLED': 'false',
}


class GateFailed(SystemExit):
    pass


def _fail(reasons):
    raise GateFailed('startup gate REFUSED:\n  - ' + '\n  - '.join(reasons))


def gate_database_is_not_production(env: dict, cfg: dict) -> list:
    reasons = []
    isolated, why = ident.is_isolated_db(cfg.get('pg_host', ''),
                                         cfg.get('db_name', ''))
    if not isolated:
        reasons.extend(why)
    for var in ('DATABASE_URL', 'SQLALCHEMY_DATABASE_URI', 'TEST_DATABASE_URL'):
        url = (env.get(var) or '').strip()
        if url and ident.looks_like_production_url(url):
            # The URL itself is never echoed: it carries a password.
            reasons.append(f'{var} does not point at a provably isolated database')
    return reasons


def gate_no_production_dotenv(cfg: dict, exists=os.path.exists) -> list:
    """load_dotenv() walks upward. No .env may be reachable from app_src."""
    reasons = []
    d = os.path.join(cfg.get('root', ''), 'app_src', 'config')
    seen = set()
    while d and d not in seen:
        seen.add(d)
        if exists(os.path.join(d, '.env')):
            reasons.append(f'.env reachable at {d} — load_dotenv() could read it')
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return reasons


def gate_no_production_credentials(env: dict) -> list:
    reasons = []
    for var in PRODUCTION_CREDENTIAL_VARS:
        if (env.get(var) or '').strip():
            reasons.append(f'{var} is set — an experiment process must not have it')
    gac = (env.get('GOOGLE_APPLICATION_CREDENTIALS') or '').strip()
    if gac:
        # Permitted ONLY when it is the experiment's own obviously-fake file.
        root = os.path.abspath(env.get('ATTLT_EXPERIMENT_ROOT') or '')
        if not root or not os.path.abspath(gac).startswith(root + os.sep):
            reasons.append('GOOGLE_APPLICATION_CREDENTIALS points outside the '
                           'experiment root')
    return reasons


def gate_ports(cfg: dict) -> list:
    reasons = []
    ws = cfg.get('ws_port')
    http = cfg.get('http_port')
    if ws == ident.PRODUCTION_WS_PORT:
        reasons.append(f'WS port is the production AI Face port {ws}')
    if http in ident.PRODUCTION_HTTP_PORTS:
        reasons.append(f'HTTP port {http} is a production-shaped port')
    if ws is None or http is None:
        reasons.append('target ports are not configured')
    return reasons


def gate_background_services_off(env: dict) -> list:
    reasons = []
    for var, want in REQUIRED_OFF.items():
        got = (env.get(var) or '').strip().lower()
        if got != want:
            reasons.append(f'{var}={got!r}, must be {want!r}')
    return reasons


def gate_worker_role(env: dict) -> list:
    """The outbox worker must declare its role and never inherit 'web'."""
    role = (env.get('MECHA_PROCESS_ROLE') or '').strip()
    if role != 'outbox-worker':
        return [f'MECHA_PROCESS_ROLE={role!r}, must be "outbox-worker"']
    return []


def gate_fake_firebase_selected(env: dict) -> list:
    reasons = []
    if env.get('ATTLT_FAKE_FIREBASE') != '1':
        reasons.append('ATTLT_FAKE_FIREBASE is not 1 — the fake would not load')
    if not (env.get('ATTLT_EXPERIMENT_ID') or '').strip():
        reasons.append('ATTLT_EXPERIMENT_ID is empty')
    if not (env.get('ATTLT_EXPERIMENT_ROOT') or '').strip():
        reasons.append('ATTLT_EXPERIMENT_ROOT is empty')
    path = env.get('PYTHONPATH') or ''
    if 'fake_firebase' not in path:
        reasons.append('fake_firebase is not on PYTHONPATH — the real '
                       'firebase_admin could be imported instead')
    return reasons


def gate_prefixes_match(identity: dict, prefixes: dict) -> list:
    reasons = []
    pairs = (('synthetic_school_prefix', 'school'),
             ('synthetic_device_prefix', 'device'),
             ('synthetic_token_prefix', 'device_token'))
    for key, name in pairs:
        if identity.get(key) != prefixes.get(name):
            reasons.append(f'{key} does not match the experiment tag')
    return reasons


def gate_sentinel_active(on_vps: bool, sentinel_running: bool | None) -> list:
    """On the VPS the production health sentinel must be proven running."""
    if not on_vps:
        return []
    if sentinel_running is None:
        return ['production health sentinel state unknown — cannot proceed on '
                'the VPS without proof it is running']
    return [] if sentinel_running else ['production health sentinel is not running']


def run_all(*, env: dict, cfg: dict, identity: dict, prefixes: dict,
            role: str, on_vps: bool = False,
            sentinel_running: bool | None = None,
            netns_proof_ok: bool | None = None,
            exists=os.path.exists) -> dict:
    """Every gate. Raises GateFailed on the first complete evaluation that has
    any violation — all reasons are reported together, not one at a time.

    `role` is 'target' or 'worker'; the fake-Firebase and worker-role gates
    apply to the worker only.
    """
    reasons = []
    reasons += gate_database_is_not_production(env, cfg)
    reasons += gate_no_production_dotenv(cfg, exists=exists)
    reasons += gate_no_production_credentials(env)
    reasons += gate_ports(cfg)
    reasons += gate_background_services_off(env)
    reasons += gate_prefixes_match(identity, prefixes)
    reasons += gate_sentinel_active(on_vps, sentinel_running)
    if role == 'worker':
        reasons += gate_worker_role(env)
        reasons += gate_fake_firebase_selected(env)
    if netns_proof_ok is None:
        reasons.append('network isolation has not been proven '
                       '(netns_proof.py has not run for this round)')
    elif not netns_proof_ok:
        reasons.append('network isolation proof FAILED')
    if identity.get('production_network_reachable'):
        reasons.append('identity states production is reachable from here')
    if reasons:
        _fail(reasons)
    return {'ok': True, 'role': role, 'gates_passed': 9 if role == 'worker' else 7}
