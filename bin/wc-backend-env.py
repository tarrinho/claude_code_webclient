#!/usr/bin/env python3
"""Emit the environment WebConsole's active backend implies, for a shell to eval.

    eval "$(bin/wc-backend-env.py --sh)"

This exists so a session started by hand in a terminal is configured by the same
code as a turn spawned by the console. `bin/wc-claude.sh` used to reimplement the
rule in bash, under a comment reading "Mirror claude_proxy._backend_env exactly,
including what it removes" -- an instruction to keep two files in step by hand.
Both now call `backend_env.deltas`, and so does the direct runner.

Why the deltas are emitted as `export`/`unset` lines rather than a finished
environment: a wrapper cannot replace the interactive environment it was
launched in, and the *removals* are the half that matters. An inherited
ANTHROPIC_AUTH_TOKEN outranks the key we set, an inherited ANTHROPIC_BASE_URL
sends the turn to a gateway nobody selected (registry #68), and an inherited
CLAUDE_CODE_SIMPLE stops the CLI reading the host login it has been told to fall
back to. None of those can be expressed by handing over a dict.

The database is read here, not passed in, so no credential ever appears in a
command line: /proc/<pid>/cmdline is world-readable. `--sh` output *does* carry
the key, because setting it is the point -- so it is meant for `eval`, and
running it into a terminal puts the key on screen and in scrollback.

Also exports WC_PROFILE, which is not a secret and is the point of the whole
thing being uniform: the console can then see which backend any given terminal
session is on, and a session with no WC_PROFILE is one that never went through
the wrapper and is silently unrouted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import backend_env


def _db_path() -> Path:
    return Path(os.environ.get("WC_DB_PATH") or (HERE / "data" / "webconsole.db"))


def machine_for(db: Path, profile: str | None = None) -> dict[str, object]:
    """The machine to use: *profile* by slug, or the console's active one.

    Selecting by name is what lets a session pin a backend for its own lifetime
    without changing what the console routes to. That is required, not a
    convenience: the shell aliases c2..c6 pin gateway model ids
    (`azure_ai/...`, `vllm/...`) that only exist on one backend, so following
    the *active* machine would send them wherever the console happened to be
    pointed and fail with a 429 that reads like a capacity problem.
    """
    if not db.is_file():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # SELECT * rather than a column list. This tool must not be the reason
        # a terminal fails to launch, and naming columns makes it fail on any
        # database that predates one of them -- `active_models` was added after
        # first release, so an older file has the machines but not the column.
        # Callers read through .get(), so a missing column is a missing value.
        rows = [dict(r) for r in con.execute("SELECT * FROM ai_machines")]
        try:
            setting = con.execute(
                "SELECT value FROM settings WHERE key = 'default_model'"
            ).fetchone()
            default_model = setting["value"] if setting else ""
        except sqlite3.Error:
            default_model = ""
    finally:
        con.close()
    if profile:
        wanted = profile.strip().lower()
        matches = [r for r in rows if profile_slug(r) == wanted]
        if not matches:
            known = ", ".join(sorted({profile_slug(r) for r in rows})) or "none"
            print(f"wc-backend-env: no backend named {profile!r}. "
                  f"Known profiles: {known}", file=sys.stderr)
            raise SystemExit(2)
        record = matches[0]
    else:
        active = [r for r in rows if r.get("active")]
        if not active:
            return {}
        record = active[0]
    if not str(record.get("model") or "").strip():
        record["model"] = default_model
    return record


def active_machine(db: Path) -> dict[str, object]:
    """The machine the console is currently routing to, or {} if none.

    Retained as the name the rest of the tree already uses. Read-only, always:
    opening this database read-write from a second process is what took the
    write path down for 37 minutes (registry #41).
    """
    return machine_for(db)


def profile_slug(machine: dict[str, object]) -> str:
    """A short, stable, shell-safe identifier for the active backend.

    Derived from the name rather than the id so it is readable in a process
    list, and sanitised because it is about to be exported into a shell.
    """
    name = str(machine.get("name") or "").strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or f"machine-{machine.get('id') or 'unknown'}"


def served_models(machine: dict[str, object]) -> list[str]:
    """The models the active backend advertises, or [] when it does not say.

    `active_models` is a JSON array written by the console. Empty or malformed
    means "unknown", which is not the same as "none" -- a backend that has never
    published a list must not be treated as serving nothing.
    """
    raw = machine.get("active_models")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [m for m in parsed if isinstance(m, str)] if isinstance(parsed, list) else []


def _check_model(machine: dict[str, object], model: str) -> int:
    """Refuse a model the active backend does not serve.

    This is the mistake the whole arrangement exists to stop. A model id is only
    meaningful against the backend serving it: `vllm/Qwen3.6-35B-A3B-NVFP4` is a
    real model on the gateway and nonsense against api.anthropic.com, and
    `claude-opus-5` is the reverse. Sent to the wrong one, the gateway answers
    429 "No deployments available for selected model" -- a routing failure
    wearing a capacity error's clothes, which is the hardest kind to read.

    Silence when the backend publishes no list. Guessing would block a model
    that works, and a check that cries wolf gets switched off.
    """
    if not machine:
        print("wc-backend-env: no active backend, so no model can be checked",
              file=sys.stderr)
        return 0
    served = served_models(machine)
    if not served:
        return 0
    if model in served:
        return 0
    name = machine.get("name") or "the active backend"
    print(
        f"wc-backend-env: {name} does not serve {model!r}.\n"
        f"  It serves: {', '.join(served)}\n"
        f"  A model id only means something against its own backend -- sending "
        f"this one gets a 429 that reads like a capacity problem.\n"
        f"  Switch backend in the console, pick a served model, or set "
        f"WC_SKIP_MODEL_CHECK=1 to proceed anyway.",
        file=sys.stderr,
    )
    return 1


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sh", action="store_true",
                       help="export/unset lines for eval (carries the key)")
    group.add_argument("--json", action="store_true",
                       help="the same decision, credential-free, for inspection")
    parser.add_argument("--profile", metavar="NAME",
                        help="use this backend rather than the console's active "
                             "one, by slug (see --json for the slug)")
    group.add_argument("--fields", action="store_true",
                       help="tab-separated provider/base_url/api_key/model/name, "
                            "for the shell wrapper's banner and poller")
    group.add_argument("--check-model", metavar="MODEL",
                       help="exit non-zero if the active backend does not serve "
                            "MODEL; prints what it does serve")
    args = parser.parse_args()

    machine = machine_for(_db_path(), args.profile)

    if args.check_model:
        return _check_model(machine, args.check_model)

    deltas = backend_env.deltas(machine or None)
    slug = profile_slug(machine) if machine else ""
    model = str(machine.get("model") or "").strip()

    if args.fields:
        # One selection implementation. The wrapper used to run its own SQL in a
        # heredoc for this, which meant the banner could name a different
        # backend than the environment was actually set to -- and it did, the
        # moment --profile existed: the banner read the *active* machine while
        # the session ran on the pinned one. The wrapper's own comment calls
        # this "the one line that tells you which backend you are about to talk
        # to", so it lying is worse than it being absent.
        #
        # "-" for empty keeps the field positions readable by `read`, and any
        # internal whitespace is collapsed so a value cannot forge a field.
        def field(value: object) -> str:
            text = "" if value is None else str(value).strip()
            return "".join(text.split()) or "-"

        def last(value: object) -> str:
            # The name is read last and `read` puts the remainder in the final
            # variable, so spaces are safe there; only newlines are not.
            text = "" if value is None else str(value).strip()
            return " ".join(text.split()) or "-"

        if not machine:
            print("\t".join(["-"] * 5))
            return 0
        print("\t".join([
            field(machine.get("provider")),
            field(deltas.set.get("ANTHROPIC_BASE_URL")),
            field(machine.get("api_key")),
            field(model),
            last(machine.get("name")),
        ]))
        return 0

    if args.json:
        # Never the key. Whether one is present is the useful fact.
        print(json.dumps({
            "profile": slug or None,
            "name": machine.get("name") if machine else None,
            "provider": machine.get("provider") if machine else None,
            "base_url": deltas.set.get("ANTHROPIC_BASE_URL"),
            "model": model or None,
            "api_key": "set" if "ANTHROPIC_API_KEY" in deltas.set else "host login",
            "set": sorted(deltas.set),
            "unset": sorted(deltas.unset),
            "summary": backend_env.describe(machine or None),
        }, indent=2))
        return 0

    lines = [deltas.as_shell()]
    if slug:
        # Not a secret, and deliberately exported: it is how the console tells a
        # routed session from one that never went through the wrapper.
        lines.append(f"export WC_PROFILE={_quote(slug)}")
        lines.append(f"export WC_PROFILE_NAME={_quote(machine.get('name') or '')}")
    else:
        # No active machine: say so positively rather than leaving a stale
        # value from a previous eval in this same shell.
        lines.append("unset WC_PROFILE")
        lines.append("unset WC_PROFILE_NAME")
    # Shell-local, not exported: the wrapper reads these to build its arguments
    # and the CLI must not inherit them as configuration.
    lines.append(f"WC_BACKEND_MODEL={_quote(model)}")
    lines.append(f"WC_BACKEND_NAME={_quote(machine.get('name') or '')}")
    print("\n".join(line for line in lines if line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
