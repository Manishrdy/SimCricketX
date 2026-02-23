"""Admin route registration."""

import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta

import yaml
from flask import Response, after_this_request, flash, jsonify, redirect, render_template, request, send_file, session, stream_with_context, url_for
from flask_login import current_user, login_user
from sqlalchemy import func
from werkzeug.utils import secure_filename


def register_admin_routes(
    app,
    *,
    login_required,
    admin_required,
    db,
    basedir,
    config,
    load_config,
    get_client_ip,
    parse_ip,
    is_path_within_base,
    coerce_config_value,
    ADMIN_CONFIG_ALLOWLIST,
    bot_defense_settings,
    _check_backup_rate_limit,
    _run_scheduled_backup,
    _list_backup_files,
    _verify_sqlite_integrity,
    _backup_scheduler_started,
    _persist_maintenance_mode,
    get_maintenance_mode,
    psutil,
    log_admin_action,
    update_user_email,
    update_user_password,
    delete_user,
    register_user,
    BLOCKED_IP_MODEL,
    FAILED_LOGIN_MODEL,
    ACTIVE_SESSION_MODEL,
    AUDIT_MODEL,
    LOGIN_HISTORY_MODEL,
    IP_WHITELIST_MODEL,
    DBUser,
    DBTeam,
    DBPlayer,
    DBMatch,
    Tournament,
    MatchScorecard,
    TournamentTeam,
    TournamentFixture,
    TournamentPlayerStatsCache,
    MatchPartnership,
    PROJECT_ROOT,
    MATCH_INSTANCES,
    MATCH_INSTANCES_LOCK,
    text,
    get_whitelist_mode,
):
    AdminAuditLog = AUDIT_MODEL
    FailedLoginAttempt = FAILED_LOGIN_MODEL
    BlockedIP = BLOCKED_IP_MODEL
    ActiveSession = ACTIVE_SESSION_MODEL
    LoginHistory = LOGIN_HISTORY_MODEL
    IPWhitelistEntry = IP_WHITELIST_MODEL
    BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "backups")

    @app.route('/admin/backup-database', methods=['POST'])
    @login_required
    @admin_required
    def backup_database():
        """Download database backup (admin only, requires token). Uses POST to prevent CSRF."""
        try:
            # Brute-force protection: 3 attempts per minute
            if _check_backup_rate_limit(current_user.id):
                app.logger.warning(f"[Admin] Backup rate limit hit by {current_user.id}")
                return jsonify({"error": "Too many attempts. Please wait 60 seconds."}), 429

            # Get token from POST body
            token = request.form.get('token', '').strip()

            # Load backup token from config (env var takes priority)
            expected_token = os.environ.get('BACKUP_TOKEN', '')
            if not expected_token:
                backup_config = config.get('backup', {})
                expected_token = str(backup_config.get('token', ''))

            if not expected_token or expected_token in ['CHANGE_ME', 'your_backup_token_here', '']:
                app.logger.error("[Admin] Backup token not configured")
                return jsonify({"error": "Backup not configured. Set BACKUP_TOKEN env var or config.yaml"}), 400

            # Verify token
            if token != expected_token:
                app.logger.warning(f"[Admin] Invalid backup token attempt by {current_user.id}")
                return jsonify({"error": "Invalid backup token"}), 403

            # Create a temporary copy to avoid exposing DB path
            src_path = os.path.join(basedir, 'cricket_sim.db')
            if not os.path.exists(src_path):
                return jsonify({"error": "Database file not found"}), 404

            import tempfile
            backup_name = f'cricket_sim_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
            tmp_dir = tempfile.mkdtemp()
            tmp_path = os.path.join(tmp_dir, backup_name)
            shutil.copy2(src_path, tmp_path)

            app.logger.info(f"[Admin] Database backup downloaded by {current_user.id}")
            log_admin_action(current_user.id, 'backup_database', None, 'Database backup downloaded', request.remote_addr)

            @after_this_request
            def _cleanup_backup_tmp(response, _dir=tmp_dir):
                shutil.rmtree(_dir, ignore_errors=True)
                return response

            return send_file(
                tmp_path,
                as_attachment=True,
                download_name=backup_name,
                mimetype='application/x-sqlite3'
            )

        except Exception as e:
            app.logger.error(f"[Admin] Database backup failed: {e}", exc_info=True)
            return jsonify({"error": "Backup failed"}), 500

    @app.route('/admin/dashboard')
    @login_required
    @admin_required
    def admin_dashboard():
        """Admin dashboard home"""
        try:
            stats = {}
            stats['total_users'] = db.session.query(DBUser).count()
            stats['total_teams'] = db.session.query(DBTeam).count()
            stats['total_matches'] = db.session.query(DBMatch).count()
            stats['total_tournaments'] = db.session.query(Tournament).count()

            # Database size
            db_path = os.path.join(basedir, 'cricket_sim.db')
            if os.path.exists(db_path):
                stats['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
            else:
                stats['db_size_mb'] = 0

            # Active users (logged in last 7 days)
            seven_days_ago = datetime.utcnow() - timedelta(days=7)
            stats['active_users_7d'] = db.session.query(DBUser).filter(DBUser.last_login >= seven_days_ago).count()

            # Active match instances
            with MATCH_INSTANCES_LOCK:
                stats['active_matches'] = len(MATCH_INSTANCES)
                stats['live_matches'] = stats['active_matches']

            # Active sessions count
            stats['active_sessions'] = ActiveSession.query.count()

            # Recent activity from audit log
            recent_audit = db.session.query(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).limit(10).all()
            audit_entries = []
            for entry in recent_audit:
                time_diff = datetime.utcnow() - entry.timestamp if entry.timestamp else None
                if time_diff:
                    if time_diff.days > 0:
                        time_str = f"{time_diff.days}d ago"
                    elif time_diff.seconds > 3600:
                        time_str = f"{time_diff.seconds // 3600}h ago"
                    else:
                        time_str = f"{max(1, time_diff.seconds // 60)}m ago"
                else:
                    time_str = "just now"
                audit_entries.append({
                    'admin': entry.admin_email,
                    'action': entry.action.replace('_', ' ').title(),
                    'target': entry.target or '',
                    'time': time_str
                })

            # Recent user logins
            recent_users = db.session.query(DBUser).order_by(DBUser.last_login.desc()).limit(10).all()
            recent_activity = []
            for user in recent_users:
                if user.last_login:
                    time_diff = datetime.utcnow() - user.last_login
                    if time_diff.days > 0:
                        time_str = f"{time_diff.days}d ago"
                    elif time_diff.seconds > 3600:
                        time_str = f"{time_diff.seconds // 3600}h ago"
                    else:
                        time_str = f"{max(1, time_diff.seconds // 60)}m ago"
                    recent_activity.append({
                        'email': user.id,
                        'action': 'logged in',
                        'time': time_str
                    })

            return render_template('admin/dashboard.html',
                                   stats=stats,
                                   recent_activity=recent_activity,
                                   audit_entries=audit_entries)
        except Exception as e:
            app.logger.error(f"[Admin] Dashboard error: {e}", exc_info=True)
            return "Error loading dashboard", 500

    @app.route('/admin/users')
    @login_required
    @admin_required
    def admin_users():
        """List all users"""
        try:
            users = db.session.query(DBUser).all()
            user_data = []
            for user in users:
                user_data.append({
                    'email': user.id,
                    'display_name': user.display_name,
                    'is_admin': user.is_admin,
                    'teams_count': db.session.query(DBTeam).filter_by(user_id=user.id).count(),
                    'matches_count': db.session.query(DBMatch).filter_by(user_id=user.id).count(),
                    'last_login': user.last_login
                })

            return render_template('admin/users_list.html', users=user_data)
        except Exception as e:
            app.logger.error(f"[Admin] Users list error: {e}", exc_info=True)
            return "Error loading users", 500

    @app.route('/admin/search')
    @login_required
    @admin_required
    def admin_search():
        q = request.args.get('q', '').strip()
        results = {
            'users': [],
            'teams': [],
            'matches': [],
            'tournaments': [],
        }
        if q:
            results['users'] = db.session.query(DBUser).filter(
                db.or_(
                    DBUser.id.ilike(f'%{q}%'),
                    DBUser.display_name.ilike(f'%{q}%')
                )
            ).order_by(DBUser.last_login.desc()).limit(20).all()

            results['teams'] = db.session.query(DBTeam).filter(
                db.or_(
                    DBTeam.name.ilike(f'%{q}%'),
                    DBTeam.short_code.ilike(f'%{q}%'),
                    DBTeam.user_id.ilike(f'%{q}%')
                )
            ).order_by(DBTeam.created_at.desc()).limit(20).all()

            results['matches'] = db.session.query(DBMatch).filter(
                db.or_(
                    DBMatch.id.ilike(f'%{q}%'),
                    DBMatch.user_id.ilike(f'%{q}%'),
                    DBMatch.result_description.ilike(f'%{q}%'),
                    DBMatch.venue.ilike(f'%{q}%')
                )
            ).order_by(DBMatch.date.desc()).limit(20).all()

            results['tournaments'] = db.session.query(Tournament).filter(
                db.or_(
                    Tournament.name.ilike(f'%{q}%'),
                    Tournament.user_id.ilike(f'%{q}%'),
                    Tournament.status.ilike(f'%{q}%')
                )
            ).order_by(Tournament.created_at.desc()).limit(20).all()

        totals = {k: len(v) for k, v in results.items()}
        return render_template('admin/search.html', q=q, results=results, totals=totals)

    @app.route('/admin/users/<user_email>')
    @login_required
    @admin_required
    def admin_user_detail(user_email):
        """View user details"""
        try:
            user = db.session.get(DBUser, user_email)
            if not user:
                return "User not found", 404

            teams = db.session.query(DBTeam).filter_by(user_id=user_email).all()
            matches = db.session.query(DBMatch).filter_by(user_id=user_email).all()
            sessions = db.session.query(ActiveSession).filter_by(user_id=user_email).order_by(ActiveSession.login_at.desc()).all()

            return render_template('admin/user_detail.html', user=user, teams=teams, matches=matches, sessions=sessions)
        except Exception as e:
            app.logger.error(f"[Admin] User detail error: {e}", exc_info=True)
            return "Error loading user", 500

    @app.route('/admin/users/<user_email>/360')
    @login_required
    @admin_required
    def admin_user_360(user_email):
        """Consolidated user profile and security/activity context."""
        try:
            user = db.session.get(DBUser, user_email)
            if not user:
                return "User not found", 404

            teams_count = db.session.query(DBTeam).filter_by(user_id=user_email).count()
            matches_count = db.session.query(DBMatch).filter_by(user_id=user_email).count()
            tournaments_count = db.session.query(Tournament).filter_by(user_id=user_email).count()
            players_count = db.session.query(DBPlayer).join(DBTeam, DBPlayer.team_id == DBTeam.id).filter(DBTeam.user_id == user_email).count()

            recent_teams = db.session.query(DBTeam).filter_by(user_id=user_email).order_by(DBTeam.created_at.desc()).limit(8).all()
            recent_matches = db.session.query(DBMatch).filter_by(user_id=user_email).order_by(DBMatch.date.desc()).limit(10).all()
            recent_tournaments = db.session.query(Tournament).filter_by(user_id=user_email).order_by(Tournament.created_at.desc()).limit(10).all()
            sessions = db.session.query(ActiveSession).filter_by(user_id=user_email).order_by(ActiveSession.last_active.desc()).limit(10).all()
            failed_logins = db.session.query(FailedLoginAttempt).filter_by(email=user_email).order_by(FailedLoginAttempt.timestamp.desc()).limit(20).all()

            cutoff_24h = datetime.utcnow() - timedelta(hours=24)
            failed_24h = db.session.query(FailedLoginAttempt).filter(
                FailedLoginAttempt.email == user_email,
                FailedLoginAttempt.timestamp >= cutoff_24h
            ).count()

            admin_actions_by_user = db.session.query(AdminAuditLog).filter(
                AdminAuditLog.admin_email == user_email
            ).order_by(AdminAuditLog.timestamp.desc()).limit(15).all()

            actions_targeting_user = db.session.query(AdminAuditLog).filter(
                AdminAuditLog.target == user_email
            ).order_by(AdminAuditLog.timestamp.desc()).limit(15).all()

            unique_ips = sorted({s.ip_address for s in sessions if s.ip_address})
            security_overview = {
                'active_sessions': len(sessions),
                'failed_logins_24h': failed_24h,
                'unique_recent_ips': len(unique_ips),
            }

            return render_template(
                'admin/user_360.html',
                user=user,
                teams_count=teams_count,
                matches_count=matches_count,
                tournaments_count=tournaments_count,
                players_count=players_count,
                recent_teams=recent_teams,
                recent_matches=recent_matches,
                recent_tournaments=recent_tournaments,
                sessions=sessions,
                failed_logins=failed_logins,
                admin_actions_by_user=admin_actions_by_user,
                actions_targeting_user=actions_targeting_user,
                unique_ips=unique_ips,
                security_overview=security_overview,
            )
        except Exception as e:
            app.logger.error(f"[Admin] User 360 error: {e}", exc_info=True)
            return "Error loading user 360", 500

    @app.route('/admin/users/<user_email>/change-email', methods=['POST'])
    @login_required
    @admin_required
    def admin_change_email(user_email):
        """Change user email"""
        try:
            new_email = request.form.get('new_email', '').strip()
            if not new_email:
                return jsonify({"error": "New email is required"}), 400
            target = db.session.get(DBUser, user_email)
            if target and target.is_admin:
                return jsonify({"error": "Cannot modify admin account email from this panel"}), 400

            success, message = update_user_email(user_email, new_email, current_user.id)
            if success:
                return jsonify({"message": message}), 200
            else:
                return jsonify({"error": message}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Change email error: {e}", exc_info=True)
            return jsonify({"error": "Failed to change email"}), 500

    @app.route('/admin/users/<user_email>/reset-password', methods=['POST'])
    @login_required
    @admin_required
    def admin_reset_password(user_email):
        """Reset user password"""
        try:
            new_password = request.form.get('new_password', '')
            if not new_password:
                return jsonify({"error": "New password is required"}), 400
            target = db.session.get(DBUser, user_email)
            if target and target.is_admin:
                return jsonify({"error": "Cannot reset admin account password from this panel"}), 400

            success, message = update_user_password(user_email, new_password, current_user.id)
            if success:
                return jsonify({"message": message}), 200
            else:
                return jsonify({"error": message}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Reset password error: {e}", exc_info=True)
            return jsonify({"error": "Failed to reset password"}), 500

    @app.route('/admin/users/<user_email>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_user(user_email):
        """Delete user (cannot delete admin or self)"""
        try:
            if user_email == current_user.id:
                return jsonify({"error": "Cannot delete your own account"}), 400

            target = db.session.get(DBUser, user_email)
            if target and target.is_admin:
                return jsonify({"error": "Cannot delete an admin account"}), 400

            success = delete_user(user_email, current_user.id)
            if success:
                return jsonify({"message": f"User {user_email} deleted successfully"}), 200
            else:
                return jsonify({"error": "Failed to delete user"}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Delete user error: {e}", exc_info=True)
            return jsonify({"error": "Failed to delete user"}), 500

    @app.route('/admin/database/stats')
    @login_required
    @admin_required
    def admin_database_stats():
        """Database statistics"""
        try:
            stats = {}

            db_path = os.path.join(basedir, 'cricket_sim.db')
            if os.path.exists(db_path):
                stats['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
            else:
                stats['db_size_mb'] = 0

            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(db.engine)
            stats['total_tables'] = len(inspector.get_table_names())

            stats['total_users'] = db.session.query(DBUser).count()
            stats['total_teams'] = db.session.query(DBTeam).count()
            stats['total_matches'] = db.session.query(DBMatch).count()
            stats['total_tournaments'] = db.session.query(Tournament).count()

            return render_template('admin/database_stats.html', stats=stats)
        except Exception as e:
            app.logger.error(f"[Admin] Database stats error: {e}", exc_info=True)
            return "Error loading database stats", 500

    @app.route('/admin/database/optimize', methods=['POST'])
    @login_required
    @admin_required
    def admin_optimize_database():
        """Optimize database (VACUUM)"""
        try:
            db.session.execute(text('VACUUM'))
            db.session.commit()
            app.logger.info(f"[Admin] Database optimized by {current_user.id}")
            log_admin_action(current_user.id, 'optimize_db', None, 'Database VACUUM executed', request.remote_addr)
            return jsonify({"message": "Database optimized successfully"}), 200
        except Exception as e:
            app.logger.error(f"[Admin] Database optimize error: {e}", exc_info=True)
            return jsonify({"error": "Failed to optimize database"}), 500

    # --- User Activity Dashboard ---
    @app.route('/admin/activity')
    @login_required
    @admin_required
    def admin_activity():
        """User activity dashboard with signup/login trends"""
        try:
            from sqlalchemy import func as sa_func

            # Signups per day (last 30 days)
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            signups_raw = db.session.query(
                sa_func.date(DBUser.created_at).label('day'),
                sa_func.count(DBUser.id).label('count')
            ).filter(DBUser.created_at >= thirty_days_ago).group_by(sa_func.date(DBUser.created_at)).all()

            signups_data = {str(row.day): row.count for row in signups_raw}

            # Logins per day (last 30 days)
            logins_raw = db.session.query(
                sa_func.date(DBUser.last_login).label('day'),
                sa_func.count(DBUser.id).label('count')
            ).filter(DBUser.last_login >= thirty_days_ago).group_by(sa_func.date(DBUser.last_login)).all()

            logins_data = {str(row.day): row.count for row in logins_raw}

            # Matches per day (last 30 days)
            matches_raw = db.session.query(
                sa_func.date(DBMatch.date).label('day'),
                sa_func.count(DBMatch.id).label('count')
            ).filter(DBMatch.date >= thirty_days_ago).group_by(sa_func.date(DBMatch.date)).all()

            matches_data = {str(row.day): row.count for row in matches_raw}

            # Build 30-day date list
            days = []
            for i in range(30, -1, -1):
                d = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
                days.append(d)

            chart_data = {
                'labels': days,
                'signups': [signups_data.get(d, 0) for d in days],
                'logins': [logins_data.get(d, 0) for d in days],
                'matches': [matches_data.get(d, 0) for d in days],
            }

            # Top users by matches
            top_users = db.session.query(
                DBUser.id,
                sa_func.count(DBMatch.id).label('match_count')
            ).outerjoin(DBMatch, DBUser.id == DBMatch.user_id).group_by(DBUser.id).order_by(sa_func.count(DBMatch.id).desc()).limit(10).all()

            # Audit log
            audit_log = db.session.query(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).limit(50).all()

            return render_template('admin/activity.html',
                                   chart_data=json.dumps(chart_data),
                                   top_users=top_users,
                                   audit_log=audit_log)
        except Exception as e:
            app.logger.error(f"[Admin] Activity page error: {e}", exc_info=True)
            return "Error loading activity", 500

    # --- System Health Page ---
    @app.route('/admin/health')
    @login_required
    @admin_required
    def admin_health():
        """System health overview"""
        try:
            health = {}

            # Disk usage
            db_path = os.path.join(basedir, 'cricket_sim.db')
            if os.path.exists(db_path):
                health['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
            else:
                health['db_size_mb'] = 0

            # Data directory size
            data_dir = os.path.join(PROJECT_ROOT, "data")
            total_data_size = 0
            if os.path.isdir(data_dir):
                for dirpath, dirnames, filenames in os.walk(data_dir):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        total_data_size += os.path.getsize(fp)
            health['data_dir_mb'] = round(total_data_size / (1024 * 1024), 2)

            # Active match instances
            with MATCH_INSTANCES_LOCK:
                health['active_matches'] = len(MATCH_INSTANCES)

            # Memory usage (if psutil available)
            if psutil:
                process = psutil.Process()
                mem = process.memory_info()
                health['memory_mb'] = round(mem.rss / (1024 * 1024), 1)
                health['cpu_percent'] = process.cpu_percent(interval=0.1)

                disk = psutil.disk_usage(basedir)
                health['disk_total_gb'] = round(disk.total / (1024**3), 1)
                health['disk_used_gb'] = round(disk.used / (1024**3), 1)
                health['disk_free_gb'] = round(disk.free / (1024**3), 1)
                health['disk_percent'] = disk.percent
            else:
                health['memory_mb'] = 'N/A'
                health['cpu_percent'] = 'N/A'
                health['disk_total_gb'] = 'N/A'
                health['disk_used_gb'] = 'N/A'
                health['disk_free_gb'] = 'N/A'
                health['disk_percent'] = 'N/A'

            # Backup status
            backups = []
            if os.path.isdir(BACKUP_DIR):
                for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
                    if fn.endswith('.db'):
                        path = os.path.join(BACKUP_DIR, fn)
                        backups.append({
                            'name': fn,
                            'size_mb': round(os.path.getsize(path) / (1024 * 1024), 2),
                            'date_dt': datetime.utcfromtimestamp(os.path.getmtime(path)),
                        })
            health['backups'] = backups[:10]
            health['backup_count'] = len(backups)

            # Log file size
            log_path = os.path.join(PROJECT_ROOT, "logs", "execution.log")
            if os.path.exists(log_path):
                health['log_size_mb'] = round(os.path.getsize(log_path) / (1024 * 1024), 2)
            else:
                health['log_size_mb'] = 0

            # Uptime (approx from process start)
            if psutil:
                create_time = process.create_time()
                uptime_seconds = time.time() - create_time
                hours = int(uptime_seconds // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                health['uptime'] = f"{hours}h {minutes}m"
            else:
                health['uptime'] = 'N/A'

            return render_template('admin/health.html', health=health)
        except Exception as e:
            app.logger.error(f"[Admin] Health page error: {e}", exc_info=True)
            return "Error loading health page", 500

    # --- Backup Management ---
    @app.route('/admin/backups')
    @login_required
    @admin_required
    def admin_backups():
        """List and manage database backups"""
        try:
            backups = _list_backup_files(prefix_filter=None)
            return render_template('admin/backups.html', backups=backups)
        except Exception as e:
            app.logger.error(f"[Admin] Backups page error: {e}", exc_info=True)
            return "Error loading backups", 500

    @app.route('/admin/restore-center')
    @login_required
    @admin_required
    def admin_restore_center():
        """Restore and rollback management."""
        try:
            db_path = os.path.join(basedir, 'cricket_sim.db')
            backups = _list_backup_files(prefix_filter=None)
            rollback_points = _list_backup_files(prefix_filter='pre_restore_')
            restore_events = db.session.query(AdminAuditLog).filter(
                AdminAuditLog.action.in_(['restore_database', 'rollback_database'])
            ).order_by(AdminAuditLog.timestamp.desc()).limit(20).all()
            current_db = {
                'exists': os.path.exists(db_path),
                'size_mb': round(os.path.getsize(db_path) / (1024 * 1024), 2) if os.path.exists(db_path) else 0,
                'modified': datetime.fromtimestamp(os.path.getmtime(db_path)).strftime('%Y-%m-%d %H:%M:%S') if os.path.exists(db_path) else 'N/A',
            }
            return render_template(
                'admin/restore_center.html',
                backups=backups,
                rollback_points=rollback_points,
                restore_events=restore_events,
                current_db=current_db,
            )
        except Exception as e:
            app.logger.error(f"[Admin] Restore center error: {e}", exc_info=True)
            return "Error loading restore center", 500

    @app.route('/admin/restore/apply', methods=['POST'])
    @login_required
    @admin_required
    def admin_restore_apply():
        """Restore live DB from a backup or rollback point."""
        try:
            filename = request.form.get('filename', '').strip()
            source_type = request.form.get('source_type', 'backup').strip().lower()
            if source_type not in {'backup', 'rollback'}:
                return jsonify({"error": "Invalid source_type"}), 400
            if not filename or not filename.endswith('.db'):
                return jsonify({"error": "Valid backup filename is required"}), 400

            safe_name = secure_filename(filename)
            source_path = os.path.join(BACKUP_DIR, safe_name)
            if not os.path.exists(source_path):
                return jsonify({"error": "Selected backup file not found"}), 404

            is_rollback_file = safe_name.startswith('pre_restore_')
            if source_type == 'backup' and is_rollback_file:
                return jsonify({"error": "Rollback points must use source_type=rollback"}), 400
            if source_type == 'rollback' and not is_rollback_file:
                return jsonify({"error": "Only rollback snapshots are allowed for rollback source_type"}), 400

            ok, status = _verify_sqlite_integrity(source_path)
            if not ok:
                return jsonify({"error": f"Backup integrity check failed: {status}"}), 400

            db_path = os.path.join(basedir, 'cricket_sim.db')
            snapshot_name = None
            if os.path.exists(db_path):
                admin_label = re.sub(r'[^a-zA-Z0-9_-]+', '_', (current_user.id or 'admin'))[:40]
                snapshot_name = f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{admin_label}.db"
                snapshot_path = os.path.join(BACKUP_DIR, snapshot_name)
                shutil.copy2(db_path, snapshot_path)

            _persist_maintenance_mode(True)
            db.session.remove()
            db.engine.dispose()
            shutil.copy2(source_path, db_path)

            ok_live, status_live = _verify_sqlite_integrity(db_path)
            if not ok_live:
                if snapshot_name:
                    snapshot_path = os.path.join(BACKUP_DIR, snapshot_name)
                    if os.path.exists(snapshot_path):
                        shutil.copy2(snapshot_path, db_path)
                return jsonify({"error": f"Post-restore integrity check failed: {status_live}"}), 500

            action = 'rollback_database' if source_type == 'rollback' else 'restore_database'
            details = f"Restored from {safe_name}. Pre-restore snapshot: {snapshot_name or 'not-created'}. Maintenance mode enabled."
            log_admin_action(current_user.id, action, safe_name, details, get_client_ip())
            return jsonify({
                "message": f"Database restore completed from {safe_name}. Maintenance mode is ON.",
                "snapshot": snapshot_name,
                "maintenance_mode": True
            }), 200
        except Exception as e:
            app.logger.error(f"[Admin] Restore apply error: {e}", exc_info=True)
            return jsonify({"error": "Failed to restore database"}), 500

    @app.route('/admin/backups/create', methods=['POST'])
    @login_required
    @admin_required
    def admin_create_backup():
        """Manually trigger a backup"""
        try:
            _run_scheduled_backup()
            log_admin_action(current_user.id, 'create_backup', None, 'Manual backup created', request.remote_addr)
            return jsonify({"message": "Backup created successfully"}), 200
        except Exception as e:
            app.logger.error(f"[Admin] Manual backup error: {e}", exc_info=True)
            return jsonify({"error": "Failed to create backup"}), 500

    @app.route('/admin/backups/<filename>/download')
    @login_required
    @admin_required
    def admin_download_backup(filename):
        """Download a specific backup file"""
        try:
            safe_name = secure_filename(filename)
            path = os.path.join(BACKUP_DIR, safe_name)
            if not os.path.exists(path):
                return jsonify({"error": "Backup not found"}), 404
            log_admin_action(current_user.id, 'download_backup', safe_name, 'Backup file downloaded', request.remote_addr)
            return send_file(path, as_attachment=True, download_name=safe_name, mimetype='application/x-sqlite3')
        except Exception as e:
            return jsonify({"error": "Download failed"}), 500

    @app.route('/admin/backups/<filename>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_backup(filename):
        """Delete a specific backup file"""
        try:
            safe_name = secure_filename(filename)
            path = os.path.join(BACKUP_DIR, safe_name)
            if not os.path.exists(path):
                return jsonify({"error": "Backup not found"}), 404
            os.remove(path)
            log_admin_action(current_user.id, 'delete_backup', safe_name, 'Backup file deleted', request.remote_addr)
            return jsonify({"message": f"Backup {safe_name} deleted"}), 200
        except Exception as e:
            return jsonify({"error": "Failed to delete backup"}), 500

    # --- User Impersonation ---
    @app.route('/admin/impersonate/<user_email>', methods=['POST'])
    @login_required
    @admin_required
    def admin_impersonate(user_email):
        """Impersonate a user (login as them). Stores original admin session."""
        try:
            target = db.session.get(DBUser, user_email)
            if not target:
                return jsonify({"error": "User not found"}), 404

            if target.is_admin:
                return jsonify({"error": "Cannot impersonate another admin"}), 400

            # Store admin identity/session for returning later.
            session['impersonating_from'] = current_user.id
            session['impersonating_from_token'] = session.get('session_token')
            log_admin_action(current_user.id, 'impersonate', user_email, f'Started impersonating {user_email}', get_client_ip())

            login_user(target)
            try:
                import secrets
                token = secrets.token_hex(32)
                session['session_token'] = token
                active = ActiveSession(
                    session_token=token,
                    user_id=user_email,
                    ip_address=get_client_ip(),
                    user_agent=request.user_agent.string[:300] if request.user_agent.string else None
                )
                db.session.add(active)
                db.session.commit()
            except Exception:
                db.session.rollback()
            flash(f"Now viewing as {user_email}. Click 'Stop Impersonating' to return.", "info")
            return redirect(url_for('home'))
        except Exception as e:
            app.logger.error(f"[Admin] Impersonate error: {e}", exc_info=True)
            return jsonify({"error": "Failed to impersonate"}), 500

    @app.route('/admin/stop-impersonation')
    @login_required
    def admin_stop_impersonation():
        """Return to the original admin account after impersonation."""
        try:
            current_token = session.get('session_token')
            original_admin = session.pop('impersonating_from', None)
            original_admin_token = session.pop('impersonating_from_token', None)
            if not original_admin:
                return redirect(url_for('home'))

            # End the impersonated user's active session token.
            if current_token:
                ActiveSession.query.filter_by(session_token=current_token).delete()
                db.session.commit()

            admin_user = db.session.get(DBUser, original_admin)
            if admin_user and admin_user.is_admin:
                log_admin_action(original_admin, 'stop_impersonate', current_user.id, 'Stopped impersonation')
                login_user(admin_user)
                if original_admin_token:
                    existing = ActiveSession.query.filter_by(
                        session_token=original_admin_token,
                        user_id=original_admin
                    ).first()
                    if existing:
                        existing.last_active = datetime.utcnow()
                        session['session_token'] = original_admin_token
                        db.session.commit()
                    else:
                        import secrets
                        token = secrets.token_hex(32)
                        session['session_token'] = token
                        db.session.add(ActiveSession(
                            session_token=token,
                            user_id=original_admin,
                            ip_address=get_client_ip(),
                            user_agent=request.user_agent.string[:300] if request.user_agent.string else None
                        ))
                        db.session.commit()
                flash("Returned to admin account.", "info")
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('home'))
        except Exception as e:
            app.logger.error(f"[Admin] Stop impersonate error: {e}", exc_info=True)
            return redirect(url_for('home'))

    # --- Config Management ---
    @app.route('/admin/config')
    @login_required
    @admin_required
    def admin_config():
        """View and edit configuration"""
        try:
            config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
            with open(config_path, "r") as f:
                current_config = yaml.safe_load(f) or {}

            # Mask sensitive values for display
            display_config = {}
            for section, values in current_config.items():
                if isinstance(values, dict):
                    display_config[section] = {}
                    for key, val in values.items():
                        if any(s in key.lower() for s in ['token', 'secret', 'password', 'key']):
                            display_config[section][key] = '***HIDDEN***'
                        else:
                            display_config[section][key] = val
                else:
                    display_config[section] = values

            return render_template('admin/config.html', display_config=display_config)
        except Exception as e:
            app.logger.error(f"[Admin] Config page error: {e}", exc_info=True)
            return "Error loading config", 500

    @app.route('/admin/config/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_config_update():
        """Update a config value"""
        try:
            section = request.form.get('section', '').strip()
            key = request.form.get('key', '').strip()
            value = request.form.get('value', '').strip()

            # Backward-compatible form: key can be passed as "section.key".
            if not section and '.' in key:
                section, key = key.split('.', 1)
                section = section.strip()
                key = key.strip()

            if not section or not key:
                return jsonify({"error": "Section and key are required"}), 400
            if any(s in key.lower() for s in ['token', 'secret', 'password', 'key']):
                return jsonify({"error": "Sensitive keys cannot be updated from the admin UI"}), 403
            section_schema = ADMIN_CONFIG_ALLOWLIST.get(section, {})
            expected_type = section_schema.get(key)
            if expected_type is None:
                return jsonify({"error": f"Config field {section}.{key} is not editable from admin UI"}), 400

            config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
            with open(config_path, "r") as f:
                current_config = yaml.safe_load(f) or {}

            if section not in current_config:
                current_config[section] = {}

            old_value = current_config.get(section, {}).get(key, '')
            value = coerce_config_value(value, expected_type)
            if section == "rate_limits":
                if key in {"max_requests", "window_seconds", "admin_multiplier"} and value <= 0:
                    return jsonify({"error": f"{section}.{key} must be greater than zero"}), 400
            current_config[section][key] = value

            with open(config_path, "w") as f:
                yaml.safe_dump(current_config, f, default_flow_style=False, sort_keys=False)

            old_safe = "***HIDDEN***" if any(s in key.lower() for s in ['token', 'secret', 'password', 'key']) else str(old_value)
            new_safe = "***HIDDEN***" if any(s in key.lower() for s in ['token', 'secret', 'password', 'key']) else str(value)
            log_admin_action(current_user.id, 'update_config', f"{section}.{key}", f"Changed from '{old_safe}' to '{new_safe}'", get_client_ip())
            return jsonify({"message": f"Config {section}.{key} updated"}), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Config update error: {e}", exc_info=True)
            return jsonify({"error": "Failed to update config"}), 500

    # --- Match/Tournament Management ---
    @app.route('/admin/matches')
    @login_required
    @admin_required
    def admin_matches():
        """View and manage matches and tournaments"""
        try:
            # Active in-memory matches
            active_matches = []
            with MATCH_INSTANCES_LOCK:
                for mid, instance in MATCH_INSTANCES.items():
                    data = getattr(instance, 'data', {})
                    home = data.get('team_home', '?').split('_')[0]
                    away = data.get('team_away', '?').split('_')[0]
                    created_at = data.get('created_at')
                    if created_at is None:
                        created_at = getattr(instance, 'created_at', None)
                    if isinstance(created_at, (int, float)):
                        created_display = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        created_display = created_at
                    active_matches.append({
                        'id': mid,
                        'teams': f"{home} vs {away}",
                        'user': data.get('created_by', '?'),
                        'created': created_display,
                    })

            # Recent DB matches
            recent_matches = db.session.query(DBMatch).order_by(DBMatch.date.desc()).limit(25).all()

            # Active tournaments
            active_tournaments = db.session.query(Tournament).filter_by(status='Active').all()
            completed_tournaments = db.session.query(Tournament).filter_by(status='Completed').order_by(Tournament.created_at.desc()).limit(10).all()

            return render_template('admin/matches.html',
                                   active_matches=active_matches,
                                   recent_matches=recent_matches,
                                   active_tournaments=active_tournaments,
                                   completed_tournaments=completed_tournaments)
        except Exception as e:
            app.logger.error(f"[Admin] Matches page error: {e}", exc_info=True)
            return "Error loading matches", 500

    @app.route('/admin/matches/<match_id>/terminate', methods=['POST'])
    @login_required
    @admin_required
    def admin_terminate_match(match_id):
        """Terminate an active in-memory match"""
        try:
            with MATCH_INSTANCES_LOCK:
                if match_id in MATCH_INSTANCES:
                    del MATCH_INSTANCES[match_id]
                    log_admin_action(current_user.id, 'terminate_match', match_id, 'Active match terminated', request.remote_addr)
                    return jsonify({"message": f"Match {match_id[:8]}... terminated"}), 200
                else:
                    return jsonify({"error": "Match not found in active instances"}), 404
        except Exception as e:
            return jsonify({"error": "Failed to terminate match"}), 500

    # --- Audit Log ---
    @app.route('/admin/audit-log')
    @login_required
    @admin_required
    def admin_audit_log():
        """View full admin audit log"""
        try:
            page = request.args.get('page', 1, type=int)
            per_page = 25
            offset = (page - 1) * per_page

            total = db.session.query(AdminAuditLog).count()
            entries = db.session.query(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).offset(offset).limit(per_page).all()

            total_pages = (total + per_page - 1) // per_page

            return render_template('admin/audit_log.html',
                                   entries=entries,
                                   page=page,
                                   total_pages=total_pages,
                                   total=total)
        except Exception as e:
            app.logger.error(f"[Admin] Audit log error: {e}", exc_info=True)
            return "Error loading audit log", 500

    # --- Maintenance Mode Toggle ---
    @app.route('/admin/maintenance/toggle', methods=['POST'])
    @login_required
    @admin_required
    def admin_toggle_maintenance():
        """Toggle maintenance mode on/off (admin only)."""
        next_state = not bool(get_maintenance_mode())
        _persist_maintenance_mode(next_state)
        state = 'enabled' if next_state else 'disabled'
        log_admin_action(current_user.id, 'toggle_maintenance', state, f'Maintenance mode {state}', request.remote_addr)
        app.logger.info(f"[Admin] Maintenance mode {state} by {current_user.id}")
        return jsonify({"maintenance_mode": bool(get_maintenance_mode()), "message": f"Maintenance mode {state}"}), 200

    @app.route('/admin/maintenance/status')
    @login_required
    @admin_required
    def admin_maintenance_status():
        """Get current maintenance mode status."""
        return jsonify({"maintenance_mode": bool(get_maintenance_mode())}), 200

    # --- Ban / Suspend Users ---
    @app.route('/admin/users/<user_email>/ban', methods=['POST'])
    @login_required
    @admin_required
    def admin_ban_user(user_email):
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.is_admin:
            return jsonify({"error": "Cannot ban an admin"}), 400
        reason = request.form.get('reason', '').strip() or 'No reason provided'
        duration = request.form.get('duration', '').strip()  # e.g. '7' for 7 days, empty=permanent
        user.is_banned = True
        user.ban_reason = reason
        if duration and duration.isdigit() and int(duration) > 0:
            user.banned_until = datetime.utcnow() + timedelta(days=int(duration))
        else:
            user.banned_until = None  # permanent
        db.session.commit()
        # Terminate their active sessions
        ActiveSession.query.filter_by(user_id=user_email).delete()
        db.session.commit()
        until_str = f"for {duration} days" if duration and duration.isdigit() else "permanently"
        log_admin_action(current_user.id, 'ban_user', user_email, f"Banned {until_str}: {reason}", request.remote_addr)
        return jsonify({"message": f"User {user_email} banned {until_str}"}), 200

    @app.route('/admin/users/<user_email>/unban', methods=['POST'])
    @login_required
    @admin_required
    def admin_unban_user(user_email):
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        user.is_banned = False
        user.banned_until = None
        user.ban_reason = None
        db.session.commit()
        log_admin_action(current_user.id, 'unban_user', user_email, 'Ban lifted', request.remote_addr)
        return jsonify({"message": f"User {user_email} unbanned"}), 200

    # --- Force Password Reset ---
    @app.route('/admin/users/<user_email>/force-reset', methods=['POST'])
    @login_required
    @admin_required
    def admin_force_password_reset(user_email):
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.is_admin:
            return jsonify({"error": "Cannot force reset on admin"}), 400
        user.force_password_reset = True
        db.session.commit()
        log_admin_action(current_user.id, 'force_password_reset', user_email, 'Flagged for password reset', request.remote_addr)
        return jsonify({"message": f"{user_email} will be forced to change password on next login"}), 200

    # --- Active Sessions ---
    @app.route('/admin/sessions')
    @login_required
    @admin_required
    def admin_sessions():
        sessions = ActiveSession.query.order_by(ActiveSession.last_active.desc()).all()
        return render_template('admin/sessions.html', sessions=sessions)

    @app.route('/admin/sessions/<int:session_id>/terminate', methods=['POST'])
    @login_required
    @admin_required
    def admin_terminate_session(session_id):
        s = db.session.get(ActiveSession, session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        target_user = s.user_id
        db.session.delete(s)
        db.session.commit()
        log_admin_action(current_user.id, 'terminate_session', target_user, f'Session {session_id} terminated', get_client_ip())
        return jsonify({"message": "Session terminated"}), 200

    @app.route('/admin/sessions/cleanup', methods=['POST'])
    @login_required
    @admin_required
    def admin_cleanup_sessions():
        cutoff = datetime.utcnow() - timedelta(days=7)
        count = ActiveSession.query.filter(ActiveSession.last_active < cutoff).delete()
        db.session.commit()
        log_admin_action(current_user.id, 'cleanup_sessions', None, f'Cleaned {count} stale sessions', get_client_ip())
        return jsonify({"message": f"Cleaned up {count} stale sessions"}), 200

    # --- Failed Login Tracker ---
    @app.route('/admin/failed-logins')
    @login_required
    @admin_required
    def admin_failed_logins():
        page = request.args.get('page', 1, type=int)
        per_page = 30
        query = FailedLoginAttempt.query.order_by(FailedLoginAttempt.timestamp.desc())
        total = query.count()
        entries = query.offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        # Top offending IPs (last 24h)
        cutoff_24h = datetime.utcnow() - timedelta(hours=24)
        top_ips = db.session.query(
            FailedLoginAttempt.ip_address,
            func.count(FailedLoginAttempt.id).label('count')
        ).filter(FailedLoginAttempt.timestamp >= cutoff_24h).group_by(
            FailedLoginAttempt.ip_address
        ).order_by(func.count(FailedLoginAttempt.id).desc()).limit(10).all()
        return render_template('admin/failed_logins.html',
                               entries=entries, page=page, total_pages=total_pages, total=total, top_ips=top_ips)

    @app.route('/admin/failed-logins/clear', methods=['POST'])
    @login_required
    @admin_required
    def admin_clear_failed_logins():
        count = FailedLoginAttempt.query.delete()
        db.session.commit()
        log_admin_action(current_user.id, 'clear_failed_logins', None, f'Cleared {count} entries', request.remote_addr)
        return jsonify({"message": f"Cleared {count} failed login records"}), 200

    # --- IP Blocklist ---
    @app.route('/admin/ip-blocklist')
    @login_required
    @admin_required
    def admin_ip_blocklist():
        blocked = BlockedIP.query.order_by(BlockedIP.blocked_at.desc()).all()
        return render_template('admin/ip_blocklist.html', blocked=blocked)

    @app.route('/admin/ip-blocklist/add', methods=['POST'])
    @login_required
    @admin_required
    def admin_block_ip():
        ip = request.form.get('ip_address', '').strip()
        reason = request.form.get('reason', '').strip() or 'No reason'
        if not ip:
            return jsonify({"error": "IP address required"}), 400
        ip_obj = parse_ip(ip)
        if not ip_obj:
            return jsonify({"error": "Invalid IP address format"}), 400
        normalized_ip = str(ip_obj)
        requester_ip = get_client_ip()
        if normalized_ip == requester_ip:
            return jsonify({"error": "Cannot block your current IP address"}), 400
        if BlockedIP.query.filter_by(ip_address=normalized_ip).first():
            return jsonify({"error": "IP already blocked"}), 400
        entry = BlockedIP(ip_address=normalized_ip, reason=reason, blocked_by=current_user.id)
        db.session.add(entry)
        db.session.commit()
        log_admin_action(current_user.id, 'block_ip', normalized_ip, reason, requester_ip)
        return jsonify({"message": f"IP {normalized_ip} blocked"}), 200

    @app.route('/admin/ip-blocklist/<int:block_id>/remove', methods=['POST'])
    @login_required
    @admin_required
    def admin_unblock_ip(block_id):
        entry = db.session.get(BlockedIP, block_id)
        if not entry:
            return jsonify({"error": "Entry not found"}), 404
        ip = entry.ip_address
        db.session.delete(entry)
        db.session.commit()
        log_admin_action(current_user.id, 'unblock_ip', ip, 'IP unblocked', get_client_ip())
        return jsonify({"message": f"IP {ip} unblocked"}), 200

    # --- Log Viewer ---
    @app.route('/admin/logs')
    @login_required
    @admin_required
    def admin_logs():
        log_path = os.path.join(PROJECT_ROOT, "logs", "execution.log")
        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                lines = ["Error reading log file"]
        # Show last 500 lines by default, most recent first
        lines = lines[-500:]
        lines.reverse()
        return render_template('admin/logs.html', lines=lines, total_lines=len(lines))

    @app.route('/admin/logs/download')
    @login_required
    @admin_required
    def admin_download_logs():
        """Download the full execution.log file."""
        log_path = os.path.join(PROJECT_ROOT, "logs", "execution.log")
        if not os.path.exists(log_path):
            return jsonify({"error": "Log file not found"}), 404
        return send_file(log_path, as_attachment=True, download_name='execution.log', mimetype='text/plain')

    # --- Rate Limit Config ---
    @app.route('/admin/rate-limits')
    @login_required
    @admin_required
    def admin_rate_limits():
        cfg = load_config()
        rl = cfg.get('rate_limits', {})
        return render_template('admin/rate_limits.html',
                               max_requests=rl.get('max_requests', 30),
                               window_seconds=rl.get('window_seconds', 10),
                               admin_multiplier=rl.get('admin_multiplier', 3),
                               login_limit=rl.get('login_limit', '10 per minute'))

    @app.route('/admin/rate-limits/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_update_rate_limits():
        try:
            try:
                max_requests = int(request.form.get('max_requests', 30))
                window_seconds = int(request.form.get('window_seconds', 10))
                admin_multiplier = int(request.form.get('admin_multiplier', 3))
            except (ValueError, TypeError):
                return jsonify({"error": "max_requests, window_seconds, and admin_multiplier must be integers"}), 400
            if max_requests < 1 or window_seconds < 1 or admin_multiplier < 1:
                return jsonify({"error": "Rate limit values must be greater than zero"}), 400

            config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
            cfg = {}
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
            cfg['rate_limits'] = {
                'max_requests': max_requests,
                'window_seconds': window_seconds,
                'admin_multiplier': admin_multiplier,
                'login_limit': request.form.get('login_limit', '10 per minute')
            }
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False)
            log_admin_action(current_user.id, 'update_rate_limits', None, json.dumps(cfg['rate_limits']), request.remote_addr)
            return jsonify({"message": "Rate limits updated (restart required for full effect)"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route('/admin/bot-defense')
    @login_required
    @admin_required
    def admin_bot_defense():
        return render_template('admin/bot_defense.html', settings=bot_defense_settings)

    @app.route('/admin/bot-defense/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_bot_defense_update():
        try:
            updated = {
                "enabled": str(request.form.get("enabled", "false")).lower() in {"1", "true", "yes", "on"},
                "base_difficulty": int(request.form.get("base_difficulty", bot_defense_settings["base_difficulty"])),
                "elevated_difficulty": int(request.form.get("elevated_difficulty", bot_defense_settings["elevated_difficulty"])),
                "high_difficulty": int(request.form.get("high_difficulty", bot_defense_settings["high_difficulty"])),
                "elevated_threshold": int(request.form.get("elevated_threshold", bot_defense_settings["elevated_threshold"])),
                "high_threshold": int(request.form.get("high_threshold", bot_defense_settings["high_threshold"])),
                "window_minutes": int(request.form.get("window_minutes", bot_defense_settings["window_minutes"])),
                "ttl_seconds": int(request.form.get("ttl_seconds", bot_defense_settings["ttl_seconds"])),
                "max_counter": int(request.form.get("max_counter", bot_defense_settings["max_counter"])),
                "max_iterations": int(request.form.get("max_iterations", bot_defense_settings["max_iterations"])),
                "trusted_ip_prefixes": (request.form.get("trusted_ip_prefixes", "") or "").strip(),
            }

            if updated["base_difficulty"] < 1 or updated["elevated_difficulty"] < updated["base_difficulty"] or updated["high_difficulty"] < updated["elevated_difficulty"]:
                return jsonify({"error": "Difficulty levels must be non-decreasing (base <= elevated <= high)"}), 400
            if updated["elevated_threshold"] < 1 or updated["high_threshold"] < updated["elevated_threshold"]:
                return jsonify({"error": "Thresholds must satisfy: 1 <= elevated <= high"}), 400
            if updated["window_minutes"] < 1 or updated["window_minutes"] > 120:
                return jsonify({"error": "Window minutes must be between 1 and 120"}), 400
            if updated["ttl_seconds"] < 30 or updated["ttl_seconds"] > 900:
                return jsonify({"error": "TTL must be between 30 and 900 seconds"}), 400
            if updated["max_counter"] < 10_000 or updated["max_counter"] > 100_000_000:
                return jsonify({"error": "Max counter out of allowed range"}), 400
            if updated["max_iterations"] < 10_000 or updated["max_iterations"] > 5_000_000:
                return jsonify({"error": "Max iterations out of allowed range"}), 400

            config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
            cfg = {}
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
            cfg["bot_defense"] = updated
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)

            bot_defense_settings.update(updated)
            details = json.dumps(updated)
            log_admin_action(current_user.id, "update_bot_defense", None, details, get_client_ip())
            return jsonify({"message": "Bot defense settings updated"}), 200
        except ValueError:
            return jsonify({"error": "All numeric fields must be valid integers"}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Bot defense update error: {e}", exc_info=True)
            return jsonify({"error": "Failed to update bot defense settings"}), 500

    # --- Global Team Browser ---
    @app.route('/admin/global-teams')
    @login_required
    @admin_required
    def admin_global_teams():
        page = request.args.get('page', 1, type=int)
        search = request.args.get('q', '').strip()
        per_page = 25
        query = DBTeam.query.filter(DBTeam.is_placeholder != True)
        if search:
            query = query.filter(
                db.or_(DBTeam.name.ilike(f'%{search}%'), DBTeam.user_id.ilike(f'%{search}%'), DBTeam.short_code.ilike(f'%{search}%'))
            )
        total = query.count()
        teams = query.order_by(DBTeam.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        return render_template('admin/global_teams.html',
                               teams=teams, page=page, total_pages=total_pages, total=total, search=search)

    # --- Global Match Browser ---
    @app.route('/admin/global-matches')
    @login_required
    @admin_required
    def admin_global_matches():
        page = request.args.get('page', 1, type=int)
        search = request.args.get('q', '').strip()
        per_page = 25
        query = DBMatch.query
        if search:
            query = query.filter(
                db.or_(DBMatch.user_id.ilike(f'%{search}%'), DBMatch.result_description.ilike(f'%{search}%'))
            )
        total = query.count()
        matches = query.order_by(DBMatch.date.desc()).offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        return render_template('admin/global_matches.html',
                               matches=matches, page=page, total_pages=total_pages, total=total, search=search)

    # --- DB Export ---
    @app.route('/admin/export')
    @login_required
    @admin_required
    def admin_export_page():
        return render_template('admin/export.html')

    @app.route('/admin/export/<table>/<fmt>')
    @login_required
    @admin_required
    def admin_export_data(table, fmt):
        if fmt not in ('csv', 'json', 'txt'):
            return jsonify({"error": "Invalid format. Use csv, json, or txt"}), 400
        table_map = {
            'users': DBUser,
            'teams': DBTeam,
            'players': DBPlayer,
            'matches': DBMatch,
            'tournaments': Tournament,
            'match_scorecards': MatchScorecard,
            'tournament_teams': TournamentTeam,
            'tournament_fixtures': TournamentFixture,
            'tournament_player_stats': TournamentPlayerStatsCache,
            'match_partnerships': MatchPartnership,
            'audit_log': AdminAuditLog,
            'failed_logins': FailedLoginAttempt,
            'blocked_ips': BlockedIP,
            'active_sessions': ActiveSession,
        }
        if table not in table_map:
            return jsonify({"error": f"Unknown table: {table}"}), 400
        model = table_map[table]
        rows = model.query.all()
        # Build list of dicts from columns
        columns = [c.name for c in model.__table__.columns]
        data = []
        for row in rows:
            d = {}
            for col in columns:
                val = getattr(row, col, None)
                if isinstance(val, datetime):
                    val = val.isoformat()
                d[col] = val
            data.append(d)
        log_admin_action(current_user.id, 'export_data', f'{table}.{fmt}', f'{len(data)} rows', request.remote_addr)
        if fmt == 'json':
            return Response(json.dumps(data, indent=2, default=str),
                            mimetype='application/json',
                            headers={'Content-Disposition': f'attachment; filename={table}_export.json'})
        elif fmt == 'csv':
            import io, csv
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(data)
            return Response(output.getvalue(),
                            mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={table}_export.csv'})
        else:  # txt
            lines = []
            for d in data:
                lines.append(' | '.join(str(d.get(c, '')) for c in columns))
            header = ' | '.join(columns)
            sep = '-' * len(header)
            content = header + '\n' + sep + '\n' + '\n'.join(lines)
            return Response(content,
                            mimetype='text/plain',
                            headers={'Content-Disposition': f'attachment; filename={table}_export.txt'})

    @app.route('/admin/export/all/<fmt>')
    @login_required
    @admin_required
    def admin_export_all(fmt):
        """Export all tables in a single ZIP file."""
        if fmt not in ('csv', 'json', 'txt'):
            return jsonify({"error": "Invalid format"}), 400
        import io, csv, zipfile
        all_tables = {
            'users': DBUser, 'teams': DBTeam, 'players': DBPlayer,
            'matches': DBMatch, 'tournaments': Tournament,
            'match_scorecards': MatchScorecard, 'tournament_teams': TournamentTeam,
            'tournament_fixtures': TournamentFixture, 'tournament_player_stats': TournamentPlayerStatsCache,
            'match_partnerships': MatchPartnership, 'audit_log': AdminAuditLog,
            'failed_logins': FailedLoginAttempt, 'blocked_ips': BlockedIP, 'active_sessions': ActiveSession,
        }
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for tbl_name, model in all_tables.items():
                columns = [c.name for c in model.__table__.columns]
                rows = model.query.all()
                data = []
                for row in rows:
                    d = {}
                    for col in columns:
                        val = getattr(row, col, None)
                        if isinstance(val, datetime):
                            val = val.isoformat()
                        d[col] = val
                    data.append(d)
                if fmt == 'json':
                    content = json.dumps(data, indent=2, default=str)
                    zf.writestr(f'{tbl_name}.json', content)
                elif fmt == 'csv':
                    output = io.StringIO()
                    writer = csv.DictWriter(output, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(data)
                    zf.writestr(f'{tbl_name}.csv', output.getvalue())
                else:
                    header = ' | '.join(columns)
                    sep = '-' * len(header)
                    lines = [' | '.join(str(d.get(c, '')) for c in columns) for d in data]
                    zf.writestr(f'{tbl_name}.txt', header + '\n' + sep + '\n' + '\n'.join(lines))
        zip_buffer.seek(0)
        log_admin_action(current_user.id, 'export_all', fmt, f'All tables exported as {fmt}', request.remote_addr)
        return Response(zip_buffer.getvalue(),
                        mimetype='application/zip',
                        headers={'Content-Disposition': f'attachment; filename=simcricketx_export_{fmt}.zip'})

    # --- Scheduled Tasks Dashboard ---
    @app.route('/admin/scheduled-tasks')
    @login_required
    @admin_required
    def admin_scheduled_tasks():
        tasks = []
        # Backup scheduler
        tasks.append({
            'name': 'Database Backup',
            'status': 'Active' if _backup_scheduler_started else 'Inactive',
            'interval': '24 hours',
            'description': 'Automatic database backup to data/backups/',
            'last_run': _get_last_backup_time(),
        })
        # Cleanup task
        tasks.append({
            'name': 'Match Instance Cleanup',
            'status': 'Active',
            'interval': '6 hours',
            'description': 'Removes old in-memory match instances and orphaned JSON files',
            'last_run': None,
        })
        # Backup retention
        tasks.append({
            'name': 'Backup Retention Cleanup',
            'status': 'Active',
            'interval': 'On each backup',
            'description': 'Removes backups older than 7 days',
            'last_run': None,
        })
        # Session cleanup hint
        tasks.append({
            'name': 'Stale Session Cleanup',
            'status': 'Manual',
            'interval': 'On demand',
            'description': 'Clean up sessions inactive for 7+ days (via Active Sessions page)',
            'last_run': None,
        })
        return render_template('admin/scheduled_tasks.html', tasks=tasks)

    def _get_last_backup_time():
        """Get timestamp of the most recent backup file."""
        backup_dir = os.path.join(PROJECT_ROOT, "data", "backups")
        if not os.path.isdir(backup_dir):
            return None
        files = [f for f in os.listdir(backup_dir) if f.endswith('.db')]
        if not files:
            return None
        files.sort(key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)), reverse=True)
        mtime = os.path.getmtime(os.path.join(backup_dir, files[0]))
        return datetime.fromtimestamp(mtime)


    @app.route('/admin/files')
    @login_required
    @admin_required
    def admin_files():
        return render_template('admin/files.html')

    @app.route('/admin/api/files')
    @login_required
    @admin_required
    def admin_api_files():
        base_dir = os.path.abspath(PROJECT_ROOT)
        req_path = request.args.get('path', '').replace('\\', '/')
        # Resolve target path securely
        target_path = os.path.abspath(os.path.join(base_dir, req_path))
        
        # Security check: Ensure target path is within base_dir
        if not is_path_within_base(base_dir, target_path):
            return jsonify({'error': 'Access denied: Cannot traverse outside project root'}), 403
            
        if not os.path.exists(target_path):
             return jsonify({'error': 'Path not found'}), 404
             
        if not os.path.isdir(target_path):
            return jsonify({'error': 'Path is not a directory'}), 400

        items = []
        try:
            with os.scandir(target_path) as entries:
                for entry in entries:
                    try:
                        stats = entry.stat()
                        # Format modification time
                        mtime = datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                        
                        is_dir = entry.is_dir()
                        # Calculate relative path from project root
                        rel_path = os.path.relpath(entry.path, base_dir).replace('\\', '/')
                        if rel_path == '.':
                            rel_path = ''

                        items.append({
                            'name': entry.name,
                            'path': rel_path,
                            'type': 'directory' if is_dir else 'file',
                            'size': stats.st_size,
                            'modified': mtime,
                            'is_dir': is_dir
                        })
                    except OSError:
                        continue # Skip inaccessible items
        except PermissionError:
             return jsonify({'error': 'Permission denied'}), 403

        # Sort: Directories first, then files (alphabetical)
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))

        # Determine parent directory for navigation
        current_rel = os.path.relpath(target_path, base_dir).replace('\\', '/')
        if current_rel == '.':
            current_rel = ''
            
        parent_rel = ''
        if current_rel:
            parent_rel = os.path.dirname(current_rel)

        return jsonify({
            'items': items,
            'current_path': current_rel,
            'parent_path': parent_rel
        })

    @app.route('/admin/api/files', methods=['DELETE'])
    @login_required
    @admin_required
    def admin_api_delete_file():
        file_path = request.args.get('path', '')
        if not file_path and request.is_json:
            payload = request.get_json(silent=True) or {}
            files = payload.get('files') if isinstance(payload, dict) else None
            if isinstance(files, list) and files:
                # Backward-compatible support for bulk-delete payloads from older clients/tests.
                file_path = str(files[0] or '').strip()
        if not file_path:
            return jsonify({'error': 'Path is required'}), 400

        base_dir = os.path.abspath(PROJECT_ROOT)
        target_path = os.path.abspath(os.path.join(base_dir, file_path))

        # Security check
        if not is_path_within_base(base_dir, target_path):
            return jsonify({'error': 'Access denied'}), 403
        
        if not os.path.exists(target_path):
            return jsonify({'error': 'File not found'}), 404
            
        if os.path.isdir(target_path):
            return jsonify({'error': 'Deleting directories is not supported'}), 400

        try:
            os.remove(target_path)
            app.logger.info(f"[FileExplorer] Admin {current_user.id} deleted file: {file_path}")
            log_admin_action(current_user.id, 'delete_file', file_path, 'File deleted from admin explorer', get_client_ip())
            return jsonify({'success': True})
        except Exception as e:
            app.logger.error(f"[FileExplorer] Error deleting file {file_path}: {e}")
            return jsonify({'error': str(e)}), 500


    # =========================================================================
    # NEW FEATURES (13 additions)
    # =========================================================================

    # --- 1. Promote / Demote Admin ---
    @app.route('/admin/users/<user_email>/toggle-admin', methods=['POST'])
    @login_required
    @admin_required
    def admin_toggle_admin(user_email):
        """Promote a regular user to admin or demote an admin to user."""
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.id == current_user.id:
            return jsonify({"error": "Cannot change your own admin status"}), 400
        user.is_admin = not user.is_admin
        db.session.commit()
        action = 'promote_admin' if user.is_admin else 'demote_admin'
        log_admin_action(current_user.id, action, user_email, f'Admin status set to {user.is_admin}', get_client_ip())
        status = 'promoted to admin' if user.is_admin else 'demoted to user'
        return jsonify({"message": f"{user_email} {status}", "is_admin": user.is_admin}), 200

    # --- 2. Create User (admin only) ---
    @app.route('/admin/users/create', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_create_user():
        """Admin creates a new user account."""
        if request.method == 'GET':
            return render_template('admin/create_user.html')
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        display_name = request.form.get('display_name', '').strip()
        make_admin = request.form.get('make_admin') == '1'
        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400
        ok = register_user(email, password, display_name or None)
        if not ok:
            return jsonify({"error": "Failed to create user. Email may already be in use or password too weak."}), 400
        if make_admin:
            new_user = db.session.get(DBUser, email)
            if new_user:
                new_user.is_admin = True
                db.session.commit()
        log_admin_action(current_user.id, 'create_user', email, f'Admin-created user, admin={make_admin}', get_client_ip())
        return jsonify({"message": f"User {email} created successfully"}), 200

    # --- 3a. Export user data as JSON (admin for any user) ---
    @app.route('/admin/users/<user_email>/export')
    @login_required
    @admin_required
    def admin_export_user(user_email):
        """Export a single user's complete data as JSON."""
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        data = _build_user_export(user)
        log_admin_action(current_user.id, 'export_user', user_email, 'Full user data exported as JSON', get_client_ip())
        return Response(
            json.dumps(data, indent=2, default=str),
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename=user_{user_email}_export.json'}
        )

    # --- 3b. Self-service data export ---
    @app.route('/export/my-data')
    @login_required
    def export_my_data():
        """Authenticated user exports their own data."""
        user = db.session.get(DBUser, current_user.id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        data = _build_user_export(user)
        return Response(
            json.dumps(data, indent=2, default=str),
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename=my_data_export.json'}
        )

    def _build_user_export(user):
        """Build a complete JSON-serialisable dict of a user's data."""
        teams = DBTeam.query.filter_by(user_id=user.id).all()
        team_data = []
        for t in teams:
            players = DBPlayer.query.filter_by(team_id=t.id).all()
            team_data.append({
                'id': t.id,
                'name': t.name,
                'short_code': t.short_code,
                'home_ground': t.home_ground,
                'created_at': t.created_at.isoformat() if t.created_at else None,
                'players': [{'id': p.id, 'name': p.name, 'role': p.role,
                             'batting_rating': p.batting_rating, 'bowling_rating': p.bowling_rating} for p in players],
            })
        matches = DBMatch.query.filter_by(user_id=user.id).order_by(DBMatch.date.desc()).limit(200).all()
        tournaments = Tournament.query.filter_by(user_id=user.id).all()
        login_hist = LoginHistory.query.filter_by(user_id=user.id).order_by(LoginHistory.timestamp.desc()).limit(100).all()
        return {
            'exported_at': datetime.utcnow().isoformat() + 'Z',
            'user': {
                'email': user.id,
                'display_name': user.display_name,
                'created_at': user.created_at.isoformat() if user.created_at else None,
                'last_login': user.last_login.isoformat() if user.last_login else None,
                'is_admin': user.is_admin,
            },
            'teams': team_data,
            'matches': [{
                'id': m.id, 'date': m.date.isoformat() if m.date else None,
                'result': m.result_description, 'venue': m.venue,
            } for m in matches],
            'tournaments': [{'id': t.id, 'name': t.name, 'status': t.status} for t in tournaments],
            'login_history': [{'timestamp': lh.timestamp.isoformat(), 'ip': lh.ip_address, 'event': lh.event} for lh in login_hist],
        }

    # --- 4. Per-user data wipe (admin only) ---
    @app.route('/admin/users/<user_email>/wipe-data', methods=['POST'])
    @login_required
    @admin_required
    def admin_wipe_user_data(user_email):
        """Delete all teams, players, matches, and tournaments for a user without deleting the account."""
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.is_admin:
            return jsonify({"error": "Cannot wipe data for an admin account"}), 400
        try:
            # Delete matches (cascades scorecards/partnerships)
            match_count = DBMatch.query.filter_by(user_id=user_email).delete()
            # Delete tournaments (cascades fixtures/teams/stats)
            tourn_count = Tournament.query.filter_by(user_id=user_email).delete()
            # Delete teams (cascades players)
            team_count = DBTeam.query.filter_by(user_id=user_email).delete()
            db.session.commit()
            log_admin_action(current_user.id, 'wipe_user_data', user_email,
                             f'Wiped {match_count} matches, {tourn_count} tournaments, {team_count} teams', get_client_ip())
            return jsonify({"message": f"Wiped data for {user_email}: {team_count} teams, {match_count} matches, {tourn_count} tournaments"}), 200
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"[Admin] Wipe data error for {user_email}: {e}", exc_info=True)
            return jsonify({"error": "Failed to wipe data"}), 500

    # --- 5. Delete DB match ---
    @app.route('/admin/matches/<match_id>/delete-db', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_db_match(match_id):
        """Permanently delete a match record from the database."""
        match = db.session.get(DBMatch, match_id)
        if not match:
            return jsonify({"error": "Match not found"}), 404
        owner = match.user_id
        db.session.delete(match)
        db.session.commit()
        log_admin_action(current_user.id, 'delete_match', match_id, f'Owned by {owner}', get_client_ip())
        return jsonify({"message": f"Match {match_id[:8]}... deleted from database"}), 200

    # --- 6a. Delete team ---
    @app.route('/admin/teams/<int:team_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_team(team_id):
        """Delete a team and all its players."""
        team = db.session.get(DBTeam, team_id)
        if not team:
            return jsonify({"error": "Team not found"}), 404
        name = team.name
        owner = team.user_id
        db.session.delete(team)
        db.session.commit()
        log_admin_action(current_user.id, 'delete_team', f'{name} (id={team_id})', f'Owner: {owner}', get_client_ip())
        return jsonify({"message": f"Team '{name}' deleted"}), 200

    # --- 6b. Delete player ---
    @app.route('/admin/players/<int:player_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_player(player_id):
        """Delete a single player."""
        player = db.session.get(DBPlayer, player_id)
        if not player:
            return jsonify({"error": "Player not found"}), 404
        name = player.name
        team_id = player.team_id
        db.session.delete(player)
        db.session.commit()
        log_admin_action(current_user.id, 'delete_player', f'{name} (id={player_id})', f'Team id: {team_id}', get_client_ip())
        return jsonify({"message": f"Player '{name}' deleted"}), 200

    # --- 7a. Delete tournament ---
    @app.route('/admin/tournaments/<int:tournament_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_tournament(tournament_id):
        """Permanently delete a tournament and all its data."""
        tourn = db.session.get(Tournament, tournament_id)
        if not tourn:
            return jsonify({"error": "Tournament not found"}), 404
        name = tourn.name
        owner = tourn.user_id
        db.session.delete(tourn)
        db.session.commit()
        log_admin_action(current_user.id, 'delete_tournament', f'{name} (id={tournament_id})', f'Owner: {owner}', get_client_ip())
        return jsonify({"message": f"Tournament '{name}' deleted"}), 200

    # --- 7b. Reset tournament (clear fixtures/results, set back to Active/league stage) ---
    @app.route('/admin/tournaments/<int:tournament_id>/reset', methods=['POST'])
    @login_required
    @admin_required
    def admin_reset_tournament(tournament_id):
        """Reset a tournament: delete all fixtures and reset standings to zero."""
        tourn = db.session.get(Tournament, tournament_id)
        if not tourn:
            return jsonify({"error": "Tournament not found"}), 404
        try:
            TournamentFixture.query.filter_by(tournament_id=tournament_id).delete()
            TournamentPlayerStatsCache.query.filter_by(tournament_id=tournament_id).delete()
            # Reset team standings
            TournamentTeam.query.filter_by(tournament_id=tournament_id).update({
                'played': 0, 'won': 0, 'lost': 0, 'tied': 0, 'no_result': 0,
                'points': 0, 'runs_scored': 0, 'runs_conceded': 0,
                'overs_faced': '0.0', 'overs_bowled': '0.0', 'net_run_rate': 0.0
            })
            tourn.status = 'Active'
            tourn.current_stage = 'league'
            db.session.commit()
            log_admin_action(current_user.id, 'reset_tournament', tourn.name, 'All fixtures and standings cleared', get_client_ip())
            return jsonify({"message": f"Tournament '{tourn.name}' reset to initial state"}), 200
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"[Admin] Reset tournament error: {e}", exc_info=True)
            return jsonify({"error": "Failed to reset tournament"}), 500

    # --- 8. Real-time dashboard SSE stream ---
    @app.route('/admin/dashboard/stream')
    @login_required
    @admin_required
    def admin_dashboard_stream():
        """Server-Sent Events stream for live dashboard stats."""
        @stream_with_context
        def generate():
            import time as time_mod
            while True:
                try:
                    total_users = DBUser.query.count()
                    total_teams = DBTeam.query.count()
                    total_matches = DBMatch.query.count()
                    total_tournaments = Tournament.query.count()
                    active_sessions = ActiveSession.query.count()
                    with MATCH_INSTANCES_LOCK:
                        live_matches = len(MATCH_INSTANCES)
                    cutoff_7d = datetime.utcnow() - timedelta(days=7)
                    active_users_7d = db.session.query(func.count(ActiveSession.user_id.distinct())).filter(
                        ActiveSession.last_active >= cutoff_7d
                    ).scalar() or 0
                    db_path = os.path.join(basedir, 'cricket_sim.db')
                    db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2) if os.path.exists(db_path) else 0
                    payload = json.dumps({
                        'total_users': total_users,
                        'total_teams': total_teams,
                        'total_matches': total_matches,
                        'total_tournaments': total_tournaments,
                        'active_sessions': active_sessions,
                        'live_matches': live_matches,
                        'active_users_7d': active_users_7d,
                        'db_size_mb': db_size_mb,
                    })
                    yield f"data: {payload}\n\n"
                except Exception:
                    yield "data: {}\n\n"
                # Keep tests deterministic and avoid hanging teardown on long-lived streams.
                if app.testing:
                    break
                time_mod.sleep(10)
        return Response(generate(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    # --- 9. IP Whitelist Mode ---
    @app.route('/admin/ip-whitelist')
    @login_required
    @admin_required
    def admin_ip_whitelist():
        """Manage IP whitelist entries."""
        entries = IPWhitelistEntry.query.order_by(IPWhitelistEntry.added_at.desc()).all()
        whitelist_on = bool(get_whitelist_mode())
        return render_template('admin/ip_whitelist.html', entries=entries, whitelist_on=whitelist_on)

    @app.route('/admin/ip-whitelist/toggle', methods=['POST'])
    @login_required
    @admin_required
    def admin_toggle_whitelist_mode():
        """Toggle IP whitelist mode on/off (stored in-process via the getter/setter closure)."""
        import sys
        current_state = bool(get_whitelist_mode())
        new_state = not current_state
        # Update the global in whatever module owns IP_WHITELIST_MODE
        for mod_name in ('app', '__main__'):
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, 'IP_WHITELIST_MODE'):
                mod.IP_WHITELIST_MODE = new_state
                break
        state_str = 'enabled' if new_state else 'disabled'
        log_admin_action(current_user.id, 'toggle_ip_whitelist', state_str, f'IP whitelist mode {state_str}', get_client_ip())
        return jsonify({"whitelist_mode": new_state, "message": f"IP whitelist mode {state_str}"}), 200

    @app.route('/admin/ip-whitelist/add', methods=['POST'])
    @login_required
    @admin_required
    def admin_add_whitelist_ip():
        """Add an IP to the whitelist."""
        ip = request.form.get('ip_address', '').strip()
        label = request.form.get('label', '').strip()
        if not ip:
            return jsonify({"error": "IP address required"}), 400
        ip_obj = parse_ip(ip)
        if not ip_obj:
            return jsonify({"error": "Invalid IP address format"}), 400
        normalized = str(ip_obj)
        if IPWhitelistEntry.query.filter_by(ip_address=normalized).first():
            return jsonify({"error": "IP already in whitelist"}), 400
        db.session.add(IPWhitelistEntry(ip_address=normalized, label=label or None, added_by=current_user.id))
        db.session.commit()
        log_admin_action(current_user.id, 'add_whitelist_ip', normalized, label or 'no label', get_client_ip())
        return jsonify({"message": f"IP {normalized} added to whitelist"}), 200

    @app.route('/admin/ip-whitelist/<int:entry_id>/remove', methods=['POST'])
    @login_required
    @admin_required
    def admin_remove_whitelist_ip(entry_id):
        """Remove an IP from the whitelist."""
        entry = db.session.get(IPWhitelistEntry, entry_id)
        if not entry:
            return jsonify({"error": "Entry not found"}), 404
        ip = entry.ip_address
        db.session.delete(entry)
        db.session.commit()
        log_admin_action(current_user.id, 'remove_whitelist_ip', ip, 'Removed from whitelist', get_client_ip())
        return jsonify({"message": f"IP {ip} removed from whitelist"}), 200

    # --- 10. User Login History ---
    @app.route('/admin/users/<user_email>/login-history')
    @login_required
    @admin_required
    def admin_user_login_history(user_email):
        """View full login history for a user."""
        user = db.session.get(DBUser, user_email)
        if not user:
            return "User not found", 404
        page = request.args.get('page', 1, type=int)
        per_page = 30
        total = LoginHistory.query.filter_by(user_id=user_email).count()
        history = LoginHistory.query.filter_by(user_id=user_email).order_by(
            LoginHistory.timestamp.desc()
        ).offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        return render_template('admin/user_login_history.html',
                               user=user, history=history,
                               page=page, total_pages=total_pages, total=total)

    # --- 11. Read-only SQL Runner ---
    @app.route('/admin/sql', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_sql_runner():
        """Execute read-only SQL queries against the database."""
        result_cols = []
        result_rows = []
        error = None
        query_sql = ''
        if request.method == 'POST':
            query_sql = request.form.get('query', '').strip()
            # Enforce read-only: only allow SELECT statements
            normalized = query_sql.upper().lstrip()
            if not normalized.startswith('SELECT'):
                error = "Only SELECT statements are allowed."
            else:
                try:
                    from sqlalchemy import text as sa_text
                    with db.engine.connect() as conn:
                        result = conn.execute(sa_text(query_sql))
                        result_cols = list(result.keys())
                        result_rows = [list(row) for row in result.fetchmany(500)]
                    log_admin_action(current_user.id, 'sql_query', None, query_sql[:200], get_client_ip())
                except Exception as e:
                    error = str(e)
        return render_template('admin/sql_runner.html',
                               query=query_sql, result_cols=result_cols,
                               result_rows=result_rows, error=error)

    # --- 12a. Per-user analytics (admin view) ---
    @app.route('/admin/users/<user_email>/analytics')
    @login_required
    @admin_required
    def admin_user_analytics(user_email):
        """Detailed analytics for a single user."""
        user = db.session.get(DBUser, user_email)
        if not user:
            return "User not found", 404
        analytics = _build_user_analytics(user_email)
        return render_template('admin/user_analytics.html', user=user, analytics=analytics)

    # --- 12b. Self-service analytics ---
    @app.route('/my-analytics')
    @login_required
    def my_analytics():
        """User views their own analytics dashboard."""
        user = db.session.get(DBUser, current_user.id)
        analytics = _build_user_analytics(current_user.id)
        return render_template('my_analytics.html', user=user, analytics=analytics)

    def _build_user_analytics(user_id):
        """Build analytics dict for a user."""
        teams = DBTeam.query.filter_by(user_id=user_id).all()
        team_ids = [t.id for t in teams]
        total_players = DBPlayer.query.filter(DBPlayer.team_id.in_(team_ids)).count() if team_ids else 0
        total_matches = DBMatch.query.filter_by(user_id=user_id).count()
        total_tournaments = Tournament.query.filter_by(user_id=user_id).count()
        # Win rate
        wins = DBMatch.query.filter(
            DBMatch.user_id == user_id,
            DBMatch.winner_team_id.in_(team_ids)
        ).count() if team_ids else 0
        win_rate = round(wins / total_matches * 100, 1) if total_matches else 0
        # Match format breakdown
        format_counts = db.session.query(DBMatch.match_format, func.count(DBMatch.id)).filter_by(
            user_id=user_id
        ).group_by(DBMatch.match_format).all()
        # Monthly matches (last 6 months)
        six_months_ago = datetime.utcnow() - timedelta(days=180)
        monthly = db.session.query(
            func.strftime('%Y-%m', DBMatch.date).label('month'),
            func.count(DBMatch.id).label('count')
        ).filter(DBMatch.user_id == user_id, DBMatch.date >= six_months_ago).group_by('month').order_by('month').all()
        # Login activity (last 30 logins)
        login_hist = LoginHistory.query.filter_by(user_id=user_id).order_by(
            LoginHistory.timestamp.desc()
        ).limit(30).all()
        # Top scoring player (by total_runs)
        top_batsman = None
        top_bowler = None
        if team_ids:
            top_batsman = DBPlayer.query.filter(DBPlayer.team_id.in_(team_ids)).order_by(
                DBPlayer.total_runs.desc()
            ).first()
            top_bowler = DBPlayer.query.filter(DBPlayer.team_id.in_(team_ids)).order_by(
                DBPlayer.total_wickets.desc()
            ).first()
        return {
            'teams_count': len(teams),
            'players_count': total_players,
            'matches_count': total_matches,
            'tournaments_count': total_tournaments,
            'wins': wins,
            'win_rate': win_rate,
            'format_breakdown': [{'format': f or 'Unknown', 'count': c} for f, c in format_counts],
            'monthly_matches': [{'month': m, 'count': c} for m, c in monthly],
            'login_history': login_hist,
            'top_batsman': top_batsman,
            'top_bowler': top_bowler,
        }

    # --- 13. Retention Cohorts ---
    @app.route('/admin/retention')
    @login_required
    @admin_required
    def admin_retention():
        """Retention cohort analysis by signup month."""
        # Group users by signup month
        cohorts_raw = db.session.query(
            func.strftime('%Y-%m', DBUser.created_at).label('cohort'),
            func.count(DBUser.id).label('signups')
        ).group_by('cohort').order_by('cohort').all()

        cutoff_30d = datetime.utcnow() - timedelta(days=30)
        cutoff_7d = datetime.utcnow() - timedelta(days=7)

        cohorts = []
        for cohort_month, signups in cohorts_raw:
            if not cohort_month:
                continue
            # Users in this cohort
            cohort_users = db.session.query(DBUser.id).filter(
                func.strftime('%Y-%m', DBUser.created_at) == cohort_month
            ).subquery()
            # Active in last 30 days (had a login in last 30d via login_history)
            active_30d = db.session.query(func.count(LoginHistory.user_id.distinct())).filter(
                LoginHistory.user_id.in_(cohort_users),
                LoginHistory.timestamp >= cutoff_30d,
                LoginHistory.event == 'login'
            ).scalar() or 0
            # Active in last 7 days
            active_7d = db.session.query(func.count(LoginHistory.user_id.distinct())).filter(
                LoginHistory.user_id.in_(cohort_users),
                LoginHistory.timestamp >= cutoff_7d,
                LoginHistory.event == 'login'
            ).scalar() or 0
            # Users who have played at least one match
            played = db.session.query(func.count(DBMatch.user_id.distinct())).filter(
                DBMatch.user_id.in_(cohort_users)
            ).scalar() or 0
            cohorts.append({
                'month': cohort_month,
                'signups': signups,
                'active_30d': active_30d,
                'active_7d': active_7d,
                'played_match': played,
                'retention_30d': round(active_30d / signups * 100, 1) if signups else 0,
                'retention_7d': round(active_7d / signups * 100, 1) if signups else 0,
                'activation_rate': round(played / signups * 100, 1) if signups else 0,
            })

        # Overall stats
        total_users = DBUser.query.count()
        active_users_30d = db.session.query(func.count(LoginHistory.user_id.distinct())).filter(
            LoginHistory.timestamp >= cutoff_30d, LoginHistory.event == 'login'
        ).scalar() or 0

        return render_template('admin/retention.html',
                               cohorts=cohorts,
                               total_users=total_users,
                               active_users_30d=active_users_30d)

    # =========================================================================
    # END NEW FEATURES
    # =========================================================================

    # Register minimal fallback admin routes if any expected endpoints are missing.
    # This prevents template/url build failures when a partial app initialization occurs.
    def _register_admin_fallback(endpoint_name, route_path):
        if endpoint_name in app.view_functions:
            return

        def _missing_admin_route():
            app.logger.warning(f"[Admin] Fallback route hit for missing endpoint: {endpoint_name}")
            flash(f"{endpoint_name.replace('_', ' ').title()} is unavailable in this process.", "warning")
            return redirect('/admin/dashboard')

        app.add_url_rule(
            route_path,
            endpoint=endpoint_name,
            view_func=login_required(admin_required(_missing_admin_route))
        )

    _register_admin_fallback('admin_dashboard', '/admin/dashboard')
    _register_admin_fallback('admin_users', '/admin/users')
    _register_admin_fallback('admin_activity', '/admin/activity')
    _register_admin_fallback('admin_health', '/admin/health')
    _register_admin_fallback('admin_matches', '/admin/matches')
    _register_admin_fallback('admin_database_stats', '/admin/database/stats')
    _register_admin_fallback('admin_backups', '/admin/backups')
    _register_admin_fallback('admin_restore_center', '/admin/restore-center')
    _register_admin_fallback('admin_bot_defense', '/admin/bot-defense')
    _register_admin_fallback('admin_search', '/admin/search')
    _register_admin_fallback('admin_config', '/admin/config')
    _register_admin_fallback('admin_audit_log', '/admin/audit-log')

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_root():
        return redirect('/admin/dashboard')

    @app.route('/admin/<path:subpath>')
    @login_required
    @admin_required
    def admin_route_catchall(subpath):
        requested = f"/admin/{subpath}"
        known_routes = sorted([r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/admin')])
        app.logger.error(
            f"[Admin] Unmatched admin route: {requested}. Known admin routes: {known_routes}. File: {os.path.abspath(__file__)}"
        )

        # Avoid redirect loops if dashboard route itself is unavailable.
        if requested != '/admin/dashboard' and '/admin/dashboard' in known_routes:
            return redirect('/admin/dashboard')

        return (
            "Admin route unavailable in this running process.\n"
            f"Requested: {requested}\n"
            f"Running file: {os.path.abspath(__file__)}\n"
            f"Known admin routes: {', '.join(known_routes)}",
            503,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

