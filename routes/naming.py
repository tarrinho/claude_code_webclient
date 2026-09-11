# naming.py — human-readable agent naming with transport + counter + task.

import re
import json
from typing import Final

import db

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

# Regex to break a prompt into words while keeping punctuation attached.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z_][A-Za-z0-9_-]*|[^A-Za-z0-9_\s]+"
)


# --- persistent counter -------------------------------------------------------

_CTR_KEY = "naming_counter"   # {transport}: {value}


def _counter_get(transport: str) -> int:
    """Return the highest *n* used in session-file names for *transport*."""
    try:
        raw = db.setting_get(_CTR_KEY)
        data = json.loads(raw) if raw else {}
        return data.get(transport, 0)
    except Exception:
        return 0


def _counter_set(transport: str, value: int) -> None:
    """Persist the highest *n* for *transport*."""
    try:
        raw = db.setting_get(_CTR_KEY)
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    data[transport] = value
    db.setting_set(_CTR_KEY, json.dumps(data))


def _counter_next(transport: str) -> int:
    """Reserve and return the next counter value for *transport*."""
    cur = _counter_get(transport)
    nxt = cur + 1
    _counter_set(transport, nxt)
    return nxt


# --- name generation ------------------------------------------------------------

def generate_name(transport_name: str, prompt: str) -> str:
    """Return a human-readable name for a new spawn.

    Format: ``{transport} : {n} : {task}`` where *n* is the next-per-transport
    counter (persistent in the DB) and *task* is the first five meaningful
    words from *prompt*.

    Filler words are stripped, words under 3 chars are kept only if they
    are common verbs/short operations.  Falls back to ``Untitled {n}``
    when the prompt is empty or too short.
    """
    counter = _counter_next(transport_name)

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


def deduplicate_existing() -> list[dict]:
    """Scan session-file names and bump duplicates so every *n* is unique.

    Returns a list of dicts describing each change:
    ``{"session_id": "...", "old": "...", "new": "..."}``.
    """
    import os
    from glob import glob
    import re as _re

    # Pattern: ``{transport} : {n} : {task}``
    _P = _re.compile(r"^([^\s:]+)\s*:\s*(\d+)\s*:\s*(.+)$")

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
        m = _P.match(name)
        if not m:
            continue

        transport, n_str, task_part = m.group(1), m.group(2), m.group(3)
        n = int(n_str)

        if transport not in counters:
            counters[transport] = set()

        existing = counters[transport]

        if n in existing:
            # Duplicate — bump until we find a free slot.
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
            # Track the highest seen counter.
            current = _counter_get(transport)
            if nxt := max(n, current):
                _counter_set(transport, nxt)

    return changes
