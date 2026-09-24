"""The experiment's identity card: run/environment_identity.json.

Written when the environment is built, read by every safety gate and by
cleanup before anything destructive happens. It contains NO credentials — only
names, ports, classifications and prefixes, so it is safe to print in a report.

The classification functions are pure, so "is this database production?" is
answerable, and testable, without connecting to anything.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from urllib.parse import urlsplit

IDENTITY_FILENAME = 'environment_identity.json'

# Hosts that mean "somewhere real". Matches the same idea as tests/conftest.py,
# deliberately kept as its own list so neither can silently relax the other.
PRODUCTION_HOST_MARKERS = (
    'render.com', 'supabase.co', 'supabase.io', 'pooler.supabase.com',
    'neon.tech', '.fl0.io', 'amazonaws.com', 'azure.com', 'digitalocean.com',
    'hostinger', 'srv', 'core-school', 'mecha-school',
)

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1', '[::1]', '0.0.0.0'}

# Container-internal service names used by the compose files. These are only
# resolvable inside the private network, so they are isolated by construction.
CONTAINER_HOSTS = {'db', 'postgres', 'pg', 'attlt-db'}

# A production database must never carry these. An experiment database must.
EXPERIMENT_DB_NAME_RE = re.compile(r'^core_school_attendance_load_test$|_test$|^attlt_')

PRODUCTION_WS_PORT = 7788
PRODUCTION_HTTP_PORTS = {80, 443, 5000, 8000}


class NotAnExperiment(SystemExit):
    """Raised, and never caught, when isolation cannot be proven."""


def classify_db_host(host: str) -> str:
    """'loopback' | 'container' | 'production-like' | 'unknown'. Pure."""
    h = (host or '').strip().lower()
    if not h:
        return 'unknown'
    if h in LOOPBACK_HOSTS:
        return 'loopback'
    if h in CONTAINER_HOSTS:
        return 'container'
    if any(marker in h for marker in PRODUCTION_HOST_MARKERS):
        return 'production-like'
    return 'unknown'


def is_isolated_db(host: str, db_name: str) -> tuple[bool, list]:
    """Fail closed: only loopback/container hosts with an experiment-shaped
    database name are considered isolated. Everything else is not."""
    reasons = []
    kind = classify_db_host(host)
    if kind not in ('loopback', 'container'):
        reasons.append(f'database host {host!r} classified {kind!r}, '
                       'not loopback or container')
    if not EXPERIMENT_DB_NAME_RE.search((db_name or '').strip()):
        reasons.append(f'database name {db_name!r} is not experiment-shaped')
    return (not reasons), reasons


def looks_like_production_url(url: str) -> bool:
    """True if a DSN points anywhere that is not provably isolated."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    host = (parts.hostname or '')
    name = (parts.path or '').lstrip('/')
    ok, _ = is_isolated_db(host, name)
    return not ok


def identity_path(root: str) -> str:
    return os.path.join(root, 'run', IDENTITY_FILENAME)


def build(cfg: dict, *, experiment_id: str, tag: str, app_commit: str,
          worker_enabled: bool, prefixes: dict,
          production_network_reachable: bool = False,
          cleanup_manifest_path: str = '') -> dict:
    """Assemble the identity record. No secret may be passed in: only the keys
    below are emitted, and every one is a name, a port or a classification."""
    host = cfg.get('pg_host', '')
    name = cfg.get('db_name', '')
    isolated, reasons = is_isolated_db(host, name)
    return {
        'experiment_id': experiment_id,
        'experiment_tag': tag,
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'app_source_commit': app_commit,
        'database_name': name,
        'database_host': host,
        'database_host_classification': classify_db_host(host),
        'database_isolated': isolated,
        'database_isolation_reasons': reasons,
        'synthetic_school_prefix': prefixes.get('school'),
        'synthetic_device_prefix': prefixes.get('device'),
        'synthetic_student_prefix': prefixes.get('student'),
        'synthetic_parent_prefix': prefixes.get('parent_user'),
        'synthetic_token_prefix': prefixes.get('device_token'),
        'target_http_port': cfg.get('http_port'),
        'target_ws_port': cfg.get('ws_port'),
        'production_ws_port': PRODUCTION_WS_PORT,
        'worker_enabled': bool(worker_enabled),
        'firebase_mode': 'fake-local',
        'production_network_reachable': bool(production_network_reachable),
        'cleanup_manifest_path': cleanup_manifest_path,
        'contains_credentials': False,
    }


FORBIDDEN_IDENTITY_SUBSTRINGS = (
    'password', 'secret', 'token=', 'private_key', 'jwt', 'dsn', 'passwd',
)


def assert_no_credentials(identity: dict) -> None:
    """A cheap structural guarantee that the identity card stays printable."""
    blob = json.dumps(identity, ensure_ascii=False).lower()
    for bad in FORBIDDEN_IDENTITY_SUBSTRINGS:
        if bad in blob:
            raise NotAnExperiment(
                f'environment_identity.json contains {bad!r} — refusing to write')


def write(root: str, identity: dict) -> str:
    assert_no_credentials(identity)
    os.makedirs(os.path.join(root, 'run'), exist_ok=True)
    path = identity_path(root)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(identity, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load(root: str) -> dict:
    with open(identity_path(root), encoding='utf-8') as fh:
        return json.load(fh)


def verify(root: str, *, experiment_id: str, tag: str) -> dict:
    """Prove this directory is the experiment it claims to be. Fail closed.

    Every destructive operation calls this first. It raises rather than
    returning False so a caller cannot forget to check a result.
    """
    try:
        identity = load(root)
    except OSError as exc:
        raise NotAnExperiment(
            f'{IDENTITY_FILENAME} missing or unreadable in {root}: {exc}')
    if identity.get('experiment_id') != experiment_id:
        raise NotAnExperiment(
            'environment identity experiment_id does not match the manifest')
    if identity.get('experiment_tag') != tag:
        raise NotAnExperiment(
            'environment identity experiment_tag does not match the supplied tag')
    if identity.get('firebase_mode') != 'fake-local':
        raise NotAnExperiment(
            'environment identity firebase_mode is not fake-local')
    if identity.get('production_network_reachable'):
        raise NotAnExperiment(
            'environment identity states production is reachable')
    isolated, reasons = is_isolated_db(identity.get('database_host', ''),
                                       identity.get('database_name', ''))
    if not isolated:
        raise NotAnExperiment(
            'database is not provably isolated: ' + '; '.join(reasons))
    if identity.get('target_ws_port') == PRODUCTION_WS_PORT:
        raise NotAnExperiment(
            f'target WS port is the production port {PRODUCTION_WS_PORT}')
    return identity
