"""Local web UI for a running Codex app-server daemon. Stdlib only.

127.0.0.1 only; password login; Host and Origin checked. One attached
thread at a time (single user). Approval requests are never auto-approved."""
import argparse, hmac, json, os, queue, secrets, threading, time, uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from client import Client, RpcError

APPROVALS = ("requestApproval", "applyPatchApproval", "execCommandApproval")


class State:
    def __init__(self, endpoint, token):
        self.sessions, self.subs, self.my_turns = set(), [], set()
        self.thread = None
        self.lock = threading.Lock()
        self.c = Client(endpoint, token, self.on_notification, self.on_request)
        self.c.connect(); self.c.initialize()

    def emit(self, ev):
        for q in list(self.subs):
            q.put(ev)

    def on_notification(self, m):
        p = m.get("params") or {}
        if p.get("threadId") in (None, self.thread):
            self.emit({"kind": "rpc", "method": m.get("method"), "params": p})

    def on_request(self, m):
        """Never approve. Our own turns: refuse. Others' turns: leave to the client that owns them."""
        p, meth = m.get("params") or {}, m.get("method", "")
        mine = p.get("turnId") in self.my_turns
        self.emit({"kind": "notice", "method": meth, "params": p, "answered": "declined" if mine else "left to another client"})
        if not mine:
            return None
        if meth.endswith(APPROVALS):
            return {"decision": "denied" if meth in APPROVALS[1:] else "decline"}
        return {"error": {"code": -32001, "message": "this web client cannot answer that request"}}


def make_handler(st, password, allowed_hosts):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass

        def _json(self, obj, code=200, headers=()):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
            for k, v in headers: self.send_header(k, v)
            self.end_headers(); self.wfile.write(b)

        def _guard(self, post):
            if self.headers.get("Host") not in allowed_hosts:
                self._json({"error": "bad host"}, 403); return False
            o = self.headers.get("Origin")
            if (post and o is None) or (o is not None and o not in {"http://" + h for h in allowed_hosts}):
                self._json({"error": "bad origin"}, 403); return False
            return True

        def _authed(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            ok = "sid" in c and c["sid"].value in st.sessions
            if not ok: self._json({"error": "login required"}, 401)
            return ok

        def do_GET(self):
            if not self._guard(False): return
            if self.path == "/":
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), "rb") as f: b = f.read()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
                self.end_headers(); self.wfile.write(b); return
            if not self._authed(): return
            if self.path == "/api/threads":
                try:
                    r = st.c.call("thread/list", {"limit": 50})
                    self._json([{k: t.get(k) for k in ("id", "name", "preview", "cwd", "updatedAt")} for t in r["data"]])
                except RpcError as e: self._json({"error": str(e)}, 502)
            elif self.path == "/api/events":
                q = queue.Queue(); st.subs.append(q)
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store"); self.send_header("Connection", "close")
                self.end_headers(); self.close_connection = True
                try:
                    while True:
                        try: ev = q.get(timeout=15); data = "data: " + json.dumps(ev) + "\n\n"
                        except queue.Empty: data = ": ping\n\n"
                        self.wfile.write(data.encode()); self.wfile.flush()
                except OSError: pass
                finally: st.subs.remove(q)
            else: self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._guard(True): return
            try: body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1 << 20)) or b"{}")
            except ValueError: return self._json({"error": "bad json"}, 400)
            if self.path == "/api/login":
                if hmac.compare_digest(str(body.get("password", "")).encode(), password.encode()):
                    sid = secrets.token_urlsafe(32); st.sessions.add(sid)
                    return self._json({"ok": True}, headers=[("Set-Cookie", f"sid={sid}; HttpOnly; SameSite=Strict; Path=/")])
                time.sleep(1); return self._json({"error": "wrong password"}, 401)
            if not self._authed(): return
            try:
                if self.path == "/api/attach":
                    r = st.c.call("thread/resume", {"threadId": str(body["threadId"])})
                    st.thread = r["thread"]["id"]
                    msgs = []
                    for t in r["thread"].get("turns") or []:
                        for it in t.get("items") or []:
                            if it.get("type") == "agentMessage": msgs.append({"role": "agent", "text": it.get("text", "")})
                            elif it.get("type") == "userMessage":
                                msgs.append({"role": "user", "text": "".join(c.get("text", "") for c in it.get("content", []))})
                    self._json({"threadId": st.thread, "history": msgs})
                elif self.path == "/api/send":
                    if not st.thread: return self._json({"error": "attach a thread first"}, 409)
                    r = st.c.call("turn/start", {"threadId": st.thread, "clientUserMessageId": str(uuid.uuid4()),
                                                  "input": [{"type": "text", "text": str(body["text"]), "text_elements": []}]})
                    st.my_turns.add(r["turn"]["id"]); self._json({"turnId": r["turn"]["id"]})
                elif self.path == "/api/interrupt":
                    if not (st.thread and body.get("turnId")): return self._json({"error": "no turn"}, 409)
                    st.c.call("turn/interrupt", {"threadId": st.thread, "turnId": str(body["turnId"])}); self._json({})
                else: self._json({"error": "not found"}, 404)
            except (KeyError, TypeError): self._json({"error": "bad request"}, 400)
            except RpcError as e: self._json({"error": str(e)}, 502)
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", required=True, help="unix:///abs/path.sock or ws://127.0.0.1:PORT")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token-env", help="env var holding the daemon's bearer token (ws only)")
    a = ap.parse_args()
    pw = os.environ.get("CODEX_SHARED_PASSWORD") or secrets.token_urlsafe(12)
    if not os.environ.get("CODEX_SHARED_PASSWORD"): print("password (random):", pw)
    st = State(a.endpoint, os.environ.get(a.token_env) if a.token_env else None)
    hosts = {f"127.0.0.1:{a.port}", f"localhost:{a.port}"}
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(st, pw, hosts))
    srv.daemon_threads = True
    print(f"http://127.0.0.1:{a.port}/"); srv.serve_forever()


if __name__ == "__main__":
    main()
