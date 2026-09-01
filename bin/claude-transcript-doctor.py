#!/usr/bin/env python3
"""Find and repair Claude Code transcripts that the API will refuse.

Switching a session between providers mid-conversation poisons its history, and
the damage only shows up *after* the switch. Two block kinds cause it:

  foreign thinking   A thinking block carries a `signature` from the provider
                     that produced it. Anthropic validates its own; a block a
                     local model produced has an empty signature and cannot be
                     replayed. Symptom:
                       400 messages.N.content.M.thinking: each thinking block
                           must contain non-whitespace thinking

  empty text         An assistant turn that was only a tool call leaves a
                     `{"type":"text","text":""}` block behind. Symptom:
                       400 messages: text content blocks must be non-empty

Both are fatal for every later request, so a session fails permanently until
the blocks are removed -- and neither is visible in the interface.

**Native thinking is left alone.** A signature-bearing block is the provider's
own and is replayed correctly; stripping those would throw away real reasoning
context to no purpose. The discriminator is the signature, never the type.

A record whose *only* content is poison is removed rather than emptied, because
an empty `content` list is refused in its own right. Removing a record orphans
its children, so each is re-linked to the removed record's parent and the
uuid/parentUuid chain still walks end to end.

Usage:
    claude-transcript-doctor.py                 scan every session, report
    claude-transcript-doctor.py --fix           repair the poisoned ones
    claude-transcript-doctor.py --fix cweb5     repair one, by name or id

A repair keeps `<file>.orig` (the first repair only, never overwritten) and
`<file>.bak` (the state just before this run), and refuses to install a result
that does not parse, leaves an empty message, or breaks the chain.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

WARNING = """    ┌─────────────────────────────────────────────────────────────────────┐
    │  CLOSE THE SESSION BEFORE CLEANING IT.                              │
    │                                                                     │
    │  A running session holds its conversation in memory and writes the  │
    │  file on its way past. Repairing the transcript underneath it       │
    │  changes nothing about what it sends, and its next write can put    │
    │  the poison straight back.                                          │
    │                                                                     │
    │      1. exit the session   (or kill its process)                    │
    │      2. run this with --fix                                         │
    │      3. start it again     claude --resume <name>                   │
    │                                                                     │
    │  The scan marks a live session `yes` under `live`, and --fix says   │
    │  so after repairing one -- but it cannot stop you, so the order is  │
    │  yours to get right. Repairing a live session then restarting it    │
    │  is fine; repairing it and carrying on is not.                      │
    └─────────────────────────────────────────────────────────────────────┘"""

SESSIONS = Path.home() / ".claude" / "sessions"
PROJECTS = Path.home() / ".claude" / "projects"


def is_poison(block: object) -> str | None:
    """Why the API would refuse this block, or None if it is fine."""
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "thinking" and not str(block.get("signature") or "").strip():
        return "foreign thinking"
    if kind == "text" and not str(block.get("text") or "").strip():
        return "empty text"
    return None


def discover() -> dict[str, tuple[Path, str]]:
    """{label: (transcript path, session id)} for every session with a file.

    Keyed by name where there is one, by session id where there is not. The
    usage line has always said "by name or id" and only name worked: a session
    started with `claude -p` is auto-named from its first prompt, so its name is
    a sentence nobody would type, and asking for it by id matched nothing.

    That failed quietly in the place it matters most. `bin/wc-claude.sh` repairs
    a transcript before resuming it, passing whatever followed --resume -- which
    is usually an id. The doctor found nothing, said so, and the session was
    resumed unrepaired.
    """
    found: dict[str, tuple[Path, str]] = {}
    for meta in SESSIONS.glob("*.json"):
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        sid = data.get("sessionId")
        if not sid:
            continue
        for transcript in PROJECTS.glob(f"*/{sid}.jsonl"):
            found[str(data.get("name") or sid)] = (transcript, str(sid))
    return dict(sorted(found.items()))


def select(sessions: dict[str, tuple[Path, str]],
           wanted: list[str]) -> dict[str, tuple[Path, str]]:
    """Filter by name or session id, so either identifier reaches its file."""
    if not wanted:
        return sessions
    return {
        label: entry for label, entry in sessions.items()
        if label in wanted or entry[1] in wanted
    }


def inspect(path: Path) -> dict:
    native = foreign = empty_text = records = unparsed = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        records += 1
        for block in (rec.get("message") or {}).get("content") or []:
            reason = is_poison(block)
            if reason == "foreign thinking":
                foreign += 1
            elif reason == "empty text":
                empty_text += 1
            elif isinstance(block, dict) and block.get("type") == "thinking":
                native += 1
    return {"records": records, "native": native, "foreign": foreign,
            "empty_text": empty_text, "unparsed": unparsed}


def repair(path: Path, apply: bool) -> dict:
    records, unparsed = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            unparsed += 1

    drop, kept = set(), []
    for rec in records:
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list) and content and all(
                is_poison(b) for b in content):
            drop.add(rec.get("uuid"))
        else:
            kept.append(rec)

    parent_of = {r.get("uuid"): r.get("parentUuid") for r in records}
    relinked = 0
    for rec in kept:
        parent = rec.get("parentUuid")
        while parent in drop:
            parent = parent_of.get(parent)
            rec["parentUuid"] = parent
            relinked += 1

    trimmed = 0
    for rec in kept:
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            clean = [b for b in content if not is_poison(b)]
            if clean and len(clean) != len(content):
                rec["message"]["content"] = clean
                trimmed += len(content) - len(clean)

    known = {r.get("uuid") for r in kept}
    problems = []
    for rec in kept:
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list) and not content:
            problems.append(f"{rec.get('uuid')}: empty content")
        if isinstance(content, list) and any(is_poison(b) for b in content):
            problems.append(f"{rec.get('uuid')}: poison survived")
        parent = rec.get("parentUuid")
        if parent is not None and parent not in known:
            problems.append(f"{rec.get('uuid')}: parent missing")

    stats = {"dropped": len(drop), "relinked": relinked, "trimmed": trimmed,
             "unparsed": unparsed, "problems": len(problems),
             "detail": problems[:3], "installed": False}
    if apply and not problems:
        original = path.with_suffix(path.suffix + ".orig")
        if not original.exists():          # never overwrite the true original
            shutil.copy2(path, original)
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                for r in kept))
        stats["installed"] = True
    return stats


def running() -> str:
    return subprocess.run(["pgrep", "-af", "claude"], check=False,
                          capture_output=True, text=True).stdout


def main() -> int:
    print(WARNING)
    print()
    ap = argparse.ArgumentParser(# Not WARNING + __doc__: main() prints the banner before
        # parse_args(), so --help would carry it twice.
        description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*",
                    help="session names or session ids to act on")
    ap.add_argument("--fix", action="store_true", help="repair, not just report")
    args = ap.parse_args()

    every = discover()
    sessions = select(every, args.names)
    if not sessions:
        # Distinguish "you asked for something that is not here" from "there is
        # nothing here at all". The first used to print the second's message,
        # which reads like a healthy empty machine rather than a failed request
        # -- and a caller repairing before a resume would carry on regardless.
        if args.names and every:
            print(f"no session matched {', '.join(args.names)} "
                  f"({len(every)} known: name or session id both work)")
        else:
            print("no transcripts found")
        return 1

    live = running()
    poisoned = 0
    print(f"  {"session":38} {"records":>8} {'native':>7} {'FOREIGN':>8} "
          f"{'emptytext':>10}  {'live':5} verdict")
    for name, (path, sid) in sessions.items():
        info = inspect(path)
        bad = info["foreign"] + info["empty_text"]
        poisoned += bool(bad)
        # A session is live if either identifier appears on a command line: an
        # auto-named session is resumed by id, a named one by name.
        is_live = f"resume {name}" in live or f"resume {sid}" in live
        print(f"  {name[:38]:38} {info['records']:8} {info['native']:7} "
              f"{info['foreign']:8} {info['empty_text']:10}  "
              f"{'yes' if is_live else 'no':5} "
              f"{'POISONED' if bad else 'clean'}")
        if bad and args.fix:
            result = repair(path, apply=True)
            state = "repaired" if result["installed"] else "REFUSED"
            print(f"      -> {state}: dropped {result['dropped']}, "
                  f"relinked {result['relinked']}, trimmed {result['trimmed']}"
                  + (f", problems {result['detail']}" if result["problems"] else ""))
            if result["installed"] and is_live:
                print("      -> still running: restart it or the repair is "
                      "not what it sends")

    if poisoned and not args.fix:
        print(f"\n  {poisoned} session(s) need repair; re-run with --fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
