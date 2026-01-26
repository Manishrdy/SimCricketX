from datetime import datetime
from flask_login import UserMixin
from sqlalchemy.orm import relationship
from database import db
import uuid

class User(UserMixin, db.Model):
    """User account"""
    __tablename__ = 'users'
    
    id = db.Column(db.String(120), primary_key=True)  # Email as ID to match legacy system
    password_hash = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    teams = relationship('Team', backref='owner', lazy=True)
    matches = relationship('Match', backref='user', lazy=True)

class Team(db.Model):
    """Cricket Team"""
    __tablename__ = 'teams'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    short_code = db.Column(db.String(10), nullable=False)
    home_ground = db.Column(db.String(100))
    pitch_preference = db.Column(db.String(50))
    team_color = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    players = relationship('Player', backref='team', cascade="all, delete-orphan")
    
    # Matches where this team played
    home_matches = relationship('Match', foreign_keys='Match.home_team_id', backref='home_team')
    away_matches = relationship('Match', foreign_keys='Match.away_team_id', backref='away_team')

class Player(db.Model):
    """Player Identity & Career Stats"""
    __tablename__ = 'players'
    
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
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
    
    # Aggregate Career Stats (Updated after every match)
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
    
    # Relationships
    scorecard_entries = relationship('MatchScorecard', backref='player_ref')

class Match(db.Model):
    """Match Archive Record"""
    __tablename__ = 'matches'
    
    id = db.Column(db.String(36), primary_key=True)  # UUID
    user_id = db.Column(db.String(120), db.ForeignKey('users.id'))
    
    home_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    away_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    
    winner_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    
    # Match Details
    venue = db.Column(db.String(100))
    pitch_type = db.Column(db.String(50))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    result_description = db.Column(db.String(200)) # e.g., "CSK won by 4 wickets"
    
    # Scores
    home_team_score = db.Column(db.Integer)
    home_team_wickets = db.Column(db.Integer)
    home_team_overs = db.Column(db.Float)
    
    away_team_score = db.Column(db.Integer)
    away_team_wickets = db.Column(db.Integer)
    away_team_overs = db.Column(db.Float)
    
    # Technical
    match_json_path = db.Column(db.String(255)) # Path to legacy full JSON
    
    # Relationships
    scorecards = relationship('MatchScorecard', backref='match', cascade="all, delete-orphan")

class MatchScorecard(db.Model):
    """Detailed stats for a player in a specific match"""
    __tablename__ = 'match_scorecards'
    
    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    
    # Batting
    runs = db.Column(db.Integer, default=0)
    balls = db.Column(db.Integer, default=0)
    fours = db.Column(db.Integer, default=0)
    sixes = db.Column(db.Integer, default=0)
    is_out = db.Column(db.Boolean, default=False)
    wicket_type = db.Column(db.String(50), nullable=True)
    
    # Bowling
    overs = db.Column(db.Float, default=0.0)
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
    overs_faced = db.Column(db.Float, default=0.0, nullable=False)
    runs_conceded = db.Column(db.Integer, default=0, nullable=False)
    overs_bowled = db.Column(db.Float, default=0.0, nullable=False)

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
