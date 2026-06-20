"""
AI Face 11 — Standalone WebSocket listener for local development / protocol testing.
=====================================================================================
Run this INSTEAD of starting the Flask app when you only need to verify the device
connects and sends data.  The Flask app's embedded WS server (app/services/ai_face_ws.py)
does NOT need to be running alongside this tool.

Usage:
    python tools/ai_face_ws_listener.py [--port PORT]

Default port: 7788   (must match the server IP/port configured on the device)

This tool logs every connection, handshake, message, and close with full context so
you can diagnose exactly what the device sends without the full Flask app context.

Logs:
    [CONNECT]   remote IP, port
    [HANDSHAKE] WebSocket upgrade path (device-configured path, if any)
    [MSG]       message length + raw content preview (no biometric data logged)
    [CMD]       parsed cmd/ret key from JSON
    [SENT]      every response sent back to the device
    [CLOSE]     WebSocket close code and reason
    [ERROR]     full traceback for unexpected exceptions

Protocol (WebSocket + JSON):
    Device → server:
        {"cmd":"reg",     "sn":"STC...", "modelname":"AiFace", ...}
        {"cmd":"sendlog", "sn":"...",    "count":N, "logindex":N, "record":[...]}
    Server → device:
        {"ret":"reg",     "result":true,  "cloudtime":"...", "nosenduser":true}
        {"ret":"sendlog", "result":true,  "count":N, "logindex":N, "cloudtime":"...", "access":1}
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import websockets

HOST        = "0.0.0.0"
PORT        = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7788
CAPTURE_DIR = Path("tools/captures_ws")
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

_seq = 0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _save(text: str) -> None:
    global _seq
    _seq += 1
    fname = CAPTURE_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_seq{_seq:04d}.json"
    fname.write_text(text, encoding="utf-8")
    print(f"  [SAVED] → {fname}")


async def handler(websocket):
    remote = websocket.remote_address
    print("\n" + "=" * 72)
    print(f"  [CONNECT] {remote[0]}:{remote[1]}  at {_now()}")

    # WebSocket upgrade path (device may connect to / or a specific path)
    try:
        req_path = getattr(getattr(websocket, 'request', None), 'path', '?') or '?'
    except Exception:
        req_path = '?'
    print(f"  [HANDSHAKE] path={req_path!r}")
    print("=" * 72)

    try:
        async for message in websocket:
            _len = len(message) if isinstance(message, (bytes, str)) else -1
            _preview = (message if isinstance(message, str)
                        else message.decode('utf-8', errors='replace'))[:400]
            print(f"\n  [MSG] {_now()}  len={_len}")
            print(f"  [MSG] preview: {_preview!r}")
            _save(message if isinstance(message, str) else _preview)

            try:
                data = json.loads(message)
            except Exception as e:
                print(f"  [ERROR] JSON decode failed: {e}")
                continue

            cmd = data.get("cmd")
            ret = data.get("ret")
            sn  = data.get("sn", "?")
            print(f"  [CMD] cmd={cmd!r}  ret={ret!r}  sn={sn!r}")

            if cmd == "reg":
                response = {
                    "ret": "reg",
                    "result": True,
                    "cloudtime": _now(),
                    "nosenduser": True,
                }
                resp_text = json.dumps(response, ensure_ascii=False)
                await websocket.send(resp_text)
                print(f"  [SENT] reg response: {resp_text}")

                # Pull offline logs immediately after registration
                pull_cmd = {"cmd": "getnewlog", "stn": True}
                pull_text = json.dumps(pull_cmd, ensure_ascii=False)
                await websocket.send(pull_text)
                print(f"  [SENT] getnewlog request: {pull_text}")

            elif cmd == "sendlog":
                records = data.get("record", [])
                print(f"  [CMD] sendlog: count={data.get('count')} logindex={data.get('logindex')} "
                      f"records_in_payload={len(records)}")
                for i, r in enumerate(records):
                    print(f"    record[{i}] enrollid={r.get('enrollid')}  "
                          f"time={r.get('time')!r}  mode={r.get('mode')}  "
                          f"inout={r.get('inout')}  event={r.get('event')}  "
                          f"image={'YES' if r.get('image') else 'NO'}")
                response = {
                    "ret": "sendlog",
                    "result": True,
                    "count": data.get("count", 0),
                    "logindex": data.get("logindex", 0),
                    "cloudtime": _now(),
                    "access": 1,
                }
                resp_text = json.dumps(response, ensure_ascii=False)
                await websocket.send(resp_text)
                print(f"  [SENT] sendlog response: {resp_text}")

            elif ret in ("getnewlog", "getalllog"):
                records = data.get("record") or []
                print(f"  [CMD] {ret} response: count={data.get('count')}  "
                      f"records_in_payload={len(records)}")
                for i, r in enumerate(records):
                    print(f"    record[{i}] enrollid={r.get('enrollid')}  "
                          f"time={r.get('time')!r}  mode={r.get('mode')}  "
                          f"inout={r.get('inout')}  event={r.get('event')}  "
                          f"image={'YES' if r.get('image') else 'NO'}")
                # Request next batch if this one had records
                if records:
                    next_cmd = {"cmd": "getnewlog", "stn": False}
                    next_text = json.dumps(next_cmd, ensure_ascii=False)
                    await websocket.send(next_text)
                    print(f"  [SENT] getnewlog next batch: {next_text}")

            elif cmd == "senduser":
                response = {"ret": "senduser", "result": True, "cloudtime": _now()}
                resp_text = json.dumps(response, ensure_ascii=False)
                await websocket.send(resp_text)
                print(f"  [SENT] senduser ack: {resp_text}")

            else:
                print(f"  [CMD] unhandled: {data}")

    except Exception as exc:
        import websockets.exceptions as _wse
        if isinstance(exc, _wse.ConnectionClosed):
            _code   = getattr(exc, 'code', None)
            _reason = getattr(exc, 'reason', '') or ''
            print(f"  [CLOSE] connection closed by device: code={_code}  reason={_reason!r}")
        else:
            import traceback
            print(f"  [ERROR] unexpected exception:")
            traceback.print_exc()
    finally:
        _close_code   = getattr(websocket, 'close_code', None)
        _close_reason = getattr(websocket, 'close_reason', None)
        print(f"  [CLOSE] session ended: {remote[0]}:{remote[1]}  "
              f"close_code={_close_code}  close_reason={_close_reason!r}")
        print("=" * 72)


async def main():
    print(f"\nAI Face 11 — WebSocket listener (diagnostic)")
    print(f"  Listening  : ws://{HOST}:{PORT}")
    print(f"  Captures   : {CAPTURE_DIR.resolve()}")
    print(f"  Protocol   : WebSocket + JSON")
    print(f"  Started at : {_now()}")
    print(f"  Ctrl-C to stop\n")

    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
