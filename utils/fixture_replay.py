"""Read-only impact planning and transactional tournament fixture replay."""
import hashlib
import json
import zipfile
from pathlib import Path

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

from database import db
from database.models import (
    FixtureReplay, VoidedFixtureMatch, Tournament, TournamentFixture, Tour,
    Match, MatchScorecard, MatchPartnership,
)
from utils.squad_rules import team_squad_readiness_error


class ReplayError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def lock_tournament(tournament_id):
    """Acquire a database write lock, including on SQLite (FOR UPDATE is ignored).

    All fixture claims, completions and replay commits use this same row.
    The caller owns commit/rollback; refresh objects after acquiring the lock.
    """
    # Tour summaries span several series: serialize those writers on the parent first.
    tour_id = db.session.query(Tournament.tour_id).filter_by(id=tournament_id).scalar()
    if tour_id:
        db.session.query(Tour).filter_by(id=tour_id).update(
            {Tour.status: Tour.status}, synchronize_session=False)
    changed = db.session.query(Tournament).filter_by(id=tournament_id).update(
        {Tournament.status: Tournament.status}, synchronize_session=False
    )
    if not changed:
        raise ReplayError('Tournament not found.', 404)
    db.session.expire_all()


def is_voided(match_id):
    return db.session.get(VoidedFixtureMatch, match_id) is not None


def downstream_fixtures(engine, fixture):
    """Use the existing engine's bracket graph without creating placeholders."""
    tournament = fixture.tournament
    query = TournamentFixture.query.filter_by(tournament_id=tournament.id)
    if fixture.stage == engine.STAGE_LEAGUE:
        if tournament.mode in (engine.MODE_ROUND_ROBIN_KNOCKOUT,
                               engine.MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT, engine.MODE_IPL_STYLE):
            return query.filter(TournamentFixture.stage != engine.STAGE_LEAGUE).order_by(TournamentFixture.id).all()
        return []
    if tournament.mode == engine.MODE_IPL_STYLE:
        playoff_stages = [item.stage for item in query.filter(TournamentFixture.stage != engine.STAGE_LEAGUE)]
        if any(playoff_stages.count(stage) != 1 for stage in (engine.STAGE_QUALIFIER_1, engine.STAGE_ELIMINATOR, engine.STAGE_QUALIFIER_2, engine.STAGE_FINAL)):
            raise ReplayError('The playoff structure is incomplete or duplicated. Repair it before replaying.')
        if fixture.stage not in {engine.STAGE_QUALIFIER_1, engine.STAGE_ELIMINATOR,
                                 engine.STAGE_QUALIFIER_2, engine.STAGE_FINAL}:
            raise ReplayError('The playoff dependencies could not be resolved safely.')
        stages = engine._ipl_downstream_stages(fixture.stage)
        return query.filter(TournamentFixture.stage.in_(stages)).order_by(TournamentFixture.id).all()
    if fixture.bracket_position is None:
        raise ReplayError('The bracket position is missing. Repair the fixture before replaying.')
    bracket = query.filter(TournamentFixture.bracket_position.isnot(None)).order_by(TournamentFixture.bracket_position).all()
    ordered = [item.bracket_position for item in bracket]
    size = len(ordered) + 1
    if (size & (size - 1)) or ordered != list(range(ordered[0], ordered[0] + len(ordered))):
        raise ReplayError('The bracket structure is inconsistent. Repair it before replaying.')
    positions = engine._get_downstream_positions(tournament.id, fixture.bracket_position)
    return query.filter(TournamentFixture.bracket_position.in_(positions)).order_by(TournamentFixture.id).all()


