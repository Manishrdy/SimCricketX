"""
Test suite for Admin routes
Tests routes defined in routes/admin_routes.py

This file tests admin-only routes including:
- Dashboard and monitoring
- User management
- Database operations
- System configuration
- Audit logs
- Security features
"""

import pytest
from datetime import datetime
from database.models import (
    User,
    Team as DBTeam,
    TeamProfile as DBTeamProfile,
    Player as DBPlayer,
    AdminAuditLog,
    BlockedIP,
    FailedLoginAttempt,
)
from app import db


class TestAdminAccess:
    """Tests for admin access control."""

    def test_admin_dashboard_as_admin(self, admin_client):
        """Test accessing admin dashboard as admin."""
        response = admin_client.get("/admin/dashboard")
        assert response.status_code == 200

    def test_admin_dashboard_as_regular_user(self, authenticated_client):
        """Test accessing admin dashboard as regular user is denied."""
        response = authenticated_client.get("/admin/dashboard")
        assert response.status_code in [403, 302]

    def test_admin_dashboard_unauthenticated(self, client):
        """Test accessing admin dashboard without login is denied."""
        response = client.get("/admin/dashboard")
        assert response.status_code in [302, 401, 403]

    def test_admin_route_redirect(self, admin_client):
        """Test admin root redirect."""
        response = admin_client.get("/admin")
        assert response.status_code in [200, 302]


