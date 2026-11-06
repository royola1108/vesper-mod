import http.client, json, os, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from client import Client, parse_endpoint
from fake_daemon import FakeDaemon
import server

PORT = 18765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = "http://" + HOST


def wait(cond, t=3):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return False


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.sock = os.path.join(cls.dir, "d.sock")
        cls.d = FakeDaemon(cls.sock)
        cls.st = server.State("unix://" + cls.sock, None)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.make_handler(cls.st, "pw-test-1234", {HOST, f"localhost:{PORT}"}))
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.d.close()

    def req(self, method, path, body=None, host=HOST, origin=ORIGIN, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        h = {"Host": host}
        if origin: h["Origin"] = origin
        if cookie: h["Cookie"] = cookie
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse(); data = r.read()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data), r

    def login(self):
        s, _, r = self.req("POST", "/api/login", {"password": "pw-test-1234"})
        self.assertEqual(s, 200)
        return r.getheader("Set-Cookie").split(";")[0]

    def test_endpoint_policy(self):
        self.assertEqual(parse_endpoint("unix:///a/b.sock"), ("unix", "/a/b.sock"))
        self.assertEqual(parse_endpoint("ws://127.0.0.1:9")[0], "tcp")
        for bad in ("ws://10.0.0.5:9", "wss://example.com:9", "http://127.0.0.1:9", "unix://rel"):
            with self.assertRaises(ValueError): parse_endpoint(bad)

    def test_raw_client(self):
        got = []
        c = Client("unix://" + self.sock, on_notification=got.append); c.connect(); c.initialize()
        self.assertEqual(c.call("thread/list")["data"][0]["id"], "t1")
        c.close()

    def test_guards(self):
        self.assertEqual(self.req("GET", "/", host="evil.example:80", origin=None)[0], 403)  # DNS rebinding
        self.assertEqual(self.req("POST", "/api/login", {"password": "pw-test-1234"}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/login", {"password": "pw-test-1234"}, origin=None)[0], 403)
        self.assertEqual(self.req("GET", "/api/threads", origin=None)[0], 401)
        self.assertEqual(self.req("POST", "/api/send", {"text": "x"})[0], 401)
        self.assertEqual(self.req("POST", "/api/login", {"password": "nope"})[0], 401)
        self.assertEqual(self.req("GET", "/", origin=None)[0], 200)

    def test_flow_and_approval_refusal(self):
        ck = self.login()
        s, ts, _ = self.req("GET", "/api/threads", cookie=ck)
        self.assertEqual([t["id"] for t in ts], ["t1", "t2"])
        s, r, _ = self.req("POST", "/api/attach", {"threadId": "t1"}, cookie=ck)
        self.assertEqual(r["history"], [{"role": "user", "text": "earlier"}, {"role": "agent", "text": "reply"}])
        self.assertEqual(self.req("POST", "/api/attach", {"threadId": "nope"}, cookie=ck)[0], 502)
        self.req("POST", "/api/attach", {"threadId": "t1"}, cookie=ck)
        ev = []
        q = __import__("queue").Queue(); self.st.subs.append(q)
        s, r, _ = self.req("POST", "/api/send", {"text": "ask"}, cookie=ck)
        self.assertEqual(s, 200)
        self.assertTrue(wait(lambda: self.d.approvals), "approval request must get an answer")
        ans = self.d.approvals[-1]
        self.assertEqual(ans["id"], 900); self.assertEqual(ans["result"], {"decision": "decline"})
        kinds = []
        while not q.empty(): kinds.append(q.get())
        self.assertTrue(any(e["kind"] == "rpc" and e["params"].get("delta") == "pong" for e in kinds))
        self.assertTrue(any(e["kind"] == "notice" and e["answered"] == "declined" for e in kinds))
        # another client's turn: not answered, only surfaced
        n = len(self.d.approvals)
        self.d.broadcast({"id": 901, "method": "item/commandExecution/requestApproval", "params": {"threadId": "t1", "turnId": "cli-turn"}})
        self.assertTrue(wait(lambda: any(e["kind"] == "notice" and e["answered"] == "left to another client" for e in list(q.queue))))
        time.sleep(0.2); self.assertEqual(len(self.d.approvals), n)
        self.assertEqual(self.req("POST", "/api/interrupt", {"turnId": "turn1"}, cookie=ck)[0], 200)

    def test_send_needs_attach(self):
        ck = self.login(); self.st.thread = None
        self.assertEqual(self.req("POST", "/api/send", {"text": "x"}, cookie=ck)[0], 409)


if __name__ == "__main__":
    unittest.main()
