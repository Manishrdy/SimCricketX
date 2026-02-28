from datetime import datetime, timezone
from flask_login import UserMixin
from sqlalchemy.orm import relationship, synonym
from database import db
import uuid

class User(UserMixin, db.Model):
    """User account

    NOTE: id is currently the email string for legacy compatibility.
    The stable_id column provides a UUID that can become the PK in a future
    migration, allowing email changes without cascading FK updates.
    """
    __tablename__ = 'users'

    id = db.Column(db.String(120), primary_key=True)  # Email as ID (legacy)
    email = synonym('id')  # Backward compatibility for tests/code using User.email
    stable_id = db.Column(db.String(36), unique=True, default=lambda: str(uuid.uuid4()))
    password_hash = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # New fields for auth migration
    last_login = db.Column(db.DateTime)
    ip_address = db.Column(db.String(50))
    mac_address = db.Column(db.String(50))
    hostname = db.Column(db.String(100))
    display_name = db.Column(db.String(100))
    
    # Admin flag for role-based access control
    is_admin = db.Column(db.Boolean, default=False, nullable=False, index=True)

    # Ban/Suspend
    is_banned = db.Column(db.Boolean, default=False, nullable=False)
    banned_until = db.Column(db.DateTime, nullable=True)  # NULL = permanent, set = temp ban
    ban_reason = db.Column(db.String(500), nullable=True)

    # Force password reset on next login
    force_password_reset = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships — cascade so deleting a User removes all owned data
    teams = relationship('Team', backref='owner', lazy=True, cascade="all, delete-orphan")
    matches = relationship('Match', backref='user', lazy=True, cascade="all, delete-orphan")
    tournaments = relationship('Tournament', backref='owner', lazy=True, cascade="all, delete-orphan")

class Team(db.Model):
    """Cricket Team"""
    __tablename__ = 'teams'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    short_code = db.Column(db.String(10), nullable=False)
    home_ground = db.Column(db.String(100))
    pitch_preference = db.Column(db.String(50))
    team_color = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_draft = db.Column(db.Boolean, default=False)
    is_placeholder = db.Column(db.Boolean, default=False)  # True for BYE/TBD teams

    # Unique constraint: one short_code per user
    __table_args__ = (
        db.UniqueConstraint('user_id', 'short_code', name='uq_team_user_short_code'),
    )

    # Relationships
    profiles = relationship('TeamProfile', back_populates='team', cascade='all, delete-orphan')
    # Read-only convenience accessor — cascade is managed through profiles
    players = relationship('Player', viewonly=True)

    # Matches where this team played
    home_matches = relationship('Match', foreign_keys='Match.home_team_id', backref='home_team')
    away_matches = relationship('Match', foreign_keys='Match.away_team_id', backref='away_team')


class TeamProfile(db.Model):
    """Format-specific squad profile for a team (T20, ListA)."""
    __tablename__ = 'team_profiles'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey('teams.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    format_type = db.Column(db.String(20), nullable=False)  # 'T20', 'ListA'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('team_id', 'format_type', name='uq_team_profile_format'),
    )

    team = relationship('Team', back_populates='profiles')
    players = relationship('Player', back_populates='profile', cascade='all, delete-orphan')

