"""Stand-in for the real Claude Code plugin hook (register.ts).

The real hook runs inside Claude Code and submits via $.prompt.submit /
$.session.append. This stand-in keeps everything else identical -- the
1 s poll, the heartbeat, taking-before-reading, the ack shape -- and
submits to a FakeEngine instead, so the protocol can be run and checked
without Claude Code installed.

Usage:
    python3 fake_mod.py --state-dir DIR --session SESSION [--refuse-next FILE]

--refuse-next points at a file containing one msg_id: the next submit of
that message is refused, mirroring the real case where Claude Code's
input gate refuses a plugin submit after a stop. Remove the file (or
never create it) for the normal path.

The poller never writes message text anywhere except reading the inbox
file itself: the .taking marker is empty, the ack holds no text, and the
inbox file is deleted once the message has been taken.

Note on taking: this stand-in claims its .taking marker with atomic
O_EXCL creation, which is stronger than the real hook (check-then-write).
The "one message, one submit" claim therefore holds here even for
concurrent pollers, but for the real hook it holds only for sequential
pollers -- see the root README.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mod_route import ModRoute  # noqa: E402

POLL_MS = 1.0
INBOX_FILE = re.compile(r"[0-9a-f]{64}\.json")
SOURCE_SHA = "demo-source-sha"


class FakeEngine:
    """Idle -> prompt submit; busy -> append; refused -> gate refusal.

    Nudge, like the real hook: a busy append is followed by no main-loop
    model request in this fake engine (the turn ends at once), so the
    append would otherwise go unread. The real hook checks the same
    condition with a step counter (mainSteps vs appendedAtStep) and only
    nudges when they are equal at turn end; here they are always equal,
    so a busy append always nudges. An ack with door "note" therefore
    means "appended", not "read by the model" -- the nudge is what makes
    it read.
    """

    def __init__(self) -> None:
        self.busy = False
        self.submitted: list[tuple[str, str]] = []  # (door, msg_id), memory only
        self.main_steps = 0
        self.appended_at_step: int | None = None

    def submit(self, msg_id: str, text: str, *, refuse: bool) -> tuple[str, str | None, bool]:
        if refuse:
            return "refused", "input gate refused the submit", False
        door = "note" if self.busy else "prompt"
        self.submitted.append((door, msg_id))
        nudged = False
        if self.busy:
            self.appended_at_step = self.main_steps
            # Turn ends at once in the fake engine: no model request
            # followed the append, so the nudge rule fires.
            if self.appended_at_step == self.main_steps:
                nudged = True
            self.appended_at_step = None
        # After one busy append the turn ends and the engine goes idle again.
        self.busy = False
        return "ok", door, nudged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--refuse-next", default=None)
    parser.add_argument("--take-and-die", action="store_true",
                        help="take the first message, delete its inbox file, "
                        "then exit without an ack: the unknown outcome")
    parser.add_argument("--heartbeat-only", action="store_true",
                        help="run_web_demo only: write the heartbeat, never take a "
                        "message, so the service times out with not_delivered")
    parser.add_argument("--take-and-hang", action="store_true",
                        help="run_web_demo only: take the first message (write "
                        ".taking, do not delete, do not ack), then hang, "
                        "so the service times out with unknown")
    args = parser.parse_args()

    def beat() -> None:
        try:
            inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(inbox, 0o700)
            tmp = inbox / ".alive.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({"at": time.time() * 1000.0, "source_sha": SOURCE_SHA}, out)
            os.replace(tmp, inbox / ".alive")
        except OSError:
            pass  # no heartbeat this second: the sender reads that as "not bound"

    route = ModRoute(Path(args.state_dir))
    inbox = Path(args.state_dir) / "mod-inbox" / args.session
    acks = Path(args.state_dir) / "mod-acks"
    acks.mkdir(mode=0o700, parents=True, exist_ok=True)
    engine = FakeEngine()
    print(f"[mod] polling session={args.session} (fake engine)", flush=True)

    while True:
        # Heartbeat, every tick, never blocking the scan below.
        beat()

        if args.heartbeat_only:
            time.sleep(POLL_MS)
            continue
        try:
            entries = list(inbox.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file() or not INBOX_FILE.fullmatch(entry.name):
                continue
            # Take BEFORE reading: a second poller seeing .taking must skip it.
            taking = acks / f"{entry.stem}.taking"
            try:
                fd = os.open(taking, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                continue  # already taken by another poller
            except OSError:
                continue
            try:
                body = json.loads(entry.read_text())
            except (OSError, ValueError):
                continue
            msg_id, text = body["msg_id"], body["text"]
            if args.take_and_die:
                # Crash after taking, before any ack: the service must
                # end this message "unknown", never resend it.
                try:
                    entry.unlink()
                except OSError:
                    pass
                print(f"[mod] {msg_id} -> took it and died (unknown)", flush=True)
                return 0
            if args.take_and_hang:
                # Took it (taking exists), no ack, no deletion: the
                # service's timeout must delete the file and judge
                # unknown. Hang here so the service times out first.
                print(f"[mod] {msg_id} -> took it and hangs (unknown)", flush=True)
                while True:
                    beat()
                    time.sleep(POLL_MS)
            refuse = False
            if args.refuse_next:
                try:
                    refuse = Path(args.refuse_next).read_text().strip() == msg_id
                except OSError:
                    refuse = False
            # The busy case: the demo driver sets busy via an env flag file.
            if (Path(args.state_dir) / "busy.flag").exists():
                engine.busy = True
            status, detail, nudged = engine.submit(msg_id, text, refuse=refuse)
            if nudged:
                # Demo instrumentation only (the real nudge is a transcript
                # row, not a file): lets the demo prove a nudge happened.
                with open(Path(args.state_dir) / ".nudge-log", "a") as log:
                    log.write(msg_id + "\n")
                print(f"[mod] {msg_id} -> nudge (busy append would go unread otherwise)", flush=True)
            ack = (
                {"msg_id": msg_id, "refused": detail, "door": "submit"}
                if status == "refused"
                else {"msg_id": msg_id, "uuid": str(uuid.uuid4()), "door": detail}
            )
            # Ack first, then delete the inbox file: plaintext lives on disk
            # only between write_inbox and here.
            tmp = acks / f".{entry.stem}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump(ack, out)
            os.replace(tmp, acks / f"{entry.stem}.json")
            try:
                entry.unlink()
            except OSError:
                pass
            print(f"[mod] {msg_id} -> {status} ({ack.get('door')})", flush=True)
        time.sleep(POLL_MS)


if __name__ == "__main__":
    raise SystemExit(main())
