# Task 1: Persist pinning and implement real conversation deletion

Read the approved spec: `/home/kali/projects/claude-code-webconsole/docs/superpowers/specs/2026-08-27-mobile-conversation-management-design.md`.

## Scope

Modify `db.py` and create `tests/test_db.py`.

## Requirements

- Add `pinned INTEGER NOT NULL DEFAULT 0`, `pinned_at TEXT`, and `deleted_at TEXT` to fresh schemas and through an idempotent startup migration.
- `chat_list(owner_id)` returns existing chat fields plus the new fields, ordered as pinned active chats first (newest `pinned_at`), then other active chats by newest activity, then archived chats by newest activity.
- `chat_get(chat_id, owner_id, include_archived=False)` is owner-scoped and excludes archived by default; include them only when requested.
- `chat_update` permits only title, description, archived, pinned, and pinned_at column names. Unexpected fields must not reach SQL.
- Pinning sets `pinned_at` and unpinning clears it. Decide whether this normalization belongs in `chat_update` or app API, but make DB behavior internally consistent and tested.
- `chat_delete` permanently deletes owned chat and messages in a transaction, returns true only if found, and never accesses or removes workspace files.
- Preserve all existing public behavior not contradicted here.
- Use `unittest.IsolatedAsyncioTestCase`, temp database/project roots, and verify migration, defaults, ordering, owner scope, deletion, message deletion, and workspace preservation.
- Run `PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_db` and the existing `test_functional.py`.
- Do not modify Docker or frontend files. Do not commit; this directory is not a git repository.

## Report

Write a detailed report to `/home/kali/projects/claude-code-webconsole/.superpowers/sdd/mobile-conversation-management/task-1-report.md` with files changed, decisions, exact commands/results, and self-review. Return only status, one-line test summary, and concerns.