class Player(db.Model):
    """Player Identity & Career Stats"""
    __tablename__ = 'players'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False, index=True)
    # Profile this player belongs to (format-specific squad)
    profile_id = db.Column(
        db.Integer,
        db.ForeignKey('team_profiles.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(50))  # Batsman, Bowler, All-rounder, Wicketkeeper

    # Skill Ratings (snapshot from last update)
    batting_rating = db.Column(db.Integer, default=0)
    bowling_rating = db.Column(db.Integer, default=0)
    fielding_rating = db.Column(db.Integer, default=0)

    # Technical Attributes
    batting_hand = db.Column(db.String(20))
    bowling_type = db.Column(db.String(50))
    bowling_hand = db.Column(db.String(20))

    # Identity flags
    is_captain = db.Column(db.Boolean, default=False)
    is_wicketkeeper = db.Column(db.Boolean, default=False)

    # Aggregate Career Stats (Updated after every match; tracked per profile = per format)
    matches_played = db.Column(db.Integer, default=0)
    total_runs = db.Column(db.Integer, default=0)
    total_balls_faced = db.Column(db.Integer, default=0)
    total_fours = db.Column(db.Integer, default=0)
    total_sixes = db.Column(db.Integer, default=0)
    total_fifties = db.Column(db.Integer, default=0)
    total_centuries = db.Column(db.Integer, default=0)
    highest_score = db.Column(db.Integer, default=0)
    not_outs = db.Column(db.Integer, default=0)

    total_balls_bowled = db.Column(db.Integer, default=0)
    total_runs_conceded = db.Column(db.Integer, default=0)
    total_wickets = db.Column(db.Integer, default=0)
    total_maidens = db.Column(db.Integer, default=0)
    five_wicket_hauls = db.Column(db.Integer, default=0)
    best_bowling_wickets = db.Column(db.Integer, default=0)
    best_bowling_runs = db.Column(db.Integer, default=0)

    # Unique constraint: one player name per profile (format squad)
    # NOTE: uq_player_team_name is replaced by uq_player_profile_name via migration
    __table_args__ = (
        db.UniqueConstraint('profile_id', 'name', name='uq_player_profile_name'),
    )

    # Relationships
    profile = relationship('TeamProfile', back_populates='players')
    scorecard_entries = relationship('MatchScorecard', backref='player_ref', passive_deletes=True)

class Match(db.Model):
    """Match Archive Record"""
    __tablename__ = 'matches'
    
    id = db.Column(db.String(36), primary_key=True)  # UUID
    user_id = db.Column(db.String(120), db.ForeignKey('users.id'))
    
    home_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), index=True)
    away_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), index=True)

    winner_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True, index=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True, index=True)
    
    # Match Details
    venue = db.Column(db.String(100))
    pitch_type = db.Column(db.String(50))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    result_description = db.Column(db.String(200)) # e.g., "CSK won by 4 wickets"
    
    # Scores
    home_team_score = db.Column(db.Integer)
    home_team_wickets = db.Column(db.Integer)
    home_team_overs = db.Column(db.String(10))

    away_team_score = db.Column(db.Integer)
    away_team_wickets = db.Column(db.Integer)
    away_team_overs = db.Column(db.String(10))
    
    # Margin of Victory
    margin_type = db.Column(db.String(10))  # 'runs', 'wickets', or 'tie'
    margin_value = db.Column(db.Integer)    # Number of runs/wickets
    
    # Toss Information
    toss_winner_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    toss_decision = db.Column(db.String(10))  # 'Bat' or 'Bowl'
    
    # Match Format
    match_format = db.Column(db.String(20), default='T20')
    overs_per_side = db.Column(db.Integer, default=20)
    is_day_night = db.Column(db.Boolean, default=False)
    
    # Technical
    match_json_path = db.Column(db.String(255)) # Path to legacy full JSON
    
    # Relationships
    scorecards = relationship('MatchScorecard', backref='match', cascade="all, delete-orphan")
    toss_winner = relationship('Team', foreign_keys=[toss_winner_team_id])

