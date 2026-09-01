# Claude Code WebConsole

A small, self-hosted web interface for a Claude Code CLI running on a trusted
machine. It provides mobile-friendly conversations, SSE response streaming,
SQLite persistence, and resumable Claude Code sessions.

> [!CAUTION]
> WebConsole launches Claude Code with `--dangerously-skip-permissions`. Anyone
> who can submit prompts effectively has the operating-system privileges of that
> process. Keep the service private, restrict the process account, and never
> expose the web application or proxy to the public internet.

## Stack

- Python 3.11+ and FastAPI (runtime dependencies are pinned and audited in CI)
- SQLite through `aiosqlite`
- Server-Sent Events for streaming
- Server-rendered HTML and vanilla JavaScript
- Claude Code CLI in direct or authenticated host-proxy mode

## Quick start: direct mode

Direct mode is the simplest option when the web application and Claude Code run
on the same machine.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

export WC_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export WC_ADMIN_PASSWORD='replace-with-a-strong-password'
export WC_PROJECTS_ROOT="$HOME/projects"
export WC_DB_PATH="$HOME/.local/share/webconsole/webconsole.db"
export WC_PROXY_ENABLED=0
export WC_LISTEN_HOST=127.0.0.1

python3 app.py
```

Open `http://127.0.0.1:8080`.

## Tailscale access

Bind the web application to the device's tailnet address, enforce restrictive
Tailscale ACLs, and use HTTPS when possible.

```bash
export WC_LISTEN_HOST=100.x.x.x
export WC_COOKIE_ALLOW_INSECURE=1  # only when using plain HTTP on the tailnet
python3 app.py
```

Then open `http://100.x.x.x:8080` from another authorized tailnet device. Do not
use `WC_COOKIE_ALLOW_INSECURE=1` on a public or untrusted network.

## Host-proxy mode

Proxy mode is useful when FastAPI runs in a container but Claude Code remains on
the host. The proxy is loopback-only by default and requires an independent
shared token.

```bash
export WC_PROXY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export WC_PROXY_LISTEN_HOST=127.0.0.1
python3 claude_proxy.py
```

Start the application with the same token and a proxy address reachable from the
application runtime:

```bash
export WC_PROXY_ENABLED=1
export WC_PROXY_HOST=127.0.0.1
export WC_PROXY_PORT=9000
export WC_PROXY_TOKEN='<same independent proxy token>'
python3 app.py
```

If the application runs in Docker, configure a protected host-to-container
network path explicitly. Do not expose port 9000 publicly.

## Docker image

Build from the repository root:

```bash
docker build -f docker/Dockerfile -t claude-code-webconsole .
```

The image starts only the FastAPI application. It expects either an external
host proxy (`WC_PROXY_ENABLED=1`) or a mounted Claude Code installation and
direct mode (`WC_PROXY_ENABLED=0`). Runtime data belongs in mounted `/data` and
`/projects` volumes.

## Configuration

Copy `.env.example` for the complete set of settings. Important variables are:

| Variable | Default | Description |
|---|---:|---|
| `WC_LISTEN_HOST` | `127.0.0.1` | Web bind address; use loopback or a private tailnet IP |
| `WC_PORT` | `8080` | Web port |
| `WC_DB_PATH` | `/data/webconsole.db` | SQLite database path |
| `WC_PROJECTS_ROOT` | `/projects` | Root for conversation workspaces |
| `WC_SESSION_SECRET` | required | Random session secret of at least 32 characters |
| `WC_ADMIN_USER` | `admin` | Bootstrap administrator name |
| `WC_ADMIN_PASSWORD` | unset | Bootstrap password, required for the first user |
| `WC_COOKIE_ALLOW_INSECURE` | `0` | Permit cookies over plain HTTP |
| `WC_PROXY_ENABLED` | `1` | Use the host-side Claude proxy |
| `WC_PROXY_HOST` | `127.0.0.1` | Proxy address used by FastAPI |
| `WC_PROXY_PORT` | `9000` | Proxy port |
| `WC_PROXY_TOKEN` | required in proxy mode | Shared proxy token, at least 32 characters |
| `WC_PROXY_LISTEN_HOST` | `127.0.0.1` | Host-side proxy bind address |
| `WC_CLAUDE_PATH` | `claude` | Claude Code executable used by the proxy |
| `WC_CLAUDE_MODEL` | unset | Optional Claude model override in proxy mode |
| `WC_MAX_CONCURRENT` | `3` | Maximum concurrent turns |
| `WC_PROMPT_MAX_CHARS` | `8000` | Maximum prompt length |

Do not put GitHub tokens or other deployment credentials in an environment file
that is copied into an image or committed. The repository ignores `.env` files.

## Architecture

```text
Browser
   │ HTTPS or private tailnet HTTP
   ▼
FastAPI WebConsole ───── SQLite
   │
   ├─ direct mode ────── Claude Code CLI
   │
   └─ authenticated TCP ─ Host proxy ─ Claude Code CLI
```

Each web conversation gets a unique workspace below `WC_PROJECTS_ROOT`. Chat
metadata and transcripts are stored in SQLite. Deleting a conversation removes
its database records but deliberately leaves its workspace on disk.

## Security model

