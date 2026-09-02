"""Attention: which conversations and sessions need a person, and why.

Extracted from app.py in 0.10.0. Twenty names, found by closing over
`classify_chat` rather than by choosing them -- everything that decides whether
a row is waiting on you, and nothing else. After closure the group referenced no
app.py name except the logger, which is what made it a seam rather than a cut.

Two surfaces call `classify_chat`: the sidebar and the supervisor members panel.
They once assembled its inputs separately and so agreed on the rule while
disagreeing on the data -- the members panel announced running work as finished
while the sidebar kept it quiet. `_cli_maps` exists to stop that recurring, and
it lives here with the decision it feeds rather than beside either caller.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
from typing import Final

import db
import prompts
import transcripts
import turns

# The note _question_to_text appends. Defined where it is written, not
# where it is detected: shared.py renders it, this module only looks for it.
from shared import _QUESTION_PENDING_NOTE

# The same logger name app.py uses, so logging.conf routes these records exactly
# as before. A new name would have needed a new section in that file, and one
# missing section is how wc.transcripts reached the log only by propagating to
# root.
_log = logging.getLogger("wc.app")


# An agent that merely finished talking is not a reason to interrupt anyone.
# The badge fires only when the last thing it said either asks for something or
# reports that it is stuck. Both lists are deliberately short: a wide net makes
# the count meaningless, and a missed alert costs one scroll of the sidebar
# while a false one costs the badge its credibility.
_ASKS_FOR_INPUT: Final[tuple[str, ...]] = (
    "let me know",
    "do you want",
    "would you like",
    "shall i",
    "should i",
    "your call",
    "yours to call",
    "say the word",
    "which would you",
    "confirm",
    "please choose",
    "waiting for your",
    "waiting on your",
    "worth doing",
    "worth fixing",
)
_REPORTS_A_BLOCKER: Final[tuple[str, ...]] = (
    "blocked",
    "cannot proceed",
    "can't proceed",
    "needs your",
    "need your",
    "waiting on you",
    "permission denied",
    "requires your",
    "i was denied",
)
def _phrase_matcher(phrases: tuple[str, ...]) -> re.Pattern[str]:
    r"""One anchored pattern for *phrases*, matched on word boundaries.

    These were tested with `phrase in text`, which matches inside words -- and
    the consequences were not theoretical. Every one of these was live:

        "whether I should include the index rebuild"  -> asks    ('should i')
        "Everything should include the index."        -> asks    ('should i')
        "Nothing should interfere."                   -> asks    ('should i')
        "I confirmed the tests pass."                 -> asks    ('confirm')
        "The task is unblocked now."                  -> blocked ('blocked')

    The last is the sharpest: a report that something is **un**blocked was read
    as a report that it is blocked, which is the opposite claim.

    This surfaced when the classifier began reading the end of a message as well
    as its opening, and was reported as a regression in that change. It was not
    -- the window only widened the exposure. `_attention(preview)` had the same
    fault for any message whose first 200 characters happened to contain
    "should include", and a fix aimed at the windows would have left that in
    place. Anchoring removes the cause, so both windows are safe to read.

    `\b` on each side of the whole phrase, not per word: "can't proceed" and
    "i was denied" contain spaces and an apostrophe, and anchoring the phrase as
    a unit keeps them matching as written.
    """
    return re.compile(
        "|".join(rf"\b{re.escape(phrase)}\b" for phrase in phrases))
_ASKS_PATTERN: Final[re.Pattern[str]] = _phrase_matcher(_ASKS_FOR_INPUT)
_BLOCKER_PATTERN: Final[re.Pattern[str]] = _phrase_matcher(_REPORTS_A_BLOCKER)
def _pending_question(turns: list[dict]) -> str | None:
    """An AskUserQuestion still awaiting a reply, or None.

    The CLI records the question and its outcome as separate blocks, matched by
    id. A question with no matching answer is genuinely outstanding; once the
    answer arrives the highlight has to go, which is the whole point of pairing
    them rather than just spotting a question.
    """
    answered: set[str] = set()
    for turn in turns:
        for block in turn.get("blocks", []):
            if block.get("kind") == "answer" and block.get("id"):
                answered.add(block["id"])
    for turn in reversed(turns):
        for block in reversed(turn.get("blocks", [])):
            if block.get("kind") != "question":
                continue
            if block.get("id") in answered:
                # The newest question has been resolved, so nothing is pending.
                return None
            first = (block.get("questions") or [{}])[0]
            return str(first.get("question") or "").strip() or "A question is waiting"
    return None
def _last_thing_said(turns: list[dict]) -> str:
    """The newest assistant text in *turns*, skipping tool calls.

    A turn carrying only a tool call has role="assistant" and no text, so
    reading the last turn alone finds nothing and the agent looks silent --
    which is what stopped every terminal session from ever being surfaced.
    """
    for turn in reversed(turns):
        if turn.get("role") != "assistant":
            continue
        text = " ".join(
            (block.get("text") or "").strip()
            for block in turn.get("blocks", [])
            if block.get("kind") == "text"
        ).strip()
        if text:
            return text
    return ""
def _attention(text: str) -> str | None:
    """Why this output needs the user, or None if it is just talk.

    Returns "asks" when the agent wants something back and "blocked" when it
    reports it cannot continue. Everything else -- progress, results, a
    finished piece of work -- is left silent on purpose.
    """
    body = (text or "").strip()
    if not body:
        return None
    lowered = body.lower()
    # A trailing question is the clearest possible request for input. Only the
    # end of the message counts: a question quoted mid-explanation is not an ask.
    tail = lowered.rstrip().rstrip("`*_)\"'")
    if tail.endswith("?"):
        return "asks"
    # Trailing ":" and "…" used to count as asks, on the reading that an agent
    # ending that way is inviting the user to complete the thought. They are
    # dropped: ordinary output ends with a colon constantly ("Here is what I
    # found:", "Changes:"), so this summoned the user for prose rather than for a
    # request, which is the false positive that made the badge worth ignoring.
    #
    # Nothing is lost by dropping them. A finished turn is now surfaced in its
    # own right, as `done` -- so a reply ending in a colon still appears, and
    # appears labelled as finished rather than as a question nobody asked.
    # Anchored: `phrase in lowered` matched inside words, so "should include"
    # read as "should i" and a finished report was announced as a question.
    # See _phrase_matcher.
    if _ASKS_PATTERN.search(lowered):
        return "asks"
    if _BLOCKER_PATTERN.search(lowered):
        return "blocked"
    return None
def _asks_a_question(text: str) -> bool:
    """Whether *text* actually asks something, rather than merely needing a reply.

    Deliberately narrower than _attention(). That returns "asks" for a trailing
    colon and for phrases requesting input, which are good reasons to go and
    look but are not questions; it also returns "asks" from a session's status
    alone, where nothing has been read at all. The panel's "?" is a claim that
    there is a question to answer, so it is made only where one is visible.

    The pending note is the strongest evidence available: a structured question
    is rendered to text ending in it, so its presence means a question block
    exists and has no answer.
    """
    body = (text or "").strip()
    if not body:
        return False
    if _QUESTION_PENDING_NOTE in body:
        return True
    # Same tail-trimming as _attention: a question can end in a quote or a
    # closing bracket and still be a question.
    return body.rstrip().rstrip("`*_)\"'").endswith("?")
def _one_line(text: str, limit: int = 120) -> str:
    """First line of *text*, collapsed, for the supervisor's preview column."""
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat
# Last failure seen per session, keyed by the transcript mtime it was read at.
# A file that has not moved cannot have gained a new failure, so the tail read
# is skipped -- the busy fast path exists to make a five-second poll affordable
# and it must stay affordable.
_failure_cache: dict[str, tuple[str, str | None]] = {}
async def _session_failure(session_id: str, file_touched: str) -> str | None:
    """The newest turn's failure text for *session_id*, or None."""
    cached = _failure_cache.get(session_id)
    if cached is not None and cached[0] == file_touched:
        return cached[1]
    try:
        failure = await transcripts.last_error(session_id)
    except Exception:  # noqa: BLE001 -- the view must render without it
        # exc_info because this catches both a malformed transcript, which is
        # expected and benign, and a programming error, which is neither. A
        # NameError from a misspelled callee produces the same line as an empty
        # transcript without it -- which is exactly how the unqualified
        # _scan_questions_sync call in this file returned "no questions found"
        # on every request instead of failing.
        _log.warning("last_error failed session=%s", session_id, exc_info=True)
        return None
    _failure_cache[session_id] = (file_touched, failure)
    return failure
