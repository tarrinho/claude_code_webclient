"""chat_tree.py -- compose the sidebar's one-level hierarchy.

Three relations, three shapes, one output. Kept a pure function over rows the
caller already fetched: the sources have different failure modes, and a
composer that fetched its own inputs could not be tested without standing up
all three of them.

Design: docs/superpowers/specs/2026-09-21-chat-list-hierarchy-design.md §8
"""
from __future__ import annotations

from typing import Any

#: One level, and no more. The cap is not a style preference: a malformed
#: `parent_chat_id` -- a cycle, or a chain -- must not be able to make the
#: sidebar recurse, and a grandchild is attached to the nearest present
#: ancestor instead of creating a second indent level the card cannot draw.
_MAX_DEPTH = 1


def build_chat_tree(
    chats: list[dict[str, Any]],
    subagents: dict[str, list[dict[str, Any]]],
    member_of: dict[str, str],
) -> list[dict[str, Any]]:
    """Return *chats* as roots, each carrying a `children` list.

    `member_of` maps a chat id to the orchestrator chat that owns it.

    Precedence when a chat could nest two ways: orchestrator member, then
    voice parent, then root. A chat appears exactly ONCE in the output, so a
    tie has to resolve rather than duplicate -- a conversation rendered twice
    would be two rows that open the same thing and disagree about their status.

    A child whose parent is not in *chats* is returned as a ROOT, never
    dropped. The parent may be archived, deleted, or filtered out by a search,
    and losing a conversation because of that would be worse than showing it
    unnested.
    """
    present = {chat["id"] for chat in chats}

    def _parent_of(chat: dict[str, Any]) -> str | None:
        """The one parent this chat nests under, or None for a root."""
        owner = member_of.get(chat["id"])
        if owner and owner in present and owner != chat["id"]:
            return owner
        voice = chat.get("parent_chat_id")
        if voice and voice in present and voice != chat["id"]:
            return voice
        return None

    # Resolved once for every chat, before anything is attached: attaching as
    # we go would make the result depend on input order for a cycle.
    parent_by_id = {chat["id"]: _parent_of(chat) for chat in chats}

    def _returns_to_itself(chat_id: str) -> bool:
        """Whether following parents from *chat_id* comes back to it.

        A chat inside a cycle has no sensible parent, so it is treated as a
        root. Deciding this up front is what makes the output independent of
        input order: an earlier draft attached as it walked, so for a two-chat
        cycle whichever row came first became the parent of the other -- the
        same malformed data producing two different trees depending on the
        endpoint's sort.
        """
        seen = {chat_id}
        current = chat_id
        while True:
            parent = parent_by_id.get(current)
            if parent is None:
                return False
            if parent == chat_id:
                return True
            if parent in seen:
                return False   # a cycle, but one this chat is not part of
            seen.add(parent)
            current = parent

    roots = {
        chat["id"] for chat in chats
        if parent_by_id[chat["id"]] is None or _returns_to_itself(chat["id"])
    }

    def _host_for(chat_id: str) -> str | None:
        """The nearest ancestor within `_MAX_DEPTH + 2` hops that is itself a
        root, or None.

        A chain deeper than one level flattens onto the root it reaches rather
        than nesting further -- the card draws one level, so a grandchild joins
        its grandparent's card instead of creating a level with nowhere to go.

        Termination does not depend on this bound: every parent chain either
        hits `None` or loops back into a cycle, and every cycle's members are
        already roots (decided up front, above), so the walk below could not
        hang even unbounded. What the bound actually does is cap how far a
        chain is followed: a chain longer than that -- e.g. a five-chat chain
        a<-b<-c<-d<-e with `_MAX_DEPTH = 1` -- has its tail (`e`) fall out of
        reach of this walk and come back as `None`, so the caller promotes it
        to its own root instead of flattening it onto `a`. See
        test_a_chain_deeper_than_the_bound_promotes_the_tail.
        """
        current = parent_by_id.get(chat_id)
        for _ in range(_MAX_DEPTH + 2):
            if current is None:
                return None
            if current in roots:
                return current
            current = parent_by_id.get(current)
        return None

    out: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for chat in chats:
        if chat["id"] in roots:
            node = {**chat, "children": []}
            index[chat["id"]] = node
            out.append(node)

    # Subagents first, so a family's own work precedes the conversations it
    # handed off to. Both lists are already in a deliberate order -- spawn
    # order for subagents, the endpoint's order for chats -- so neither is
    # resorted here.
    for node in out:
        for row in subagents.get(node["id"], []):
            # Literal last: a row's own "kind" key (there shouldn't be one,
            # but rows come from a DB query the caller controls) must never
            # override what this loop is asserting about it.
            node["children"].append({**row, "kind": "subagent"})

    for chat in chats:
        if chat["id"] in roots:
            continue
        host_id = _host_for(chat["id"])
        host = index.get(host_id) if host_id else None
        if host is None:
            # Its whole chain is absent. Promote rather than drop -- and
            # give it its own subagents too: this node was not in `out` for
            # the loop above, so without this it would render with an empty
            # children list even when `subagents` has rows for it.
            node = {**chat, "children": []}
            for row in subagents.get(chat["id"], []):
                node["children"].append({**row, "kind": "subagent"})
            index[chat["id"]] = node
            out.append(node)
            continue
        # The parent that actually won for this chat, per _parent_of's
        # precedence -- not member_of alone. member_of can name an
        # orchestrator that is absent from `chats` (archived, deleted, or
        # filtered out by a search); _parent_of then falls through to the
        # voice parent, and labelling that "orchestrator" would describe a
        # nesting that did not happen.
        winning_parent = parent_by_id[chat["id"]]
        host["children"].append({
            "kind": "chat",
            "id": chat["id"],
            "title": chat.get("title"),
            "relation": ("orchestrator"
                         if winning_parent == member_of.get(chat["id"])
                         else "voice"),
        })

    return out
