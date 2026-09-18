"""A rolling recording of the most recent conversation.

One file, overwritten each time, holding the latest conversation between a
person and an agent. This does NOT change retention: `chats` and `messages`
keep everything exactly as before, and nothing here deletes a row. It is an
additional, always-current copy in a fixed place.

Handling, and why it is this strict
-----------------------------------
The operator's brief: *"this file will contain raw audio/text on disk,
gitignore it and set restrictive file permissions by default."* So the file is
treated as sensitive from the moment it exists, not after:

* the directory is `0700` and the file `0600`, **asserted after creation**
  rather than assumed -- `os.makedirs` and `open` both subtract the process
  umask, so asking for a mode is not the same as getting one;
* the mode is set **at open time** via `os.open(..., 0o600)`, never by a
  `chmod` after writing. A `chmod` afterwards leaves a window in which the
  content exists at the umask's mode, and that window is the whole problem;
* the write is atomic (temp file in the same directory, then `os.replace`),
  so a reader never sees a half-written conversation and the replacement
  inherits the temp file's restrictive mode;
* `recordings/` is gitignored, as an explicit entry rather than relying on
  `/data/` -- a path that is only private because of an unrelated rule is one
  refactor away from being public;
* nothing here logs the conversation's content. Failures name the path and
  the error, never what was being written.

No audio is persisted anywhere in this codebase today (`voice_turn_timing`
holds timings only, and nothing writes audio bytes to disk), so in practice
this file is text. The handling above is written for what it is *for* rather
than for what it currently happens to contain.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Final

_log = logging.getLogger("wc.app")

#: Directory and file modes. Owner-only, both.
DIR_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600

#: Where the rolling copy lives. A dedicated directory rather than a loose
#: file so the `0700` applies to anything added beside it later -- a second
#: recording added next to a private one must not have to remember to be
#: private on its own account.
RECORDING_DIRNAME: Final[str] = "recordings"
RECORDING_FILENAME: Final[str] = "last-conversation.json"


def default_root() -> Path:
    """Where recordings live when no root is given: **beside the database**.

    Not beside this module, which is the obvious choice and the wrong one.
    `bin/wc-deploy.sh` exports each commit to its own release directory and
    points `current` at it, pruning old releases as they age -- so a recording
    written next to the code would live inside a release, be replaced by an
    empty directory on the next deploy, and be deleted outright when that
    release was pruned. "Always keeps a recording of the most recent
    conversation" would quietly mean "until the next deploy".

    The database directory is the deployment's stable data location: it
    survives deploys, it is where `config.DB_PATH` already points (honouring
    `WC_DB_PATH`, which is how the systemd unit aims it at the real file), and
    it is already gitignored as `/data/`.
    """
    try:
        import config
        return Path(config.DB_PATH).resolve().parent
    except Exception:                      # config unimportable -- tests, tools
        return Path(__file__).resolve().parent


def recording_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """The recordings directory. Defaults beside the database, not the code --
    see `default_root`."""
    base = Path(root) if root is not None else default_root()
    return base / RECORDING_DIRNAME


def recording_path(root: str | os.PathLike[str] | None = None) -> Path:
    return recording_dir(root) / RECORDING_FILENAME


def ensure_recording_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Create the directory `0700`, and tighten it if it already exists wider.

    The tightening half is the part that matters on a real host: a directory
    created earlier under a laxer umask, or by a different tool, keeps its old
    mode forever otherwise, and the first recording written into it would be
    protected by a file mode alone.
    """
    directory = recording_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    current = directory.stat().st_mode & 0o777
    if current != DIR_MODE:
        os.chmod(directory, DIR_MODE)
    return directory


ARCHIVE_DIRNAME: Final[str] = "archive"


def archive_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Where kept samples live. A subdirectory of `recording_dir`, so the
    `/data/recordings/` gitignore entry and the `0700` mode both cover it
    without a second rule to keep in step."""
    return recording_dir(root) / ARCHIVE_DIRNAME


def ensure_archive_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Create the archive directory `0700`, tightening an existing wider one,
    for the same reason `ensure_recording_dir` does."""
    ensure_recording_dir(root)
    directory = archive_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    current = directory.stat().st_mode & 0o777
    if current != DIR_MODE:
        os.chmod(directory, DIR_MODE)
    return directory


