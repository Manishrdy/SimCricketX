# Diagnostic recording removed

The temporary instrumentation and database insert code have been removed.
The table and existing records remain available for analysis. Setting
`MATCH_DIAGNOSTICS` no longer enables recording. The instructions below
are historical documentation of how the existing report was captured.

# Temporary scoreboard diagnostics

After deploying the code, run `python migrate.py` from the project environment
before enabling tracing. The new table is `match_diagnostic_events` in
`cricket_sim.db`. The local database has already had this table created.

Enable only the match being investigated, in the service environment or .env:

```
MATCH_DIAGNOSTICS=2a53298b-189b-4ee8-bcb2-0ca49ebccc31
```

Comma-separated match IDs are supported; `*` traces every match and should only
be used briefly. Restart `simcricketx` and reload the match page to activate both
server and browser tracing. Restarting can restore from an earlier checkpoint;
use a fresh reproduction when possible. Remove the setting and restart/reload
when finished. Rows are retained for investigation, not automatically purged.

```bash
sudo systemctl restart simcricketx
sqlite3 -header -csv cricket_sim.db "SELECT * FROM match_diagnostic_events ORDER BY id" > match-diagnostics.csv
```

Each row contains UTC timestamp, match ID, request trace ID, source, stage,
worker PID, server monotonic time, payload JSON, original payload byte count,
truncation flag, and resource metrics JSON. Browser payloads add client time,
monotonic milliseconds, page visibility and online status. Engine results and
browser-received responses include scorecard data and transition flags.
Payloads above 128 KiB retain a preview and their original size.

Metrics include process/system CPU percentage, cumulative process CPU times,
RSS/VMS, total/available/used system RAM, swap use and swap I/O counters,
load averages and thread count. CPU percentages are nonblocking samples since
the previous sample, not per-request CPU attribution; the first can be zero.
Unavailable OS metrics are null. Browser-stage metrics describe the server at
receipt; they do not measure the user's computer. No background sampler runs
while the process is blocked, so compare the final recorded sample with OCI or
OS monitoring for a stall that freezes the worker.

Trace order: browser request_sent → ws_received/http_received → engine_start →
engine_result → snapshot_start/file_loaded/finished (when applicable) →
ws_emit_start/returned or http_response → browser response_received →
scorecard_shown/close. An emit returning proves only that the server emit call
returned, not delivery. A browser response_timeout fires at 15 seconds without
retrying the ball. Socket connection/disconnection and processing exceptions
are recorded too. Browser console uses the `[MatchTrace]` prefix.

Tracing is best effort: each write uses a separate SQLite connection with a
50 ms lock timeout and never commits the gameplay ORM session. Failed writes
are logged under `[MatchTrace]`; browser ingestion failures remain visible in
the console. High-frequency tracing adds DB/network load, so enable one match
for a short reproduction. Payloads contain match/player data; no cookies,
auth headers, passwords or tokens are collected by the instrumentation.
