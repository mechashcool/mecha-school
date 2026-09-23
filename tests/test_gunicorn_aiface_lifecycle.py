# -*- coding: utf-8 -*-
"""Gunicorn worker lifecycle + AI Face listening-socket ownership.

Production evidence this covers (mecha-school.service, commit 269084a):

    Gunicorn master started 11:55, the CURRENT worker started 13:04, and port
    7788 was owned by the WORKER, not the master. systemd NRestarts was 0
    because Gunicorn had replaced only its child. That is routine
    max_requests recycling: with workers=1 Gunicorn does not pre-spawn the
    replacement, so every recycle is a full service gap, and the AI Face
    listener died with the worker (ConnectionRefusedError for reconnecting
    devices).

Two independent guarantees are asserted here:

  1. A single worker does not recycle by request count at all.
  2. The listening socket belongs to the MASTER, so it survives whatever the
     worker does.

Nothing in this file touches a database, a device, a notification, or the
network beyond 127.0.0.1 on an ephemeral port.
"""
import errno
import importlib
import importlib.util
import os
import re
import select
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUNICORN_CONF = REPO_ROOT / 'gunicorn.conf.py'

# Environment variables every gunicorn.conf.py load in this module controls.
_LIFECYCLE_VARS = (
    'WEB_CONCURRENCY', 'GUNICORN_THREADS', 'GUNICORN_TIMEOUT',
    'GUNICORN_GRACEFUL_TIMEOUT', 'GUNICORN_MAX_REQUESTS',
    'GUNICORN_MAX_REQUESTS_JITTER',
)


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

