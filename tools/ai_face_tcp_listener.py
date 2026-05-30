"""
AI Face 11 — Raw TCP Protocol Discovery Listener  (v2)
=======================================================
Usage:
  python tools/ai_face_tcp_listener.py [OPTIONS]

Options:
  --port  PORT          TCP port to listen on (default: 7005)
  --save                Write each full payload to tools/captures/
  --ack   MODE          ACK to send after draining a connection:
                          ok-crlf   (default) → b"OK\\r\\n"
                          ok                  → b"OK"
                          none                → send nothing
                          echo-chunk          → echo back the last received chunk
                          echo-full           → echo back the entire payload
                          hex                 → send --ack-hex bytes
  --ack-hex HEX         Hex string to send when --ack hex is used
                        Example: --ack hex --ack-hex "5a a5 00 00"

Interactive controls (while the listener is running):
  Press Enter  →  mark the *next* incoming frame as [AFTER TRIGGER]
               so you can tell apart heartbeats from an attendance event
               you deliberately trigger.
  Ctrl-C       →  stop

Frame diff:
  For every new same-length frame, bytes that changed vs the previous
  frame of that length are printed in a compact diff line.
"""

import argparse
import os
import struct
import socket
import sys
import threading
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
HOST         = "0.0.0.0"
DEFAULT_PORT = 7005
RECV_SIZE    = 8192
IDLE_TIMEOUT = 5      # seconds idle before closing

# ── Global state ───────────────────────────────────────────────────────────────
_seq_lock  = threading.Lock()
_seq       = 0

# Marker: press Enter to label the next frame specially
_mark_event = threading.Event()

# Frame diff: store last seen payload per length
_diff_lock     = threading.Lock()
_prev_by_len: dict[int, bytes] = {}

# ── Helpers ────────────────────────────────────────────────────────────────────

def hex_dump(data: bytes, width: int = 16) -> str:
    """Classic hex+ASCII dump, 16 bytes per row."""
    lines = []
    for i in range(0, len(data), width):
        chunk    = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {i:04x}  {hex_part:<{width * 3}}  {asc_part}")
    return "\n".join(lines)


def guess_fields(data: bytes) -> None:
    """Print printable substrings ≥4 chars and 4-byte LE Unix timestamps."""
    print("  [field hints]")

    buf, start = [], None
    for i, b in enumerate(data):
        if 32 <= b < 127:
            if start is None:
                start = i
            buf.append(chr(b))
        else:
            if len(buf) >= 4:
                print(f"    string @ {start:#06x}: {''.join(buf)!r}")
            buf, start = [], None
    if len(buf) >= 4:
        print(f"    string @ {start:#06x}: {''.join(buf)!r}")

    TS_MIN = 1_577_836_800   # 2020-01-01
    TS_MAX = 2_051_222_400   # 2035-01-01
    for i in range(len(data) - 3):
        val = struct.unpack_from("<I", data, i)[0]
        if TS_MIN <= val <= TS_MAX:
            ts = datetime.utcfromtimestamp(val)
            print(f"    u32-LE timestamp @ {i:#06x}: {val} → {ts} UTC")


def frame_diff(new: bytes) -> str | None:
    """
    Compare `new` against the last stored payload of the same length.
    Returns a compact diff string, or None if this is the first frame of that length.
    Updates the stored frame.
    """
    length = len(new)
    with _diff_lock:
        prev = _prev_by_len.get(length)
        _prev_by_len[length] = new

    if prev is None:
        return None

    changes = [(i, prev[i], new[i]) for i in range(length) if prev[i] != new[i]]
    if not changes:
        return "  [diff] identical to previous same-length frame"

    parts = [f"{i:#06x}: {old:02x}→{new_b:02x}" for i, old, new_b in changes]
    return "  [diff] " + "  ".join(parts)


def build_ack(mode: str, ack_hex: bytes, last_chunk: bytes, full_payload: bytes) -> bytes:
    if mode == "none":
        return b""
    if mode == "ok-crlf":
        return b"OK\r\n"
    if mode == "ok":
        return b"OK"
    if mode == "echo-chunk":
        return last_chunk
    if mode == "echo-full":
        return full_payload
    if mode == "hex":
        return ack_hex
    return b"OK\r\n"


# ── Marker stdin thread ────────────────────────────────────────────────────────

def _stdin_marker_thread() -> None:
    """Background thread: press Enter to set the mark event."""
    print("[marker] Press Enter at any time to label the NEXT frame as [AFTER TRIGGER]")
    try:
        while True:
            sys.stdin.readline()
            _mark_event.set()
            print("\n[marker] >>> MARKER SET — next frame will be labelled <<<\n")
    except Exception:
        pass


# ── Per-connection handler ──────────────────────────────────────────────────────