class MatchScorecard(db.Model):
    """Detailed stats for a player in a specific match"""
    __tablename__ = 'match_scorecards'
    
    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=False, index=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False, index=True)
    innings_number = db.Column(db.Integer, default=1, nullable=False)
    record_type = db.Column(db.String(20), default="batting", nullable=False)
    position = db.Column(db.Integer, nullable=True)
    
    # Batting
    runs = db.Column(db.Integer, default=0)
    balls = db.Column(db.Integer, default=0)
    fours = db.Column(db.Integer, default=0)
    sixes = db.Column(db.Integer, default=0)
    is_out = db.Column(db.Boolean, default=False)
    wicket_type = db.Column(db.String(50), nullable=True)
    
    # Bowling
    overs = db.Column(db.String(10), default='0.0')
    balls_bowled = db.Column(db.Integer, default=0)
    runs_conceded = db.Column(db.Integer, default=0)
    wickets = db.Column(db.Integer, default=0)
    maidens = db.Column(db.Integer, default=0)
    wides = db.Column(db.Integer, default=0)
    noballs = db.Column(db.Integer, default=0)
    
    # Fielding
    catches = db.Column(db.Integer, default=0)
    run_outs = db.Column(db.Integer, default=0)

    # New fields for detailed scorecard
    wicket_taker_name = db.Column(db.String(100), nullable=True)
    fielder_name = db.Column(db.String(100), nullable=True)
    
    # Detailed Batting Stats
    ones = db.Column(db.Integer, default=0)
    twos = db.Column(db.Integer, default=0)
    threes = db.Column(db.Integer, default=0)
    dot_balls = db.Column(db.Integer, default=0)
    strike_rate = db.Column(db.Float, default=0.0)
    batting_position = db.Column(db.Integer)
    
    # Detailed Bowling Stats
    dot_balls_bowled = db.Column(db.Integer, default=0)
    wickets_bowled = db.Column(db.Integer, default=0)
    wickets_caught = db.Column(db.Integer, default=0)
    wickets_lbw = db.Column(db.Integer, default=0)
    wickets_stumped = db.Column(db.Integer, default=0)
    wickets_run_out = db.Column(db.Integer, default=0)
    wickets_hit_wicket = db.Column(db.Integer, default=0)

class Tournament(db.Model):
    """Tournament / League Container"""
    __tablename__ = 'tournaments'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Active')  # Active, Completed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Tournament Mode Configuration
    # Modes: 'round_robin', 'double_round_robin', 'knockout',
    #        'round_robin_knockout', 'double_round_robin_knockout', 'ipl_style', 'custom_series'
    mode = db.Column(db.String(50), default='round_robin', nullable=False)

    # Cricket format for all matches in this tournament (T20, ListA)
    format_type = db.Column(db.String(20), default='T20', nullable=False)

    # Current stage for multi-stage tournaments
    # Stages: 'league', 'qualifier', 'eliminator', 'semifinal', 'final', 'completed'
    current_stage = db.Column(db.String(30), default='league', nullable=False)

    # Number of teams that qualify from league stage (for knockout/IPL modes)
    # Default 4 for IPL-style, can be 2/4/8 for knockout modes
    playoff_teams = db.Column(db.Integer, default=4, nullable=False)

    # Custom series configuration (JSON string for flexibility)
    # Example: {"matches": [{"home": 1, "away": 2, "venue": "home"}, ...], "series_name": "Ashes"}
    series_config = db.Column(db.Text, nullable=True)

    # Relationships
    participating_teams = relationship('TournamentTeam', backref='tournament', cascade="all, delete-orphan")
    fixtures = relationship('TournamentFixture', backref='tournament', cascade="all, delete-orphan")
    player_stats_cache = relationship('TournamentPlayerStatsCache', backref='tournament', cascade="all, delete-orphan")

class MatchPartnership(db.Model):
    """Batting partnership records for each innings"""
    __tablename__ = 'match_partnerships'
    
    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=False)
    innings_number = db.Column(db.Integer, nullable=False)   # 1 or 2
    wicket_number = db.Column(db.Integer, nullable=False)   # 1st wicket, 2nd wicket, etc.
    
    # Partnership participants
    batsman1_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    batsman2_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    
    # Partnership statistics
    runs = db.Column(db.Integer, default=0)
    balls = db.Column(db.Integer, default=0)
    batsman1_contribution = db.Column(db.Integer, default=0)
    batsman2_contribution = db.Column(db.Integer, default=0)
    
    # Partnership duration
    start_over = db.Column(db.Float)
    end_over = db.Column(db.Float)
    
    # Relationships
    batsman1 = relationship('Player', foreign_keys=[batsman1_id])
    batsman2 = relationship('Player', foreign_keys=[batsman2_id])
    match = relationship('Match', backref=db.backref('partnerships', cascade="all, delete-orphan"))
    
    # Indexes for efficient queries
    __table_args__ = (
        db.Index('ix_partnership_match_innings', 'match_id', 'innings_number'),
    )

