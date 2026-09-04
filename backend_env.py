"""One definition of "what environment does a backend imply".

The rule existed three times: `claude_proxy._backend_env` for proxied turns,
`runner._build_env` for direct turns, and `apply_env` in `bin/wc-claude.sh` for
a session started by hand in a terminal. All three had to agree about which
variables to set *and* which to remove, and the shell one carried a comment
saying so out loud -- "Mirror claude_proxy._backend_env exactly, including what
it removes." A rule maintained in three places by hand is a rule that drifts,
and registry #68 is the scar: the proxy path inherited `ANTHROPIC_BASE_URL`
from its shell, so switching a machine off a gateway and back to Anthropic kept
sending turns to the gateway with nothing in the UI to say so.

This module is the single definition. It is deliberately pure -- it reads no
environment, opens no database and touches no files -- because the three callers
need the answer in three different shapes:

* the proxy copies the whole parent environment and applies the deltas to it
* the direct runner builds from an allowlist and applies the same deltas
* the shell wrapper cannot replace a user's interactive environment at all, and
  needs `export` and `unset` lines to apply to the one it already has

Which is why the answer is *deltas* rather than a finished environment. Only an
explicit "unset this" survives all three, and the removals are the half that a
finished dict cannot express to a shell.

The credential never travels as an argument. `/proc/<pid>/cmdline` is
world-readable, so a key on the command line is readable by every process on
the host -- which is not hypothetical here: the four live sessions' routing was
read exactly that way while designing this.
"""
from __future__ import annotations

from typing import Any, Final

# Set for every spawned turn regardless of backend, so a beta the CLI enables by
# default cannot change behaviour under us between releases.
_ALWAYS_SET: Final[dict[str, str]] = {
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
}


class Deltas:
    """The environment changes a backend implies.

    ``set`` maps names to values; ``unset`` names variables that must not reach
    the CLI whatever the caller's environment holds. A name never appears in
    both.
    """

    __slots__ = ("set", "unset")

    def __init__(self, set_: dict[str, str], unset: list[str]):
        self.set = set_
        self.unset = unset

    def apply_to(self, env: dict[str, str]) -> dict[str, str]:
        """Return *env* with these deltas applied. Does not mutate the input."""
        result = dict(env)
        for name in self.unset:
            result.pop(name, None)
        result.update(self.set)
        return result

    def as_shell(self) -> str:
        """`export`/`unset` lines for a shell to `eval`.

        Values are single-quoted with embedded quotes escaped, so a base URL or
        key containing a shell metacharacter cannot break out into a command.
        """
        lines: list[str] = []
        for name in self.unset:
            lines.append(f"unset {name}")
        for name, value in self.set.items():
            escaped = str(value).replace("'", "'\\''")
            lines.append(f"export {name}='{escaped}'")
        return "\n".join(lines)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Deltas):
            return NotImplemented
        return self.set == other.set and sorted(self.unset) == sorted(other.unset)

    def __repr__(self) -> str:
        return f"Deltas(set={self.set!r}, unset={self.unset!r})"


def _text(value: Any) -> str:
    """A stripped string, or "" for anything that is not usable text."""
    return value.strip() if isinstance(value, str) else ""


def deltas(backend: Any) -> Deltas:
    """The environment changes *backend* implies.

    *backend* is a machine record — ``{provider, base_url, api_key}`` — or
    anything else, which is treated as "no backend".

    This took a ``simple_supported`` flag at first, on the reasoning that only
    the direct runner sets ``CLAUDE_CODE_SIMPLE`` so only it should be told to
    remove it. The equivalence test against the proxy disproved that: the proxy
    removes an *inherited* one too, and has to. The variable does not need to
    have been set by us to be present, and while it is set the CLI ignores OAuth
    and the keychain -- so a keyless backend under an inherited
    ``CLAUDE_CODE_SIMPLE`` sends a turn with no credentials at all. Removal is
    unconditional on being keyless, and the flag is gone: it described the
    caller when the rule is about the environment.
    """
    set_: dict[str, str] = dict(_ALWAYS_SET)
    unset: list[str] = []

    # Unconditional, before any branch. The CLI prefers ANTHROPIC_AUTH_TOKEN
    # over ANTHROPIC_API_KEY, and a machine record never supplies a token --
    # it holds `api_key`. So an inherited token silently outranks the key set
    # below and the turn goes out with whatever credentials the launching shell
    # was pointed at, which is the failure that is hardest to see because
    # everything succeeds, just against the wrong account.
    unset.append("ANTHROPIC_AUTH_TOKEN")

    if not isinstance(backend, dict) or backend.get("provider") != "anthropic":
        # A non-anthropic backend -- or no backend at all -- must not inherit
        # Anthropic variables. When the CLI is talking to a gateway, a base URL
        # or key from the host shell reaching the child would route the turn,
        # or authenticate it, somewhere nobody selected. Missing ANTHROPIC_API_KEY
        # here left a key exported by an *earlier* call in the same long-lived
        # process (the shell wrapper's hot-swap, mid-session) reaching a child
        # that should have had none: switching off an Anthropic machine did not
        # clear its key, only switching to a different Anthropic machine did.
        unset.append("ANTHROPIC_BASE_URL")
        unset.append("ANTHROPIC_API_KEY")
        return Deltas(set_, unset)

    base_url = _text(backend.get("base_url"))
    if base_url:
        set_["ANTHROPIC_BASE_URL"] = base_url
    else:
        # No base_url means "the official API" -- which requires *removing* an
        # inherited one, not leaving it alone. Leaving it alone is registry #68.
        unset.append("ANTHROPIC_BASE_URL")

    api_key = _text(backend.get("api_key"))
    if api_key:
        set_["ANTHROPIC_API_KEY"] = api_key
    else:
        # Fall through to the host's own login: drop any inherited key so a
        # stale one from the launching shell cannot be used instead.
        unset.append("ANTHROPIC_API_KEY")
        unset.append("CLAUDE_CODE_SIMPLE")
    return Deltas(set_, unset)


def describe(backend: Any) -> str:
    """A one-line, credential-free summary for logs and for the banner.

    Never includes the key. Whether a key is present is the operationally
    useful fact and is safe to state; the value is not.
    """
    if not isinstance(backend, dict) or backend.get("provider") != "anthropic":
        provider = _text(backend.get("provider")) if isinstance(backend, dict) else ""
        return f"provider={provider or 'none'} anthropic_vars=stripped"
    return (
        f"provider=anthropic "
        f"base_url={_text(backend.get('base_url')) or '<default>'} "
        f"api_key={'set' if _text(backend.get('api_key')) else 'host login'}"
    )
