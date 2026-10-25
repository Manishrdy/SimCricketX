# Network access policy

Feature 7 is disabled by default. `IDENTITY_NETWORK_ENFORCE=true` enables public
registration refusal and Community-only access for authenticated non-admin users
on positively classified VPN, proxy, Tor, hosting, anonymous or Private Relay
networks. A business network alone does not trigger a restriction.

## Deployment

Apply the standard migrations, including `add_identity_network`, before enabling.
Configure `PROXYCHECK_API_KEY` with a free-tier key and the existing stable
`IDENTITY_SIGNUP_KEY` (at least 32 bytes). Startup refuses enforcement without
these keys. Never commit keys or put them in browser code. This implementation
does not purchase or upgrade a subscription. Production activation requires
operator configuration and verification of the trusted client-IP ingress described
in the identity plan; an incorrect ingress configuration can classify the proxy
instead of the visitor.

The server calls ProxyCheck v3 over HTTPS with only the public IP, API key, version
and `tag=0`. Requests do not include account details. Redirects and retries are
disabled, responses are size bounded, and the lookup uses a two-second gevent
deadline plus connect/read timeouts. Production must retain the app's gevent
network monkey-patching for the total deadline to interrupt network I/O.

A shared database budget reserves at most 1,000 attempts per UTC day, including
failed requests. Share this key only with this deployment if that budget must
cover all key use. Clean results are cached for 15 minutes, flagged results for
five minutes (or an earlier provider delisting deadline), unknown results for one
minute. In-flight lookups use a three-second unknown placeholder to avoid duplicate
requests. Cache keys are purpose-separated HMACs of IPs; raw visitor IPs are not
stored in these network tables. Existing application logging remains separate.

Unknown results, exhausted quota, provider failures and classifier storage failures
allow access **under the network rule only**. Authentication, session validity,
verification, bans and password remediation remain enforced. Detection cannot
identify every VPN and may classify legitimate networks incorrectly.

## Operator jobs

Run `flask --app app refresh-private-relay-feed` daily using the deployment's
configured environment. It fetches Apple's fixed HTTPS egress CSV and replaces
the local list atomically. A failed refresh preserves the previous list; lists
older than 48 hours are ignored. No scheduler is installed automatically.

Run `flask --app app purge-network-evidence` daily to remove expired cache entries,
old budget counters, 180-day transition events, and closed/expired review evidence
past its retention window. Active exceptions are preserved. Open requests remain
available until resolved; closed requests are retained for 180 days, and approved
requests for 180 days after expiry or revocation.

## Access and review

Login still checks credentials before classification. Restricted users receive a
persistent banner, Community navigation and a private review link. An explicit
server endpoint allowlist covers Community, its media/APIs and account remediation.
Other HTML reads redirect to Community; other writes/API requests return 403.
Socket.IO rejects restricted connections and checks account/session/network state
again for each `next_ball` event. Cache expiry bounds detection changes for an
unchanged IP; a new IP uses a separate cache entry.

`/community/network-appeal` is private to the requesting account and admins. It
bypasses the ordinary 24-hour Community posting wait and permits a muted user to
submit this private request only. Ordinary Community permissions stay in effect.
Public applicants blocked before signup can use `/signup-review` for help.

Admins review requests and classifier health at `/admin/identity/network`.
Confirmed, reasoned decisions use version checks and atomic audit writes. Approval
grants a seven-day account-bound network exception; revocation applies on the next
HTTP request or game event. Exceptions never override account restrictions.
Admin users are network-exempt, except while impersonating a user. Private review
submission and administrative decisions are unavailable during impersonation.

The user-facing disclosure is `/privacy/network-checks`. Rollback consists of
turning `IDENTITY_NETWORK_ENFORCE` off and restarting workers; retain the tables
and stable identity key for audit/other identity features.
