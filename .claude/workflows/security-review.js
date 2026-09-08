export const meta = {
  name: "code-review-webconsole",
  description: "Comprehensive security & quality review of the webconsole project",
  phases: [{ title: "Review" }, { title: "Verify" }],
}

const DIMENSIONS = [
  {
    key: "auth-z",
    prompt: `You are doing a security review focused on authentication and authorization in this web application. The project is a FastAPI-based web console at /home/kali/projects/claude-code-webconsole.

Focus areas:
1. Authentication bypass: can any endpoint be accessed without valid session? Check middleware exemptions, login paths, static file serving, health endpoints.
2. Authorization gaps: do handlers check owner_id for every DB write? Can a logged-in user see/write another users data? Check the session-scoped user lookup in every handler that reads/writes user data.
3. Session management: is session cookie secure? HTTP-only? SameSite? Any fixation risk?
4. Access control on API routes: are admin-only endpoints properly guarded? Is the session check consistent (AuthMiddleware exempt paths vs. admin checks)?
5. Privilege escalation: can a regular user become admin? Check any role/permission system.

Read every handler in routes/ fully. Follow the session/owner pattern from middleware into each handler.

Return an object with key findings -- a list of {filePath, line, category, severity, explanation, fix} records. severity is critical/high/medium. Return findings:[] if no issues found.`,
  },
  {
    key: "injection",
    prompt: `You are doing a security review focused on injection vulnerabilities in this FastAPI web application at /home/kali/projects/claude-code-webconsole.

Focus areas:
1. SQL injection: check ALL raw SQL queries (execute/fetchall/fetchone with string formatting, f-strings, % formatting, .format()). FastAPI/SQLite params should use ? placeholders with parameter arrays. Check db_usage.py, db_chats.py, routes that build SQL dynamically.
2. Command injection: check any subprocess/call/Popen/shell=True usage. Check wc-claude.sh, runner.py, shell commands built from user input.
3. XSS: check any HTML rendering, template injection, innerHTML usage, unescaped user input rendered to HTML.
4. Path traversal: check any file read/write that uses user-supplied paths (transcript files, session files, project paths).
5. Template injection: check any Jinja2/python string interpolation of user data into code/queries.

Read every file that does I/O, SQL, subprocess calls, or HTML generation. Follow user input from route params to handler to query/command.

Return an object with key findings -- a list of {filePath, line, category, severity, explanation, fix} records. severity is critical/high/medium. Return findings:[] if no issues found.`,
  },
  {
    key: "secrets-crypto",
    prompt: `You are doing a security review focused on secrets management and cryptography in this FastAPI web application at /home/kali/projects/claude-code-webconsole.

Focus areas:
1. Hardcoded secrets, API keys, tokens, passwords in source code (grep for api_key, secret, token, password, key=, auth).
2. Environment variable usage: are secrets read from env or config? Is there a default fallback that could expose in production?
3. TLS/SSL configuration: certificate paths, self-signed certs, SSL verification in HTTP requests.
4. CSRF protection: does the app need CSRF tokens for state-changing POST/PUT/PATCH/DELETE requests?
5. Cookie security: httponly, samesite, secure flags, domain/scoping.
6. Logging of sensitive data: check log handlers -- do error messages, request logs, or debug output leak tokens, keys, auth headers, or PII?
7. Cryptographic operations: any hashing, encryption, signing? Check correctness and library versions.

Read the full source: middleware, config, routes, any crypto code, logging config.

Return an object with key findings -- a list of {filePath, line, category, severity, explanation, fix} records. severity is critical/high/medium. Return findings:[] if no issues found.`,
  },
  {
    key: "input-validation",
    prompt: `You are doing a security review focused on input validation and data integrity in this FastAPI web application at /home/kali/projects/claude-code-webconsole.

Focus areas:
1. Missing validation on route parameters, query params, request bodies. Check for type coercion gaps, range validation (especially for counts, limits, days, delays).
2. Integer overflow/underflow in math operations (especially in token counting, cost calculation, rate limiting).
3. Missing validation on JSON API responses that could crash the frontend (null/undefined handling).
4. Race conditions in concurrent turn handling (multiple simultaneous requests for same chat/machine).
5. Missing validation on uploaded data (transcripts, CSV imports).
6. Edge cases in numeric parsing (NaN, Infinity, very large numbers, negative values used where unsigned expected).
7. Missing input length limits on free-text fields that get stored.

Read all route handlers and DB functions. Check every user-controlled value from request to storage.

Return an object with key findings -- a list of {filePath, line, category, severity, explanation, fix} records. severity is critical/high/medium. Return findings:[] if no issues found.`,
  },
  {
    key: "architecture",
    prompt: `You are doing a security review focused on architecture and configuration in this FastAPI web application at /home/kali/projects/claude-code-webconsole.

Focus areas:
1. CORS configuration: are cross-origin requests properly restricted? Check for overly permissive CORS.
2. Rate limiting: does it work? Are there bypasses (different routes, missing rate-limited endpoints)?
3. Error handling: do unhandled exceptions leak stack traces or internal state to the client? Check exception handlers.
4. File serving: check StaticFiles configuration. Can users access files outside the intended directory? Check for path traversal in StaticFiles.
5. Database: check connection pooling, SQLite WAL mode, concurrent access, migration safety.
6. Subprocess/worker isolation: are Claude Code sessions sandboxed? Can they affect the host?
7. CSP (Content Security Policy): any CSP headers? Are inline scripts blocked or allowed?
8. Static file serving for JS/CSS: is there cache-busting, and does the CSP allow the sources?

Read middleware, config files, route setup, exception handlers, StaticFiles config, requirements.

Return an object with key findings -- a list of {filePath, line, category, severity, explanation, fix} records. severity is critical/high/medium. Return findings:[] if no issues found.`,
  },
  {
    key: "correctness",
    prompt: `You are doing a code review focused on correctness and logic bugs in this FastAPI web application at /home/kali/projects/claude-code-webconsole.

Focus areas:
1. Off-by-one errors, wrong loop bounds, incorrect date math (especially in usage reporting, retention, cutoff calculations).
2. Null/double-free patterns, use-after-free logic, race conditions in async code.
3. Event listener / subscription leaks (duplicate handlers, missing cleanup).
4. Incorrect data transformations (wrong aggregation, summing instead of averaging, etc.).
5. Inconsistent state between related data stores (e.g., DB and file system out of sync).
6. Error paths that silently drop data or leave the app in a bad state.
7. The ES-module identity split issue: different ?v= cache-buster versions across index.html and imports causing duplicate module instances.

Read the full source, focusing on async flows, data transforms, and error handling.

Return an object with key findings -- a list of {filePath, line, category, severity, explanation, fix} records. severity is critical/high/medium. Return findings:[] if no issues found.`,
  },
]

