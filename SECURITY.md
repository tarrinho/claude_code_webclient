# Security

WebConsole is an authenticated administrative interface that launches Claude Code
with `--dangerously-skip-permissions`. A user who can submit prompts can cause
commands and file operations to run with the operating-system privileges of the
Claude Code process. Treat access to WebConsole and its proxy as equivalent to
shell access on the host.

## Deployment requirements

- Run only on a dedicated, restricted host.
- Keep the web application and proxy on loopback by default.
- If remote access is needed, bind the web application to a private Tailscale
  address and enforce restrictive tailnet ACLs. Never expose it to the public
  internet.
- Use TLS whenever possible. Set `WC_COOKIE_ALLOW_INSECURE=1` only for plain HTTP
  on a trusted private network.
- Set a unique, random `WC_SESSION_SECRET`, admin password, and `WC_PROXY_TOKEN`.
- Never reuse a GitHub personal access token as an application or proxy secret.
- Restrict the Claude process account, filesystem permissions, and network egress.
- Do not place secrets in project directories that Claude can read unless access
  is explicitly intended.

## Proxy security

The proxy binds to `127.0.0.1` by default and requires a shared bearer token in
its protocol handshake. Do not bind it to a non-loopback interface unless the
network path is independently protected and the risks are understood.

## Automated security controls

Every push and pull request is checked by GitHub Actions:

- `pip-audit` checks the pinned Python dependency graph against known advisories.
- `safety` provides an additional Python dependency advisory check.
- `Bandit` and `Ruff` scan Python source for common security and correctness issues.
- `Gitleaks` scans repository history and prevents credential patterns from being
  merged or pushed by CI.
- `Trivy` scans the Docker image for high and critical OS/library vulnerabilities.

The repository also configures a local `.githooks/pre-push` hook. Enable it with:

```bash
git config core.hooksPath .githooks
```

Install `gitleaks` locally before pushing. A missing scanner is a hard failure;
do not bypass the hook to publish a repository containing unknown secrets.

Dependency updates should be reviewed, tested, and accompanied by a fresh
`pip-audit` result. Do not suppress an advisory without documenting the package,
CVE/advisory, affected code path, and compensating control.

## Secrets management

- Keep `.env` files local; only `.env.example` belongs in Git.
- In CI, use GitHub Actions encrypted secrets or OIDC-based short-lived cloud
  credentials. Never print secret values or pass long-lived tokens on command
  lines where they can be captured in logs.
- Prefer a deployment secret manager (for example, a cloud/Vault/Kubernetes
  secret store) to copying `.env` files onto hosts.
- Rotate `WC_SESSION_SECRET`, `WC_PROXY_TOKEN`, admin passwords, and provider
  credentials after suspected exposure or staff/host changes.
- Use separate credentials for GitHub, WebConsole, Claude Code, and the proxy.
- Grant CI least privilege. The workflows request read-only repository access;
  SARIF upload is limited to security scanning jobs.
- Review GitHub secret-scanning alerts and revoke exposed credentials before
  removing the string from history. Rewriting Git history alone does not revoke
  a credential.

## Reporting a vulnerability

Do not open a public issue containing exploit details or credentials. Contact the
repository owner privately through GitHub instead.
