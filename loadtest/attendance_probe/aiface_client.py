"""Synthetic AI Face 11 device speaking the server's actual WebSocket protocol.

Mirrors app/services/ai_face_ws.py (revision under test):
  device → server : {"cmd":"reg","sn":..,"devinfo":{..}}
                    {"cmd":"sendlog","sn":..,"count":N,"logindex":M,"record":[..]}
  server → device : {"ret":"reg","result":true,"cloudtime":..,"nosenduser":true}
                    {"ret":"sendlog","result":true,"count":N,"logindex":M,"cloudtime":..,"access":1}
                    {"cmd":"getnewlog","stn":true|false}   (+ getalllog fallback)
  device reply    : {"ret":"getnewlog","result":true,"count":0,"logindex":M,"record":[]}
Pings from the server (20 s interval, 10 s timeout) are answered automatically
by websocket-client while the reader loop is receiving.

Acks are correlated by the echoed `logindex` (unique per device connection
lifetime). An ack only proves the server finished processing the frame — it is
returned with result:true even for unknown devices or failed records — so
commits are always verified separately against the database.

Works with plain threads or under gevent monkey-patching (Locust).
"""
from __future__ import annotations

import json
import threading
import time

import websocket  # websocket-client

EXPECTED_SERVER_COMMANDS = {'getnewlog', 'getalllog'}


class DeviceDisconnected(Exception):
    pass


class AckTimeout(Exception):
    pass


class AiFaceDevice:
    def __init__(self, url: str, sn: str, *, on_log=None, logindex_start: int = 1000):
        self.url = url
        self.sn = sn
        self.ws = None
        self.on_log = on_log or (lambda *a, **k: None)
        self._send_lock = threading.Lock()
        self._reg_evt = threading.Event()
        self._reg_payload = None
        self._pending: dict[int, dict] = {}
        self._pending_lock = threading.Lock()
        self._reader = None
        self.connected = False
        self.logindex = logindex_start
        self.stats = {'connects': 0, 'reconnects': 0, 'disconnects': 0, 'server_polls': 0,
                      'unexpected_commands': 0, 'late_acks': 0, 'unmatched_acks': 0}
        self.unexpected_commands: list[dict] = []

    # ── connection ────────────────────────────────────────────────────────────
    def connect(self, timeout: float = 15.0) -> float:
        """Open the socket, send reg, wait for the reg ack. Returns reg latency (s)."""
        self.ws = websocket.create_connection(self.url, timeout=timeout, enable_multithread=True)
        self.ws.settimeout(None)
        self.connected = True
        if self.stats['connects']:
            self.stats['reconnects'] += 1
        self.stats['connects'] += 1
        self._reg_evt.clear()
        self._reader = threading.Thread(target=self._read_loop, args=(self.ws,), daemon=True,
                                        name=f'aiface-reader-{self.sn}')
        self._reader.start()
        t0 = time.perf_counter()
        self._send({'cmd': 'reg', 'sn': self.sn, 'devinfo': {
            'modelname': 'AiFace', 'firmware': 'attlt-synthetic', 'usersize': 5000, 'facesize': 5000,
            'logsize': 500000, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}})
        if not self._reg_evt.wait(timeout):
            raise AckTimeout(f'reg ack not received within {timeout}s')
        if not (self._reg_payload or {}).get('result'):
            raise RuntimeError(f'reg rejected: {self._reg_payload}')
        return time.perf_counter() - t0

    def close(self):
        self.connected = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def _send(self, obj: dict):
        data = json.dumps(obj, ensure_ascii=False)
        with self._send_lock:
            if not self.connected or self.ws is None:
                raise DeviceDisconnected(self.sn)
            try:
                self.ws.send(data)
            except Exception as exc:
                self._mark_disconnected()
                raise DeviceDisconnected(f'{self.sn}: {exc}') from exc

    def _mark_disconnected(self):
        if self.connected:
            self.connected = False
            self.stats['disconnects'] += 1
        with self._pending_lock:
            for slot in self._pending.values():
                slot['evt'].set()

    def _read_loop(self, ws):
        try:
            while True:
                raw = ws.recv()          # also answers server pings
                if raw is None or raw == '':
                    if not ws.connected:
                        break
                    continue
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                self._dispatch(msg)
        except Exception:
            pass
        finally:
            if ws is self.ws:
                self._mark_disconnected()

    def _dispatch(self, msg: dict):
        if 'ret' in msg:
            ret = msg['ret']
            if ret == 'reg':
                self._reg_payload = msg
                self._reg_evt.set()
            elif ret == 'sendlog':
                li = msg.get('logindex')
                with self._pending_lock:
                    slot = self._pending.get(li)
                if slot is None:
                    self.stats['unmatched_acks'] += 1
                    return
                if slot.get('ack') is not None:
                    return
                slot['ack'] = msg
                slot['ack_t'] = time.perf_counter()
                if slot.get('timed_out'):
                    self.stats['late_acks'] += 1
                slot['evt'].set()
            return
        cmd = msg.get('cmd')
        if cmd in EXPECTED_SERVER_COMMANDS:
            self.stats['server_polls'] += 1
            try:
                self._send({'ret': cmd, 'result': True, 'count': 0, 'logindex': self.logindex, 'record': []})
            except DeviceDisconnected:
                pass
        elif cmd:
            # setuserinfo / deleteuser / anything else must never happen in this test
            self.stats['unexpected_commands'] += 1
            self.unexpected_commands.append({'cmd': cmd, 'keys': sorted(msg.keys())})
            try:
                self._send({'ret': cmd, 'result': False})
            except DeviceDisconnected:
                pass

    # ── attendance ────────────────────────────────────────────────────────────
    def begin_sendlog(self, records: list[dict]) -> dict:
        """Send one sendlog frame; returns a slot to await with wait_ack()."""
        self.logindex += 1
        li = self.logindex
        slot = {'logindex': li, 'evt': threading.Event(), 'ack': None, 'sent_t': None}
        with self._pending_lock:
            self._pending[li] = slot
        slot['sent_t'] = time.perf_counter()
        self._send({'cmd': 'sendlog', 'sn': self.sn, 'count': len(records), 'logindex': li,
                    'record': records})
        return slot

    def wait_ack(self, slot: dict, timeout: float) -> dict:
        if not slot['evt'].wait(timeout):
            slot['timed_out'] = True
            raise AckTimeout(f'sendlog logindex={slot["logindex"]} not acked in {timeout}s')
        if slot['ack'] is None:
            raise DeviceDisconnected(f'{self.sn} disconnected before ack')
        with self._pending_lock:
            self._pending.pop(slot['logindex'], None)
        ack = slot['ack']
        if not (ack.get('result') is True and ack.get('logindex') == slot['logindex']):
            raise RuntimeError(f'malformed sendlog ack: {ack}')
        return ack

    @staticmethod
    def record(enrollid: int, date_str: str, time_str: str) -> dict:
        return {'enrollid': int(enrollid), 'name': '', 'time': f'{date_str} {time_str}',
                'mode': 3, 'inout': 0, 'event': 0}