class TestUserManagement:
    """Tests for admin user management routes."""

    def test_admin_users_list(self, admin_client):
        """Test viewing all users list."""
        response = admin_client.get("/admin/users")
        assert response.status_code == 200

    def test_admin_users_list_non_admin(self, authenticated_client):
        """Test accessing users list as non-admin is denied."""
        response = authenticated_client.get("/admin/users")
        assert response.status_code in [403, 302]

    def test_admin_user_detail(self, admin_client, regular_user):
        """Test viewing specific user details."""
        response = admin_client.get(f"/admin/users/{regular_user.email}")
        assert response.status_code == 200

    def test_admin_user_360_view(self, admin_client, regular_user):
        """Test 360 view of user."""
        response = admin_client.get(f"/admin/users/{regular_user.email}/360")
        assert response.status_code == 200

    def test_admin_change_user_email(self, admin_client, regular_user):
        """Test changing user email as admin."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/change-email",
            data={"new_email": "newemail@example.com"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_reset_user_password(self, admin_client, regular_user):
        """Test resetting user password as admin."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/reset-password",
            data={"new_password": "NewPassword123!"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_delete_user(self, admin_client, regular_user):
        """Test deleting user as admin."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/delete",
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_ban_user(self, admin_client, regular_user):
        """Test banning user."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/ban",
            data={"reason": "Test ban"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_unban_user(self, admin_client, banned_user):
        """Test unbanning user."""
        response = admin_client.post(
            f"/admin/users/{banned_user.email}/unban",
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_force_password_reset(self, admin_client, regular_user):
        """Test forcing user to reset password."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/force-reset",
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_toggle_admin_flag(self, admin_client, regular_user):
        """Test toggling admin flag for user."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/toggle-admin",
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_create_user_get(self, admin_client):
        """Test accessing create user page."""
        response = admin_client.get("/admin/users/create")
        assert response.status_code == 200

    def test_admin_create_user_post(self, admin_client):
        """Test creating user as admin."""
        response = admin_client.post(
            "/admin/users/create",
            data={
                "email": "admincreatec@example.com",
                "password": "AdminCreate123!",
                "display_name": "Admin Created User",
                "is_admin": False,
            },
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestDatabaseManagement:
    """Tests for database management routes."""

    def test_admin_database_stats(self, admin_client):
        """Test viewing database statistics."""
        response = admin_client.get("/admin/database/stats")
        assert response.status_code == 200

    def test_admin_database_optimize(self, admin_client):
        """Test optimizing database."""
        response = admin_client.post("/admin/database/optimize")
        assert response.status_code in [200, 302]

    def test_admin_backup_database(self, admin_client):
        """Test database backup endpoint.

        NOTE: The route requires a valid BACKUP_TOKEN environment variable.
        Without it the response will be an auth/token error — we assert a
        non-5xx code only.
        """
        response = admin_client.post("/admin/backup-database")
        assert response.status_code in [200, 302, 400, 401, 403]

    def test_admin_backups_list(self, admin_client):
        """Test viewing backups list."""
        response = admin_client.get("/admin/backups")
        assert response.status_code == 200

    def test_admin_restore_center(self, admin_client):
        """Test accessing restore center."""
        response = admin_client.get("/admin/restore-center")
        assert response.status_code == 200

    def test_admin_create_backup(self, admin_client):
        """Test creating manual backup."""
        response = admin_client.post("/admin/backups/create")
        assert response.status_code in [200, 302]


class TestSystemConfiguration:
    """Tests for system configuration routes."""

    def test_admin_config_view(self, admin_client):
        """Test viewing system configuration."""
        response = admin_client.get("/admin/config")
        assert response.status_code == 200

    def test_admin_config_update(self, admin_client):
        """Test updating system configuration."""
        response = admin_client.post(
            "/admin/config/update",
            data={
                "key": "app.maintenance_mode",
                "value": "false",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_rate_limits_view(self, admin_client):
        """Test viewing rate limits."""
        response = admin_client.get("/admin/rate-limits")
        assert response.status_code == 200

    def test_admin_rate_limits_update(self, admin_client):
        """Test updating rate limits."""
        response = admin_client.post(
            "/admin/rate-limits/update",
            data={
                "max_requests": "50",
                "window_seconds": "60",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestMaintenanceMode:
    """Tests for maintenance mode management."""

    def test_admin_maintenance_toggle(self, admin_client):
        """Test toggling maintenance mode."""
        response = admin_client.post("/admin/maintenance/toggle")
        assert response.status_code in [200, 302]

    def test_admin_maintenance_status(self, admin_client):
        """Test checking maintenance status."""
        response = admin_client.get("/admin/maintenance/status")
        assert response.status_code == 200


class TestSecurityFeatures:
    """Tests for security-related admin routes."""

    def test_admin_failed_logins(self, admin_client):
        """Test viewing failed login attempts."""
        response = admin_client.get("/admin/failed-logins")
        assert response.status_code == 200

    def test_admin_clear_failed_logins(self, admin_client):
        """Test clearing failed login attempts."""
        response = admin_client.post("/admin/failed-logins/clear")
        assert response.status_code in [200, 302]

    def test_admin_ip_blocklist(self, admin_client):
        """Test viewing IP blocklist."""
        response = admin_client.get("/admin/ip-blocklist")
        assert response.status_code == 200

    def test_admin_add_ip_to_blocklist(self, admin_client):
        """Test adding IP to blocklist."""
        response = admin_client.post(
            "/admin/ip-blocklist/add",
            data={"ip_address": "192.168.1.100", "reason": "Test block"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_remove_ip_from_blocklist(self, admin_client, app):
        """Test removing IP from blocklist.

        Seeds a BlockedIP record directly then calls the removal endpoint.
        Uses stdlib datetime directly — not pytest.importorskip (which is only
        for optional third-party packages).
        """
        blocked_ip = BlockedIP(
            ip_address="192.168.1.101",
            reason="Test",
            blocked_at=datetime.utcnow(),
        )
        db.session.add(blocked_ip)
        db.session.commit()
        block_id = blocked_ip.id

        response = admin_client.post(
            f"/admin/ip-blocklist/{block_id}/remove",
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_ip_whitelist(self, admin_client):
        """Test viewing IP whitelist."""
        response = admin_client.get("/admin/ip-whitelist")
        assert response.status_code == 200

    def test_admin_ip_whitelist_toggle(self, admin_client):
        """Test toggling IP whitelist mode."""
        response = admin_client.post("/admin/ip-whitelist/toggle")
        assert response.status_code in [200, 302]

    def test_admin_ip_whitelist_add(self, admin_client):
        """Test adding IP to whitelist."""
        response = admin_client.post(
            "/admin/ip-whitelist/add",
            data={"ip_address": "192.168.1.200"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_bot_defense(self, admin_client):
        """Test viewing bot defense settings."""
        response = admin_client.get("/admin/bot-defense")
        assert response.status_code == 200

    def test_admin_bot_defense_update(self, admin_client):
        """Test updating bot defense settings."""
        response = admin_client.post(
            "/admin/bot-defense/update",
            data={"enabled": "true", "base_difficulty": "3"},
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestSessionManagement:
    """Tests for session management routes."""

    def test_admin_sessions_list(self, admin_client):
        """Test viewing active sessions."""
        response = admin_client.get("/admin/sessions")
        assert response.status_code == 200

    def test_admin_sessions_cleanup(self, admin_client):
        """Test cleaning up old sessions."""
        response = admin_client.post("/admin/sessions/cleanup")
        assert response.status_code in [200, 302]


class TestAuditLog:
    """Tests for audit log routes."""

    def test_admin_audit_log(self, admin_client):
        """Test viewing audit log."""
        response = admin_client.get("/admin/audit-log")
        assert response.status_code == 200

    def test_admin_user_login_history(self, admin_client, regular_user):
        """Test viewing user login history."""
        response = admin_client.get(f"/admin/users/{regular_user.email}/login-history")
        assert response.status_code == 200


class TestActivityMonitoring:
    """Tests for activity monitoring routes."""

    def test_admin_activity(self, admin_client):
        """Test viewing system activity."""
        response = admin_client.get("/admin/activity")
        assert response.status_code == 200

    def test_admin_health(self, admin_client):
        """Test system health check."""
        response = admin_client.get("/admin/health")
        assert response.status_code == 200

    def test_admin_dashboard_stream(self, admin_client):
        """Test dashboard real-time stream endpoint.

        SSE endpoints may open a long-lived connection; only assert a non-5xx
        response to avoid blocking in tests.
        """
        response = admin_client.get(
            "/admin/dashboard/stream",
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code in [200, 302, 404]


class TestDataManagement:
    """Tests for data management routes."""

    def test_admin_global_teams(self, admin_client):
        """Test viewing all teams."""
        response = admin_client.get("/admin/global-teams")
        assert response.status_code == 200

    def test_admin_global_matches(self, admin_client):
        """Test viewing all matches."""
        response = admin_client.get("/admin/global-matches")
        assert response.status_code == 200

    def test_admin_matches_list(self, admin_client):
        """Test viewing admin matches list."""
        response = admin_client.get("/admin/matches")
        assert response.status_code == 200

    def test_admin_delete_team(self, admin_client):
        """Test deleting non-existent team as admin returns 404 or redirects."""
        response = admin_client.post("/admin/teams/99999/delete")
        assert response.status_code in [200, 302, 404]

    def test_admin_delete_team_removes_profiles_and_players(self, admin_client, regular_user):
        """Test admin team deletion also removes all team profiles and players."""
        team = DBTeam(
            name="Admin Delete Test Team",
            short_code="ADT",
            user_id=regular_user.id,
            is_placeholder=False,
            is_draft=False,
        )
        db.session.add(team)
        db.session.flush()

        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()

        db.session.add(DBPlayer(team_id=team.id, profile_id=profile.id, name="Profile Player"))
        db.session.add(DBPlayer(team_id=team.id, profile_id=None, name="Legacy Player"))
        db.session.commit()

        response = admin_client.post(f"/admin/teams/{team.id}/delete")
        assert response.status_code == 200
        assert db.session.get(DBTeam, team.id) is None
        assert DBTeamProfile.query.filter_by(team_id=team.id).count() == 0
        assert DBPlayer.query.filter_by(team_id=team.id).count() == 0

    def test_admin_delete_player(self, admin_client):
        """Test deleting non-existent player as admin returns 404 or redirects."""
        response = admin_client.post("/admin/players/99999/delete")
        assert response.status_code in [200, 302, 404]

    def test_admin_delete_tournament(self, admin_client):
        """Test deleting non-existent tournament as admin returns 404 or redirects."""
        response = admin_client.post("/admin/tournaments/99999/delete")
        assert response.status_code in [200, 302, 404]

    def test_admin_reset_tournament(self, admin_client):
        """Test resetting non-existent tournament as admin returns 404 or redirects."""
        response = admin_client.post("/admin/tournaments/99999/reset")
        assert response.status_code in [200, 302, 404]

    def test_admin_delete_match(self, admin_client):
        """Test deleting a non-existent match as admin returns 404 or redirects."""
        response = admin_client.post("/admin/matches/test-match-id/delete-db")
        assert response.status_code in [200, 302, 404]

    def test_admin_terminate_match(self, admin_client):
        """Test terminating a non-existent active match returns 404 or redirects."""
        response = admin_client.post("/admin/matches/test-match-id/terminate")
        assert response.status_code in [200, 302, 404]


class TestDataExport:
    """Tests for data export routes."""

    def test_admin_export_page(self, admin_client):
        """Test export page."""
        response = admin_client.get("/admin/export")
        assert response.status_code == 200

    def test_admin_export_table_csv(self, admin_client):
        """Test exporting table as CSV."""
        response = admin_client.get("/admin/export/users/csv")
        assert response.status_code in [200, 404]

    def test_admin_export_table_json(self, admin_client):
        """Test exporting table as JSON."""
        response = admin_client.get("/admin/export/users/json")
        assert response.status_code in [200, 404]

    def test_admin_export_all(self, admin_client):
        """Test exporting all data."""
        response = admin_client.get("/admin/export/all/json")
        assert response.status_code in [200, 404]

    def test_admin_export_user_data(self, admin_client, regular_user):
        """Test exporting specific user data."""
        response = admin_client.get(f"/admin/users/{regular_user.email}/export")
        assert response.status_code in [200, 404]

    def test_user_export_own_data(self, authenticated_client):
        """Test user exporting their own data."""
        response = authenticated_client.get("/export/my-data")
        assert response.status_code in [200, 404]


class TestUserImpersonation:
    """Tests for user impersonation feature."""

    def test_admin_impersonate_user(self, admin_client, regular_user):
        """Test starting user impersonation as admin."""
        response = admin_client.post(
            f"/admin/impersonate/{regular_user.email}",
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_admin_stop_impersonation(self, admin_client):
        """Test stopping impersonation session."""
        response = admin_client.get("/admin/stop-impersonation")
        assert response.status_code in [200, 302]

    def test_regular_user_cannot_impersonate(self, authenticated_client, admin_user):
        """Test that regular users cannot use the impersonation endpoint."""
        response = authenticated_client.post(f"/admin/impersonate/{admin_user.email}")
        assert response.status_code in [403, 302]


class TestFileManagement:
    """Tests for file management routes."""

    def test_admin_files_page(self, admin_client):
        """Test accessing files management page."""
        response = admin_client.get("/admin/files")
        assert response.status_code == 200

    def test_admin_api_files_list(self, admin_client):
        """Test listing files via API."""
        response = admin_client.get("/admin/api/files")
        assert response.status_code == 200

    def test_admin_api_files_delete(self, admin_client):
        """Test deleting non-existent files via API returns 200 or 404."""
        response = admin_client.delete(
            "/admin/api/files",
            json={"files": ["nonexistent.txt"]},
        )
        assert response.status_code in [200, 404]


class TestLogsManagement:
    """Tests for logs management."""

    def test_admin_logs_view(self, admin_client):
        """Test viewing logs."""
        response = admin_client.get("/admin/logs")
        assert response.status_code == 200

    def test_admin_logs_download(self, admin_client):
        """Test downloading logs."""
        response = admin_client.get("/admin/logs/download")
        assert response.status_code in [200, 404]


class TestAnalytics:
    """Tests for analytics routes."""

    def test_admin_user_analytics(self, admin_client, regular_user):
        """Test viewing user analytics."""
        response = admin_client.get(f"/admin/users/{regular_user.email}/analytics")
        assert response.status_code == 200

    def test_user_own_analytics(self, authenticated_client):
        """Test user viewing their own analytics."""
        response = authenticated_client.get("/my-analytics")
        assert response.status_code == 200

    def test_admin_retention(self, admin_client):
        """Test viewing retention analytics."""
        response = admin_client.get("/admin/retention")
        assert response.status_code == 200


class TestAdvancedFeatures:
    """Tests for advanced admin features."""

    def test_admin_search(self, admin_client):
        """Test admin search functionality."""
        response = admin_client.get("/admin/search?q=test")
        assert response.status_code == 200

    def test_admin_sql_console_get(self, admin_client):
        """Test accessing SQL console."""
        response = admin_client.get("/admin/sql")
        assert response.status_code == 200

    def test_admin_scheduled_tasks(self, admin_client):
        """Test viewing scheduled tasks."""
        response = admin_client.get("/admin/scheduled-tasks")
        assert response.status_code == 200

    def test_admin_wipe_user_data(self, admin_client, regular_user):
        """Test wiping user data as admin."""
        response = admin_client.post(
            f"/admin/users/{regular_user.email}/wipe-data",
            follow_redirects=True,
        )
        assert response.status_code == 200
