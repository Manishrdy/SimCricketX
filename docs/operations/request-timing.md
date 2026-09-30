# Diagnosing intermittent requests

Flask emits one JSON `http_request` record to stderr per request. No logging
schema or SQLite database is created. With systemd, verify the service uses
`StandardError=journal`, then inspect `journalctl -u simcricketx --since '10 minutes ago' -o cat`.
The dedicated logger bypasses existing application file and session handlers.
Existing application/exception logs retain their existing storage behavior.

## Nginx setup (server administrator)

1. Inspect `sudo nginx -T` locally to identify the active http/server/location
   blocks; do not publish its output because configuration can contain secrets.
2. Install `deploy/nginx/request-timing.conf` in a directory included by the
   `http` block (commonly `/etc/nginx/conf.d/`). Apply its commented access_log,
   error_log and proxy_set_header directives to the existing site configuration.
   Replace the site's previous access_log directive rather than duplicating it.
   Do not replace the site configuration or remove existing proxy headers.
3. Run `sudo nginx -t`; reload with `sudo systemctl reload nginx` only on success.
4. Fetch a page, copy its X-Request-ID response header and search for that ID in
   both Nginx access logs and the journal. Nginx must overwrite client-supplied
   X-Request-ID. Bind Gunicorn to loopback, as configured in this repository.

This configuration is intentionally NOT applied by the application deployment:
it must be integrated with the real site's existing Nginx configuration.
The application works without it, generating its own correlation IDs, but those
IDs will not match Nginx's IDs until the proxy configuration is applied.

## Reading timings

Nginx timings are seconds; Flask timings are milliseconds. Upstream fields are
strings because Nginx can emit `-` or multiple values for retries.
Flask duration includes before/after-request hooks and response construction;
it excludes queueing before Flask dispatch, streamed-body delivery and socket
transfer. SQL timings count cursor executions (including failed executions),
not pool acquisition, fetching/ORM hydration, commits or every database wait.
A high upstream duration with low Flask duration suggests waiting outside the
measured app work. High SQL duration suggests investigating query execution.
A 522 with no corresponding request in Nginx calls for network/firewall/server
capacity investigation; absence is not conclusive because logs can be delayed.
Use timestamps and Cloudflare Ray IDs to correlate these failures.

Only route patterns are logged by the timing logger; concrete URL values,
queries, SQL/parameters, user identities, cookies and bodies are omitted.
Unmatched paths use `<unmatched>`. Exceptions include their class, not message.
Nginx timing logs deliberately omit paths; join by request ID to get Flask's
route. Nginx's native error logs may contain URLs, so restrict their access.
An abrupt process kill cannot emit a Flask completion record.

## Retention and validation

Use the distribution's existing Nginx logrotate policy (check that it covers
these filenames); target 7–14 days with compression and daily/size rotation.
Configure journald size and retention limits appropriate to the server, e.g.
`SystemMaxUse=250M` and `MaxRetentionSec=14day` in a journald drop-in. Journald
settings affect other services too; review the server's total disk budget first.
Check persistent journal storage is enabled if records must survive a reboot.
Keep logs accessible only to administrators. Monitor dropped/rate-limited
journal messages and disk space. Logging is a small synchronous stderr write;
if volume becomes substantial, add a bounded collector/queue and monitor drops.

Compare external page probes with requests through local Nginx using the proper
Host/TLS name. Record p50/p95/p99 and error rate over time, correlate with CPU,
memory, connections and match activity. Do not infer the cause from one sample.
