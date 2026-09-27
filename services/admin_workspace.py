"""Read-only data loaders for the tabbed admin workspaces."""
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import current_app
from sqlalchemy import inspect
from database import db
from database.models import (User, Team, Player, Match, Tournament,
                             ActiveSession)

BACKUP_INTERVAL_SECONDS = 24 * 3600
MATCH_CLEANUP_INTERVAL_SECONDS = 6 * 3600
BACKUP_RETENTION_DAYS = 7
STALE_SESSION_DAYS = 7


def session_cutoff(now=None):
    return (now or datetime.utcnow()) - timedelta(
        minutes=current_app.config.get('SESSION_INACTIVITY_MINUTES', 120))


def user_counts(user_id):
    return {
        'teams': Team.query.filter_by(user_id=user_id).count(),
        'players': Player.query.join(Team, Player.team_id == Team.id).filter(Team.user_id == user_id).count(),
        'matches': Match.query.filter_by(user_id=user_id).count(),
        'tournaments': Tournament.query.filter_by(user_id=user_id).count(),
    }


def user_overview(user_id):
    now = datetime.utcnow()
    return dict(
        counts=user_counts(user_id),
        active_sessions=ActiveSession.query.filter(ActiveSession.user_id == user_id,
                                                   ActiveSession.last_active >= session_cutoff(now)).count(),
    )


def paginate(query, value, per_page=25):
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = max(1, min(pages, int(value)))
    except (TypeError, ValueError):
        page = 1
    return dict(items=query.offset((page - 1) * per_page).limit(per_page).all(),
                page=page, pages=pages, total=total)


def user_security(user_id, args):
    return dict(
        cutoff=session_cutoff(),
        sessions=paginate(
            ActiveSession.query.filter_by(user_id=user_id).order_by(
                ActiveSession.last_active.desc(), ActiveSession.id.desc()),
            args.get('sessions_page')),
    )


def _safe(fn):
    try:
        return fn()
    except Exception:
        current_app.logger.warning('An admin system metric is unavailable', exc_info=True)
        return None


def database_metrics():
    engine = db.engine
    path = engine.url.database if engine.dialect.name == 'sqlite' else None
    file_backed = bool(path and path != ':memory:' and not str(path).startswith('file::memory:'))
    return dict(
        kind='SQLite file' if file_backed else ('SQLite in-memory' if engine.dialect.name == 'sqlite' else engine.dialect.name),
        size_mb=_safe(lambda: round(os.path.getsize(path) / 1024**2, 2)) if file_backed else None,
        tables=_safe(lambda: len(inspect(engine).get_table_names())),
        counts={name: _safe(lambda model=model: model.query.count()) for name, model in
                [('users', User), ('teams', Team), ('matches', Match), ('tournaments', Tournament)]},
    )


def latest_backup(root):
    def read():
        paths = [p for p in (Path(root) / 'data' / 'backups').iterdir()
                 if p.is_file() and p.suffix == '.db' and not p.name.startswith('pre_restore_')]
        if not paths:
            return {'timestamp': None, 'age_hours': None}
        stamp = max(p.stat().st_mtime for p in paths)
        return {'timestamp': datetime.utcfromtimestamp(stamp), 'age_hours': round(max(0, time.time() - stamp) / 3600, 1)}
    return _safe(read)


def system_overview(root, psutil, match_instances, match_lock):
    data = dict(updated_at=datetime.utcnow(), backup=latest_backup(root))
    def directory_size():
        total = 0
        def fail(error):
            raise error
        for directory, _, files in os.walk(Path(root) / 'data', onerror=fail):
            for name in files:
                total += os.path.getsize(os.path.join(directory, name))
        if not (Path(root) / 'data').is_dir():
            raise FileNotFoundError('Data directory unavailable')
        return round(total / 1024**2, 2)
    data['data_mb'] = _safe(directory_size)
    data['log_mb'] = _safe(lambda: round((Path(root) / 'logs' / 'execution.log').stat().st_size / 1024**2, 2))
    with match_lock:
        data['matches'] = len(match_instances)
    process = _safe(lambda: psutil.Process()) if psutil else None
    data['memory_mb'] = _safe(lambda: round(process.memory_info().rss / 1024**2, 1)) if process else None
    data['cpu'] = _safe(lambda: process.cpu_percent(interval=0.1)) if process else None
    data['uptime_hours'] = _safe(lambda: round((time.time() - process.create_time()) / 3600, 1)) if process else None
    data['disk'] = _safe(lambda: psutil.disk_usage(root)) if psutil else None
    return data


def system_tasks(root, get_backup_status, get_cleanup_status):
    cleanup = _safe(get_cleanup_status)
    return dict(updated_at=datetime.utcnow(), backup_started=_safe(get_backup_status),
                cleanup_started=cleanup[0] if cleanup else None,
                cleanup_last_run=cleanup[1] if cleanup else None, backup=latest_backup(root),
                backup_hours=BACKUP_INTERVAL_SECONDS // 3600,
                cleanup_hours=MATCH_CLEANUP_INTERVAL_SECONDS // 3600,
                retention_days=BACKUP_RETENTION_DAYS, stale_days=STALE_SESSION_DAYS)
