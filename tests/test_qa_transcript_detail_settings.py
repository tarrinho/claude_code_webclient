"""Transcript detail settings: the three knobs must actually change output.

Added 2026-09-15 after Pedro reported terminal chats looking incomplete in the
console. The investigation split the report in two, and both halves are
asserted here because the difference is what stops the next person re-running
the same investigation:

  * Tool output and history depth WERE the console's doing -- a 2000-character
    cap on tool detail and result, and a 500-turn tail. Both are now settings
    and both tests below prove a changed setting changes what is returned.

  * Reasoning was NOT. Anthropic models write a thinking block carrying a
    signature and an empty ``thinking`` field, so the plaintext never reaches
    the transcript. ``test_empty_thinking_is_dropped_whatever_the_setting``
    pins that: turning the setting ON does not conjure text that was never
    written, which is the failure mode a well-meaning "fix" would introduce.

Each test drives the real ``_blocks_from_content`` / ``_cap`` rather than a
mock, because the bug being guarded against is a cap that silently stops being
consulted -- and a mock of the cap would agree with that bug.
"""

import config
import transcripts


def _thinking(text: str, signature: str = "sig") -> dict:
    return {"type": "thinking", "thinking": text, "signature": signature}


# --- reasoning ------------------------------------------------------------

def test_reasoning_with_text_is_shown_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_SHOW_REASONING", True)
    blocks = transcripts._blocks_from_content([_thinking("weighing two options")])
    assert [b["kind"] for b in blocks] == ["thinking"]
    assert blocks[0]["text"] == "weighing two options"


def test_reasoning_is_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_SHOW_REASONING", False)
    blocks = transcripts._blocks_from_content([_thinking("weighing two options")])
    assert blocks == []


def test_disabling_reasoning_leaves_other_blocks_alone(monkeypatch):
    """The setting hides reasoning, not the turn it was part of."""
    monkeypatch.setattr(config, "TRANSCRIPT_SHOW_REASONING", False)
    blocks = transcripts._blocks_from_content([
        _thinking("hidden"),
        {"type": "text", "text": "the answer"},
    ])
    assert [b["kind"] for b in blocks] == ["text"]


def test_empty_thinking_is_dropped_whatever_the_setting(monkeypatch):
    """A signature with no text renders nothing, even with the setting ON.

    This is the Anthropic shape: measured across every transcript on this host,
    claude-opus-5 had 0 thinking blocks carrying text against 9,292 empty. No
    setting can surface content the CLI never wrote, and a change that made
    this test fail would be rendering empty boxes.
    """
    for enabled in (True, False):
        monkeypatch.setattr(config, "TRANSCRIPT_SHOW_REASONING", enabled)
        blocks = transcripts._blocks_from_content([_thinking("", "x" * 920)])
        assert blocks == [], f"empty thinking rendered with setting={enabled}"


# --- tool output cap ------------------------------------------------------

def _tool_result(text: str) -> list[dict]:
    return [{"type": "tool_result", "tool_use_id": "t1", "content": text}]


def test_tool_result_is_cut_at_the_configured_limit(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_TOOL_OUTPUT_MAX", 300)
    blocks = transcripts._blocks_from_content(_tool_result("x" * 5000))
    assert len(blocks) == 1
    assert blocks[0]["truncated"] is True
    assert len(blocks[0]["text"]) <= 300


def test_raising_the_limit_returns_more_of_the_same_result(monkeypatch):
    """The point of the setting: a bigger number yields more text."""
    payload = "y" * 5000
    monkeypatch.setattr(config, "TRANSCRIPT_TOOL_OUTPUT_MAX", 300)
    small = transcripts._blocks_from_content(_tool_result(payload))[0]
    monkeypatch.setattr(config, "TRANSCRIPT_TOOL_OUTPUT_MAX", 8000)
    large = transcripts._blocks_from_content(_tool_result(payload))[0]
    assert len(large["text"]) > len(small["text"])
    assert small["truncated"] is True
    # 8000 is above the payload's own 5000, so nothing is cut at all -- the
    # limit stops binding rather than merely binding later.
    assert large["truncated"] is False
    assert len(large["text"]) == len(payload)


def test_tool_output_limit_has_a_floor(monkeypatch):
    """A nonsensical setting must not reduce every result to nothing."""
    monkeypatch.setattr(config, "TRANSCRIPT_TOOL_OUTPUT_MAX", 0)
    assert transcripts._tool_output_max() >= 200


# --- history depth --------------------------------------------------------

def _pairs(n: int) -> list[tuple[int, dict]]:
    return [(i * 100, {"n": i}) for i in range(n)]


def test_history_depth_keeps_the_most_recent_turns(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_MAX_TURNS", 5)
    turns, start, truncated = transcripts._cap(_pairs(20), 0, False)
    assert len(turns) == 5
    assert [t["n"] for t in turns] == [15, 16, 17, 18, 19], "kept the wrong end"
    assert truncated is True
    assert start == 1500, "start must move to the first kept turn, or paging back skips history"


def test_raising_history_depth_returns_more_turns(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_MAX_TURNS", 5)
    few, _, _ = transcripts._cap(_pairs(20), 0, False)
    monkeypatch.setattr(config, "TRANSCRIPT_MAX_TURNS", 50)
    many, _, truncated = transcripts._cap(_pairs(20), 0, False)
    assert len(many) == 20 > len(few)
    assert truncated is False


def test_history_depth_has_a_floor(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_MAX_TURNS", 0)
    assert transcripts._max_turns() >= 1
