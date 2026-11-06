"""Minimal Codex app-server client: WebSocket (RFC 6455) over a Unix socket
or loopback TCP, JSON-RPC frames, stdlib only. Reader runs in a thread."""
import base64, json, os, socket, struct, threading, itertools
from urllib.parse import urlsplit


class RpcError(RuntimeError):
    pass


def parse_endpoint(url):
    """Return ('unix', path) or ('tcp', (host, port)). Refuses non-local ws."""
    u = urlsplit(url)
    if u.scheme == "unix" and u.path.startswith("/") and not u.netloc:
        return "unix", u.path
    if u.scheme == "ws" and u.hostname in ("127.0.0.1", "localhost", "::1") and u.port:
        return "tcp", (u.hostname, u.port)
    raise ValueError("endpoint must be unix:///abs/path or ws://127.0.0.1:PORT "
                     "(plaintext non-local ws is refused)")


class Client:
    def __init__(self, endpoint, token=None, on_notification=None, on_request=None):
        self.kind, self.addr = parse_endpoint(endpoint)
        self.token = token
        self.on_notification = on_notification or (lambda m: None)
        self.on_request = on_request or (lambda m: {"error": {"code": -32601, "message": "unsupported"}})
        self._ids = itertools.count(1)
        self._pending = {}
        self._wlock = threading.Lock()
        self.closed = threading.Event()

    # --- transport
    def connect(self):
        if self.kind == "unix":
            s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(self.addr)
        else:
            s = socket.create_connection(self.addr, timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        hdr = ["GET / HTTP/1.1", "Host: localhost", "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        if self.token:
            hdr.append(f"Authorization: Bearer {self.token}")
        s.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise ConnectionError("closed during handshake")
            buf += c
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("websocket upgrade refused: " + head.split(b"\r\n")[0].decode(errors="replace"))
        s.settimeout(None)
        self.sock = s
        threading.Thread(target=self._reader, daemon=True).start()

    def _send_frame(self, opcode, payload=b""):
        n = len(payload)
        h = bytes([0x80 | opcode])
        h += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                               else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._wlock:
            self.sock.sendall(h + mask + body)

    def _read(self, n):
        while len(self._buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError("closed")
            self._buf += c
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _reader(self):
        msg = b""
        try:
            while True:
                b1, b2 = self._read(2)
                op, n = b1 & 15, b2 & 127
                if n == 126: n = struct.unpack(">H", self._read(2))[0]
                elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
                data = self._read(n)  # server frames are unmasked
                if op == 9: self._send_frame(10, data); continue
                if op == 8: break
                if op in (1, 2): msg = data
                elif op == 0: msg += data
                if b1 & 0x80 and op in (0, 1, 2):
                    self._dispatch(json.loads(msg)); msg = b""
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.closed.set()
            for ev in list(self._pending.values()):
                ev[0].set()

    def _dispatch(self, m):
        if "id" in m and "method" not in m:  # response
            ev = self._pending.get(m["id"])
            if ev:
                ev[1] = m; ev[0].set()
        elif "id" in m:  # server request
            threading.Thread(target=self._answer, args=(m,), daemon=True).start()
        else:
            self.on_notification(m)

    def _answer(self, m):
        r = self.on_request(m)
        if r is None:  # deliberately left to another client
            return
        out = {"id": m["id"]}
        out.update(r if "error" in r else {"result": r})
        self._send_frame(1, json.dumps(out).encode())

    # --- rpc
    def call(self, method, params=None, timeout=30):
        i = next(self._ids)
        ev = [threading.Event(), None]
        self._pending[i] = ev
        try:
            self._send_frame(1, json.dumps({"method": method, "id": i, "params": params}).encode())
            if not ev[0].wait(timeout) or ev[1] is None:
                raise RpcError(f"{method}: no response (timeout or connection closed)")
        finally:
            self._pending.pop(i, None)
        if "error" in ev[1]:
            raise RpcError(f"{method}: {json.dumps(ev[1]['error'])}")
        return ev[1].get("result")

    def notify(self, method, params=None):
        self._send_frame(1, json.dumps({"method": method, "params": params or {}}).encode())

    def initialize(self, name="codex_shared_web", version="0.1.0"):
        r = self.call("initialize", {"clientInfo": {"name": name, "title": "codex-shared", "version": version},
                                     "capabilities": {}})
        self.notify("initialized")
        return r

    def close(self):
        try:
            self._send_frame(8, b"")
            self.sock.close()
        except OSError:
            pass