async def _cli_maps(marks: dict) -> tuple[dict, dict, dict, dict]:
    """The four CLI lookups `classify_chat` needs, keyed by session id.

    Extracted for the same reason `classify_chat` itself was: both the sidebar
    and the members panel classify conversations, and a classifier given
    different inputs on each surface reaches different answers however carefully
    it is written. The members panel passed `{}, {}, {}` and therefore could not
    see that a linked terminal was still working -- so it promoted running work
    to "finished" while the sidebar, holding the same rule and better inputs,
    correctly kept it quiet. Two surfaces, one function, and still a
    disagreement, because the shared thing was the logic and not the data.

    The fourth map answers "is this session showing a prompt right now", read
    off its terminal. It is here rather than inside `classify_chat` because that
    function is pure and must stay so to be testable; and it is in `_cli_maps`
    rather than at one call site for the reason the other three are, which is
    that a surface computing its own inputs is how the two disagreed before.

    Only sessions that already look blocked are captured. The capture costs a
    subprocess, both callers are polled, and a session that is busy or idle is
    not sitting on a prompt -- so asking about one would spend the subprocess to
    be told what its status already said.

    Returns empty maps when the session registry cannot be read: unknown status
    is the safe default everywhere it is consulted.
    """
    try:
        sessions = await db.read_claude_sessions()
    except Exception:  # noqa: BLE001 -- a surface must render without them
        return {}, {}, {}, {}
    status: dict[str, str] = {}
    dismissed: dict[str, str] = {}
    updated: dict[str, str] = {}
    for entry in sessions:
        session_id = entry.get("sessionId", "")
        if not session_id:
            continue
        status[session_id] = (entry.get("status") or "").lower()
        dismissed[session_id] = marks.get(
            ("session", session_id), {}).get("dismissed_at", "")
        updated[session_id] = entry.get("status_updated_at", "")
    blocked = [sid for sid, value in status.items() if _session_needs_a_person(value)]
    prompting: dict[str, bool] = {}
    for session_id in blocked:
        prompting[session_id] = await asyncio.to_thread(
            prompts.has_prompt, session_id)
    return status, dismissed, updated, prompting
