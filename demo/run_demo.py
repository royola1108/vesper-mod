"""Run the whole demo end to end. No Claude Code needed.

    python3 demo/run_demo.py

What happens, in order:
  1. A fake mod process starts polling a fresh temp state dir.
  2. The sender checks the heartbeat is alive, then sends a normal
     message and waits for its ack. The ack is printed and asserted to
     contain no message text.
  3. A second fake mod process is started; it must NOT re-take the next
     message (the first mod's .taking marker wins) -- only one ack appears.
  4. A busy message is sent (busy.flag set, so the fake engine appends
     instead of submitting fresh: door "note" instead of "prompt").
  5. A refused message is sent (its msg_id is put in refuse-next): the
     sender reads a refusal, not an ack.
  6. Both mod processes are stopped; after the 5 s heartbeat window the
     sender reports the session as not alive.
  7. A crash mod takes one message and dies before any ack: the sender
     classifies it "unknown", and a restarted mod does not re-ack it
     (the .taking marker still guards it). It is never resent.

Total runtime is about 15 s, most of it the heartbeat-expiry wait in
step 6. Exit code 0 means every check passed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION = "demo-session"


def wait_for(predicate, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def main() -> int:
    from mod_route import ModRoute

    with tempfile.TemporaryDirectory(prefix="vesper-mod-demo-") as tmp:
        state = Path(tmp)
        route = ModRoute(state)
        assert route.sweep() == 0

        def start_mod(extra: list[str] | None = None) -> subprocess.Popen:
            cmd = [sys.executable, str(HERE / "fake_mod.py"),
                   "--state-dir", str(state), "--session", SESSION]
            if extra:
                cmd += extra
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            return proc

        mod = start_mod()
        try:
            print(f"state dir: {state}")
            wait_for(lambda: route.alive(SESSION), 5, "heartbeat")
            print("1. heartbeat alive: OK")

            # 2. Normal message.
            msg1 = "hello-from-the-web"
            path1 = route.write_inbox(SESSION, "msg-1", msg1)
            assert oct(path1.stat().st_mode & 0o777) == "0o600"
            wait_for(lambda: route.read_ack("msg-1") is not None, 5, "ack for msg-1")
            ack1 = route.read_ack("msg-1")
            assert ack1 is not None and msg1 not in json.dumps(ack1)
            assert not path1.exists(), "inbox file must be gone after take"
            print(f"2. normal message acked, ack has no text: OK ({ack1})")

            # 3. Duplicate-take guard, sequential pollers only: the second
            # poller starts after the first took the message, so it sees
            # .taking and skips it. A truly simultaneous race is not
            # covered by the real hook's check-then-write taking.
            mod2 = start_mod()
            try:
                route.write_inbox(SESSION, "msg-2", "second-message")
                wait_for(lambda: route.read_ack("msg-2") is not None, 5, "ack for msg-2")
                ack2 = route.read_ack("msg-2")
                assert ack2 is not None
                time.sleep(1.5)  # a double take would have produced a second ack file
                assert route.read_ack("msg-2") == ack2
                print("3. second poller did not re-take: OK (exactly one ack)")
            finally:
                mod2.terminate()
                mod2.wait(timeout=5)

            # 4. Busy message -> appended, door "note", plus a nudge.
            # The ack alone does not mean the model read it; the nudge
            # is what re-opens a round for the appended line.
            (state / "busy.flag").write_text("1")
            route.write_inbox(SESSION, "msg-3", "while-you-are-busy")
            wait_for(lambda: route.read_ack("msg-3") is not None, 5, "ack for msg-3")
            assert route.read_ack("msg-3")["door"] == "note"
            wait_for(lambda: (state / ".nudge-log").exists()
                     and "msg-3" in (state / ".nudge-log").read_text(),
                     5, "nudge for msg-3")
            (state / "busy.flag").unlink()
            print("4. busy message appended (door=note) and nudged: OK")

            # 5. Refused message -> refusal, never an ack.
            refuse_file = state / "refuse-next.txt"
            refuse_file.write_text("msg-4")
            mod.terminate()
            mod.wait(timeout=5)
            mod = start_mod(extra=["--refuse-next", str(refuse_file)])
            wait_for(lambda: route.alive(SESSION), 5, "heartbeat after restart")
            route.write_inbox(SESSION, "msg-4", "please-refuse-me")
            wait_for(lambda: route.read_refusal("msg-4") is not None, 5, "refusal for msg-4")
            assert route.read_ack("msg-4") is None
            print(f"5. refused message reported, not acked: OK "
                  f"({route.read_refusal('msg-4')})")
        finally:
            mod.terminate()
            try:
                mod.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mod.kill()

        # 6. Heartbeat expiry: no poller, so after 5 s the session is dead.
        print("6. waiting out the 5 s heartbeat window...")
        time.sleep(6)
        assert not route.alive(SESSION)
        print("6. stale heartbeat is not alive: OK")

        # 7. Unknown: taken, then the mod died before any ack.
        crash = start_mod(extra=["--take-and-die"])
        try:
            route.write_inbox(SESSION, "msg-5", "took-then-died")
            wait_for(lambda: route.taking_exists("msg-5"), 5, "taking for msg-5")
            wait_for(lambda: not (state / "mod-inbox" / SESSION / f"{__import__('hashlib').sha256(b'msg-5').hexdigest()}.json").exists(),
                     5, "inbox file gone for msg-5")
            crash.wait(timeout=5)
            assert route.read_ack("msg-5") is None
            assert route.read_refusal("msg-5") is None
            assert route.classify("msg-5", mod_alive=False) == "unknown"
            print("7. taken then died -> unknown, not guessed, not resent: OK")
            # A restarted mod must not re-ack it either, even if the
            # inbox file is written again with the same msg_id: the
            # taking marker still guards it.
            route.write_inbox(SESSION, "msg-5", "took-then-died")
            again = start_mod()
            try:
                time.sleep(2)
                assert route.read_ack("msg-5") is None
                print("7b. restarted mod did not re-ack the unknown message: OK")
            finally:
                again.terminate()
                again.wait(timeout=5)
        finally:
            if crash.poll() is None:
                crash.terminate()
                crash.wait(timeout=5)

        print("\nDemo passed: inbox -> heartbeat -> taking -> ack/refusal/unknown, "
              "no text in any ack, busy messages nudged.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
