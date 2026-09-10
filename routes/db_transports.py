# routes/db_transports.py — SSH transport CRUD (the connection, not the backend).
#
# A transport is the SSH connection to a remote host: ssh_host/ssh_user/
# ssh_key_path/ssh_host_key_fingerprint. ai_machines.transport_id points at
# one of these when a backend's `claude` process runs there instead of
# locally. See docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.

from typing import Any

import db

_TRANSPORT_COLUMNS = (
    "id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
    "ssh_host_key_fingerprint, remote_path, last_synced_sha, "
    "last_qa_synced_sha, created_at, updated_at"
)

_DEFAULT_REMOTE_PATH = "~/wc-proxy"


async def ssh_transport_create(
    transport_id: str,
    name: str,
    owner_id: str,
    ssh_host: str,
    ssh_user: str,
    ssh_key_path: str,
    remote_path: str = _DEFAULT_REMOTE_PATH,
) -> str:
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO ssh_transports "
        "(id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
        " ssh_host_key_fingerprint, remote_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
        (transport_id, name, owner_id, ssh_host, ssh_user, ssh_key_path,
         remote_path or _DEFAULT_REMOTE_PATH, now, now),
    )
    await db.db_conn.commit()
    return now


async def ssh_transport_get(transport_id: str, owner_id: str) -> dict[str, Any] | None:
    cur = await db.db_conn.execute(
        f"SELECT {_TRANSPORT_COLUMNS} FROM ssh_transports "  # nosec B608: columns are static
        "WHERE id = ? AND owner_id = ?",
        (transport_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ssh_transports_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        f"SELECT {_TRANSPORT_COLUMNS} FROM ssh_transports "  # nosec B608: columns are static
        "WHERE owner_id = ? ORDER BY name ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def ssh_transports_list_all() -> list[dict[str, Any]]:
    """Every transport, regardless of owner.

    For sync_request_watcher.py's background sweep -- unscoped for the same
    reason chats_with_auto_answer() is: a watcher runs on behalf of no
    particular request, so owner-scoping it would silently stop watching
    every owner but the first one checked.
    """
    cur = await db.db_conn.execute(
        f"SELECT {_TRANSPORT_COLUMNS} FROM ssh_transports "  # nosec B608: columns are static
        "ORDER BY owner_id, name ASC",
    )
    return [dict(r) for r in await cur.fetchall()]


async def ssh_transport_update(transport_id: str, owner_id: str, **fields: Any) -> bool:
    allowed = {"name", "ssh_host", "ssh_user", "ssh_key_path", "remote_path"}
    pairs = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
    if not pairs:
        return False
    sets = [f"{k} = ?" for k, _ in pairs]
    sets.append("updated_at = ?")
    vals = [v for _, v in pairs] + [db._now(), transport_id, owner_id]
    sql = (
        "UPDATE ssh_transports SET " + ", ".join(sets)
        + " WHERE id = ? AND owner_id = ?"
    )  # nosec B608: fields are allowlisted
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ssh_transport_delete(transport_id: str, owner_id: str) -> bool:
    cur = await db.db_conn.execute(
        "DELETE FROM ssh_transports WHERE id = ? AND owner_id = ?",
        (transport_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ssh_transport_set_host_key_fingerprint(transport_id: str, fingerprint: str) -> None:
    """Pin the SSH host key fingerprint accepted on first connect.

    No owner_id scoping: called from tunnel_manager_ssh.connect(), a
    background loop with no request session to scope against -- same
    reasoning as ai_machine_set_ssh_host_key_fingerprint.
    """
    await db.db_conn.execute(
        "UPDATE ssh_transports SET ssh_host_key_fingerprint = ? WHERE id = ?",
        (fingerprint, transport_id),
    )
    await db.db_conn.commit()


async def ssh_transport_set_last_synced_sha(transport_id: str, sha: str) -> None:
    """Advance the sync pointer. Only called after every file in a sync
    batch succeeds -- see transport_sync.py and the 2026-09-09 design doc's
    'no silent pointer advancement' rule. No owner_id scoping, matching
    ssh_transport_set_host_key_fingerprint: the sync engine runs from a
    request handler that has already checked ownership before calling this.
    """
    await db.db_conn.execute(
        "UPDATE ssh_transports SET last_synced_sha = ? WHERE id = ?",
        (sha, transport_id),
    )
    await db.db_conn.commit()


async def ssh_transport_set_last_qa_synced_sha(transport_id: str, sha: str) -> None:
    """Advance the QA sync pointer. Independent of last_synced_sha -- see
    ssh_transport_set_last_synced_sha's own docstring for the "no silent
    pointer advancement" rule, which applies here identically. No owner_id
    scoping, matching that setter: the caller (qa_remote.py) runs from a
    request handler that has already checked ownership.
    """
    await db.db_conn.execute(
        "UPDATE ssh_transports SET last_qa_synced_sha = ? WHERE id = ?",
        (sha, transport_id),
    )
    await db.db_conn.commit()