- Server-side sessions use `HttpOnly`, `SameSite=Strict` cookies.
- Cookies are Secure by default.
- Passwords use Argon2id, with a scrypt fallback.
- Login attempts are rate-limited with bounded exponential backoff.
- Prompts are length-limited and passed as subprocess arguments without a shell.
- Workspace paths are resolved and constrained below `WC_PROJECTS_ROOT`.
- The host proxy binds to loopback by default and authenticates its handshake.
- Claude Code runs with `--dangerously-skip-permissions`; this is an intentional,
  high-impact trust decision, not a sandbox.
- **Every path requires authentication** except `POST/GET /login` and
  `/assets/*`. There is no exempt prefix: a `/dev/*` exemption existed briefly
  and is gone, because a route added under an exempt prefix is unauthenticated
  by default, and on a server that spawns Claude Code with
  `--dangerously-skip-permissions` that is remote code execution.

Read [SECURITY.md](SECURITY.md) before deployment.

## API tokens

Scripts, cron jobs and other machines authenticate with a token instead of a
cookie. This exists so that "a caller that cannot log in through a browser" has
a supported answer — the absence of one is what produced an unauthenticated
debug endpoint that minted admin sessions on request.

Mint one from the shell. The server does not need to be stopped, but doing it
while stopped avoids a second process writing to the live database at all:

```bash
bin/wc-token.py create --user pedro --name "nightly backup" --days 90
bin/wc-token.py list   --user pedro
bin/wc-token.py revoke --user pedro --id wct_xxxxxxxxxxxx
```

`create` writes the secret to `~/.local/share/webconsole/api-token` with mode
0600 and prints only the id. It does not print the token: stdout ends up in
scrollback, in `script` logs and in CI output, and a credential that has been
printed has been disclosed to all of them. Pass `--stdout` when you are
deliberately piping it somewhere.

Then use it as a bearer credential:

```bash
TOKEN="$(cat ~/.local/share/webconsole/api-token)"
curl -sk -H "Authorization: Bearer $TOKEN" https://<host>/api/chats
curl -sk -H "X-API-Token: $TOKEN"          https://<host>/api/system
# Mutating requests need no CSRF header -- there is no cookie to forge against:
curl -sk -X POST -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"title":"from a script"}' https://<host>/api/chats
```

`GET /api/tokens`, `POST /api/tokens` and `DELETE /api/tokens/{id}` manage them
over HTTP as well. Notes worth knowing before you rely on them:

- **The secret is shown once.** Only a sha256 hash is stored, so a database
  backup is not a set of working keys and a lost token is replaced, not
  recovered.
- **A token carries its owner's identity and role**, and grants nothing that
  owner does not already have. A `user`-role token is refused by admin routes.
- **A token cannot create another token** — `POST /api/tokens` requires a
  logged-in session — so one leaked credential cannot become a supply of them.
  It *can* revoke itself, because needing a browser to retire a credential you
  think is loose is the wrong way round.
- **Tokens skip CSRF, sessions do not.** A browser never attaches an
  `Authorization` header on its own, so there is no ambient credential to
  forge; the exemption keys on what the auth middleware accepted, not on the
  presence of a header, so an invented token cannot switch the check off.
- `--days` sets an expiry (max 365). The default is no expiry, deliberately: a
  cron job should not stop working at 3am because nobody renewed it.

## Tests and security checks

Runtime tests:

```bash
python3 -m py_compile app.py auth.py claude_proxy.py config.py db.py runner.py
python3 -m unittest discover -s . -p 'test*.py' -v
```

The suite is layered across the QA pyramid:

- **Unit tests** cover pure helpers, authentication boundaries, command construction, environment filtering, and frame normalization.
- **Integration tests** exercise SQLite CRUD, ownership isolation, message batching, CLI session-file synchronization, and app/database interactions.
- **Component/API tests** verify route contracts, validation, authentication boundaries, and handler behavior with runner/proxy dependencies mocked.
- **System/E2E tests** run a complete create → submit → persist → reload transcript flow with a fake Claude turn.
- **Acceptance/UAT tests** validate user requirements for CLI-session resume, conversation export, sidebar session visibility, and cross-user privacy.

The layered additions are in `tests/test_qa_layers.py`. They use temporary databases and mocked Claude boundaries, so the suite is deterministic and does not require a live model. The current suite contains 173 automated tests, including coverage for malformed session metadata, migration recovery, error contracts, duplicate workspace names, failed turns, model selection, skills inventory, and full export workflows.

Install development and security tooling with:

```bash
python3 -m pip install -r requirements-dev.txt
pip-audit -r requirements.txt --strict
# Optional: requires SAFETY_API_KEY in the environment
safety --stage cicd --key "$SAFETY_API_KEY" scan --target .
bandit -r app.py auth.py claude_proxy.py config.py db.py runner.py -ll
ruff check app.py auth.py claude_proxy.py config.py db.py runner.py tests test_functional.py
```

The GitHub Actions security workflow also runs Gitleaks and scans the Docker
image with Trivy. Install Gitleaks locally and enable the pre-push hook:

```bash
git config core.hooksPath .githooks
```

Tests do not require a live Claude Code connection. See [SECURITY.md](SECURITY.md)
for secret-management and vulnerability-handling requirements.

## License

Proprietary. See [LICENSE](LICENSE).