def impact(engine, fixture, user_id):
    if fixture.tournament.user_id != user_id:
        raise ReplayError('Fixture not found.', 404)
    if fixture.tournament.mode not in ('round_robin', 'double_round_robin', 'custom_series', 'knockout', 'round_robin_knockout', 'double_round_robin_knockout', 'ipl_style'):
        raise ReplayError('This tournament format is not supported for replay.')
    if fixture.status != 'Completed' or not fixture.match_id:
        raise ReplayError('Only a completed fixture with a recorded match can be replayed.')
    if fixture.active_match_id:
        raise ReplayError('This fixture still has an active match. Finish processing it before replaying.')
    for team in (fixture.home_team, fixture.away_team):
        if not team or team.is_placeholder or team.user_id != user_id:
            raise ReplayError('The fixture team assignments are invalid.')
    affected = [fixture] + downstream_fixtures(engine, fixture)
    rows, blockers, match_ids = [], [], set()
    for team in (fixture.home_team, fixture.away_team):
        reason = team_squad_readiness_error(team, fixture.tournament.format_type)
        if reason:
            from engine.format_catalog import squad_format
            blockers.append({'team': team.name, 'reason': reason, 'team_id': team.id,
                             'format': squad_format(fixture.tournament.format_type)})
    for item in affected:
        if any(team and team.user_id != user_id for team in (item.home_team, item.away_team)):
            raise ReplayError('A dependent fixture has invalid team ownership.')
        match = db.session.get(Match, item.match_id) if item.match_id else None
        if item.match_id:
            if (not match or match.user_id != user_id or match.tournament_id != fixture.tournament_id
                    or (match.home_team_id, match.away_team_id) != (item.home_team_id, item.away_team_id)
                    or match.id in match_ids):
                raise ReplayError('Recorded match links are inconsistent. No results have been changed.')
            if TournamentFixture.query.filter_by(match_id=match.id).count() != 1:
                raise ReplayError('A match is linked to multiple fixtures. Repair its links first.')
            if match.stats_incomplete:
                raise ReplayError('A scorecard has incomplete player statistics. Repair it before replaying.')
            if not MatchScorecard.query.filter_by(match_id=match.id).first() and not engine._is_no_result(match):
                raise ReplayError('A scorecard is missing or still being saved. Replay is unavailable until it is complete.')
            match_ids.add(match.id)
        elif item.standings_applied or (item.status == 'Completed' and not (
                (item.home_team and item.home_team.is_placeholder) or
                (item.away_team and item.away_team.is_placeholder))):
            raise ReplayError('A dependent result is missing its match data. Repair it before replaying.')
        if item.active_match_id:
            if item.active_match_id in match_ids or db.session.get(Match, item.active_match_id):
                raise ReplayError('An active match has inconsistent result data. Try again after it finishes.')
            match_ids.add(item.active_match_id)
        rows.append({'id': item.id, 'stage': item.stage.replace('_', ' ').title(),
                     'home': item.home_team.name if item.home_team else 'TBD',
                     'away': item.away_team.name if item.away_team else 'TBD',
                     'status': item.status, 'match_id': item.match_id, 'active_match_id': item.active_match_id,
                     'result': match.result_description if match else None,
                     'kind': 'completed' if match else 'in_progress' if item.active_match_id else 'unplayed'})
    result = {'fixture_id': fixture.id, 'tournament_id': fixture.tournament_id,
              'tournament': fixture.tournament.name, 'fixtures': rows, 'blockers': blockers,
              'standings_change': fixture.stage == engine.STAGE_LEAGUE,
              'tour_change': bool(fixture.tournament.tour_id),
              'reopens': fixture.tournament.status == 'Completed'}
    # Include the whole tournament to detect progression elsewhere while a dialog is open.
    state = [[f.id, f.status, f.match_id, f.active_match_id, f.home_team_id, f.away_team_id,
              f.winner_team_id, f.standings_applied, f.stage, f.bracket_position]
             for f in TournamentFixture.query.filter_by(tournament_id=fixture.tournament_id).order_by(TournamentFixture.id)]
    cards = [{column.name: getattr(card, column.name) for column in MatchScorecard.__table__.columns}
             for card in MatchScorecard.query.filter(MatchScorecard.match_id.in_(match_ids)).order_by(MatchScorecard.id)]
    recorded = [{column.name: getattr(match, column.name) for column in Match.__table__.columns}
                for match in Match.query.filter(Match.id.in_(match_ids)).order_by(Match.id)]
    result['version'] = hashlib.sha256(json.dumps([result, state, cards, recorded,
        fixture.tournament.mode, fixture.tournament.current_stage, fixture.tournament.format_type,
        fixture.tournament.scheduled_overs], sort_keys=True, default=str).encode()).hexdigest()
    return result


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='fixture-replay-v1')


def preview_token(plan, user_id):
    return signer().dumps({'user': user_id, 'fixture': plan['fixture_id'], 'version': plan['version']})