# Session statuses that do NOT mean "a person is needed".
#
# Claude Code writes `status` into ~/.claude/sessions/<pid>.json. Three values
# are observed on 2.1.252: `busy` (working), `idle` (nothing further to do --
# the task concluded) and `waiting` (blocked, needs a human). Membership here is
# the allowlist rather than a check against `waiting`, so that a value nobody has
# seen before is treated as blocked and reaches a person; the cost of
# over-reporting is a row to dismiss, and the cost of under-reporting is an
# agent stuck with nobody told.
#
# An absent status means the build is older than the field, which is "unknown"
# and not "idle" -- and unknown is handled by the `if cli_status` guard, so it
# never reaches this set.
_CLI_STATUS_NOT_BLOCKED: Final[frozenset[str]] = frozenset({"busy", "idle"})
def _dismissed_at(mark: dict, cli_dismiss_map: dict, session_id: str) -> str:
    """When this row was last silenced, under *either* of its two identities.

    A conversation linked to a terminal session can be dismissed as a
    conversation -- the control writes ``("chat", id)`` -- or as a session, and
    silencing one must silence the other. Otherwise the user dismisses the
    terminal row and the web row summons them back for the same piece of work.
    The later timestamp wins.

    Extracted because this rule was written out twice, sixty lines apart, once
    in the blocked-session branch and once in the dismissal check below, each
    with its own paragraph explaining the same thing. Two copies of one rule is
    how the pair drifts: the blocked branch consulted only the session mark for
    a while, so a dismissal was recorded faithfully and then never read, and the
    control looked inert.

    Returns "" when neither identity has been dismissed, which sorts before
    every real timestamp and so never silences anything by accident.
    """
    return max(
        cli_dismiss_map.get(session_id, "") or "",
        mark.get("dismissed_at") or "",
    )
