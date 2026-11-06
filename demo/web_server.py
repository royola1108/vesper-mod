"""Mini service: the only server between the browser and the mod.

    browser --HTTP--> mini service --file inbox--> mod (plugin)

Auth, because a message into a session is shell access on this machine,
and any local web page could otherwise POST to a localhost port:
  * binds 127.0.0.1 only, never 0.0.0.0;
  * every route checks the Host header: it must be a localhost host.
    A page on another origin cannot read this service even by DNS
    rebinding (a domain that resolves to 127.0.0.1 still sends the
    attacker's Host, which is refused);
  * every route also checks Origin/Referer when present: a foreign
    origin is refused;
  * POST /send requires header X-Vesper-Key, equal to VESPER_MOD_TOKEN
    (generated and printed at startup when the variable is unset --
    local demo only, do not paste that log anywhere);
  * GET / serves the demo page with the token injected, so the
    same-origin page can send. A foreign page cannot fetch this page
    to learn the token (no CORS headers are sent).

Lifecycle, because the real mod never deletes an inbox file itself:
  * at startup, sweep() clears leftover inbox files (never .alive or
    acks); if sweep cannot clear, the service does not start
    (fail-closed, so old text is not left on disk);
  * after an ack or a refusal, the service deletes that message's
    inbox file at once -- without this, text would stay on disk
    after the ack;
  * acks stay on disk, so a msg_id that already has one is refused
    (409): the old ack must not pass for the new text;
  * before sending, the service checks alive(): no heartbeat means
    an immediate refusal, not a written file that later times out;
  * on timeout (5 s, no ack, no refusal), the service deletes the
    inbox file FIRST, so a late-starting mod cannot deliver an old
    sentence, then judges by .taking via classify(): taking present
    means unknown, absent means not delivered.

Demo limits, stated plainly: one token, no TLS, no multi-user
isolation. Do not expose this service.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mod_route import InboxNotCleared, ModRoute  # noqa: E402

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def make_handler(route: ModRoute, token: str, session: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the demo output readable
            pass

        def _send_json(self, status: int, value: dict) -> None:
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _checks_ok(self) -> bool:
            # Host first: this is what stops DNS rebinding, and it
            # applies to GET / and every other route, not just POST.
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
            if host not in LOCAL_HOSTS:
                self._send_json(403, {"error": "foreign_host"})
                return False
            for header in ("Origin", "Referer"):
                value = self.headers.get(header)
                if value and urlparse(value).hostname not in LOCAL_HOSTS:
                    self._send_json(403, {"error": "foreign_origin"})
                    return False
            return True

        def do_GET(self):
            if not self._checks_ok():
                return
            path = urlparse(self.path).path
            if path == "/":
                html = (Path(__file__).resolve().parent / "index.html").read_bytes()
                # The served page is same-origin, so it may carry the token;
                # a foreign page cannot fetch this page to learn it (no CORS).
                html = html.replace(b"__VESPER_TOKEN__", token.encode())
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            else:
                self._send_json(404, {"error": "unknown_route"})

        def do_POST(self):
            if not self._checks_ok():
                return
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad_json"})
                return
            if path == "/send":
                if not secrets.compare_digest(self.headers.get("X-Vesper-Key") or "", token):
                    self._send_json(401, {"error": "bad_token"})
                    return
                msg_id, text = body.get("msg_id"), body.get("text")
                if not isinstance(msg_id, str) or not isinstance(text, str):
                    self._send_json(400, {"error": "msg_id_and_text_required"})
                    return
                if route.ack_path(msg_id).exists() or route.taking_exists(msg_id):
                    self._send_json(409, {"error": "duplicate_msg_id"})
                    return
                # No heartbeat: refuse at once, do not write a file that
                # would sit on disk and time out later.
                if not route.alive(session):
                    self._send_json(503, {"error": "mod_not_alive"})
                    return
                path = route.write_inbox(session, msg_id, text)
                deadline = time.time() + 5
                while time.time() < deadline:
                    ack = route.read_ack(msg_id)
                    if ack is not None:
                        route.remove_inbox(path)
                        self._send_json(200, {"outcome": "acked", "ack": ack})
                        return
                    refused = route.read_refusal(msg_id)
                    if refused is not None:
                        route.remove_inbox(path)
                        self._send_json(200, {"outcome": "refused", "refused": refused})
                        return
                    time.sleep(0.1)
                # Timeout: delete FIRST, so a late mod cannot deliver
                # this sentence afterwards, then judge by .taking.
                route.remove_inbox(path)
                # At timeout the mod has not answered for this message,
                # so judge as if not alive: taking present -> unknown,
                # absent -> pending, which here means not delivered
                # (the file is gone, so it can never be taken now).
                outcome = route.classify(msg_id, mod_alive=False)
                if outcome == "pending":
                    outcome = "not_delivered"
                self._send_json(504, {"error": "no_ack", "outcome": outcome})
            else:
                self._send_json(404, {"error": "unknown_route"})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--session", default="demo-session",
                        help="the session id the mod is bound to; "
                        "use the real session id when running a real session. "
                        "After /clear the id changes: restart the service "
                        "with the new id, or sends will be refused")
    args = parser.parse_args()
    route = ModRoute(Path(args.state_dir))
    try:
        cleared = route.sweep()
    except InboxNotCleared:
        print("[web] refused to start: inbox could not be cleared, "
              "old text may still be on disk", flush=True)
        return 1
    if cleared:
        print(f"[web] sweep cleared {cleared} leftover inbox file(s)", flush=True)
    token = os.environ.get("VESPER_MOD_TOKEN") or secrets.token_hex(16)
    if "VESPER_MOD_TOKEN" not in os.environ:
        print("[web] generated a one-run token (set VESPER_MOD_TOKEN to fix it): "
              f"{token} -- do not paste this log anywhere", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(route, token, args.session))
    print(f"[web] mini service on http://127.0.0.1:{args.port}/ "
          f"(localhost only; /send requires a token; session={args.session})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