def execute(engine, fixture_id, user_id, token):
    if not isinstance(token, str):
        raise ReplayError('Invalid replay preview. Review the impact again.', 400)
    try:
        signed = signer().loads(token, max_age=1800)
    except BadData:
        raise ReplayError('This preview expired or is invalid. Review the impact again.')
    if signed.get('user') != user_id or signed.get('fixture') != fixture_id:
        raise ReplayError('This preview does not belong to this fixture.', 403)
    operation_id = hashlib.sha256(token.encode()).hexdigest()
    fixture = db.session.get(TournamentFixture, fixture_id)
    if not fixture or fixture.tournament.user_id != user_id:
        raise ReplayError('Fixture not found.', 404)
    lock_tournament(fixture.tournament_id)
    receipt = db.session.get(FixtureReplay, operation_id)
    if receipt:
        db.session.rollback()
        return receipt, False
    plan = impact(engine, fixture, user_id)
    if signed.get('version') != plan['version']:
        raise ReplayError('The tournament changed. Review the updated impact before resetting.')
    if plan['blockers']:
        raise ReplayError('Fix the squad problems before resetting this fixture.')
    receipt = FixtureReplay(id=operation_id, user_id=user_id, tournament_id=fixture.tournament_id,
                            fixture_id=fixture.id, impact_json=json.dumps(plan))
    db.session.add(receipt)
    db.session.flush()
    from match_archiver import reverse_player_aggregates
    player_ids = set()
    target_match = db.session.get(Match, fixture.match_id)
    # Reverse points only once, with cascade disabled: the preview is the reset manifest.
    if fixture.stage != engine.STAGE_LEAGUE or fixture.standings_applied:
        engine.reverse_standings(target_match, commit=False, cascade=False)
    tbd = engine._get_placeholder_team_id(fixture.tournament_id, 'TBD') if len(plan['fixtures']) > 1 else None
    for row in plan['fixtures']:
        item = db.session.get(TournamentFixture, row['id'])
        match = db.session.get(Match, row['match_id']) if row['match_id'] else None
        for match_id in {row['match_id'], row['active_match_id']} - {None}:
            db.session.add(VoidedFixtureMatch(match_id=match_id, replay_id=operation_id,
                                             json_path=match.match_json_path if match and match.id == match_id else None))
        item.match_id = None
        item.active_match_id = None
        item.winner_team_id = None
        item.standings_applied = False
        item.status = 'Scheduled' if item.id == fixture_id else 'Locked'
        if item.id != fixture_id:
            item.home_team_id = item.away_team_id = tbd
        if match:
            cards = MatchScorecard.query.filter_by(match_id=match.id).all()
            player_ids.update(c.player_id for c in cards)
            reverse_player_aggregates(cards, logger=current_app.logger)
            MatchPartnership.query.filter_by(match_id=match.id).delete(synchronize_session=False)
            MatchScorecard.query.filter_by(match_id=match.id).delete(synchronize_session=False)
            db.session.delete(match)
            db.session.flush()
    engine.rebuild_player_stats_cache(fixture.tournament_id, player_ids)
    fixture.tournament.status = 'Active'
    fixture.tournament.current_stage = fixture.stage
    if fixture.tournament.tour_id:
        from engine.tour_engine import refresh_tour
        refresh_tour(fixture.tournament.tour)
    db.session.commit()
    return receipt, True


def cleanup(receipt, project_root, instances, instances_lock):
    """Idempotent post-commit cleanup. Failures stay in the durable retry queue."""
    matches = VoidedFixtureMatch.query.filter_by(replay_id=receipt.id).all()
    directory = (Path(project_root) / 'data' / 'matches').resolve()
    errors = []
    for match in matches:
        with instances_lock:
            instances.pop(match.match_id, None)
        paths = {directory / f'match_{match.match_id}.json'}
        if match.json_path and match.json_path != 'autosaved':
            stored = Path(match.json_path)
            paths.add(stored if stored.is_absolute() else directory / stored)
        # Include legacy filenames whose name was not persisted accurately.
        if directory.exists():
            for path in directory.glob('*.json'):
                if path in paths:
                    continue
                try:
                    if json.loads(path.read_text()).get('match_id') == match.match_id:
                        paths.add(path)
                except (OSError, ValueError, AttributeError):
                    continue
        for path in paths:
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(directory):
                    raise OSError('Refused artifact path outside the match directory')
                resolved.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(str(exc))
    # Downloadable archives also contain the old scorecard. Match by embedded ID,
    # never by team names/timestamps (which can collide with unrelated matches).
    invalid_ids = {match.match_id for match in matches}
    for archive in directory.parent.glob('*.zip'):
        try:
            with zipfile.ZipFile(archive) as bundle:
                remove = any(
                    json.loads(bundle.read(info)).get('match_id') in invalid_ids
                    for info in bundle.infolist() if info.filename.endswith('.json')
                )
            if remove:
                archive.unlink()
        except (ValueError, zipfile.BadZipFile, AttributeError):
            continue
        except OSError as exc:
            errors.append(str(exc))
    receipt.cleanup_pending = bool(errors)
    receipt.cleanup_error = '; '.join(errors) or None
    db.session.commit()
    return not errors
