# db_specs.py — manual status override for the Settings > Specs gallery.
#
# specs_gallery.py's own status (implemented/planned/spec_only) is purely
# git/filesystem-derived and never reads this table; routes/specs.py overlays
# a row here on top of that computed value for specs an admin has manually
# set, keyed by the spec's repo-relative path (specs_gallery.discover_specs's
# "path" field).

from typing import Final

import db

ALLOWED_STATUSES: Final[frozenset[str]] = frozenset({
    "spec_only", "planning", "implementing", "done",
})


async def spec_status_get_all(paths: set[str]) -> dict[str, str]:
    """Fetch the manual status for every path in *paths* that has one.
    Paths with no manual override are simply absent from the result --
    same "unrequested/unset reads as absent" convention as setting_get_all."""
    if not paths:
        return {}
    placeholders = ",".join("?" for _ in paths)
    cur = await db.db_conn.execute(
        f"SELECT path, status FROM spec_status WHERE path IN ({placeholders})",
        tuple(paths),
    )
    return {row["path"]: row["status"] for row in await cur.fetchall()}


async def spec_status_set(path: str, status: str) -> bool:
    """Set *path*'s manual status. Returns False without writing anything
    if *status* is not one of ALLOWED_STATUSES -- the caller decides how to
    surface that (routes/specs.py returns 400)."""
    if status not in ALLOWED_STATUSES:
        return False
    await db.db_conn.execute(
        "INSERT INTO spec_status (path, status, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
        (path, status, db._now()),
    )
    await db.db_conn.commit()
    return True
