import asyncio
import json
from datetime import datetime
from pathlib import Path

import websockets

HOST = "0.0.0.0"
PORT = 7788
CAPTURE_DIR = Path("tools/captures_ws")
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

seq = 0


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_payload(text: str):
    global seq
    seq += 1
    filename = CAPTURE_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_seq{seq:04d}.json"
    filename.write_text(text, encoding="utf-8")
    print(f"saved -> {filename}")


async def handler(websocket):
    print("\n" + "=" * 70)
    print("WEBSOCKET CONNECTED")
    print("At:", now_str())
    print("=" * 70)

    try:
        async for message in websocket:
            print("\n--- MESSAGE RECEIVED ---")
            print(message)
            save_payload(message)

            try:
                data = json.loads(message)
            except Exception as e:
                print("JSON decode error:", e)
                continue

            cmd = data.get("cmd")
            print("cmd:", cmd)

            if cmd == "reg":
                response = {
                    "ret": "reg",
                    "result": True,
                    "cloudtime": now_str(),
                    "nosenduser": True
                }

            elif cmd == "sendlog":
                response = {
                    "ret": "sendlog",
                    "result": True,
                    "count": data.get("count", 0),
                    "logindex": data.get("logindex", 0),
                    "cloudtime": now_str(),
                    "access": 1
                }

            elif cmd == "senduser":
                response = {
                    "ret": "senduser",
                    "result": True,
                    "cloudtime": now_str()
                }

            else:
                response = {
                    "ret": cmd or "unknown",
                    "result": True,
                    "cloudtime": now_str()
                }

            response_text = json.dumps(response, ensure_ascii=False)
            print("--- RESPONSE SENT ---")
            print(response_text)
            await websocket.send(response_text)

    except Exception as e:
        print("WebSocket error:", e)


async def main():
    print(f"AI Face 11 WebSocket listener")
    print(f"Listening on ws://{HOST}:{PORT}")
    print(f"Captures folder: {CAPTURE_DIR}")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