def _attention_reason(last: dict) -> str | None:
    """Why this message needs a person, or None.

    Reads the **end** of the message as well as its opening. ``_attention``
    decides mostly on how text ends, and ``preview`` is only the first 200
    characters, so a question at the end of anything longer was invisible to it:
    the row fell through to the "done" promotion and was announced as completed
    work, with no "?" on it, while the agent sat waiting for an answer.

    Both, not just the tail, because ``_attention`` also matches blocker phrases
    anywhere in the text and those often open a message rather than close it.

    The pending note is checked last and is the stronger signal: it marks a
    structured question that is definitely unanswered, and it is appended to the
    end of the rendered text.
    """
    preview = last.get("preview") or ""
    tail = last.get("tail") or preview
    reason = _attention(tail) or _attention(preview)
    if not reason and _QUESTION_PENDING_NOTE in (tail + preview):
        reason = "asks"
    return reason
def _session_needs_a_person(cli_status: str) -> bool:
    """Whether a linked CLI session's own status means someone is required.

    This read ``cli_status != "busy"``, which put a session that had simply
    FINISHED into the waiting feed with reason "asks" -- reporting an agent that
    needs nothing as one blocked on a question. Claude Code 2.1.252 writes three
    values, not the one the old comment described: ``busy``, ``waiting`` and
    ``idle``. Only ``waiting`` means a person is required; ``idle`` means the
    task concluded, which belongs in the routine-output path where a read mark
    retires it. Conflating them padded the badge with rows that wanted nothing,
    and the badge is only worth having while every row in it is real.

    An unrecognised value counts as blocked rather than finished: a status this
    code has never seen should over-report to a human, not quietly retire an
    agent that may be stuck.
    """
    return bool(cli_status) and cli_status not in _CLI_STATUS_NOT_BLOCKED