def archive_current(root: str | os.PathLike[str] | None = None) -> Path | None:
    """Keep a copy of the rolling recording so the next voice chat cannot take
    it. Returns the archived path, or None when there is nothing to archive.

    The rolling file exists to hold exactly one conversation and is overwritten
    at the start of the next one -- that is the feature, and it is also why a
    sample worth benchmarking against has a lifetime of "until somebody speaks
    to the console again". This is the deliberate act that ends that.

    The name carries the recording's own start time rather than the archiving
    time, so re-archiving the same file lands on the same name instead of
    accumulating one copy per invocation, and so the ordering on disk is the
    order the conversations happened rather than the order somebody got around
    to keeping them. A content hash is appended because a chat that is resumed
    keeps its id and its `created_at`, so those two alone do not distinguish a
    later, longer recording of the same conversation from an earlier one.

    Identical content already archived is a no-op returning the existing path:
    archiving twice is something an operator will do, and a directory of
    duplicates is worse than no answer.
    """
    source = recording_path(root)
    if not source.exists():
        return None
    payload = source.read_text(encoding="utf-8")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # A truncated or half-written rolling file is not worth refusing over;
        # keeping it is still better than letting the next chat overwrite it.
        data = {}
    chat = data.get("chat") or {}
    stamp = str(chat.get("created_at") or "unknown").replace(":", "").replace("-", "")
    chat_id = str(chat.get("id") or "unknown")[:8]
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    directory = ensure_archive_dir(root)
    target = directory / f"{stamp}-{chat_id}-{digest}.json"
    if target.exists():
        return target
    _write_private_atomic(target, payload)
    return target


