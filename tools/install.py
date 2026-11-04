"""Install a copy of the vesper-mod plugin outside any git work tree.

    python3 tools/install.py --plugin-dir plugin --state-dir <state_dir>

The copy lands in <state_dir>/mod/<sha12>/, where sha12 is the first 12
hex characters of a hash over the plugin's files (relative paths + bytes,
sorted). A changed plugin therefore installs to a new directory instead of
overwriting a running copy, and an unchanged plugin installs to the same
directory again (idempotent).

The copy also gets a config.json the hook reads at start:

    {"state_dir": "<state_dir>", "source_sha": "<full sha256>"}

An optional "preview_url" in a previously installed copy's config.json
is carried over into the new copy, so reinstalling after a plugin
change does not silently drop a preview receiver you set by hand.

Refusals, on purpose:
  * state_dir must not be inside a git work tree -- the copy lands
    under state_dir/mod/, so a state_dir inside a repo would put the
    copy in the repo, where Claude Code hot-reloads it on every save.
    The source may sit in a repo (a git clone); only state_dir is checked;
  * the destination is written to a temporary sibling directory and then
    atomically renamed, never written in place.

This is the install half of the protocol only. It does not start Claude
Code, and it does not start the service; see README.md
for the full one-sentence test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path


def source_sha(plugin_dir: Path) -> tuple[str, list[Path]]:
    files = sorted(p for p in plugin_dir.rglob("*") if p.is_file())
    h = hashlib.sha256()
    for p in files:
        rel = str(p.relative_to(plugin_dir))
        h.update(rel.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest(), files


def previous_preview_url(state_dir: Path) -> str | None:
    mod_dir = state_dir / "mod"
    if not mod_dir.exists():
        return None
    found: list[tuple[float, str]] = []
    for d in mod_dir.iterdir():
        if not d.is_dir():
            continue
        cfg_path = d / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception:
            continue
        url = cfg.get("preview_url")
        if isinstance(url, str) and url:
            found.append((d.stat().st_mtime, url))
    if not found:
        return None
    found.sort()
    return found[-1][1]


def inside_git_worktree(path: Path) -> bool:
    for parent in [path.resolve(), *path.resolve().parents]:
        if (parent / ".git").exists():
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()

    plugin_dir = Path(args.plugin_dir).resolve()
    state_dir = Path(args.state_dir).resolve()
    if not (plugin_dir / ".claude-plugin" / "plugin.json").exists():
        print(f"refused: {plugin_dir} is not a plugin directory (no .claude-plugin/plugin.json)", file=sys.stderr)
        return 2
    if inside_git_worktree(state_dir):
        print(f"refused: {state_dir} is inside a git work tree; the install copy "
              f"would land in the repo and be hot-reloaded, "
              f"choose a state_dir outside any repo", file=sys.stderr)
        return 2

    sha, files = source_sha(plugin_dir)
    dest = state_dir / "mod" / sha[:12]
    if dest.exists():
        print(f"already installed: {dest}")
        return 0

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "mod").mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{sha[:12]}-", dir=state_dir / "mod"))
    try:
        for p in files:
            rel = p.relative_to(plugin_dir)
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
        config = {"state_dir": str(state_dir), "source_sha": sha}
        prev_url = previous_preview_url(state_dir)
        if prev_url:
            config["preview_url"] = prev_url
        (tmp / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        tmp.rename(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f"installed to {dest}")
    print(f"source_sha: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