def classify_chat(
    chat: dict,
    last: dict,
    live_ids: set | frozenset,
    queued: dict,
    marks: dict,
    cli_status_map: dict,
    cli_dismiss_map: dict,
    cli_status_updated_map: dict,
    cli_prompt_map: dict | None = None,
) -> dict | None:
    """Classify one conversation as waiting, working or updated.

    Returns the entry with a "status" key set, or None when the conversation
    should not be listed at all.

    Extracted so the sidebar and the supervisor members panel share one
    definition of what "stuck" means. They had to: two implementations would
    agree only by coincidence, and would drift the first time either was
    touched -- the same argument that made backend_kind a single function
    rather than a rule reimplemented client-side.

    A pure function of what it is given, which is what makes it callable for
    one member as cheaply as for the whole sidebar. handle_supervisor does far
    more than classify -- it merges CLI sessions, reads marks and applies
    dismissals -- so calling that handler to learn one member's status would
    have paid for all of it and turned its response shape into an API nobody
    intended to depend on.
    """
    entry = {
        "kind": "chat",
        "id": chat["id"],
        "title": chat.get("title") or "Untitled",
        "preview": _one_line(last.get("preview") or ""),
        "since": last.get("created_at") or "",
    }
    # Busy outranks everything, and is asked rather than inferred. A queued
    # prompt counts as busy too: the user has already said what they want and
    # is waiting on us, not the other way round.
    if chat["id"] in live_ids or queued.get(chat["id"]):
        return {**entry, "status": "working"}
    # A turn the user stopped is not something to be summoned back to. It is
    # absent from running_ids, so it never looked busy that way -- but a cancel
    # that persisted nothing leaves the user's own prompt newest, which the
    # branch below reports as working, and it would stay that way rather than
    # clearing when the buffer is reaped.
    live = turns.get(chat["id"])
    if live is not None and live.state == "cancelled":
        return None
    if last.get("role") != "assistant":
        # The newest message is the user's own and no turn is registered. That
        # is ambiguous -- a turn that died, or one that has not started yet --
        # and the common case is the second: answering a question makes the
        # user's reply newest for the moment before the turn begins. Calling
        # that "waiting" would put the highlight back the instant it was
        # answered, which is the opposite of what was asked for.
        return {**entry, "status": "working"}
    # An ask or a blocker outranks everything: it stays listed until it is
    # actually answered, which for a conversation means the newest message
    # stops being the agent's. Opening it is not answering it -- clearing on
    # read let a question be dismissed by glancing at it.
    mark = marks.get(("chat", chat["id"]), {})
    stamp = last.get("created_at") or ""
    preview = last.get("preview") or ""
    tail = last.get("tail") or preview
    reason = _attention_reason(last)
    if reason:
        # Only an explicit dismissal silences an unanswered question.
        if mark.get("dismissed_at") and stamp <= mark["dismissed_at"]:
            return None
        return {
            **entry, "status": "waiting", "reason": reason,
            # The tail, not the preview: an agent asks at the end, so the
            # opening 200 characters answer this about the wrong part of the
            # message. Falls back to the preview for a message short enough
            # that they are the same text.
            "question": _asks_a_question(tail),
        }
    # A web conversation linked to a CLI session that has stopped and is
    # *blocked* is not "updated" -- it needs a person. Defer to the session's own
    # status so the user sees the question that triggered it rather than a
    # truncated preview that _attention() cannot match.
    #
    # This read `cli_status != "busy"`, which put a session that had simply
    # FINISHED into the waiting feed with reason "asks" -- reporting an agent
    # that needs nothing as one blocked on a question. Claude Code 2.1.252
    # writes three values here, not the one the comment in db.read_claude_sessions
    # used to describe: `busy`, `waiting`, and `idle`. Only `waiting` means a
    # person is required; `idle` means the task concluded, which belongs in the
    # routine-output path below where a read mark retires it. Conflating them is
    # what padded the badge with rows that wanted nothing, and the badge is only
    # worth having while every row in it is real.
    #
    # An unrecognised value is treated as blocked rather than finished: a future
    # status this code has never seen should over-report to a human, not quietly
    # retire an agent that may be stuck.
    session_id = chat.get("session_id", "")
    cli_status = cli_status_map.get(session_id, "")
    blocked_and_dismissed = False
    if _session_needs_a_person(cli_status):
        dismissed = _dismissed_at(mark, cli_dismiss_map, session_id)
        # Fall back to the conversation's own last activity when the session
        # file carries no status timestamp, so the guard below cannot fail open
        # and relist unconditionally.
        #
        # The note that used to sit here said the timestamp was absent on "every
        # non-busy session on this machine". That was measured against an older
        # CLI; on 2.1.252 every live session carries `statusUpdatedAt`,
        # whatever its status. The fallback stays because a build without the
        # field is still possible and the failure mode it prevents is silent,
        # but it is now the rare path rather than the usual one.
        status_updated = cli_status_updated_map.get(session_id, "") or stamp
        if not (dismissed and status_updated and status_updated <= dismissed):
            return {
                **entry,
                "status": "waiting",
                "reason": "asks",
                "reason_detail": f"session={cli_status}",
                # "asks" here comes from the session's status, which says a
                # person is needed but not that a question was put to them. Two
                # things can supply that, and both are observations rather than
                # inferences: the message text, and a prompt actually on screen.
                #
                # The screen is what closes the case this could not see. A
                # permission prompt is never written to the transcript, so a
                # session blocked on one had no message to match and no "?" --
                # it appeared in the badge as an agent that had merely stopped,
                # with nothing to say a keystroke would free it.
                "question": (
                    _asks_a_question(
                        last.get("tail") or entry.get("preview") or "")
                    or bool((cli_prompt_map or {}).get(session_id))
                ),
            }
        # Dismissed while blocked. Noted rather than returned: the read check
        # below must still run, because `read_mark_set` writes the same timestamp
        # to `read_at` and `dismissed_at`, so a dismissal is also a read and a
        # read retires the row completely. Returning here skipped that and left
        # the conversation listed quietly when the contract says it leaves.
        #
        # But it must not fall all the way through either. That was harmless
        # while the tail could only file it as `updated`; the "done" promotion
        # re-raised it, because the row's own message is usually newer than the
        # dismissal -- so a dismissed question came straight back as a
        # completion. The user dismissed *this row*.
        blocked_and_dismissed = True
    # Seen already: nothing to say at all.
    if mark.get("read_at") and stamp <= mark["read_at"]:
        return None
    # Listed, but never a summons. `updated` stays the quiet bucket it always
    # was; what changed is only which rows are promoted out of it.
    quiet = {**entry, "status": "updated"}
    if blocked_and_dismissed:
        return quiet
    # A linked terminal session that is still busy has NOT ended -- its work is
    # happening in the terminal, where this process cannot see a turn. Its
    # output is still worth listing, but announcing it as finished would be the
    # "text is still being sent" case the highlight is meant to exclude.
    if cli_status == "busy":
        return quiet
    # Dismissed: demoted to quiet rather than deleted. The user said "stop
    # summoning me", not "forget this happened", and the row reappearing after a
    # dismissal is the complaint that made that control look inert once already.
    #
    dismissed_either = _dismissed_at(mark, cli_dismiss_map, session_id)
    if dismissed_either and stamp <= dismissed_either:
        return quiet
    # The action has ended: the agent spoke last, no turn is registered, the
    # linked session (if any) is not busy, and it is asking for nothing. That is
    # the outcome the user is waiting for, so it is promoted to a highlight with
    # its own reason rather than filed silently.
    #
    # Retired by *reading* it, which is what keeps the count meaningful -- the
    # docstring below warned that "finished at some point" would mark every
    # completed conversation for ever, and unread-since-it-finished is the
    # narrower claim. A question is different and still needs answering or
    # dismissing, because looking at a question does not answer it.
    return {**entry, "status": "waiting", "reason": "done", "question": False}