def load_gunicorn_conf(**env):
    """Execute gunicorn.conf.py under an exact environment and return it.

    Every lifecycle variable is cleared first, so "not configured" really means
    not configured and cannot leak in from the developer's shell or from a
    previous test.
    """
    saved = {k: os.environ.get(k) for k in _LIFECYCLE_VARS}
    try:
        for key in _LIFECYCLE_VARS:
            os.environ.pop(key, None)
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        spec = importlib.util.spec_from_file_location(
            'gunicorn_conf_under_test', GUNICORN_CONF)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def free_port():
    """An ephemeral port that is free right now, released before returning."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class FakeArbiter:
    """Stands in for the Gunicorn ``server`` (arbiter) object.

    when_ready() stores the listening socket on this object; that attribute is
    the master's strong reference, and dropping it would close the listener.
    """


def _is_listening(sock):
    return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1


def _close_quietly(*socks):
    for s in socks:
        try:
            s.close()
        except (OSError, AttributeError):
            pass


def forked_descriptor(listening):
    """A second, independent descriptor for the same listening socket.

    This is what fork() gives the worker: its own descriptor referring to the
    master's socket. ``socket.dup()`` is used rather than ``os.dup()`` because
    on Windows a socket handle is not a CRT file descriptor and ``os.dup``
    fails on it with EBADF.

    Returns (fd, owner_socket). ``owner_socket`` holds the descriptor open;
    close it to simulate the worker process exiting.
    """
    owner = listening.dup()
    return owner.fileno(), owner


def hand_descriptor_to(module, fd, owner):
    """Detach ``owner`` so the module under test takes ownership of ``fd``.

    Exactly one object may own a descriptor: if both the test and the module
    wrapped it, whichever was garbage-collected first would close a socket the
    other still uses.
    """
    owner.detach()
    os.environ['AIFACE_WS_FD'] = str(fd)


@pytest.fixture
def gconf_module():
    """gunicorn.conf.py loaded with AI Face DISABLED by default.

    Individual tests re-load it with the settings they need; this fixture only
    provides the module object for the socket-ownership tests.
    """
    return load_gunicorn_conf()


@pytest.fixture
def aiface(monkeypatch):
    """app.services.ai_face_ws with its per-process socket cache reset.

    ``_inherited_listen_socket`` memoises its result (wrapping one fd in two
    socket objects would close it on the first garbage collection), so the
    cache must be cleared between tests or the second test would silently reuse
    the first test's socket.
    """
    module = importlib.import_module('app.services.ai_face_ws')
    monkeypatch.setattr(module, '_inherited_sock', False, raising=False)
    yield module
    module._inherited_sock = False


# ═════════════════════════════════════════════════════════════════════════════
#  1. Single worker, nothing configured → recycling fully off
# ═════════════════════════════════════════════════════════════════════════════

def test_single_worker_defaults_to_no_recycling():
    """The exact production shape: 1 worker, no max-request env vars."""
    conf = load_gunicorn_conf()

    assert conf.workers == 1
    assert conf.worker_class == 'gthread'
    assert conf.threads == 4
    assert conf.max_requests == 0, (
        'a single worker must not recycle by request count: Gunicorn does not '
        'pre-spawn the replacement, so every recycle is a full service gap')
    assert conf.max_requests_jitter == 0, (
        'jitter is ADDED to max_requests and the SUM becomes the limit, so a '
        'non-zero jitter with max_requests=0 yields a small RANDOM limit')


def test_jitter_is_zero_whenever_recycling_is_disabled():
    """The regression this test exists for.

    max_requests=0 with jitter=50 is NOT "no recycling". Gunicorn computes the
    worker's limit as max_requests + randint(0, jitter) and only treats the
    setting as unlimited when that sum is zero. 0 + randint(0, 50) is a small
    positive number on almost every boot, which would recycle the worker after
    a few dozen requests — an order of magnitude worse than the 500 this change
    removes.
    """
    conf = load_gunicorn_conf(WEB_CONCURRENCY=1)
    assert (conf.max_requests, conf.max_requests_jitter) == (0, 0)

    # Even when an operator leaves a stale jitter behind.
    stale = load_gunicorn_conf(WEB_CONCURRENCY=1,
                               GUNICORN_MAX_REQUESTS_JITTER=50)
    assert stale.max_requests == 0
    assert stale.max_requests_jitter == 0, (
        'a stale jitter with recycling disabled must be neutralised, not obeyed')
    assert stale._jitter_overridden_to_zero == 50, (
        'the override must be recorded so on_starting can report it')


def test_explicit_max_requests_is_respected():
    """Operator intent still wins — this change removes a default, not a knob."""
    conf = load_gunicorn_conf(WEB_CONCURRENCY=1, GUNICORN_MAX_REQUESTS=750)
    assert conf.max_requests == 750
    assert conf.max_requests_jitter == 50, (
        'once recycling is explicitly enabled, the historical jitter default '
        'applies so replacements are not synchronised')

    both = load_gunicorn_conf(WEB_CONCURRENCY=1, GUNICORN_MAX_REQUESTS=750,
                              GUNICORN_MAX_REQUESTS_JITTER=13)
    assert (both.max_requests, both.max_requests_jitter) == (750, 13)

    # An explicit 0 also disables jitter, because the sum is what matters.
    off = load_gunicorn_conf(WEB_CONCURRENCY=1, GUNICORN_MAX_REQUESTS=0)
    assert (off.max_requests, off.max_requests_jitter) == (0, 0)


def test_multi_worker_keeps_the_historical_default():
    """Only the SINGLE-worker case changes; a real pool may still recycle."""
    conf = load_gunicorn_conf(WEB_CONCURRENCY=3)
    assert conf.workers == 3
    assert conf.max_requests == 500
    assert conf.max_requests_jitter == 50


def test_invalid_configuration_fails_loudly():
    """A typo must stop the boot, not silently re-enable recycling."""
    with pytest.raises(RuntimeError, match='GUNICORN_MAX_REQUESTS'):
        load_gunicorn_conf(GUNICORN_MAX_REQUESTS='5OO')      # letter O

    with pytest.raises(RuntimeError, match='not an integer'):
        load_gunicorn_conf(GUNICORN_MAX_REQUESTS_JITTER='abc')

    with pytest.raises(RuntimeError, match='zero or positive'):
        load_gunicorn_conf(GUNICORN_MAX_REQUESTS=-1)

    with pytest.raises(RuntimeError, match='WEB_CONCURRENCY'):
        load_gunicorn_conf(WEB_CONCURRENCY='two')

    # Blank/whitespace is "not configured", not an error.
    blank = load_gunicorn_conf(GUNICORN_MAX_REQUESTS='   ')
    assert (blank.max_requests, blank.max_requests_jitter) == (0, 0)


# ═════════════════════════════════════════════════════════════════════════════
#  2. The master owns and retains the listening socket
# ═════════════════════════════════════════════════════════════════════════════

def test_master_binds_and_retains_the_listening_socket(gconf_module, monkeypatch):
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'true')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.setenv('PORT', str(free_port()))
    monkeypatch.delenv('AIFACE_WS_FD', raising=False)

    server = FakeArbiter()
    gconf_module.when_ready(server)

    sock = getattr(server, '_aiface_ws_sock', None)
    assert sock is not None, 'the master must keep a strong reference'
    try:
        assert _is_listening(sock), 'the master socket must be listening'
        assert sock.getsockname()[1] == port
        assert sock.get_inheritable() is True, (
            'the fd must survive fork() into the worker')
        assert os.environ.get('AIFACE_WS_FD') == str(sock.fileno()), (
            'the descriptor must be published for the worker to pick up')

        # It is a real listener: a client connection is accepted into the
        # backlog even though nothing is calling accept() yet.
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            assert client.fileno() != -1
    finally:
        _close_quietly(sock)
        os.environ.pop('AIFACE_WS_FD', None)


def test_when_ready_is_idempotent_on_reload(gconf_module, monkeypatch):
    """A Gunicorn reload must keep the socket it already owns."""
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'true')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.setenv('PORT', str(free_port()))

    server = FakeArbiter()
    gconf_module.when_ready(server)
    first = server._aiface_ws_sock
    try:
        gconf_module.when_ready(server)          # simulate SIGHUP reload
        assert server._aiface_ws_sock is first, (
            'a reload must not rebind — that would drop the listener')
    finally:
        _close_quietly(first)
        os.environ.pop('AIFACE_WS_FD', None)


def test_disabled_mode_creates_no_socket(gconf_module, monkeypatch):
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'false')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.delenv('AIFACE_WS_FD', raising=False)

    server = FakeArbiter()
    gconf_module.when_ready(server)

    assert getattr(server, '_aiface_ws_sock', None) is None
    assert 'AIFACE_WS_FD' not in os.environ

    # Nothing is listening: the port is still bindable.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', port))          # would raise if occupied
        probe.listen(1)


def test_master_refuses_to_bind_the_web_port(gconf_module, monkeypatch):
    """Existing safety guard must survive this change."""
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'true')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.setenv('PORT', str(port))        # same port → refuse
    monkeypatch.delenv('AIFACE_WS_FD', raising=False)

    server = FakeArbiter()
    gconf_module.when_ready(server)

    assert getattr(server, '_aiface_ws_sock', None) is None
    assert 'AIFACE_WS_FD' not in os.environ


# ═════════════════════════════════════════════════════════════════════════════
#  3. The worker uses the inherited fd and never rebinds
# ═════════════════════════════════════════════════════════════════════════════

def test_worker_uses_the_inherited_socket(aiface, gconf_module, monkeypatch):
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'true')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.setenv('PORT', str(free_port()))

    server = FakeArbiter()
    gconf_module.when_ready(server)
    master_sock = server._aiface_ws_sock
    inherited = None
    try:
        # fork() gives the child its own descriptor for the same socket.
        worker_fd, owner = forked_descriptor(master_sock)
        hand_descriptor_to(aiface, worker_fd, owner)
        monkeypatch.setattr(aiface, '_WS_PORT', port)

        inherited = aiface._inherited_listen_socket()
        assert inherited is not None, 'the worker must accept the inherited fd'
        assert inherited.fileno() == worker_fd, (
            'the worker must use the descriptor it was given, not a new one')
        assert inherited.getsockname()[1] == port
        assert _is_listening(inherited)

        # Memoised: a second call must return the SAME object, never a second
        # wrapper around the same fd.
        assert aiface._inherited_listen_socket() is inherited
    finally:
        _close_quietly(inherited, master_sock)
        os.environ.pop('AIFACE_WS_FD', None)


def test_worker_startup_does_not_create_a_new_socket(aiface, gconf_module,
                                                     monkeypatch):
    """start_ai_face_ws_server must skip the pre-bind probe when it inherited.

    Proven structurally: every fresh socket construction is recorded, and an
    inherited start must record none. A rebind would be exactly such a
    construction, and against the master's own socket it would be either
    EADDRINUSE or a hijack.
    """
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'true')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.setenv('PORT', str(free_port()))

    server = FakeArbiter()
    gconf_module.when_ready(server)
    master_sock = server._aiface_ws_sock
    worker_fd, owner = forked_descriptor(master_sock)
    hand_descriptor_to(aiface, worker_fd, owner)
    monkeypatch.setattr(aiface, '_WS_PORT', port)

    fresh_sockets = []
    real_socket = aiface._socket.socket

    class RecordingSocket(real_socket):
        def __init__(self, *args, **kwargs):
            if 'fileno' not in kwargs:
                fresh_sockets.append((args, sorted(kwargs)))
            super().__init__(*args, **kwargs)

    started = []

    class FakeThread:
        def __init__(self, *a, **kw):
            started.append(kw.get('name'))

        def start(self):
            pass

    monkeypatch.setattr(aiface._socket, 'socket', RecordingSocket)
    monkeypatch.setattr(aiface.threading, 'Thread', FakeThread)

    try:
        aiface.start_ai_face_ws_server(object())

        assert started == ['aiface-ws'], 'the receiver thread must still start'
        assert fresh_sockets == [], (
            'the worker must not construct a listening socket of its own when '
            f'the master handed it one; created: {fresh_sockets}')
    finally:
        _close_quietly(aiface._inherited_sock, master_sock)
        os.environ.pop('AIFACE_WS_FD', None)


@pytest.mark.parametrize('bad_fd', ['', 'not-an-int', '-1', '999999'])
def test_bad_descriptor_falls_back_instead_of_crashing(aiface, monkeypatch,
                                                       bad_fd):
    """A stale AIFACE_WS_FD (e.g. after a re-exec) must degrade, not crash."""
    monkeypatch.setenv('AIFACE_WS_FD', bad_fd)
    monkeypatch.setattr(aiface, '_WS_PORT', free_port())
    assert aiface._inherited_listen_socket() is None


def test_descriptor_pointing_at_the_wrong_socket_is_rejected(aiface, monkeypatch):
    """Validation must reject a live fd that is not OUR listener.

    And it must DETACH rather than close: closing a descriptor we do not own
    would break whatever actually uses it.
    """
    other_port = free_port()
    other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    other.bind(('127.0.0.1', other_port))
    other.listen(1)
    try:
        monkeypatch.setenv('AIFACE_WS_FD', str(other.fileno()))
        monkeypatch.setattr(aiface, '_WS_PORT', free_port())   # different port

        assert aiface._inherited_listen_socket() is None
        # Still alive — proof it was detached, not closed.
        assert _is_listening(other)
        assert other.getsockname()[1] == other_port
    finally:
        _close_quietly(other)


# ═════════════════════════════════════════════════════════════════════════════
#  4. Worker replacement keeps the listener available
# ═════════════════════════════════════════════════════════════════════════════

def test_worker_replacement_never_drops_the_listener(aiface, gconf_module,
                                                     monkeypatch):
    """The production failure, reproduced structurally.

    Master binds once. Worker A inherits and dies. A client connects during the
    gap — which used to raise ConnectionRefusedError. Worker B inherits the
    same master socket and accepts the connection that was waiting in the
    backlog.
    """
    port = free_port()
    monkeypatch.setenv('AIFACE_WS_ENABLED', 'true')
    monkeypatch.setenv('AIFACE_WS_PORT', str(port))
    monkeypatch.setenv('PORT', str(free_port()))

    server = FakeArbiter()
    gconf_module.when_ready(server)
    master_sock = server._aiface_ws_sock
    client = accepted = sock_a = sock_b = None
    try:
        monkeypatch.setattr(aiface, '_WS_PORT', port)

        # ── worker A boots and inherits ─────────────────────────────────────
        fd_a, owner_a = forked_descriptor(master_sock)
        hand_descriptor_to(aiface, fd_a, owner_a)
        sock_a = aiface._inherited_listen_socket()
        assert sock_a is not None and sock_a.fileno() == fd_a

        # ── worker A exits: its descriptor is closed with the process ───────
        sock_a.close()
        sock_a = None
        aiface._inherited_sock = False          # a fresh process's state

        # ── the gap: the MASTER still owns the listener ─────────────────────
        assert _is_listening(master_sock), (
            'the master socket must outlive the worker')
        # This is the connection that used to be refused.
        client = socket.create_connection(('127.0.0.1', port), timeout=5)

        # ── worker B boots and inherits the SAME socket ─────────────────────
        fd_b, owner_b = forked_descriptor(master_sock)
        hand_descriptor_to(aiface, fd_b, owner_b)
        sock_b = aiface._inherited_listen_socket()
        assert sock_b is not None, 'the replacement worker must inherit too'
        assert sock_b.fileno() == fd_b
        assert sock_b.getsockname()[1] == port

        # The connection made during the gap is still queued and acceptable.
        ready, _, _ = select.select([sock_b], [], [], 5.0)
        assert ready, 'the connection opened during the gap must be pending'
        sock_b.setblocking(True)
        accepted, _addr = sock_b.accept()
        assert accepted is not None
    finally:
        _close_quietly(accepted, client, sock_a, sock_b, master_sock)
        os.environ.pop('AIFACE_WS_FD', None)
        aiface._inherited_sock = False


# ═════════════════════════════════════════════════════════════════════════════
#  5. Nothing else about AI Face changed
# ═════════════════════════════════════════════════════════════════════════════

def _function_source(text, name):
    """The source of one top-level def, from the start of its line to the next
    top-level statement. Used to compare HEAD against the working tree."""
    lines = text.replace('\r\n', '\n').split('\n')
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'^(async )?def {re.escape(name)}\(', line):
            start = i
            break
    assert start is not None, f'{name}() not found'
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j] and not lines[j][0].isspace():
            end = j
            break
    return '\n'.join(lines[start:end]).rstrip()


# Everything that parses device events, writes attendance, notifies parents, or
# routes commands. None of it may differ from the deployed commit.
_UNCHANGED_FUNCTIONS = (
    '_process_record_list',
    '_process_employee_punch',
    '_handle_getnewlog_records',
    '_handle_reg',
    '_handle_sendlog',
    '_db_touch_device',
    'queue_command_for_device',
    'send_command_to_device',
    '_send_and_wait',
    'is_device_connected',
    'get_connected_sns',
    'get_device_status',
    'get_all_device_status',
    '_safe_log_payload',
)


def test_device_and_attendance_logic_is_byte_identical_to_head():
    """Structural proof that this change is listener-only.

    Device authentication, school isolation, event parsing, attendance writes
    and command routing must be character-for-character what production runs.
    """
    head = subprocess.run(
        ['git', 'show', 'HEAD:app/services/ai_face_ws.py'],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding='utf-8')
    assert head.returncode == 0, head.stderr
    current = (REPO_ROOT / 'app' / 'services' / 'ai_face_ws.py').read_text(
        encoding='utf-8')

    differing = [name for name in _UNCHANGED_FUNCTIONS
                 if _function_source(head.stdout, name)
                 != _function_source(current, name)]
    assert differing == [], (
        f'these functions must not change in a listener-only fix: {differing}')


def test_command_queue_behaviour_is_unchanged(aiface, monkeypatch):
    """The in-process command routing this fix must not disturb."""
    monkeypatch.setattr(aiface, '_pending_commands', {})
    monkeypatch.setattr(aiface, '_connections', {})

    assert aiface.get_connected_sns() == []
    assert aiface.is_device_connected('SN-TEST-1') is False

    aiface.queue_command_for_device('SN-TEST-1', {'cmd': 'setuserinfo'}, 'note')
    aiface.queue_command_for_device('SN-TEST-1', {'cmd': 'deleteuser'}, 'note2')

    queued = aiface._pending_commands['SN-TEST-1']
    assert [q['payload']['cmd'] for q in queued] == ['setuserinfo', 'deleteuser'], (
        'queued commands must stay in FIFO order')
    assert 'SN-OTHER' not in aiface._pending_commands, (
        'queueing for one device must never touch another'
    )


def test_offline_device_command_still_raises_device_offline(aiface, monkeypatch):
    """send_command_to_device must keep BOTH of its existing failure modes.

    Documented contract: RuntimeError when the receiver thread is not running,
    DeviceOfflineError when it is running but the device is not connected. The
    socket-ownership change must not reorder or merge these.
    """
    monkeypatch.setattr(aiface, '_connections', {})

    monkeypatch.setattr(aiface, '_ws_loop', None)
    with pytest.raises(RuntimeError, match='not running'):
        aiface.send_command_to_device('SN-NOT-CONNECTED', {'cmd': 'getuserinfo'})

    # Receiver running, device absent → offline, not RuntimeError.
    monkeypatch.setattr(aiface, '_ws_loop', object())
    with pytest.raises(aiface.DeviceOfflineError):
        aiface.send_command_to_device('SN-NOT-CONNECTED', {'cmd': 'getuserinfo'})
