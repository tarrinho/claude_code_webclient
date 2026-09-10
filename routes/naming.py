# naming.py — human-readable agent naming with transport + counter + task.

import re
from typing import Final

import config

# Filler words stripped from the front before taking the task summary.
_FILLERS: Final[set[str]] = {
    "a", "an", "the", "to", "for", "in", "on", "at", "by", "with",
    "from", "of", "and", "or", "not", "do", "does", "did", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had",
}
# Words kept under 3 chars after strip (e.g. "fix bugs" stays).
_SHORT_KEEP: Final[set[str]] = {"fix", "add", "run", "stop", "kill", "log", "get", "set", "use", "make", "find", "try"}

# Per-transport spawn counter.
_spawn_counter: dict[str, int] = {}

# Regex to break a prompt into words while keeping punctuation attached.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*|[^A-Za-z0-9_\s]+")


def generate_name(transport_name: str, session_id: str, prompt: str) -> str:
    """Return a human-readable name for *session_id*.

    Format: ``{transport} : {n} : {task}`` where *n* is the per-transport
    spawn counter and *task* is the first five meaningful words from
    *prompt*.

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