class TournamentPlayerStatsCache(db.Model):
    """Cached player statistics for a specific tournament"""
    __tablename__ = 'tournament_player_stats_cache'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)

    # Batting Stats
    matches_played = db.Column(db.Integer, default=0)
    innings_batted = db.Column(db.Integer, default=0)
    runs_scored = db.Column(db.Integer, default=0)
    balls_faced = db.Column(db.Integer, default=0)
    fours = db.Column(db.Integer, default=0)
    sixes = db.Column(db.Integer, default=0)
    not_outs = db.Column(db.Integer, default=0)
    highest_score = db.Column(db.Integer, default=0)
    fifties = db.Column(db.Integer, default=0)
    centuries = db.Column(db.Integer, default=0)
    batting_average = db.Column(db.Float, default=0.0)
    batting_strike_rate = db.Column(db.Float, default=0.0)

    # Bowling Stats
    innings_bowled = db.Column(db.Integer, default=0)
    overs_bowled = db.Column(db.String(10), default='0.0')
    runs_conceded = db.Column(db.Integer, default=0)
    wickets_taken = db.Column(db.Integer, default=0)
    maidens = db.Column(db.Integer, default=0)
    best_bowling_wickets = db.Column(db.Integer, default=0)
    best_bowling_runs = db.Column(db.Integer, default=0)
    five_wicket_hauls = db.Column(db.Integer, default=0)
    bowling_average = db.Column(db.Float, default=0.0)
    bowling_economy = db.Column(db.Float, default=0.0)
    bowling_strike_rate = db.Column(db.Float, default=0.0)

    # Fielding Stats
    catches = db.Column(db.Integer, default=0)
    run_outs = db.Column(db.Integer, default=0)
    stumpings = db.Column(db.Integer, default=0)

    # Relationships
    player = relationship('Player')
    team = relationship('Team')

    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'player_id', name='uq_tournament_player_cache'),
        db.Index('ix_tournament_player_cache_tournament_id', 'tournament_id'),
    )

class TournamentTeam(db.Model):
    """Team stats within a specific tournament"""
    __tablename__ = 'tournament_teams'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)

    # Standings Stats
    played = db.Column(db.Integer, default=0, nullable=False)
    won = db.Column(db.Integer, default=0, nullable=False)
    lost = db.Column(db.Integer, default=0, nullable=False)
    tied = db.Column(db.Integer, default=0, nullable=False)
    no_result = db.Column(db.Integer, default=0, nullable=False)
    points = db.Column(db.Integer, default=0, nullable=False)

    # NRR Components
    runs_scored = db.Column(db.Integer, default=0, nullable=False)
    overs_faced = db.Column(db.String(10), default='0.0', nullable=False)
    runs_conceded = db.Column(db.Integer, default=0, nullable=False)
    overs_bowled = db.Column(db.String(10), default='0.0', nullable=False)

    net_run_rate = db.Column(db.Float, default=0.0, nullable=False)

    # Relationship to access Team details (name, etc.)
    team = relationship('Team')

    # Ensure each team can only appear once per tournament
    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'team_id', name='uq_tournament_team'),
    )

class TournamentFixture(db.Model):
    """Scheduled Match in a Tournament"""
    __tablename__ = 'tournament_fixtures'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    home_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    away_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    round_number = db.Column(db.Integer, default=1, nullable=False)
    status = db.Column(db.String(20), default='Scheduled', nullable=False)
    stage = db.Column(db.String(30), default='league', nullable=False)
    stage_description = db.Column(db.String(100), nullable=True)
    bracket_position = db.Column(db.Integer, nullable=True)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=True)
    winner_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    series_match_number = db.Column(db.Integer, nullable=True)
    standings_applied = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships
    home_team = relationship('Team', foreign_keys=[home_team_id])
    away_team = relationship('Team', foreign_keys=[away_team_id])
    winner_team = relationship('Team', foreign_keys=[winner_team_id])
    match = relationship('Match')

    __table_args__ = (
        db.Index('ix_fixture_tournament_status', 'tournament_id', 'status'),
        db.Index('ix_fixture_tournament_stage', 'tournament_id', 'stage'),
    )