const results = await pipeline(
  DIMENSIONS,
  d =>
    agent(d.prompt, {
      label: "review:" + d.key,
      phase: "Review",
      schema: {
        type: "object",
        properties: {
          findings: {
            type: "array",
            items: {
              type: "object",
              properties: {
                filePath: { type: "string" },
                line: { type: "integer" },
                category: { type: "string" },
                severity: { type: "string", enum: ["critical", "high", "medium", "low"] },
                explanation: { type: "string" },
                fix: { type: "string" },
              },
              required: ["filePath", "category", "severity", "explanation"],
            },
          },
        },
        required: ["findings"],
      },
    }),
)

// Deduplicate by category+filepath
const seen = new Set()
const unique = results
  .flat()
  .filter(f => {
    const key = f.category + "-" + f.filePath
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })

// Rank by severity
const sevOrder = { critical: 0, high: 1, medium: 2, low: 3 }
unique.sort((a, b) => (sevOrder[a.severity] || 4) - (sevOrder[b.severity] || 4))

log("Found " + unique.length + " unique findings across " + results.flat().length + " total")

// Now adversarially verify each finding
const verified = await parallel(
  unique.map(f => () =>
    agent(
      "Try to refute this finding: In file " + f.filePath + (f.line ? " (if line " + f.line + ")" : "") +
      ":\n\ncategory: " + f.category +
      "\nseverity: " + f.severity +
      "\nexplanation: " + f.explanation +
      "\nfix: " + f.fix +
      "\n\nIs this a real vulnerability/bug? Could the fix be unnecessary? Are there mitigating factors? Default to refuted=true if uncertain.",
      {
        phase: "Verify",
        schema: {
          type: "object",
          properties: {
            refuted: { type: "boolean" },
            reason: { type: "string" },
          },
          required: ["refuted", "reason"],
        },
      }
    )
  ),
)

const confirmed = unique
  .map((f, i) => ({ finding: f, verdict: verified[i] }))
  .filter(x => x.verdict && !x.verdict.refuted)

if (confirmed.length === 0) {
  log("No findings survived adversarial verification")
  console.log(JSON.stringify({ findings: [] }))
} else {
  log(confirmed.length + " findings confirmed after verification")
  console.log(JSON.stringify({ findings: confirmed.map(c => c.finding) }))
}
