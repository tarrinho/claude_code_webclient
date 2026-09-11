# naming.py — human-readable agent naming with transport + counter + task.

import json
import os
import re
from glob import glob
from typing import Final

# Filler words stripped from the front before taking the task summary.
_FILLERS: Final[set[str]] = {
    "a", "an", "the", "to", "for", "in", "on", "at", "by", "with",
    "from", "of", "and", "or", "not", "do", "does", "did", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had",
}
# Words kept under 3 chars after strip (e.g. "fix bugs" stays).
_SHORT_KEEP: Final[set[str]] = {
    "fix", "add", "run", "stop", "kill", "log", "get", "set",
    "use", "make", "find", "try", "write", "read", "copy", "move",
    "edit", "update", "create", "send", "build", "test", "check",
    "list", "show", "find", "open", "close", "save", "load",
}

# Per-transport spawn counter. Seeded at startup from session files so the
# value survives restarts; incremented in-process on every new spawn.
_spawn_counter: dict[str, int] = {}

# Regex to break a prompt into words while keeping punctuation attached.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z_][A-Za-z0-9_-]*|[^A-Za-z0-9_\s]+"
)

# Pattern used to parse session-file names: ``{transport} : {N} : {task}``
_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^([^\s:]+)\s*:\s*(\d+)\s*:\s*(.+)$"
)


# --- name generation ------------------------------------------------------------

def generate_name(transport_name: str, prompt: str) -> str:
    """Return a human-readable name for a new spawn.

    Format: ``{transport} : {n} : {task}`` where *n* is the next-per-transport
    counter (seeded at startup from all existing session files so it never
    collides across restarts) and *task* is the first five meaningful words
    from *prompt*.

    Filler words are stripped, words under 3 chars are kept only if they
    are common verbs/short operations.  Falls back to ``Untitled {n}``
    when the prompt is empty or too short.
    """
    counter = _spawn_counter.get(transport_name, 0) + 1
    _spawn_counter[transport_name] = counter

    clean = _extract_task_words(prompt)

    if not clean:
        return f"{transport_name} : {counter} : Untitled task"

    return f"{transport_name} : {counter} : {' '.join(clean)}"


def _extract_task_words(prompt: str) -> list[str]:
    """Extract up to five meaningful words from *prompt*."""
    words = _TOKEN_RE.findall(prompt)
    if not words:
        return []

    cleaned: list[str] = []
    for w in words:
        low = w.lower()
        if low in _FILLERS:
            continue
        if len(w) < 3 and low not in _SHORT_KEEP:
            continue
        cleaned.append(_title(w))

    # Cap at 5 words.
    return cleaned[:5]


def _title(s: str) -> str:
    """Capitalize the first character; keep the rest as-is."""
    if not s:
        return s
    return s[0].upper() + s[1:]


# --- startup seeding / deduplication -------------------------------------------

def seed_counter() -> int:
    """Scan session-file names and initialise the in-memory counter.

    Every session file under ``~/.claude/sessions/`` that matches the
    ``{transport} : {n} : {task}`` pattern is read.  The highest *n* per
    transport is stored in ``_spawn_counter`` so new spawns continue the
    sequence even after a restart — no two sessions will ever get the same *n*.

    Returns the number of transports whose counter was seeded.
    """
    max_seen: dict[str, int] = {}

    for path in glob(os.path.expanduser("~/.claude/sessions/*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        name = data.get("name") or ""
        m = _NAME_RE.match(name)
        if m:
            transport, n = m.group(1), int(m.group(2))
            max_seen[transport] = max(max_seen.get(transport, 0), n)

    for transport, hi in max_seen.items():
        _spawn_counter[transport] = hi

    return len(max_seen)


def deduplicate_existing() -> list[dict]:
    """Bump duplicate session-file names so every *n* is unique.

    Scans all session files, finds transport groups where two or more share
    the same *n*, and bumps each duplicate to the next free slot.  Also
    updates ``_spawn_counter`` to reflect the highest used value so future
    sessions never collide.

    Existing names that are already unique are left untouched.

    Returns a list of dicts:
    ``{"session_id": "...", "old": "...", "new": "..."}``.
    """
    files = glob(os.path.expanduser("~/.claude/sessions/*.json"))
    counters: dict[str, set[int]] = {}   # transport -> set of used n
    changes: list[dict] = []

    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        name = data.get("name") or ""
        m = _NAME_RE.match(name)
        if not m:
            continue

        transport, n_str, task_part = m.group(1), m.group(2), m.group(3)
        n = int(n_str)

        if transport not in counters:
            counters[transport] = set()

        existing = counters[transport]

        if n in existing:
            # Duplicate — bump to the next free slot.
            nxt = n + 1
            while nxt in existing:
                nxt += 1
            existing.add(nxt)
            new_name = f"{transport} : {nxt} : {task_part}"
            data["name"] = new_name
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                changes.append({
                    "session_id": os.path.basename(path).replace(".json", ""),
                    "old": f"{transport} : {n} : {task_part}",
                    "new": new_name,
                })
            except Exception:
                pass
        else:
            existing.add(n)

    # Update _spawn_counter to reflect the highest used value per transport.
    for transport, used in counters.items():
        _spawn_counter[transport] = max(_spawn_counter.get(transport, 0), max(used))

    return changes
