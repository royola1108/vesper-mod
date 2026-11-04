"""Prove the mini service wire without a browser.

    python3 demo/run_web_demo.py

Starts, all on localhost:
  * fake_mod.py      (the mod stand-in, polling the file inbox)
  * web_server.py    (the mini service)

Then, over plain HTTP, it checks:
  1. startup sweep: a leftover inbox file is gone once the service is up;
  2. browser -> service -> mod: POST /send, expect an ack with no text,
     and the inbox file gone afterwards (the service deletes it, since
     the real mod never deletes its own inbox file);
  3. auth, which must be there even for a single user: /send without
     a token -> 401, /send from a foreign Origin -> 403, and any
     request with a foreign Host -> 403 (this is what stops DNS
     rebinding from reading GET / or any other route);
  4. timeout: a heartbeat-only mod -> 504 with outcome not_delivered,
     inbox file deleted, no .taking;
  5. timeout: a take-and-hang mod -> 504 with outcome unknown,
     inbox file deleted, .taking present;
  6. no heartbeat: /send -> 503 mod_not_alive, no inbox file written.

Exit code 0 means all checks passed. The only processes here are the
mod stand-ins and the service, all on 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"
SESSION = "demo-session"

TOKEN = "vesper-demo-token"


def post(path: str, value: dict, *, headers: dict | None = None) -> tuple[int, dict]:
    all_headers = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(value).encode(),
                                 headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_http_ok(path: str, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + path, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}")


def inbox_path(state: str, msg_id: str) -> Path:
    stem = hashlib.sha256(msg_id.encode()).hexdigest()
    return Path(state) / "mod-inbox" / SESSION / f"{stem}.json"


def taking_path(state: str, msg_id: str) -> Path:
    stem = hashlib.sha256(msg_id.encode()).hexdigest()
    return Path(state) / "mod-acks" / f"{stem}.taking"


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vesper-mod-web-") as tmp:
        state = tmp
        import os
        server_env = dict(os.environ, VESPER_MOD_TOKEN=TOKEN)

        def start_mod(extra: list[str] | None = None) -> subprocess.Popen:
            cmd = [sys.executable, str(HERE / "fake_mod.py"),
                   "--state-dir", state, "--session", SESSION]
            if extra:
                cmd += extra
            return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)

        # 1. Startup sweep: leave a file behind before the service starts.
        leftover = inbox_path(state, "leftover-1")
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text(json.dumps({"msg_id": "leftover-1", "text": "old"}))
        server = subprocess.Popen([sys.executable, str(HERE / "web_server.py"),
                                   "--state-dir", state, "--port", str(PORT),
                                   "--session", SESSION],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  env=server_env)
        mod = None
        try:
            wait_http_ok("/")
            assert not leftover.exists(), "startup sweep must clear leftover inbox files"
            print("1. startup sweep cleared a leftover inbox file: OK")

            mod = start_mod()
            time.sleep(1.5)  # let the mod heartbeat land
            print("2. mini service + mod are up (localhost only)")

            status, result = post("/send", {"msg_id": "web-1", "text": "hello from the page"},
                                  headers={"X-Vesper-Key": TOKEN})
            assert status == 200 and result["outcome"] == "acked", result
            assert "hello from the page" not in json.dumps(result["ack"])
            assert not inbox_path(state, "web-1").exists(), \
                "service must delete the inbox file after the ack"
            print(f"3. POST /send acked, ack has no text, inbox file gone: OK ({result['ack']})")

            status, _ = post("/send", {"msg_id": "web-9", "text": "no token"})
            assert status == 401, status
            status, _ = post("/send", {"msg_id": "web-9", "text": "foreign"},
                             headers={"X-Vesper-Key": TOKEN, "Origin": "https://evil.example"})
            assert status == 403, status
            status, _ = post("/send", {"msg_id": "web-9", "text": "foreign host"},
                             headers={"X-Vesper-Key": TOKEN, "Host": "evil.example"})
            assert status == 403, status
            print("4. auth: no token -> 401, foreign Origin -> 403, foreign Host -> 403: OK")

            # 5. Timeout, not delivered: heartbeat only, never takes.
            stop(mod)
            mod = start_mod(extra=["--heartbeat-only"])
            time.sleep(1.5)
            status, result = post("/send", {"msg_id": "web-2", "text": "nobody takes me"},
                                  headers={"X-Vesper-Key": TOKEN})
            assert status == 504 and result.get("outcome") == "not_delivered", result
            assert not inbox_path(state, "web-2").exists(), "timeout must delete the file first"
            assert not taking_path(state, "web-2").exists(), "never taken, so no .taking"
            print("5. timeout with no take -> 504 not_delivered, file deleted: OK")

            # 6. Timeout, unknown: took it, never acked.
            stop(mod)
            mod = start_mod(extra=["--take-and-hang"])
            time.sleep(1.5)
            status, result = post("/send", {"msg_id": "web-3", "text": "took then hung"},
                                  headers={"X-Vesper-Key": TOKEN})
            assert status == 504 and result.get("outcome") == "unknown", result
            assert not inbox_path(state, "web-3").exists(), "timeout must delete the file first"
            assert taking_path(state, "web-3").exists(), "taken, so .taking must be present"
            print("6. timeout after take -> 504 unknown, file deleted, .taking present: OK")

            # 7. No heartbeat: refuse at once, write nothing.
            stop(mod)
            mod = None
            print("7. waiting out the 5 s heartbeat window...")
            time.sleep(6)
            status, result = post("/send", {"msg_id": "web-4", "text": "nobody home"},
                                  headers={"X-Vesper-Key": TOKEN})
            assert status == 503 and result.get("error") == "mod_not_alive", result
            assert not inbox_path(state, "web-4").exists(), "no heartbeat means no file written"
            print("7. no heartbeat -> 503 mod_not_alive, no file written: OK")

            print("\nWeb demo passed: browser -> mini service -> mod, "
                  "localhost only, token required, text deleted after result.")
            return 0
        finally:
            if mod is not None:
                stop(mod)
            stop(server)


if __name__ == "__main__":
    raise SystemExit(main())
