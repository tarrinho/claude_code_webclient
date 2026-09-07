# QA Test Coverage

## Previously uncovered handlers — now covered

The following 22 route handlers were uncovered before the 5 new QA test files were created.
Each is now tested by at least one test in the corresponding test file.

### 1. SSH Transports (`tests/test_qa_transports.py` — 22 tests)

| Handler | Route | Tests |
|---------|-------|-------|
| `handle_transports_list` | GET /api/transports | list_empty, list_owner_scoped, login_required |
| `handle_transport_get` | GET /api/transports/{id} | get_by_id, get_404_unknown, get_owner_scoped, login_required |
| `handle_transport_create` | POST /api/transports | create_success, create_db_store, create_validation_* (5 tests), create_truncation, create_defaults, login_required |
| `handle_transport_patch` | PATCH /api/transports/{id} | patch_update_name, patch_accepts_null, patch_reject_null_values, patch_reject_empty_host, patch_reject_unknown_field, login_required |
| `handle_transport_delete` | DELETE /api/transports/{id} | delete_success, delete_owner_scoped, delete_404_unknown, login_required |
| `handle_transport_test_raw` | POST /api/transports/test | test_raw_success, test_raw_invalid_host, test_raw_login_required |
| `handle_transport_test_saved` | POST /api/transports/{id}/test | test_saved_success, test_saved_404, test_saved_login_required |

### 2. API Tokens (`tests/test_qa_tokens.py` — 19 tests)

| Handler | Route | Tests |
|---------|-------|-------|
| `handle_tokens_get` | GET /api/tokens | list_empty, list_owner_scoped, list_excludes_secret, login_required |
| `handle_tokens_create` | POST /api/tokens | create_success, create_db_store, create_truncate_name, create_never_expiry(2), create_custom_ttl, create_reject_invalid_ttl(3), create_login_required, create_reject_token_auth |
| `handle_tokens_revoke` | POST /api/tokens/{token_id}/revoke | revoke_success, revoke_db_remove, revoke_owner_scoped, revoke_404, revoke_already_revoked, login_required |

### 3. Session Delete (`tests/test_qa_session_delete.py` — 7 tests)

| Handler | Route | Tests |
|---------|-------|-------|
| `handle_session_delete` | DELETE /api/sessions/{session_id} | delete_success, delete_404_unknown, delete_non_webconsole_409, path_traversal_sanitize, login_required |
| `db.delete_claude_session_file` | (DB function) | returns_true_when_exists, returns_false_nonexistent |

### 4. Queue Operations (`tests/test_qa_queue_routes.py` — 14 tests)

| Handler | Route | Tests |
|---------|-------|-------|
| `handle_queue_list` | GET /api/chats/{id}/queue | list_empty, list_404_unknown_chat, list_owner_scoped, login_required |
| `handle_queue_delete` | DELETE /api/chats/{id}/queue/{queue_id} | delete_success, delete_removes_prompt, delete_404_unknown_id, delete_404_unknown_chat, delete_owner_scoped, login_required |
| `handle_queue_release` | POST /api/chats/{id}/queue/{queue_id}/release | release_success, release_404_unknown_id, release_404_unknown_chat, release_owner_scoped, login_required |

### 5. Orchestrator Sub-Routes (`tests/test_qa_orchestrator_routes.py` — 20 tests)

| Handler | Route | Tests |
|---------|-------|-------|
| `handle_orchestrator_stream` | GET /api/orchestrators/{id}/stream | stream_404_unknown, stream_owner_scoped, stream_login_required |
| `handle_orchestrator_task_stream` | GET /api/orchestrators/{id}/tasks/{task_id}/stream | stream_404_orchestrator, stream_404_task, stream_owner_scoped, stream_login_required |
| `handle_orchestrator_tasks_get` | GET /api/orchestrators/{id}/tasks | tasks_empty, tasks_unknown_200, tasks_owner_scoped, tasks_login_required |
| `handle_orchestrator_messages_get` | GET /api/orchestrators/{id}/messages | messages_empty, messages_unknown, messages_filter_by_after, messages_login_required |
| `handle_changelog_get` | GET /api/changelog | changelog_200, changelog_sections, changelog_login_required |

**Total: 22 handlers, 82 new tests**

## Test patterns

All tests follow the same pattern:
1. Create temp DB via `patch.object(config, "DB_PATH", ...)`
2. Create alice and bob users via `db.user_create()`
3. Login via `POST /login` to get CSRF cookie
4. Exercise the route handler under test
5. Verify owner scoping by switching between alice and bob clients

SSE streaming routes (200 path) cannot be tested with TestClient due to infinite `while True` loops. These routes test auth/404/ownership pre-validation paths; streaming loops are covered by browser/integration tests.
