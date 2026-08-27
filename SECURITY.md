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

## Reporting a vulnerability

Do not open a public issue containing exploit details or credentials. Contact the
repository owner privately through GitHub instead.
