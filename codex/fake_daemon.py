"""Fake Codex app-server for tests: WebSocket over a Unix socket, canned
JSON-RPC. Not a model; only enough to exercise client.py and server.py."""
import base64, hashlib, json, os, socket, struct, threading

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeDaemon:
    def __init__(self, path):
        self.path, self.conns, self.log = path, [], []
        self.threads = {"t1": {"id": "t1", "name": None, "preview": "hello", "cwd": "/w", "updatedAt": 1},
                        "t2": {"id": "t2", "name": "second", "preview": "", "cwd": "/w", "updatedAt": 2}}
        self.approvals = []  # decisions received
        self.srv = socket.socket(socket.AF_UNIX); self.srv.bind(path); self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try: c, _ = self.srv.accept()
            except OSError: return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _rd(c, n):
        b = b""
        while len(b) < n:
            d = c.recv(n - len(b))
            if not d: raise ConnectionError
            b += d
        return b

    def _serve(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf: buf += c.recv(4096)
        key = [l.split(b": ")[1] for l in buf.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + GUID).digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        self.conns.append(c)
        try:
            while True:
                b1, b2 = self._rd(c, 2)
                n = b2 & 127
                if n == 126: n = struct.unpack(">H", self._rd(c, 2))[0]
                elif n == 127: n = struct.unpack(">Q", self._rd(c, 8))[0]
                assert b2 & 0x80, "client frames must be masked"
                mk = self._rd(c, 4); p = bytes(x ^ mk[i % 4] for i, x in enumerate(self._rd(c, n)))
                if b1 & 15 == 8: return
                if b1 & 15 == 1: self._handle(c, json.loads(p))
        except (ConnectionError, OSError, AssertionError):
            pass

    def send(self, c, obj):
        p = json.dumps(obj).encode(); n = len(p)
        h = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
        c.sendall(h + p)

    def broadcast(self, obj):
        for c in list(self.conns):
            try: self.send(c, obj)
            except OSError: pass

    def _handle(self, c, m):
        self.log.append(m)
        if "method" not in m:  # response to our server request
            self.approvals.append(m); return
        meth, i, p = m["method"], m.get("id"), m.get("params") or {}
        if i is None: return
        ok = lambda r: self.send(c, {"id": i, "result": r})
        if meth == "initialize": ok({"userAgent": "fake"})
        elif meth == "thread/list": ok({"data": list(self.threads.values()), "nextCursor": None})
        elif meth == "thread/resume":
            t = self.threads.get(p["threadId"])
            if not t: return self.send(c, {"id": i, "error": {"code": -32600, "message": "no such thread"}})
            ok({"thread": dict(t, turns=[{"id": "u0", "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "earlier"}]},
                {"type": "agentMessage", "text": "reply"}]}])})
        elif meth == "turn/start":
            ok({"turn": {"id": "turn1"}})
            tid = p["threadId"]
            self.broadcast({"method": "item/agentMessage/delta", "params": {"threadId": tid, "turnId": "turn1", "itemId": "i1", "delta": "pong"}})
            if p["input"][0]["text"] == "ask":
                self.broadcast({"id": 900, "method": "item/commandExecution/requestApproval",
                                "params": {"threadId": tid, "turnId": "turn1", "itemId": "i2", "command": "rm -rf x"}})
            self.broadcast({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": "turn1", "status": "completed"}}})
        elif meth == "turn/interrupt": ok({})
        else: self.send(c, {"id": i, "error": {"code": -32601, "message": "unknown method"}})

    def close(self):
        self.srv.close()
        for c in self.conns:
            try: c.close()
            except OSError: pass
        try: os.unlink(self.path)
        except OSError: pass
