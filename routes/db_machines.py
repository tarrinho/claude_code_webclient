# db_machines.py — AI machines CRUD and routing helpers.
#
# Extracted from db.py so the machine route path does not need the full
# database module.

import logging
import sqlite3
from typing import Any

import db

_log = logging.getLogger("wc.db.machines")

_BACKEND_COLUMNS = (
    "id, name, provider, host, port, model, base_url, api_key, transport_id"
)


async def ai_machine_active(owner_id: str) -> dict[str, Any] | None:
    """Return the owner's active machine without exposing its API key."""
    if db.db_conn is None:
        raise sqlite3.Error("database not connected")
    cur = await db.db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "active, enabled "
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machines_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        "CASE WHEN enabled = 1 THEN 1 ELSE 0 END AS enabled, "
        "created_at, updated_at, transport_id "
        "FROM ai_machines WHERE owner_id = ? ORDER BY active DESC, name ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def ai_machine_get(id: str, owner_id: str) -> dict[str, Any] | None:
    # ssh_host/ssh_user/ssh_key_path added: tunnel_manager_ssh.connect() reads
    # them from this function's return value to actually reach an ssh_proxy
    # machine. Without them here every connect attempt got "" for all three
    # regardless of what was saved in Settings.
    cur = await db.db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "ssh_host, ssh_user, ssh_key_path, ssh_host_key_fingerprint, transport_id, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        "CASE WHEN enabled = 1 THEN 1 ELSE 0 END AS enabled, "
        "CASE WHEN api_key IS NOT NULL AND TRIM(api_key) <> '' THEN 1 ELSE 0 END "
        "AS has_api_key, "
        "created_at, updated_at, "
        "models_list, models_updated_at "
        "FROM ai_machines WHERE id = ? AND owner_id = ?",
        (id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machine_set_ssh_host_key_fingerprint(
    machine_id: str, fingerprint: str
) -> None:
    """Pin the SSH host key fingerprint accepted on first connect.

    No owner_id scoping: called from tunnel_manager_ssh.connect(), the
    background loop, not a per-request handler -- there is no session to
    scope it to, and the fingerprint is not secret (it identifies the
    *server*, not a credential).
    """
    await db.db_conn.execute(
        "UPDATE ai_machines SET ssh_host_key_fingerprint = ? WHERE id = ?",
        (fingerprint, machine_id),
    )
    await db.db_conn.commit()


async def ai_machine_create(
    machine_id: str,
    name: str,
    host: str,
    port: int,
    api_key: str | None,
    model: str,
    base_url: str | None,
    description: str | None,
    owner_id: str,
    provider: str = "claude_code",
    transport_id: str | None = None,
) -> str:
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO ai_machines "
        "(id, name, provider, host, port, api_key, model, base_url, description, "
        "active, owner_id, created_at, updated_at, transport_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            machine_id, name, provider, host, port, api_key, model, base_url,
            description, owner_id, now, now, transport_id,
        ),
    )
    await db.db_conn.commit()
    return now


