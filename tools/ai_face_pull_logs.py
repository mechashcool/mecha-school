import asyncio
import json
from datetime import datetime

import websockets

HOST = "0.0.0.0"
PORT = 7788


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def handler(websocket):
    print("\n=== DEVICE CONNECTED ===")

    async for message in websocket:
        print("\n--- RECEIVED ---")
        print(message)

        try:
            data = json.loads(message)
        except Exception as e:
            print("JSON error:", e)
            continue

        cmd = data.get("cmd")
        ret = data.get("ret")

        if cmd == "reg":
            response = {
                "ret": "reg",
                "result": True,
                "cloudtime": now_str(),
                "nosenduser": True
            }
            await websocket.send(json.dumps(response))
            print("\n--- SENT REG RESPONSE ---")
            print(response)

            # بعد التسجيل مباشرة نسحب السجلات الجديدة
            pull_cmd = {
                "cmd": "getnewlog",
                "stn": True
            }
            await websocket.send(json.dumps(pull_cmd))
            print("\n--- SENT GET NEW LOG ---")
            print(pull_cmd)

        elif cmd == "sendlog":
            response = {
                "ret": "sendlog",
                "result": True,
                "count": data.get("count", 0),
                "logindex": data.get("logindex", 0),
                "cloudtime": now_str(),
                "access": 1
            }
            await websocket.send(json.dumps(response))
            print("\n--- SENT SENDLOG RESPONSE ---")
            print(response)

        elif ret in ["getnewlog", "getalllog"]:
            print("\n=== PULLED LOGS RESULT ===")
            print("ret:", ret)
            print("result:", data.get("result"))
            print("count:", data.get("count"))
            print("from:", data.get("from"))
            print("to:", data.get("to"))

            records = data.get("record", [])
            for r in records:
                print(
                    "LOG:",
                    "enrollid=", r.get("enrollid"),
                    "time=", r.get("time"),
                    "mode=", r.get("mode"),
                    "inout=", r.get("inout"),
                    "event=", r.get("event"),
                    "image=", "YES" if r.get("image") else "NO"
                )

            # إذا الجهاز يرجع دفعات، هذا يطلب الدفعة التالية
            if records:
                next_cmd = {
                    "cmd": "getnewlog",
                    "stn": False
                }
                await websocket.send(json.dumps(next_cmd))
                print("\n--- SENT GET NEXT NEW LOG PACKAGE ---")
                print(next_cmd)

        else:
            print("Unhandled message:", data)


async def main():
    print(f"Pull logs WebSocket server listening on ws://{HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
