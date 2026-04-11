"""GitHub webhook handler — PLAN-IR-001 Phase 3.

Receives `issues` events from GitHub and mirrors status changes onto the
local `IssueReport` and `ExceptionLog` rows. GitHub remains the source
of truth — the webhook just keeps the local copy in sync.

Endpoint
--------
POST /webhooks/github/issues
    Headers:
      X-Hub-Signature-256  — HMAC SHA256 of the raw request body, using
                             the shared secret in GITHUB_WEBHOOK_SECRET
      X-GitHub-Event       — should be "issues" (anything else is logged
                             but ignored)
      X-GitHub-Delivery    — unique delivery id, used as idempotency key

Behavior
--------
- Reads the *raw* body BEFORE Flask parses it (HMAC requires the exact
  bytes GitHub signed).
- Verifies the signature via services.github_issues.verify_webhook_signature.
- Inserts an IssueWebhookEvent row for every accepted delivery (one row
  per delivery_id; replays return 200 quickly).
- Updates IssueReport / ExceptionLog by github_issue_number.

Status semantics
----------------
GitHub action -> local effect:
  opened       : (no-op for now — we only care about state transitions)
  edited       : (no-op)
  closed       : status -> closed (or label-derived) ; ExceptionLog.resolved=True
  reopened     : status -> open ; ExceptionLog.resolved=False
  labeled      : re-evaluate status from labels
  unlabeled    : re-evaluate status from labels
  deleted      : (no-op — we keep the local row as a historical record)
"""

from __future__ import annotations

import json
from datetime import datetime

from flask import jsonify, request

from utils.exception_tracker import log_exception


# ---------------------------------------------------------------------------
# Label -> local status mapping (also used by manual sync in admin_issue_routes)
# ---------------------------------------------------------------------------

LABEL_STATUS_MAP = {
    "status:triaged": "open",
    "status:in-progress": "in_progress",
    "status:resolved": "resolved",
    "status:deferred": "deferred",
    "status:wont-fix": "wont_fix",
    "status:duplicate": "closed",
}


def _label_names(payload_labels):
    out = []
    for lbl in payload_labels or []:
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if name:
                out.append(str(name).lower())
    return out


def _resolve_status(state: str | None, labels: list[str]) -> str | None:
    """Translate (GitHub state, labels) into a local IssueReport status.

    Label-derived status takes precedence over the bare state so admins
    can mark something as `status:deferred` *and* leave the issue open
    on GitHub if they want.
    """
    for lbl in labels:
        if lbl in LABEL_STATUS_MAP:
            return LABEL_STATUS_MAP[lbl]
    if state == "closed":
        return "closed"
    if state == "open":
        return "open"
    return None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_webhook_routes(app, *, db, csrf=None):
    from database.models import IssueReport, ExceptionLog, IssueWebhookEvent
    from services import github_issues

    @app.route("/webhooks/github/issues", methods=["POST"])
    def github_issues_webhook():
        # Read the raw body BEFORE Flask consumes it for JSON parsing.
        # HMAC must use the exact bytes GitHub signed.
        raw_body = request.get_data(cache=True, as_text=False)

        signature_header = request.headers.get("X-Hub-Signature-256", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        event_type = request.headers.get("X-GitHub-Event", "")

        # ---- Sanity checks --------------------------------------------------
        if not delivery_id:
            return jsonify({"error": "missing X-GitHub-Delivery"}), 400

        # ---- Idempotency check ---------------------------------------------
        # GitHub re-delivers webhooks; we must dedupe.
        existing = IssueWebhookEvent.query.filter_by(delivery_id=delivery_id).first()
        if existing is not None:
            return jsonify({"status": "duplicate", "delivery_id": delivery_id}), 200

        # ---- Signature verification ----------------------------------------
        signature_valid = github_issues.verify_webhook_signature(raw_body, signature_header)
        if not signature_valid:
            # Still record the failed attempt for forensics.
            try:
                event = IssueWebhookEvent(
                    delivery_id=delivery_id,
                    event_type=event_type or None,
                    action=None,
                    github_issue_number=None,
                    payload_json=None,
                    signature_valid=False,
                    processed=False,
                    processing_error="invalid signature",
                    received_at=datetime.utcnow(),
                )
                db.session.add(event)
                db.session.commit()
            except Exception:
                db.session.rollback()
                log_exception(source="backend", context={"scope": "webhook_signature_audit"})
            return jsonify({"error": "invalid signature"}), 401

        # ---- Parse JSON body ------------------------------------------------
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            payload = {}

        action = (payload.get("action") or "").lower() or None
        issue_obj = payload.get("issue") or {}
        github_issue_number = issue_obj.get("number")
        github_state = (issue_obj.get("state") or "").lower() or None
        github_labels = _label_names(issue_obj.get("labels"))

        # Trim payload before persisting — full GitHub bodies can be huge.
        try:
            trimmed_payload = json.dumps({
                "action": action,
                "issue": {
                    "number": github_issue_number,
                    "state": github_state,
                    "labels": github_labels,
                    "title": issue_obj.get("title"),
                    "html_url": issue_obj.get("html_url"),
                },
                "sender": (payload.get("sender") or {}).get("login"),
            })[:8000]
        except Exception:
            trimmed_payload = None

        processing_error = None
        processed = False

        try:
            if event_type != "issues":
                # We only care about Issues events. Record + ack so GitHub
                # doesn't keep retrying.
                processing_error = f"ignored event type: {event_type}"
            elif github_issue_number is None:
                processing_error = "missing issue number"
            elif action == "deleted":
                # Keep the local row, just record that GitHub side was deleted.
                processed = True
            else:
                # Resolve target status (None means "no change").
                new_status = _resolve_status(github_state, github_labels)

                # Touch every IssueReport that points at this GitHub issue
                # (almost always 0 or 1).
                report_rows = IssueReport.query.filter_by(github_issue_number=github_issue_number).all()
                for r in report_rows:
                    if new_status:
                        r.status = new_status
                    r.github_last_synced_at = datetime.utcnow()

                # Same for ExceptionLog rows (auto-filed exceptions).
                exc_rows = ExceptionLog.query.filter_by(github_issue_number=github_issue_number).all()
                for e in exc_rows:
                    if github_state == "closed":
                        e.resolved = True
                        if not e.resolved_at:
                            e.resolved_at = datetime.utcnow()
                            e.resolved_by = "github-webhook"
                    elif github_state == "open":
                        e.resolved = False
                    e.github_last_synced_at = datetime.utcnow()

                processed = True
        except Exception as exc:
            processing_error = str(exc)[:500]
            log_exception(exc, source="backend", context={
                "scope": "github_issues_webhook",
                "delivery_id": delivery_id,
                "action": action,
            })

        # ---- Persist audit row + status updates in a single commit ---------
        try:
            event = IssueWebhookEvent(
                delivery_id=delivery_id,
                event_type=event_type or None,
                action=action,
                github_issue_number=github_issue_number,
                payload_json=trimmed_payload,
                signature_valid=True,
                processed=bool(processed),
                processing_error=processing_error,
                received_at=datetime.utcnow(),
            )
            db.session.add(event)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "webhook_audit_commit"})
            return jsonify({"error": "commit failed"}), 500

        return jsonify({
            "status": "ok",
            "delivery_id": delivery_id,
            "processed": processed,
            "github_issue_number": github_issue_number,
            "action": action,
        }), 200

    # CSRF-exempt the webhook (it's authenticated via HMAC instead).
    if csrf is not None:
        try:
            csrf.exempt(github_issues_webhook)
        except Exception:
            log_exception(source="backend", context={"scope": "webhook_csrf_exempt"})