async def ai_machine_update(
    machine_id: str,
    owner_id: str,
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    description: str | None = None,
    provider: str | None = None,
    transport_id: str | None = None,
) -> bool:
    pairs: list[tuple[str, Any]] = [
        ("name", name),
        ("provider", provider),
        ("host", host),
        ("port", port),
        ("api_key", api_key),
        ("model", model),
        ("base_url", base_url),
        ("description", description),
        ("transport_id", transport_id),
    ]
    sets: list[str] = []
    vals: list[Any] = []
    for field, value in pairs:
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(db._now())
    vals.extend([machine_id, owner_id])
    sql = (
        "UPDATE ai_machines SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?"
    )  # nosec B608: fields are allowlisted
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_clear_transport(machine_id: str, owner_id: str) -> bool:
    """Set transport_id back to NULL -- a bare None through ai_machine_update
    is indistinguishable from "field not supplied" (its pairs-building only
    sets a field when the value is not None), so clearing needs its own path."""
    cur = await db.db_conn.execute(
        "UPDATE ai_machines SET transport_id = NULL, updated_at = ? "
        "WHERE id = ? AND owner_id = ?",
        (db._now(), machine_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_activate(machine_id: str, owner_id: str) -> bool:
    """Deactivate all machines and activate the one requested.

    No explicit BEGIN: db_conn is shared across every writer in the
    process, and a literal "BEGIN" raises "cannot start a transaction
    within a transaction" if another coroutine's write already opened
    one implicitly and has not committed yet (registry #48). The first
    UPDATE below opens its own implicit transaction, which already
    covers atomicity up to the commit/rollback below.
    """
    # Refuse before touching anything. A disabled backend must not become the
    # default: that would route every unpinned turn to a backend the operator
    # shelved, while the flag still said disabled.
    #
    # Checked before the deactivate-everything UPDATE below, not after: a
    # refusal that had already run that statement would leave the owner with
    # no default at all, which is worse than the state it declined to leave.
    cur = await db.db_conn.execute(
        "SELECT enabled FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    if not row or not row["enabled"]:
        return False
    try:
        await db.db_conn.execute(
            "UPDATE ai_machines SET active = 0 WHERE owner_id = ?",
            (owner_id,),
        )
        cur = await db.db_conn.execute(
            "UPDATE ai_machines SET active = 1, updated_at = ? WHERE id = ? AND owner_id = ?",
            (db._now(), machine_id, owner_id),
        )
        await db.db_conn.commit()
        return cur.rowcount > 0
    except Exception:
        await db.db_conn.rollback()
        raise


async def ai_machine_delete(machine_id: str, owner_id: str) -> bool:
    """Delete a backend, and unpin any conversation that named it.

    The unpin is the point: `chats.ai_machine_id` has no foreign key (nothing
    in this schema does), so deleting a machine used to leave every chat pinned
    to it pointing at an id that resolves to nothing. The frontend then asked
    `/api/models?machine_id=<gone>` on every open of such a conversation and got
    a 404 -- five chats on this deployment were in that state, all naming the
    same deleted machine.

    NULL means "follow whichever backend is active", which is the picker's own
    default, so an unpinned conversation keeps working. `db._clear_dangling_machine_pins`
    repairs rows written before this; this stops new ones being created.
    """
    cur = await db.db_conn.execute(
        "DELETE FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    deleted = cur.rowcount > 0
    if deleted:
        await db.db_conn.execute(
            "UPDATE chats SET ai_machine_id = NULL "
            "WHERE ai_machine_id = ? AND owner_id = ?",
            (machine_id, owner_id),
        )
    await db.db_conn.commit()
    return deleted


async def ai_machine_set_models(
    machine_id: str, owner_id: str, active: list[str], default: str | None
) -> bool:
    """Set which models a machine offers, and which one it defaults to."""
    sets = ["active_models = ?", "updated_at = ?"]
    vals: list[Any] = [__import__("json").dumps(active), db._now()]
    if default:
        sets.insert(1, "model = ?")
        vals.insert(1, default)
    cur = await db.db_conn.execute(
        "UPDATE ai_machines SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?",
        [*vals, machine_id, owner_id],
    )  # nosec B608: column names are literals, values parameterised
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_set_models_list(
    machine_id: str, owner_id: str, models_list: str, models_updated_at: str
) -> bool:
    """Persist the full model list (JSON) and its timestamp for a machine."""
    cur = await db.db_conn.execute(
        "UPDATE ai_machines SET models_list = ?, models_updated_at = ? "
        "WHERE id = ? AND owner_id = ?",
        (models_list, models_updated_at, machine_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_api_key(machine_id: str, owner_id: str) -> str | None:
    """Return one machine's API key. Kept separate from ai_machine_get so the
    key is only ever fetched where it is deliberately needed."""
    cur = await db.db_conn.execute(
        "SELECT api_key FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    return row["api_key"] if row else None


async def ai_machine_seed_anthropic(owner_id: str) -> str | None:
    """Ensure the owner has a machine pointing at the official Anthropic API.

    Keyed on the *host*, not on a provider literal, and that is the whole
    point of this function's shape.

    It used to look for ``provider = 'anthropic'`` and create rows with that
    same value. Then the provider rename landed, and ``db.py``'s migration
    runs unconditionally on every ``db.init()``:

        UPDATE ai_machines SET provider = 'claude_code' WHERE provider = 'anthropic'

    So every startup erased the exact marker this guard searched for, and the
    next caller -- ``routes/machines.py`` seeds on every Backends panel load
    -- found nothing and created another one. One new "Anthropic API" per
    restart-then-open cycle. Seven had accumulated on this deployment before
    anyone noticed, because they are all inactive with no declared models and
    nothing routes through them.

    ``provider='anthropic'`` was also no longer a value the API would accept:
    ``_MACHINE_PROVIDERS`` is ``{"claude_code", "direct"}``, so the rename
    missed this function on both sides -- the literal it searched for and the
    literal it wrote.

    The host is the durable identity here: it is what makes a machine *the
    official Anthropic API* rather than a gateway, it survives a rename of the
    provider vocabulary, and it survives the user renaming the machine itself.
    A backend the owner has already pointed at api.anthropic.com satisfies
    this function's promise whatever it is called, so that row is returned
    rather than shadowed by a fresh duplicate.
    """
    cur = await db.db_conn.execute(
        # Oldest first, so repeated calls are stable rather than depending on
        # row order if duplicates already exist from before this fix.
        "SELECT id FROM ai_machines WHERE owner_id = ? AND host = ? "
        "ORDER BY created_at LIMIT 1",
        (owner_id, db._ANTHROPIC_HOST),
    )
    row = await cur.fetchone()
    if row:
        return row["id"]
    machine_id = __import__("uuid").uuid4().hex
    await ai_machine_create(
        machine_id,
        "Anthropic API",
        db._ANTHROPIC_HOST,
        443,
        None,
        db.config.ANTHROPIC_MODEL,
        db.config.ANTHROPIC_BASE_URL,
        "Official Anthropic API — Claude Code's default backend.",
        owner_id,
        provider="claude_code",
    )
    return machine_id


async def chat_owner(chat_id: str) -> str | None:
    """Return the owner of *chat_id*, so the runner can resolve its backend."""
    cur = await db.db_conn.execute("SELECT owner_id FROM chats WHERE id = ?", (chat_id,))
    row = await cur.fetchone()
    return row["owner_id"] if row else None


async def ai_machine_backend(owner_id: str) -> dict[str, Any] | None:
    """Return the active machine *including* its API key, for the runner only."""
    cur = await db.db_conn.execute(
        f"SELECT {_BACKEND_COLUMNS} "  # nosec B608: columns are static
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machine_backend_by_id(
    machine_id: str, owner_id: str
) -> dict[str, Any] | None:
    """Return one specific machine including its API key, for the runner only."""
    if not machine_id:
        return None
    cur = await db.db_conn.execute(
        f"SELECT {_BACKEND_COLUMNS} "  # nosec B608: columns are static
        "FROM ai_machines WHERE id = ? AND owner_id = ? LIMIT 1",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def chat_routing(chat_id: str) -> dict[str, Any]:
    """Resolve where a conversation's next turn should go."""
    cur = await db.db_conn.execute(
        "SELECT owner_id, model, ai_machine_id FROM chats WHERE id = ?", (chat_id,)
    )
    row = await cur.fetchone()
    if not row:
        return {"owner": None, "model": None, "machine": None, "pinned": False}
    owner = row["owner_id"]
    if row["ai_machine_id"]:
        machine = await ai_machine_backend_by_id(row["ai_machine_id"], owner)
        if machine:
            return {"owner": owner, "model": row["model"],
                    "machine": machine, "pinned": True}
    return {
        "owner": owner,
        "model": row["model"],
        "machine": await ai_machine_backend(owner),
        "pinned": False,
    }


async def chat_set_machine(chat_id: str, owner_id: str, machine_id: str | None) -> bool:
    """Pin a conversation to a machine, or clear the pin with None."""
    cur = await db.db_conn.execute(
        "UPDATE chats SET ai_machine_id = ?, updated_at = ? "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (machine_id or None, db._now(), chat_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


def parse_active_models(raw: Any) -> list[str]:
    """Decode the active_models column into a list of ids."""
    if not raw:
        return []
    if isinstance(raw, list):
        entries = raw
    else:
        try:
            entries = __import__("json").loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(entries, list):
        return []
    seen: set[str] = set()
    models: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip() and entry.strip() not in seen:
            seen.add(entry.strip())
            models.append(entry.strip()[:200])
    return models


async def ai_machine_set_enabled(
    machine_id: str, owner_id: str, enabled: bool
) -> bool:
    """Shelve a backend, or bring it back. True when a row changed.

    Its own function rather than a field on ai_machine_update: that builder
    only writes a field when the value is not None, so a bare False reads
    ambiguously beside eight text fields -- and the callers that must refuse
    (routes/machines.handle_machine_patch) need one obvious entry point to
    guard rather than a value buried in a dict.
    """
    cur = await db.db_conn.execute(
        "UPDATE ai_machines SET enabled = ?, updated_at = ? "
        "WHERE id = ? AND owner_id = ?",
        (1 if enabled else 0, db._now(), machine_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0