def archived_samples(root: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every kept sample, oldest first by the name's leading timestamp."""
    directory = archive_dir(root)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.json") if p.is_file())


def _write_private_atomic(path: Path, payload: str) -> None:
    """Write `payload` to `path` atomically, never wider than `FILE_MODE`.

    `os.open` with `O_CREAT | O_EXCL` and an explicit mode is what makes the
    content private for its whole life: the descriptor is created with the
    mode already applied, so there is no instant at which the bytes exist at
    the umask's default. `os.replace` is atomic within a filesystem and keeps
    the source file's mode, so the published file is `0600` too.

    The umask still subtracts from the requested mode, which can only make it
    STRICTER, never wider -- so the result is checked and tightened rather
    than trusted.
    """
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():                      # a previous crash between open and replace
        tmp.unlink()
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if tmp.stat().st_mode & 0o777 != FILE_MODE:
        os.chmod(tmp, FILE_MODE)
    os.replace(tmp, path)


def build_recording(chat: dict[str, Any], messages: list[dict[str, Any]],
                    handoff_summary: str | None = None,
                    turns: list[dict[str, Any]] | None = None
                    ) -> dict[str, Any]:
    """The recorded shape, kept deliberately small and explicit.

    Only fields that describe the conversation are copied. The `chats` row
    carries a lot that is operational rather than conversational -- routing,
    positions, auto-answer state -- and a recording that copied the whole row
    would grow silently every time that table gains a column.
    """
    return {
        "chat": {
            "id": chat.get("id"),
            "title": chat.get("title"),
            "model": chat.get("model"),
            "voice_mode": chat.get("voice_mode"),
            # Recorded because this console is multi-user: "the most recent
            # conversation" is whichever one happened last, which is not
            # necessarily the reader's own. Naming the owner is what keeps
            # that visible instead of surprising.
            "owner_id": chat.get("owner_id"),
            "created_at": chat.get("created_at"),
            "updated_at": chat.get("updated_at"),
        },
        "messages": [
            {
                "role": m.get("role"),
                "content": m.get("content"),
                "created_at": m.get("created_at"),
            }
            for m in messages
        ],
        "message_count": len(messages),
        # What was actually sent to the model, per turn -- the part that makes
        # this replayable rather than merely readable. See `_TURNS`.
        "turns": turns or [],
        # The summary voice_handoff writes into the PARENT chat. Recorded here
        # too because it is part of this conversation -- and because, once the
        # voice chat is deleted, the summary in the parent is the only thing
        # that would otherwise survive to describe it.
        "handoff_summary": handoff_summary,
    }


def write_recording(chat: dict[str, Any], messages: list[dict[str, Any]],
                    root: str | os.PathLike[str] | None = None,
                    handoff_summary: str | None = None,
                    turns: list[dict[str, Any]] | None = None) -> Path:
    """Overwrite the rolling recording with this conversation.

    Returns the path written. Raises on failure -- callers on a hot path
    should use `record_conversation`, which swallows and logs instead.
    """
    ensure_recording_dir(root)
    path = recording_path(root)
    _write_private_atomic(
        path, json.dumps(build_recording(chat, messages, handoff_summary,
                                         turns),
                         indent=2, ensure_ascii=False) + "\n")
    return path


#: Per-chat replay records for the turns seen since this process started.
#:
#: The `messages` table stores the user's prompt and the assistant's reply,
#: which is what a person needs to read the conversation back -- and not what
#: a benchmark needs to REPRODUCE it. `routes/voice.stream_voice_turn` sends
#: the model a system prompt plus, when the chat has a parent, a structured
#: context block distilled at call time from the parent's last 12 messages by
#: an inline heuristic. That block cannot be rebuilt afterwards: the parent
#: chat moves on, and the same code run later against it produces different
#: text. Replaying from the stored messages alone would send different input
#: and score the difference as a model result.
#:
#: Held in memory rather than in a table because it is scratch: it is folded
#: into the recording on the next write, which happens on the same turn.
_TURNS: dict[str, list[dict[str, Any]]] = {}

#: A voice conversation is a handful of turns; this only bounds the damage if
#: a chat is never torn down (a crash between turns, say) so the dict cannot
#: grow without limit in a long-lived service.
MAX_RECORDED_TURNS: Final[int] = 200


def note_turn(chat_id: str, turn: dict[str, Any]) -> None:
    """Record what was actually sent to the model for one voice turn.

    Called by `stream_voice_turn` before it stores the messages, so the write
    that follows picks this up. Never raises: a replay record is an extra,
    and losing it must not fail the turn it describes.
    """
    try:
        turns = _TURNS.setdefault(chat_id, [])
        if len(turns) >= MAX_RECORDED_TURNS:
            turns.pop(0)
        turns.append(turn)
    except Exception:                                # noqa: BLE001
        pass


def forget_turns(chat_id: str) -> None:
    """Drop a chat's replay records, once they have been written out."""
    _TURNS.pop(chat_id, None)


#: Recording is ON by default: every voice chat is recorded.
#:
#: This reversed on 2026-09-17 and both positions are recorded, because the
#: reasons pull against each other and whoever revisits this needs both.
#:
#: The written brief scoped it to "voice-benchmark test conversations, not a
#: general recording feature for all chats", which argued for opt-in: a
#: `voice_mode` filter alone catches every voice conversation anyone has, and
#: a file holding raw conversation content is a standing privacy surface.
#:
#: The operator then asked for every voice chat to be recorded, so that
#: examples exist to benchmark against. That wins, because it is the later
#: instruction and because the earlier one was about SCOPE OF PURPOSE rather
#: than about a flag: in this deployment the voice chats are the benchmark
#: conversations.
#:
#: The setting remains, so recording can still be switched OFF -- which is the
#: half that matters now that the default is on. Anything other than a stored
#: `"0"` leaves it enabled.
BENCHMARK_RECORDING_SETTING: Final[str] = "voice_benchmark_recording"
BENCHMARK_RECORDING_DEFAULT: Final[bool] = True


async def benchmark_recording_enabled() -> bool:
    """Whether voice-benchmark recording is switched on.

    Anything other than a stored `"0"` leaves recording ON, including a
    missing row and a malformed value. That polarity flipped with the default
    (2026-09-17): when recording was opt-in, an unparseable row had to mean
    "do not write raw conversation content"; now that every voice chat is
    recorded on purpose, an unparseable row must not silently stop the
    recording an operator is relying on. Switching it off is an explicit
    `"0"`, which is the one value that has to be unambiguous.
    """
    try:
        from routes.db_users import setting_get
        raw = await setting_get(BENCHMARK_RECORDING_SETTING)
    except Exception:                                # noqa: BLE001
        return BENCHMARK_RECORDING_DEFAULT
    if raw is None:
        return BENCHMARK_RECORDING_DEFAULT
    return raw.strip() != "0"


async def is_voice_chat(chat_id: str) -> bool:
    """Whether this chat is a voice conversation (`chats.voice_mode`)."""
    import db
    cur = await db.db_conn.execute(
        "SELECT voice_mode FROM chats WHERE id = ?", (chat_id,))
    row = await cur.fetchone()
    return bool(row and row["voice_mode"])


async def record_conversation(chat_id: str,
                              root: str | os.PathLike[str] | None = None,
                              handoff_summary: str | None = None,
                              require_voice: bool = True) -> Path | None:
    """Record `chat_id` as the most recent conversation, or do nothing.

    **Only while voice-benchmark recording is switched on**
    (`BENCHMARK_RECORDING_SETTING`, off by default), and **voice conversations
    only** within that.

    Written at the END of a conversation, never during one, so the file holds
    the PREVIOUS conversation while a new one is in progress -- which is what
    a benchmark reading it needs: a complete conversation, not a partial one
    being appended to. "Overwritten at the start of each new conversation" and
    "written when the previous one ends" produce the same observable state,
    and the second never loses a conversation that has no successor.

    The rest of the original note still applies. They are the ones that need it:
    `routes/voice.voice_handoff` summarises a voice chat into its parent and
    then calls `db.chat_delete` on every one of its three exit paths, so a
    voice conversation is destroyed as a matter of course and only a 2-4
    sentence summary survives. Ordinary text chats are already kept forever
    and need no rolling copy. `require_voice=False` is for the handoff caller,
    which has already established what it is holding.

    `handoff_summary`, when given, is stored alongside the messages: the
    summary is part of the voice conversation being recorded, not a separate
    artefact of the parent chat.

    **Never raises.** This is called from the message write path, and a
    recording is a convenience: failing to write it must not fail the message
    that triggered it, or a full disk would stop the console storing
    conversations at all -- trading the thing that matters for the copy of it.

    The failure is logged with the path and the error and never with the
    content.
    """
    try:
        import db
        from routes import db_chats

        # Read the row directly rather than through `db_chats.chat_get`, which
        # takes an `owner_id` and scopes to it. That scoping is an
        # authorisation check for a REQUEST, and this is not one -- it runs
        # under the service, after a message has already been stored, with no
        # user in scope to check against. Inventing an owner to satisfy the
        # signature would be worse than not calling it: it would look like an
        # authorisation check had been made when none had.
        #
        # The consequence is recorded rather than hidden: `owner_id` goes into
        # the recording, so whose conversation this is can never be ambiguous
        # to whoever reads the file.
        if not await benchmark_recording_enabled():
            return None
        cur = await db.db_conn.execute(
            "SELECT id, title, model, voice_mode, owner_id, parent_chat_id, "
            "created_at, updated_at FROM chats WHERE id = ?", (chat_id,))
        chat = await cur.fetchone()
        if chat is None:
            return None
        if require_voice and not chat["voice_mode"]:
            return None
        messages = await db_chats.messages_get(chat_id)
        return write_recording(dict(chat), [dict(m) for m in messages], root,
                               handoff_summary, _TURNS.get(chat_id))
    except Exception as exc:                         # noqa: BLE001 - see above
        _log.warning("conversation recording failed for chat_id=%s path=%s: %s",
                     chat_id, recording_path(root), exc)
        return None
