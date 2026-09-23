# -*- coding: utf-8 -*-
"""Explicit process-role gate for shared background services.

The defect this exists to fix
─────────────────────────────
create_app() decided whether to start the attendance scheduler, the AI Face
WebSocket server, the Hikvision sync loop, the fee-reminder scheduler and the
durable-push consumer from ``sys.argv[1]``:

    _cli_cmd = sys.argv[1] if len(sys.argv) >= 2 else ''
    _skip_schedulers = _cli_cmd in ('db', 'shell', 'routes', 'digest', 'collect')

That only matches when the subcommand happens to land in argv[1]. Two real
production invocations do not:

    python -m app.services.outbox_worker run
        argv[1] == 'run'    → not matched → the outbox worker started the
                              attendance scheduler and ticked across schools.

    python -m flask --app manage db current
        argv[1] == '--app'  → not matched → a read-only migration query logged
                              "[attendance] auto-attendance scheduler started".

A process must not acquire background work by accident. It declares what it is,
once, before the application is constructed.

How it works
────────────
The role lives in ONE place, resolved in this order:

  1. an explicit set_role() call by the entry point (authoritative);
  2. the MECHA_PROCESS_ROLE environment variable, so a systemd unit can state
     the role as defence in depth and so the value survives into any late
     import;
  3. ROLE_WEB by default.

Defaulting to WEB is deliberate: it preserves today's behaviour exactly for
every entry point that does not declare a role (notably run.py, the
development server). Only a process that explicitly says it is NOT the web
server loses the background services — so this change can never silently
disable them in production.

Adding a role never grants extra privileges; ROLE_WEB is the only role that may
start shared background services, and the caller's existing checks (the argv
heuristic, app.testing) still apply on top. This gate can only ever make
startup MORE restrictive, never less.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger('mecha.lifecycle')

ROLE_ENV_VAR = 'MECHA_PROCESS_ROLE'

#: The Gunicorn/development web server. Starts the shared background services.
ROLE_WEB = 'web'
#: Flask CLI, Alembic migrations, management commands. Starts nothing.
ROLE_CLI = 'cli'
#: The durable notification outbox worker. Starts nothing but its own loop.
ROLE_OUTBOX_WORKER = 'outbox-worker'
#: The test suite. Starts nothing.
ROLE_TEST = 'test'

ROLES = (ROLE_WEB, ROLE_CLI, ROLE_OUTBOX_WORKER, ROLE_TEST)

#: The ONLY role permitted to start the shared background services.
_BACKGROUND_SERVICE_ROLES = frozenset({ROLE_WEB})

_role: str | None = None


def set_role(role: str, *, override: bool = False) -> str:
    """Declare this process's role. Call BEFORE create_app().

    Also exports MECHA_PROCESS_ROLE so a module imported later — or a child
    process — resolves the same answer without being passed anything.

    Re-declaring the SAME role is a no-op, which keeps entry points idempotent
    (manage.py may be imported twice by the Flask CLI). A CONFLICTING
    re-declaration is refused unless override=True: silently flipping a running
    process from 'outbox-worker' to 'web' is exactly the accident this module
    prevents.
    """
    global _role
    role = (role or '').strip().lower()
    if role not in ROLES:
        raise ValueError(
            f'unknown process role {role!r}; expected one of {ROLES}')

    if _role is not None and _role != role and not override:
        log.warning('[lifecycle] refusing to change process role %r -> %r; '
                    'keeping %r', _role, role, _role)
        return _role

    _role = role
    os.environ[ROLE_ENV_VAR] = role
    return _role


def current_role() -> str:
    """The resolved role: explicit call, then environment, then ROLE_WEB."""
    if _role is not None:
        return _role
    env = (os.environ.get(ROLE_ENV_VAR) or '').strip().lower()
    if env in ROLES:
        return env
    if env:
        log.warning('[lifecycle] %s=%r is not a known role — treating this '
                    'process as %r', ROLE_ENV_VAR, env, ROLE_WEB)
    return ROLE_WEB


def reset_role_for_tests() -> None:
    """Clear the in-process role. Test-support only."""
    global _role
    _role = None
    os.environ.pop(ROLE_ENV_VAR, None)


def background_services_allowed(app=None) -> bool:
    """True only when this process should start the SHARED background services.

    Shared services means: the auto-attendance scheduler, the AI Face WebSocket
    receiver, the Hikvision sync loop, the fee-reminder scheduler and the
    durable-push consumer. It does NOT mean the outbox worker's own loop, which
    the worker process runs directly and which is not started by create_app().
    """
    if app is not None and getattr(app, 'testing', False):
        return False
    return current_role() in _BACKGROUND_SERVICE_ROLES


def describe() -> str:
    """One-line summary for the startup log."""
    role = current_role()
    return (f'role={role} '
            f'background_services={"yes" if role in _BACKGROUND_SERVICE_ROLES else "no"}')