class AdminAuditLog(db.Model):
    """Persistent audit trail for all admin actions"""
    __tablename__ = 'admin_audit_log'

    id = db.Column(db.Integer, primary_key=True)
    admin_email = db.Column(db.String(120), nullable=False, index=True)
    action = db.Column(db.String(50), nullable=False)  # e.g. 'reset_password', 'delete_user', 'change_email'
    target = db.Column(db.String(200), nullable=True)   # target user/entity
    details = db.Column(db.Text, nullable=True)          # extra context (JSON or text)
    ip_address = db.Column(db.String(50), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        db.Index('ix_audit_admin_action', 'admin_email', 'action'),
    )


class FailedLoginAttempt(db.Model):
    """Track failed login attempts for security monitoring"""
    __tablename__ = 'failed_login_attempts'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    __table_args__ = (
        db.Index('ix_failed_login_ip', 'ip_address'),
    )


class BlockedIP(db.Model):
    """IP addresses blocked by admin"""
    __tablename__ = 'blocked_ips'

    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(50), nullable=False, unique=True)
    reason = db.Column(db.String(300), nullable=True)
    blocked_by = db.Column(db.String(120), nullable=False, default='system')
    blocked_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class ActiveSession(db.Model):
    """Track active user sessions for admin monitoring"""
    __tablename__ = 'active_sessions'

    id = db.Column(db.Integer, primary_key=True)
    session_token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.String(120), db.ForeignKey('users.id'), nullable=False, index=True)
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    login_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    last_active = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship('User', backref=db.backref('sessions', cascade='all, delete-orphan'))


class SiteCounter(db.Model):
    """Key-value store for site-wide counters (visits, matches simulated, etc.)"""
    __tablename__ = 'site_counters'

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Integer, default=0, nullable=False)


class AnnouncementBanner(db.Model):
    """Single global announcement banner configured by admins."""
    __tablename__ = 'announcement_banner'

    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.Text, nullable=False, default="")
    is_enabled = db.Column(db.Boolean, nullable=False, default=False)
    color_preset = db.Column(db.String(20), nullable=False, default="urgent")
    position = db.Column(db.String(10), nullable=False, default="bottom")
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_by = db.Column(db.String(120), db.ForeignKey('users.id'), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    admin_user = relationship('User', foreign_keys=[updated_by])


class UserBannerDismissal(db.Model):
    """Per-user dismissal state keyed by banner version."""
    __tablename__ = 'user_banner_dismissals'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    banner_version = db.Column(db.Integer, nullable=False)
    dismissed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = relationship('User', backref=db.backref('banner_dismissals', cascade='all, delete-orphan', passive_deletes=True))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'banner_version', name='uq_user_banner_dismissal'),
        db.Index('ix_user_banner_dismissal_user_ver', 'user_id', 'banner_version'),
    )


class LoginHistory(db.Model):
    """Persistent log of every successful login and logout event"""
    __tablename__ = 'login_history'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    event = db.Column(db.String(10), default='login', nullable=False)  # 'login' or 'logout'
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = relationship('User', backref=db.backref('login_history', cascade='all, delete-orphan', passive_deletes=True))

    __table_args__ = (
        db.Index('ix_login_history_user_ts', 'user_id', 'timestamp'),
    )


class IPWhitelistEntry(db.Model):
    """IP addresses allowed when whitelist mode is enabled"""
    __tablename__ = 'ip_whitelist'

    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(50), nullable=False, unique=True)
    label = db.Column(db.String(100), nullable=True)
    added_by = db.Column(db.String(120), nullable=False)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class UserGroundConfig(db.Model):
    """Per-user ground conditions configuration.

    One row per user. When absent, the engine falls back to the factory
    defaults in config/ground_conditions_defaults.yaml.
    """
    __tablename__ = 'user_ground_configs'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(120), db.ForeignKey('users.id'),
                           unique=True, nullable=False, index=True)
    config_json = db.Column(db.JSON, nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    user = relationship('User', backref=db.backref(
        'ground_config', uselist=False, cascade='all, delete-orphan'))
