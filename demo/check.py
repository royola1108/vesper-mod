"""Fast, timing-free checks for the protocol core. No subprocesses.

    python3 demo/check.py

Covers what run_demo.py shows live, but with a fake clock so the
heartbeat-expiry case does not actually wait 5 seconds:
  * write_inbox creates 0700 dirs / 0600 files, atomically (no .tmp left);
  * alive() is true with a fresh heartbeat, false once it is 5 s stale,
    and falls back to mtime for a torn .alive file;
  * read_ack only accepts a well-formed ack for this msg_id;
  * read_refusal only accepts a well-formed refusal (no uuid, door=submit);
  * sweep() removes inbox files and temps, but never .alive or acks;
  * unsafe session ids are rejected before anything touches disk.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mod_route import ModRoute, stem  # noqa: E402


def main() -> int:
    now = [time.time() * 1000.0]
    with tempfile.TemporaryDirectory(prefix="vesper-mod-check-") as tmp:
        route = ModRoute(Path(tmp), now_ms=lambda: now[0])
        state = Path(tmp)

        # Unsafe session id: rejected, nothing created.
        try:
            route.write_inbox("../escape", "m", "x")
            raise SystemExit("FAIL: unsafe session id was accepted")
        except ValueError:
            pass

        path = route.write_inbox("s1", "msg-a", "hello")
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert not list(path.parent.glob(".*.tmp")), "temp file must not remain"
        assert json.loads(path.read_text()) == {"msg_id": "msg-a", "text": "hello"}

        # Heartbeat: fresh, then stale, then torn-file fallback.
        inbox = state / "mod-inbox" / "s1"
        (inbox / ".alive").write_text(json.dumps({"at": now[0], "source_sha": "x"}))
        assert route.alive("s1")
        now[0] += 6000
        assert not route.alive("s1"), "5 s stale heartbeat must not count"
        now[0] -= 6000
        (inbox / ".alive").write_text("")  # torn write: fall back to mtime
        assert route.alive("s1")

        # Acks: only well-formed ones count, and refusals are separate.
        acks = state / "mod-acks"
        acks.mkdir(mode=0o700, parents=True, exist_ok=True)
        (acks / f"{stem('msg-a')}.json").write_text(
            json.dumps({"msg_id": "msg-a", "uuid": "u1", "door": "prompt"}))
        assert route.read_ack("msg-a")["uuid"] == "u1"
        assert route.read_refusal("msg-a") is None  # an ack is not a refusal
        (acks / f"{stem('msg-b')}.json").write_text(
            json.dumps({"msg_id": "msg-b", "refused": "gate refused", "door": "submit"}))
        assert route.read_refusal("msg-b") == "gate refused"
        assert route.read_ack("msg-b") is None

        # Sweep: inbox files and temps go, .alive and acks stay.
        assert route.sweep() == 1
        assert not path.exists()
        assert (inbox / ".alive").exists()
        assert (acks / f"{stem('msg-a')}.json").exists()

    print("check.py passed: permissions, heartbeat, ack/refusal shapes, sweep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