async def _classify_cli_session(cli: dict, meta: dict, mark: dict) -> dict | None:
    """Classify one terminal session as waiting, working, or not listed at all.

    The counterpart of `classify_chat`, and named to say so. Both halves of the
    supervisor feed answer the same question -- is a person needed here -- about
    two kinds of thing, but only one of them was a function. The other was a
    hundred-and-forty-line loop body inside the handler, and that asymmetry is
    why the two drifted: every rule the conversation path learned had to be
    learned again here, separately and late. `if status:` treated a finished
    agent as a blocked one; a trailing tool call was read as speech because only
    the role was checked; mtime was briefly trusted as a positive signal when it
    is only ever a negative one.

    Returns an entry carrying its own "status", so the caller files it in
    exactly the buckets it uses for a conversation, or None when the session
    should not be listed.

    Async because deciding can require reading. A blocked or newly-quiet session
    needs its transcript, and a busy one needs its last-failure check; both are
    skipped on the common path, which is what keeps this cheap enough to poll
    every few seconds.
    """
    session_id = cli.get("sessionId") or ""
    # Epoch seconds from the file, ISO from the marks: compare like for like.
    file_touched = datetime.datetime.fromtimestamp(
        meta.get("updated_at") or 0, datetime.UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = mark.get("read_at", "")
    status = (cli.get("status") or "").strip().lower()
    # A busy agent needs no transcript read at all, which is what keeps
    # this cheap enough to poll: it is the common case.
    if status == "busy":
        row = {
            "kind": "session", "id": session_id,
            "title": cli.get("name") or meta.get("title") or session_id,
            "preview": "", "since": file_touched, "status": "working",
        }
        # Busy is honest but incomplete: a session retrying a dead endpoint
        # reports busy the whole time, so a run going nowhere looks exactly
        # like one doing work. The failure is read from the model field --
        # the CLI writes "<synthetic>" on a failed turn -- rather than from
        # the wording, so an agent *discussing* an API error is not mistaken
        # for one suffering it. Cached against the transcript's mtime: an
        # unmoved file cannot have gained a new failure, which keeps the
        # fast path fast on the common case of a session quietly working.
        failure = await _session_failure(session_id, file_touched)
        if failure:
            dismissed = mark.get("dismissed_at") or ""
            # Retired only by an explicit dismissal, never by being read: a
            # failing endpoint does not fix itself by being looked at. It
            # also clears itself once the agent produces real output again,
            # because last_error only reports a failure that is still the
            # newest turn.
            if not (dismissed and file_touched <= dismissed):
                return {
                    **row, "preview": _one_line(failure),
                    "status": "waiting", "reason": "failed",
                    # A failure is not a question, however it is worded.
                    "question": False,
                }
        return row
    # For a session that is not blocked -- no status field at all, or an
    # explicit `idle` -- fall back to mtime as a negative filter only: an
    # untouched file certainly has nothing new. It must never decide
    # "waiting" on its own -- one cross-session message deposits dozens of
    # queue-operation and attachment records into the receiving session, so
    # with several agents talking the mtime is never still.
    #
    # `busy` has already been handled and return Noned above, so reaching here
    # with a status in the not-blocked set means `idle`.
    if (not status or status in _CLI_STATUS_NOT_BLOCKED) and seen \
            and file_touched <= seen:
        return None
    page = await transcripts.read_turns(session_id)
    page_turns = page.get("turns") or []
    if not page_turns:
        return None
    entry = {
        "kind": "session",
        "id": session_id,
        "title": cli.get("name") or meta.get("title") or session_id,
        "preview": "",
    }
    # Claude Code reports its own state, which beats inferring one from the
    # transcript: a session at a permission prompt and one running a tool
    # look identical in the file. A *blocked* session stays listed until it
    # starts working again, which only happens once someone answers it, so a
    # read mark deliberately does not retire this.
    #
    # This read `if status:`, on the stated belief that "any non-busy value
    # means it has stopped and is waiting on a human". That was true while
    # `busy` was the only value the CLI wrote. 2.1.252 also writes `idle`,
    # which means the opposite -- the task concluded and nothing is needed --
    # so every finished agent was being listed as blocked, with its last
    # sentence presented as though it were a question. Idle now falls through
    # to the routine-output path below, where being read retires it.
    if status and status not in _CLI_STATUS_NOT_BLOCKED:
        spoke_at = cli.get("status_updated_at") or file_touched
        dismissed = mark.get("dismissed_at") or ""
        if dismissed and spoke_at <= dismissed:
            return None
        # A structured question outranks prose: it is an unambiguous ask,
        # and its answer block is an unambiguous resolution.
        pending = _pending_question(page_turns)
        said = pending or _last_thing_said(page_turns)
        return {
            **entry,
            "since": spoke_at,
            "preview": _one_line(said),
            "status": "waiting",
            "reason": "asks" if pending else (_attention(said) or "idle"),
            "question": bool(pending) or _asks_a_question(said),
        }
    last_turn = page_turns[-1]
    # A trailing tool call means the agent is still running, not that it
    # has stopped with something to say. Checking only role was wrong:
    # every tool turn carries role="assistant" too.
    last_is_speech = last_turn.get("role") == "assistant" and any(
        b.get("kind") == "text" and (b.get("text") or "").strip()
        for b in last_turn.get("blocks", [])
    )
    if not last_is_speech:
        return {**entry, "since": file_touched, "status": "working"}
    # The real signal: when the agent last said something, not when its
    # file was last written.
    spoke_at = str(last_turn.get("timestamp") or "") or file_touched
    if seen and spoke_at <= seen:
        return None
    # The last block of the turn, not the first: a turn often opens with a
    # sentence of narration and ends with the actual question.
    text = " ".join(
        (b.get("text") or "").strip()
        for b in last_turn.get("blocks", [])
        if b.get("kind") == "text"
    ).strip()
    pending = _pending_question(page_turns)
    if pending:
        text = pending
    reason = "asks" if pending else _attention(text)
    row = {**entry, "since": spoke_at, "preview": _one_line(text)}
    if reason:
        # Unanswered outranks read, same as for conversations.
        dismissed = mark.get("dismissed_at") or ""
        if not (dismissed and spoke_at <= dismissed):
            return {
                **row, "status": "waiting", "reason": reason,
                "question": bool(pending) or _asks_a_question(text),
            }
    elif not (seen and spoke_at <= seen):
        # The agent finished speaking and is asking nothing. That is the
        # outcome the user is waiting for, so it is surfaced rather than
        # filed quietly -- with its own reason, because calling it "needs an
        # answer" would be a lie. Unread-since-it-spoke, so reading retires
        # it; a question would instead need answering.
        return {
            **row, "status": "waiting", "reason": "done", "question": False,
        }
    return None
# A location hint, not a state claim. The message body is fixed at import time
# and cannot be revised when the answer arrives in a later sync, so "waiting for
# an answer" would keep asserting that forever -- including next to the
# "Declined in the terminal" message that immediately follows it. Where to
# answer stays true either way; the outcome is reported by its own message.
