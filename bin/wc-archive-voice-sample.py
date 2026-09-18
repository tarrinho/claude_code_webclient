#!/usr/bin/env python3
"""Keep the current voice recording so the next conversation cannot take it.

The rolling recording (`data/recordings/last-conversation.json`) holds exactly
one voice conversation and is overwritten at the start of the next one. That is
the feature as asked for -- "overwriting it each time a new conversation
happens, rather than keeping every past" -- and it is also why a conversation
worth benchmarking against has a lifetime of "until somebody speaks to the
console again".

This is the deliberate act that ends that lifetime. It copies the rolling file
into `data/recordings/archive/`, named for the conversation's own start time
plus a content hash, at `0600` inside a `0700` directory. Archiving the same
recording twice is a no-op rather than a second copy.

Nothing here is automatic. A voice conversation is archived because somebody
decided it was worth keeping.

    bin/wc-archive-voice-sample.py            # keep the current recording
    bin/wc-archive-voice-sample.py --list     # show what is already kept

Contents are raw conversation text and per-turn timings. They are gitignored
(`/data/` and `/data/recordings/`) and must stay that way; no credential is
recorded -- `routes/voice.py` deliberately omits `base_url` and `api_key` from
every turn record -- but the transcript itself is the user's own speech.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conversation_recording as cr  # noqa: E402


def _describe(path: Path) -> str:
    """One line per sample: when, which chat, how many turns, which model."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"{path.name}  (unreadable)"
    chat = data.get("chat") or {}
    turns = data.get("turns") or []
    served = {t.get("model_served") for t in turns if t.get("model_served")}
    return (
        f"{path.name}\n"
        f"    started {chat.get('created_at', '?')}  "
        f"messages {data.get('message_count', len(data.get('messages') or []))}  "
        f"turns {len(turns)}\n"
        f"    model   {', '.join(sorted(served)) or '?'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list", action="store_true",
        help="List the samples already kept and exit, changing nothing.")
    args = parser.parse_args(argv)

    if args.list:
        samples = cr.archived_samples()
        if not samples:
            print(f"no samples kept yet in {cr.archive_dir()}")
            return 0
        print(f"{len(samples)} sample(s) in {cr.archive_dir()}:")
        for path in samples:
            print(f"  {_describe(path)}")
        return 0

    source = cr.recording_path()
    if not source.exists():
        print(
            f"nothing to keep: {source} does not exist. A voice conversation "
            "writes it; if none has happened since this deployment started, "
            "there is no recording yet.",
            file=sys.stderr)
        return 1

    before = set(cr.archived_samples())
    target = cr.archive_current()
    if target is None:                       # raced with a delete
        print(f"nothing to keep: {source} disappeared", file=sys.stderr)
        return 1
    if target in before:
        print(f"already kept, unchanged: {target}")
    else:
        print(f"kept: {target}")
    print(f"  {_describe(target)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
