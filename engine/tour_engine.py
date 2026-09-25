"""Tour orchestration. Child tournaments remain the only statistics writers."""
from collections import Counter
from datetime import datetime
import re

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from database import db
from database.models import Tour, Team, Match, MatchScorecard, Player
from engine.tournament_engine import TournamentEngine

from engine.format_catalog import TOUR_FORMATS, FORMAT_LABELS, squad_format
FORMATS = TOUR_FORMATS


def team_format_availability(team):
    """Use actual format profiles and the same legality rules as squad publishing."""
    from utils.squad_rules import team_squad_readiness_error
    result = {}
    profiles = {p.format_type: p for p in team.profiles}
    for fmt in FORMATS:
        profile = profiles.get(squad_format(fmt))
        players = list(profile.players) if profile else []
        reason = team_squad_readiness_error(team, fmt)
        result[fmt] = {'available': not reason, 'reason': reason or 'Squad ready.',
                       'players': len(players)}
    return result


def validate_tour_squads(teams, formats):
    for team in teams:
        if not team:
            raise ValueError('A tour team is no longer available.')
        availability = team_format_availability(team)
        for fmt in FORMATS:
            if fmt in formats and not availability[fmt]['available']:
                label = 'List A' if fmt == 'ListA' else fmt
                raise ValueError(f"{team.name} — {label}: {availability[fmt]['reason']}")



def create_tour(name, user_id, host_id, visitor_id, counts, order, creation_token=None, scheduled_overs=None):
    from engine.format_config import resolve_scheduled_overs
    scheduled_overs = resolve_scheduled_overs('ListA', scheduled_overs)
    name = (name or '').strip()
    if not name or len(name) > 100:
        raise ValueError('Enter a tour name of 1–100 characters.')
    if creation_token and len(creation_token) > 64:
        raise ValueError('Invalid creation token.')
    parsed = {}
    for fmt in FORMATS:
        value = str(counts.get(fmt, '0'))
        if not re.fullmatch(r'[0-9]+', value):
            raise ValueError('Match counts must be non-negative whole numbers.')
        parsed[fmt] = int(value)
    included = {fmt for fmt, count in parsed.items() if count > 0}
    if not included:
        raise ValueError('Choose at least one format with a positive match count.')
    if len(order) != len(set(order)) or set(order) != included:
        raise ValueError('Arrange every included format exactly once in the series order.')
    host_id, visitor_id = int(host_id), int(visitor_id)
    if host_id == visitor_id:
        raise ValueError('Choose two different teams.')
    teams = Team.query.filter(Team.id.in_([host_id, visitor_id]), Team.user_id == user_id,
                              Team.is_placeholder.isnot(True)).all()
    if len(teams) != 2:
        raise ValueError('Choose two teams owned by you.')
    validate_tour_squads(teams, included)
    if creation_token:
        existing = Tour.query.filter_by(user_id=user_id, creation_token=creation_token).first()
        if existing:
            return existing
    try:
        tour = Tour(name=name, user_id=user_id, host_team_id=host_id,
                    visiting_team_id=visitor_id, creation_token=creation_token)
        db.session.add(tour)
        db.session.flush()
        engine = TournamentEngine()
        for position, fmt in enumerate(f for f in order if f in included):
            series = engine.create_tournament(
                name=f'{name[:85]} — {fmt}', user_id=user_id,
                team_ids=[host_id, visitor_id], mode='custom_series', format_type=fmt,
                scheduled_overs=scheduled_overs if fmt == 'ListA' else None,
                series_config={'matches': [{'home': 0, 'match_num': n + 1}
                                           for n in range(parsed[fmt])]}, commit=False)
            series.tour = tour
            series.tour_order = position
        db.session.commit()
        return tour
    except IntegrityError:
        db.session.rollback()
        if creation_token:
            existing = Tour.query.filter_by(user_id=user_id, creation_token=creation_token).first()
            if existing:
                return existing
        raise
    except Exception:
        db.session.rollback()
        raise


def ordered_series(tour):
    return sorted(tour.series, key=lambda s: s.tour_order)


