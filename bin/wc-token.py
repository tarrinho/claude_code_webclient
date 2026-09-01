#!/usr/bin/env python3
"""Mint, list and revoke WebConsole API tokens from the shell.

The HTTP routes (`/api/tokens`) need a logged-in session, which is exactly what
an operator does not have when they are setting a machine up for the first time
-- so this exists to break that circle. It talks to the database directly.

    bin/wc-token.py create --user admin --name "nightly cron" --out FILE
    bin/wc-token.py list   --user admin
    bin/wc-token.py revoke --user admin --id wct_ab12cd34ef56

`create` writes the secret to a file with mode 0600 and prints only the id. It
does not print the token, and that is not politeness: standard output lands in
scrollback, in `script` logs, in CI output, and in whatever transcript a person
or an agent is keeping. A credential that has been printed has been disclosed to
everything downstream of the terminal, and the id is all anyone needs to revoke
it. Use `--stdout` to override when you are piping it somewhere deliberately.

Run against the same database the server uses -- `WC_DB_PATH`, or the default
`config.DB_PATH`. Running it against a throwaway path mints a token the server
has never heard of, which then fails authentication for no visible reason.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth
import config
import db


def _expiry(days: int | None) -> str | None:
    if not days:
        return None
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _create(args) -> int:
    user = await db.user_get_by_name(args.user)
    if not user:
        # Refused rather than created, because a token for a user that does not
        # exist authenticates as a name nothing else recognises: every
        # owner-scoped query would return nothing and the failure would look
        # like empty data rather than a bad credential.
        print(f"error: no such user: {args.user}", file=sys.stderr)
        return 2
    role = user.get("role") or "user"
    token_id, secret, token_hash = auth.new_api_token()
    await db.api_token_create(
        token_id, args.name, token_hash, args.user, role, _expiry(args.days)
    )

    if args.stdout:
        print(secret)
    else:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        # Written through a fresh fd with the mode set at creation time: writing
        # first and chmod-ing after leaves the secret world-readable for however
        # long the two syscalls are apart.
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret + "\n")
        print(f"token written to {out} (mode 0600)")

    print(f"id:      {token_id}")
    print(f"user:    {args.user} (role {role})")
    print(f"expires: {_expiry(args.days) or 'never'}")
    print("use:     Authorization: Bearer <token>   or   X-API-Token: <token>")
    return 0


async def _list(args) -> int:
    rows = await db.api_token_list(args.user, include_revoked=args.all)
    if not rows:
        print("(no tokens)")
        return 0
    for row in rows:
        state = "revoked" if row["revoked_at"] else "live"
        print(f"{row['id']}  {state:8} {row['name'][:30]:30} "
              f"created={row['created_at']} "
              f"expires={row['expires_at'] or 'never'} "
              f"last_used={row['last_used_at'] or 'never'}")
    return 0


async def _revoke(args) -> int:
    if await db.api_token_revoke(args.id, args.user):
        print(f"revoked {args.id}")
        return 0
    print(f"error: not found, not yours, or already revoked: {args.id}",
          file=sys.stderr)
    return 1


async def _main(args) -> int:
    await db.init()
    try:
        return await {"create": _create, "list": _list, "revoke": _revoke}[
            args.command](args)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="mint a token")
    create.add_argument("--user", required=True, help="the user it authenticates as")
    create.add_argument("--name", default="cli", help="a label, for the token list")
    create.add_argument("--days", type=int, default=None,
                        help="expire after N days (default: never)")
    create.add_argument("--out", default=str(
        Path.home() / ".local" / "share" / "webconsole" / "api-token"),
        help="file to write the secret to, mode 0600")
    create.add_argument("--stdout", action="store_true",
                        help="print the secret instead of writing a file")

    listing = sub.add_parser("list", help="list a user's tokens")
    listing.add_argument("--user", required=True)
    listing.add_argument("--all", action="store_true", help="include revoked")

    revoke = sub.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("--user", required=True)
    revoke.add_argument("--id", required=True)

    args = parser.parse_args()
    print(f"database: {config.DB_PATH}", file=sys.stderr)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