def handle_connection(
    conn: socket.socket,
    addr: tuple,
    save_dir: str | None,
    ack_mode: str,
    ack_hex: bytes,
) -> None:
    global _seq
    conn.settimeout(IDLE_TIMEOUT)

    with _seq_lock:
        _seq += 1
        seq = _seq

    marked = _mark_event.is_set()
    if marked:
        _mark_event.clear()

    ts_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    sep      = "=" * 72

    label = " *** [AFTER TRIGGER] ***" if marked else ""
    print(f"\n{sep}")
    print(f"  CONNECTION #{seq}{label}")
    print(f"  From : {addr[0]}:{addr[1]}")
    print(f"  At   : {ts_label}")
    print(sep)

    full_payload = b""
    last_chunk   = b""
    chunk_no     = 0

    try:
        while True:
            try:
                chunk = conn.recv(RECV_SIZE)
            except TimeoutError:
                if full_payload:
                    print("  [idle timeout — device stopped sending]")
                break

            if not chunk:
                print("  [device closed connection]")
                break

            chunk_no    += 1
            last_chunk   = chunk
            full_payload += chunk
            fts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            print(f"\n--- chunk #{chunk_no}  {len(chunk)} bytes  @ {fts} ---")
            print(f"  raw : {chunk!r}")
            print(f"  hex :\n{hex_dump(chunk)}")
            try:
                decoded = chunk.decode("utf-8", errors="replace")
                print(f"  utf8: {decoded!r}")
            except Exception:
                pass

    except Exception as exc:
        print(f"  [recv error: {exc}]")

    # ── Full payload summary ───────────────────────────────────────────────────
    if full_payload:
        print(f"\n--- full payload  {len(full_payload)} bytes ---")
        print(f"  hex :\n{hex_dump(full_payload)}")
        guess_fields(full_payload)

        # Frame diff vs previous same-length frame
        diff = frame_diff(full_payload)
        if diff is not None:
            print(diff)

        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            fname_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            suffix   = "_TRIGGER" if marked else ""
            path     = os.path.join(save_dir, f"{fname_ts}_seq{seq:04d}{suffix}.bin")
            with open(path, "wb") as fh:
                fh.write(full_payload)
            print(f"  saved → {path}")
    else:
        print("  [no data received]")

    # ── Send ACK ───────────────────────────────────────────────────────────────
    ack_bytes = build_ack(ack_mode, ack_hex, last_chunk, full_payload)
    if ack_bytes:
        try:
            conn.sendall(ack_bytes)
            print(f"  [sent ACK] {ack_bytes!r}  ({len(ack_bytes)} bytes)  hex: {ack_bytes.hex(' ')}")
        except Exception as e:
            print(f"  [ACK send error: {e}]")
    else:
        print("  [sent ACK] (none — --ack none)")

    conn.close()
    print(f"\n{sep}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Face 11 TCP protocol discovery listener (v2)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--port",    type=int, default=DEFAULT_PORT,
                        help=f"TCP port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--save",    action="store_true",
                        help="Save each full payload as a .bin file in tools/captures/")
    parser.add_argument("--ack",     default="ok-crlf",
                        choices=["ok-crlf", "ok", "none", "echo-chunk", "echo-full", "hex"],
                        help=(
                            "ACK to send after draining the connection:\n"
                            "  ok-crlf     b'OK\\r\\n'  (default)\n"
                            "  ok          b'OK'\n"
                            "  none        send nothing\n"
                            "  echo-chunk  echo the last received chunk\n"
                            "  echo-full   echo the entire accumulated payload\n"
                            "  hex         send --ack-hex bytes"
                        ))
    parser.add_argument("--ack-hex", default="",
                        help="Hex bytes to send when --ack hex is used. "
                             'Example: "5a a5 00 00" or "5aa50000"')
    args = parser.parse_args()

    # Parse --ack-hex
    ack_hex = b""
    if args.ack == "hex":
        raw = args.ack_hex.replace(" ", "")
        if not raw:
            parser.error("--ack hex requires --ack-hex <hex string>")
        try:
            ack_hex = bytes.fromhex(raw)
        except ValueError as e:
            parser.error(f"--ack-hex is not valid hex: {e}")

    save_dir = os.path.join(os.path.dirname(__file__), "captures") if args.save else None

    # Start the stdin marker thread
    t_marker = threading.Thread(target=_stdin_marker_thread, daemon=True)
    t_marker.start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, args.port))
        srv.listen(10)

        print(f"\nAI Face 11 TCP listener  (v2)")
        print(f"  listening      : {HOST}:{args.port}")
        print(f"  ACK mode       : {args.ack}" +
              (f"  →  {ack_hex.hex(' ')}" if args.ack == "hex" else ""))
        print(f"  save captures  : {'yes → ' + save_dir if save_dir else 'no  (--save to enable)'}")
        print(f"  idle timeout   : {IDLE_TIMEOUT}s per connection")
        print(f"  frame diff     : enabled — changed bytes highlighted per same-length frame")
        print(f"  Ctrl-C to stop\n")

        try:
            while True:
                conn, addr = srv.accept()
                t = threading.Thread(
                    target=handle_connection,
                    args=(conn, addr, save_dir, args.ack, ack_hex),
                    daemon=True,
                )
                t.start()
        except KeyboardInterrupt:
            print("\n[stopped]")


if __name__ == "__main__":
    main()