def is_complete(series):
    return bool(series.fixtures) and all(f.status == 'Completed' for f in series.fixtures)


def has_started(series):
    return bool(series.tour_started_at or any(f.match_id or f.status == 'Completed' for f in series.fixtures))


def refresh_tour(tour):
    for series in tour.series:
        if has_started(series) and not series.tour_started_at:
            series.tour_started_at = datetime.utcnow()
        series.status = 'Completed' if is_complete(series) else 'Active'
        series.current_stage = 'completed' if is_complete(series) else 'league'
    tour.status = 'Completed' if tour.series and all(is_complete(s) for s in tour.series) else 'Active'


def fixture_block_reason(fixture):
    series = fixture.tournament
    if not series.tour_id:
        return None
    if fixture.status != 'Scheduled':
        return 'This fixture is not available to start.'
    try:
        validate_tour_squads([fixture.home_team, fixture.away_team], {series.format_type})
    except ValueError as exc:
        return str(exc)
    for earlier in ordered_series(series.tour):
        if earlier.id == series.id:
            break
        if not is_complete(earlier):
            return 'Complete the earlier tour series before starting this match.'
    return None


def reorder_series(tour, ids):
    current = ordered_series(tour)
    if len(ids) != len(set(ids)) or set(ids) != {s.id for s in current}:
        raise ValueError('Supply every included series exactly once.')
    for index, series in enumerate(current):
        if has_started(series) and ids[index] != series.id:
            raise ValueError('Active and completed series must remain in their current positions.')
    by_id = {s.id: s for s in current}
    for index, series_id in enumerate(ids):
        by_id[series_id].tour_order = index


def tour_summary(tour, format_type=None):
    series = [s for s in ordered_series(tour) if not format_type or s.format_type == format_type]
    fixtures = [f for s in series for f in s.fixtures]
    completed = [f for f in fixtures if f.status == 'Completed']
    matches = Match.query.filter(Match.id.in_([f.match_id for f in completed if f.match_id])).all()
    match_ids = [m.id for m in matches]
    def leaders(record_type, column):
        return (db.session.query(Player.name, func.sum(column).label('total'))
                .join(MatchScorecard, MatchScorecard.player_id == Player.id)
                .filter(MatchScorecard.match_id.in_(match_ids),
                        MatchScorecard.record_type == record_type,
                        MatchScorecard.is_super_over.isnot(True))
                .group_by(Player.id, Player.name).order_by(func.sum(column).desc(), Player.id).limit(10).all())
    awards = (db.session.query(Player.name, func.count(Match.id))
              .join(Match, Match.motm_player_id == Player.id).filter(Match.id.in_(match_ids))
              .group_by(Player.id, Player.name).order_by(func.count(Match.id).desc(), Player.id).limit(10).all())
    return dict(total=len(fixtures), played=len(completed),
                wins=Counter(f.winner_team_id for f in completed if f.winner_team_id),
                undecided=sum(not f.winner_team_id for f in completed),
                runs=sum(sum(getattr(m, key) or 0 for key in ('home_team_score', 'away_team_score',
                         'home_team_score_innings2', 'away_team_score_innings2')) for m in matches),
                wickets=sum(sum(getattr(m, key) or 0 for key in ('home_team_wickets', 'away_team_wickets',
                            'home_team_wickets_innings2', 'away_team_wickets_innings2')) for m in matches),
                batters=leaders('batting', MatchScorecard.runs),
                bowlers=leaders('bowling', MatchScorecard.wickets), awards=awards)


def series_result(series, tour):
    wins = Counter(f.winner_team_id for f in series.fixtures if f.status == 'Completed' and f.winner_team_id)
    host, visitor = wins[tour.host_team_id], wins[tour.visiting_team_id]
    result = 'In progress' if has_started(series) else 'Upcoming'
    if is_complete(series):
        winner = tour.host_team if host > visitor else tour.visiting_team
        result = 'Series drawn' if host == visitor else f'{winner.name if winner else "Deleted team"} won'
    return {'series': series, 'score': f'{host}–{visitor}', 'result': result,
            'played': sum(f.status == 'Completed' for f in series.fixtures), 'started': has_started(series)}
