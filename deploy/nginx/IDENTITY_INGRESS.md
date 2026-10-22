# Feature 1: trusted client IP deployment

Prepared for the supplied OCI nginx → loopback Gunicorn configuration. Website
DNS screenshot confirms Cloudflare proxying for simcricketx.app and the displayed
www record. The legacy simcricketx.manishbotta.me proxy status is unverified.
Direct origin access remains supported; this change is not a firewall rollout.

## Contract

nginx trusts CF-Connecting-IP only when the transport peer belongs to the official
Cloudflare ranges. It replaces X-Real-IP and X-Forwarded-For with its normalized
$remote_addr. Direct visitors retain their actual transport IP, regardless of
forged Cloudflare/forwarded headers. Flask accepts X-Real-IP only from explicitly
configured nginx peers. It does not interpret XFF chains. Missing/malformed
normalized headers fall back to the peer, not to another untrusted header.

The WSGI boundary normalizes REMOTE_ADDR for auth, auditing and IP rate limits.
It strips competing forwarding headers. Only a single http/https forwarded
protocol from a trusted peer is accepted; forwarded host/port/prefix are ignored.
nginx already sets Host. Local processes capable of connecting to Gunicorn are
inside this trust boundary; loopback is not process authentication.

No VPN detection, signup blocks, identity schema changes or deletion clock are
included. Keep workers=1, GeventWebSocketWorker and timeout=600 unchanged.

## Install order (operator actions, not executed by development)

1. Save protected backups of the deployed application revision, configuration and
   nginx site. Capture `systemctl cat --no-pager simcricketx.service`, `nginx -T`
   and the deployed gunicorn.conf.py. Do not put secrets in version control.
2. Check nginx has the real-IP module (`nginx -V`). Review current official
   https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6 against
   cloudflare-realip.conf. Verify Cloudflare does not strip visitor-IP headers or
   overwrite IPv6 addresses with pseudo IPv4 values for this zone. Use Full
   (strict) TLS to the existing HTTPS origin; verify the actual zone setting.
3. Copy cloudflare-realip.conf and app-proxy.conf into a dedicated directory,
   e.g. /etc/nginx/snippets/simcricketx/. Do not install these as top-level
   conf.d files: their directives belong inside server/location contexts.
4. In the existing HTTPS server block add:

   ```nginx
   include /etc/nginx/snippets/simcricketx/cloudflare-realip.conf;
   ```

   Replace the existing `location /` proxy directives with:

   ```nginx
   location / {
       include /etc/nginx/snippets/simcricketx/app-proxy.conf;
   }
   ```

   Keep the existing $connection_upgrade map, Certbot TLS directives, HTTP
   redirects and server names. Apply the same header contract to any additional
   proxy locations. Preserve additional timing headers if configured there.
5. Run `sudo nginx -t`; only on success, reload nginx. Deploy nginx normalization
   BEFORE the new application resolver: the old app also benefits from the
   overwritten XFF. Check HTTPS and Socket.IO after reload.
6. Merge these settings into the existing application security configuration
   (do not replace the file or its other values):

   ```yaml
   security:
     trust_proxy_headers: true
     trusted_proxy_cidrs:
       - 127.0.0.1/32
       - ::1/128
   ```

   Use only actual nginx peers; do not add Cloudflare ranges here because nginx
   is the application's peer. With no CIDRs or trust disabled, headers are
   ignored. Invalid CIDRs or catch-all /0 networks fail startup explicitly.
7. Deploy the application revision and restart the systemd service during an
   agreed window: restarting the single worker can affect in-memory matches.
   Check service status/startup logs, login, rate limiting and WebSocket upgrade.
   No production restart is part of local implementation/testing.

## Verification before marking production complete

- Normal browser request through Cloudflare: authenticated session/login audit
  records the visitor IP, not a Cloudflare edge or 127.0.0.1.
- Direct-origin HTTPS request with correct hostname/SNI and forged X-Real-IP,
  X-Forwarded-For and CF-Connecting-IP: record the actual connecting peer.
  If the origin firewall rejects direct traffic already, record that result.
- Changing the forged XFF first entry does not change recorded visitor identity.
- Verify IPv4 and IPv6 where available. Missing/malformed headers never select
  an attacker-controlled fallback. Exercise the legacy hostname if still used.
- Confirm HTTPS links and Socket.IO WebSocket upgrade; keep existing host routing.
- Do not add a publicly accessible debug-IP endpoint. Use existing authorized
  records/logs and shared QA accounts, with no credentials in output.

Local automated tests cover the application trust boundary; they are not evidence
of nginx installation or a working production firewall. `nginx -t` and traffic
checks must be recorded separately. Cloudflare Workers that can influence headers
within the account are also part of the deployment trust review.

## Rollback and maintenance

Restore the previous application revision/config and nginx files from backups,
validate nginx before reload, and restart the service in the agreed window.
Prefer retaining the overwritten XFF header while rolling back application code;
restoring the old append/first-entry pairing restores its spoofing weakness.
Check live matches/Socket.IO after any restart. No database rollback is needed.

The operator must review Cloudflare ranges periodically and before each rollout;
range changes require reviewed config updates, nginx validation and reload. No
scheduled updater or production cron job has been installed. Direct-origin
blocking, legacy-host retirement and firewall policy require separate decisions.

References: https://nginx.org/en/docs/http/ngx_http_realip_module.html and the
Cloudflare range URLs above (checked October 22, 2026).
