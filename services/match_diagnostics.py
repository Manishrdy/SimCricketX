"""Opt-in, short-lived match tracing. Never commits the gameplay session."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import psutil
from flask import current_app, g
from database import db

_LIMIT = 131072


def enabled(match_id):
    targets = {s.strip() for s in os.getenv('MATCH_DIAGNOSTICS', '').split(',') if s.strip()}
    return '*' in targets or str(match_id) in targets


def record(match_id, stage, payload=None, trace_id=None, source='server'):
    if not enabled(match_id):
        return
    try:
        process = psutil.Process(os.getpid())
        metrics = {}
        samples = {
            'process_cpu_percent': lambda: process.cpu_percent(None),
            'system_cpu_percent': lambda: psutil.cpu_percent(None),
            'process_cpu_times': lambda: process.cpu_times()._asdict(),
            'process_memory': lambda: process.memory_info()._asdict(),
            'process_rss_bytes': lambda: process.memory_info().rss,
            'system_memory': lambda: psutil.virtual_memory()._asdict(),
            'swap': lambda: psutil.swap_memory()._asdict(),
            'load_average': os.getloadavg,
            'threads': process.num_threads,
        }
        for name, sample in samples.items():
            try:
                metrics[name] = sample()
            except (psutil.Error, OSError):
                metrics[name] = None
        encoded = json.dumps(payload, default=str, ensure_ascii=True)
        size = len(encoded.encode())
        truncated = size > _LIMIT
        if truncated:
            encoded = json.dumps({'preview': encoded[:_LIMIT], 'original_bytes': size})
        # A separate short-timeout connection avoids committing/rolling back
        # gameplay transactions. A busy DB drops diagnostics, never blocks play.
        path = db.engine.url.database
        if db.engine.dialect.name != 'sqlite':
            raise RuntimeError('Match diagnostics currently requires SQLite')
        with sqlite3.connect(path, timeout=0.05) as conn:
            conn.execute('''INSERT INTO match_diagnostic_events
                (created_at, match_id, trace_id, source, stage, pid, monotonic_seconds,
                 payload_json, payload_bytes, payload_truncated, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (datetime.now(timezone.utc).isoformat(), str(match_id)[:64],
                 str(trace_id or getattr(g, 'match_trace_id', '') or '')[:64], source,
                 stage[:64], os.getpid(), time.monotonic(), encoded, size,
                 int(truncated), json.dumps(metrics)))
    except Exception as exc:
        current_app.logger.warning('[MatchTrace] stage=%s match=%s write failed: %s', stage, match_id, exc)
