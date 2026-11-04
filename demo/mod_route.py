"""File-based inbox between a web sender and a Claude Code session.

This is a small, standalone re-implementation of the on-disk contract
used by the original mod, kept deliberately close to it so the demo
proves the same invariants:

  state_dir/
    mod-inbox/<session_id>/<sha256(msg_id)>.json   {"msg_id", "text"}
    mod-inbox/<session_id>/.alive                  {"at": epoch_ms, "source_sha": ...}
    mod-acks/<sha256(msg_id)>.taking               empty marker, written BEFORE the
                                                   inbox file is read
    mod-acks/<sha256(msg_id)>.json                 {"msg_id", "uuid", "door"}
                                                   or {"msg_id", "refused", "door": "submit"}

Rules the demo checks:
  * inbox dirs are 0700, inbox files are 0600;
  * a heartbeat older than ALIVE_MAX_AGE_S (5 s) does not count as alive,
    and a torn/empty .alive falls back to the file's mtime;
  * an ack never contains the message text -- only msg_id, a uuid and a door,
    or a refusal reason;
  * a second poller that sees a .taking marker must not re-take the message
    -- but only for sequential pollers: .taking in the real hook is
    check-then-write, not atomic, so two pollers racing at the same
    instant can still duplicate (the demo stand-in uses atomic O_EXCL,
    which is stronger than the real hook);
  * the sender's own durable record stays text-free (it only ever stores
    acks) -- this is about the protocol's files, not about Claude Code's
    own transcript, which keeps the full text;
  * a taken message with no ack and no refusal, once the mod is known
    dead, is "unknown": it must not be resent, because it may already
    have reached the session (see classify()).

What this demo leaves out on purpose (see ../docs/advanced.md):
  the controls channel, and other inbox kinds: v1 handles user kind
  only. The real hook in ../plugin/ does the same.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

ALIVE_MAX_AGE_S = 5.0
DOORS = frozenset({"prompt", "note"})
_INBOX_NAME = re.compile(r"[0-9a-f]{64}\.json")
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def stem(msg_id: str) -> str:
    return hashlib.sha256(msg_id.encode()).hexdigest()


class InboxNotCleared(OSError):
    """Plaintext may still be on disk. Fixed code, no path, no text."""


class ModRoute:
    def __init__(self, state_dir: Path, *, now_ms: Callable[[], float] | None = None) -> None:
        self._state = Path(state_dir)
        self._now_ms = now_ms or (lambda: time.time() * 1000.0)

    # -- paths ---------------------------------------------------------
    def _inbox(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SAFE.match(session_id):
            raise ValueError("unsafe session id")
        return self._state / "mod-inbox" / session_id

    def ack_path(self, msg_id: str) -> Path:
        return self._state / "mod-acks" / f"{stem(msg_id)}.json"

    def taking_path(self, msg_id: str) -> Path:
        return self._state / "mod-acks" / f"{stem(msg_id)}.taking"

    def taking_exists(self, msg_id: str) -> bool:
        return os.path.lexists(self.taking_path(msg_id))

    # -- heartbeat -----------------------------------------------------
    def _beat_ms(self, session_id: str) -> float | None:
        path = self._inbox(session_id) / ".alive"
        try:
            raw = path.read_text()
            mtime = path.stat().st_mtime
        except OSError:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        at = value.get("at") if isinstance(value, dict) else None
        if type(at) in (int, float) and not isinstance(at, bool):
            return float(at)
        return mtime * 1000.0  # torn/empty file: judge by its own time

    def alive(self, session_id: str) -> bool:
        beat = self._beat_ms(session_id)
        if beat is None:
            return False
        return -1.0 <= (self._now_ms() - beat) / 1000.0 <= ALIVE_MAX_AGE_S

    # -- the inbox -----------------------------------------------------
    def write_inbox(self, session_id: str, msg_id: str, text: str) -> Path:
        inbox = self._inbox(session_id)
        for directory in (inbox.parent, inbox):
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
        os.chmod(inbox, 0o700)
        final = inbox / f"{stem(msg_id)}.json"
        tmp = inbox / f".{stem(msg_id)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"msg_id": msg_id, "text": text}).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)  # atomic: a reader never sees a half-written file
        return final

    def remove_inbox(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def sweep(self) -> int:
        """Service start: drop every inbox file and stray temp, never .alive or acks."""
        root = self._state / "mod-inbox"
        count, failed = 0, False
        try:
            folders = [f for f in root.iterdir() if f.is_dir()]
        except FileNotFoundError:
            return 0
        except OSError:
            raise InboxNotCleared("mod_inbox_not_cleared") from None
        for folder in folders:
            try:
                names = list(folder.iterdir())
            except OSError:
                failed = True
                continue
            for path in names:
                if _INBOX_NAME.fullmatch(path.name) or (
                    path.name.startswith(".") and path.name.endswith(".tmp")
                ):
                    try:
                        os.unlink(path)
                        count += 1
                    except FileNotFoundError:
                        pass
                    except OSError:
                        failed = True
        if failed:
            raise InboxNotCleared("mod_inbox_not_cleared")
        return count

    # -- the ack -------------------------------------------------------
    def read_ack(self, msg_id: str) -> dict | None:
        try:
            value = json.loads(self.ack_path(msg_id).read_text())
        except (OSError, ValueError):
            return None
        if (
            isinstance(value, dict)
            and value.get("msg_id") == msg_id
            and isinstance(value.get("uuid"), str)
            and value["uuid"]
            and value.get("door") in DOORS
        ):
            return value
        return None

    def read_refusal(self, msg_id: str) -> str | None:
        try:
            value = json.loads(self.ack_path(msg_id).read_text())
        except (OSError, ValueError):
            return None
        if (
            isinstance(value, dict)
            and value.get("msg_id") == msg_id
            and "uuid" not in value
            and value.get("door") == "submit"
            and isinstance(value.get("refused"), str)
            and value["refused"]
        ):
            return value["refused"]
        return None

    def classify(self, msg_id: str, *, mod_alive: bool) -> str:
        """One of "acked", "refused", "unknown", "pending".

        "unknown" is deliberately narrow: taking exists, no ack, no
        refusal, AND the mod is known dead (its process ended, or its
        heartbeat is long gone). Taking alone is only "pending" -- the
        mod may still be working, and calling it unknown too early is
        how a caller talks itself into a resend that breaks at-most-once.
        """
        if self.read_ack(msg_id) is not None:
            return "acked"
        if self.read_refusal(msg_id) is not None:
            return "refused"
        if self.taking_exists(msg_id) and not mod_alive:
            return "unknown"
        return "pending"
