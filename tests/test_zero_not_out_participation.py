"""Real 0* appearances count; DNB and ambiguous legacy rows do not."""
import uuid
from datetime import datetime

import pytest
from database import db
from database.models import Match, MatchScorecard, Player, Tournament, TournamentFixture, TournamentPlayerStatsCache
from engine.format_catalog import SUPPORTED_FORMATS, default_scheduled_overs, squad_format
from engine.stats_service import StatsService
from engine.tournament_engine import TournamentEngine
from engine.match import Match as LiveMatch
from match_archiver import MatchArchiver
from test_super_over_career_stats import _make_match


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
@pytest.mark.parametrize('dnb_marker', [False, None])
def test_zero_not_out_participation_across_formats(app, regular_user, test_team, test_team_2, fmt, dnb_marker):
    players = Player.query.filter_by(team_id=test_team.id).order_by(Player.id).limit(4).all()
    for p in players:
        p.profile_id = None
    zero_not_out, dnb, zero_out, faced_dot = players
    tournament = Tournament(name='Participation confirmation', user_id=regular_user.id, mode='round_robin', format_type=fmt)
    db.session.add(tournament)
    db.session.flush()
    match = Match(id=str(uuid.uuid4()), user_id=regular_user.id, home_team_id=test_team.id,
                  away_team_id=test_team_2.id, match_format=fmt, date=datetime.utcnow(), tournament_id=tournament.id)
    db.session.add(match)
    db.session.flush()
    db.session.add(TournamentFixture(tournament_id=tournament.id, match_id=match.id, home_team_id=test_team.id,
                                    away_team_id=test_team_2.id, round_number=1, stage='league', status='Completed'))
    for player in players:
        db.session.add(MatchScorecard(match_id=match.id, player_id=player.id, team_id=test_team.id,
                                     innings_number=1, record_type='batting', runs=0,
                                     balls=int(player == faced_dot), is_out=(player == zero_out),
                                     did_bat=(True if player == zero_not_out else dnb_marker) if player in (zero_not_out, dnb) else None))
    # Super-over appearances must still be excluded from innings statistics.
    db.session.add(MatchScorecard(
        match_id=match.id, player_id=zero_not_out.id, team_id=test_team.id,
        innings_number=3, record_type='batting', runs=99, balls=6,
        is_out=False, is_super_over=True, did_bat=True,
    ))
    db.session.commit()
    service = StatsService()
    overall = service.get_overall_stats(regular_user.id, match_format=fmt)
    assert {row['player_id'] for row in overall['batting']} == {zero_not_out.id, zero_out.id, faced_dot.id}
    insights = service.get_insights(regular_user.id, match_format=fmt)
    form = {row['player']: row['series'] for row in insights['form']['batting']}
    assert form[zero_not_out.name] == [0]
    assert dnb.name not in form
    profile = service.get_player_profile(zero_not_out.id, regular_user.id, match_format=fmt)
    assert (profile['batting']['innings'], profile['batting']['not_outs']) == (1, 1)
    assert profile['batting']['average'] is profile['batting']['strike_rate'] is None
    assert len(profile['match_log'][0]['batting_innings']) == 1
    detailed = service._get_player_detailed_stats(zero_not_out.id, regular_user.id, match_format=fmt)['batting']
    assert (detailed['innings'], detailed['not_outs']) == (1, 1)
    assert service.get_player_profile(dnb.id, regular_user.id, match_format=fmt)['batting'] == {}
    ids = [f'player:{p.id}' for p in (zero_not_out, dnb)]
    compared = service.compare_players_cross_format(regular_user.id, ids)
    batting = compared['players'][0]['formats'][fmt]['batting']
    assert (batting['innings'], batting['not_outs'], batting['runs'], batting['balls']) == (1, 1, 0, 0)
    assert batting['average'] is batting['strike_rate'] is None
    assert len(compared['players'][0]['formats'][fmt]['recent']['batting']) == 1
    assert compared['players'][1]['formats'][fmt]['batting'] == {}
    TournamentEngine().rebuild_player_stats_cache(tournament.id, {p.id for p in players})
    for player in (zero_not_out, dnb):
        row = TournamentPlayerStatsCache.query.filter_by(tournament_id=tournament.id, player_id=player.id).one()
        assert (row.innings_batted, row.not_outs) == ((1, 1) if player == zero_not_out else (0, 0))
        assert row.batting_average is row.batting_strike_rate is None
    cached = service.get_tournament_stats(regular_user.id, tournament.id, match_format=fmt)
    cached_batting = {row['player_id']: row for row in cached['batting']}
    assert dnb.id not in cached_batting
    row = cached_batting[zero_not_out.id]
    assert (row['innings'], row['not_outs'], row['runs'], row['balls']) == (1, 1, 0, 0)
    assert row['average'] is row['strike_rate'] is None
    out_row = TournamentPlayerStatsCache.query.filter_by(tournament_id=tournament.id, player_id=zero_out.id).one()
    faced_row = TournamentPlayerStatsCache.query.filter_by(tournament_id=tournament.id, player_id=faced_dot.id).one()
    assert (out_row.innings_batted, out_row.not_outs) == (1, 0)
    assert (faced_row.innings_batted, faced_row.not_outs) == (1, 1)


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_archive_and_rearchive_preserve_zero_not_out_participation(app, regular_user, test_team, test_team_2, fmt):
    from database.models import TeamProfile
    # Format-specific profiles are required for T10/Hundred archive lookups.
    profile = TeamProfile(team_id=test_team.id, format_type=squad_format(fmt))
    db.session.add(profile)
    db.session.flush()
    for player in Player.query.filter_by(team_id=test_team.id):
        player.profile_id = profile.id
    original = _make_match(regular_user.id)
    data = dict(original.match_data, match_format=fmt, overs=default_scheduled_overs(fmt) or 20,
                scheduled_overs=default_scheduled_overs(fmt))
    match = LiveMatch(data)
    match.result = 'TW won'
    match.first_batting_team_name = 'TW'
    striker = Player.query.filter_by(team_id=test_team.id, name='Bob Keeper').one()
    nonstriker = Player.query.filter_by(team_id=test_team.id, name='John Doe').one()
    unused = Player.query.filter_by(team_id=test_team.id, name='Batsman 1').one()
    stats = {p.name: LiveMatch._new_batting_stats({'id': p.id}) for p in (striker, nonstriker, unused)}
    stats[striker.name].update(runs=1, balls=1, ones=1, did_bat=True)
    stats[nonstriker.name]['did_bat'] = True
    match.current_striker = {'id': striker.id, 'name': striker.name}
    match.current_non_striker = {'id': nonstriker.id, 'name': nonstriker.name}
    match.first_innings_batting_stats = stats
    match.first_innings_score = 1
    if fmt == 'FC':
        match.fc_innings_stats = [{
            'innings_number': 1, 'batting_side': 'home', 'bowling_side': 'away',
            'batting_stats': stats, 'bowling_stats': {},
        }]
        match.fc_innings_totals = {1: {'score': 1, 'wickets': 0, 'overs_str': '0.1'}}
    match.super_over_career_batting = {}
    match.super_over_career_bowling = {}
    arch = MatchArchiver(match.match_data, match)
    arch.filenames = {'json': f'/tmp/{match.match_data["match_id"]}.json'}
    assert arch._save_to_database() is not False
    db.session.commit()
    cards = [MatchScorecard.query.filter_by(match_id=match.match_data['match_id'], player_id=p.id, record_type='batting').one()
             for p in (nonstriker, unused)]
    fields = ('runs', 'balls', 'is_out', 'wicket_type', 'fours', 'sixes')
    assert [getattr(cards[0], f) for f in fields] == [getattr(cards[1], f) for f in fields]
    assert (cards[0].runs, cards[0].balls, cards[0].is_out) == (0, 0, False)
    assert cards[0].did_bat is True
    assert cards[1].did_bat is False
    assert nonstriker.not_outs == (0 if fmt == 'Hundred' else 1)
    assert unused.not_outs == 0
    batting = StatsService().get_overall_stats(regular_user.id)['batting']
    assert {row['player_id'] for row in batting} == {striker.id, nonstriker.id}
    assert arch._save_to_database() is not False
    db.session.commit()
    assert nonstriker.not_outs == (0 if fmt == 'Hundred' else 1)
    from match_archiver import reverse_player_aggregates
    reverse_player_aggregates(MatchScorecard.query.filter_by(match_id=match.match_data['match_id']).all())
    assert nonstriker.not_outs == 0


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_live_delivery_marks_both_batters_and_preserves_dnb(app, regular_user, monkeypatch, fmt):
    import engine.match as match_module
    from test_super_over_career_stats import _build_xi

    data = {
        'match_id': str(uuid.uuid4()), 'created_by': regular_user.id,
        'team_home': f'TW_{regular_user.id}', 'team_away': f'TC_{regular_user.id}',
        'stadium': 'Test Ground', 'pitch': 'Flat', 'weather_forecast': 'clear',
        'toss': 'Heads', 'toss_winner': 'TW', 'toss_decision': 'Bat',
        'match_format': fmt, 'overs': default_scheduled_overs(fmt) or 20,
        'simulation_mode': 'auto',
        'playing_xi': {'home': _build_xi('H'), 'away': _build_xi('A')},
        'substitutes': {'home': [], 'away': []},
    }
    match = LiveMatch(data)
    assert all(not s['did_bat'] for s in match.batsman_stats.values())
    striker = match.current_striker['name']
    nonstriker = match.current_non_striker['name']
    unused = match.batting_team[2]['name']
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kwargs: {
        'runs': 1, 'batter_out': False, 'is_extra': True,
        'extra_type': 'Wide', 'wicket_type': None, 'description': 'Wide',
    })
    result = match.next_ball()
    assert 'error' not in result
    for name in (striker, nonstriker):
        assert match.batsman_stats[name]['did_bat'] is True
        assert (match.batsman_stats[name]['runs'], match.batsman_stats[name]['balls']) == (0, 0)
    assert match.batsman_stats[unused]['did_bat'] is False
    scorecard = match._generate_detailed_scorecard()
    rows = {row['name']: row for row in scorecard['players']}
    assert rows[nonstriker]['status'] == 'not out'
    assert (rows[nonstriker]['runs'], rows[nonstriker]['balls']) == (0, 0)
    assert rows[unused]['runs'] == ''
    assert rows[nonstriker]['strike_rate'] == '—'
    match._save_first_innings_stats()
    assert match.first_innings_batting_stats[nonstriker]['did_bat'] is True
    if fmt == 'FC':
        import json
        snapshot = json.loads(json.dumps(match.serialize_fc_snapshot()))
        restored = LiveMatch(data)
        restored.restore_fc_snapshot(snapshot)
        assert restored.batsman_stats[nonstriker]['did_bat'] is True
        assert restored.batsman_stats[unused]['did_bat'] is False


def test_manual_override_does_not_count_provisional_batter(app, regular_user):
    match = _make_match(regular_user.id)
    provisional, selected = 2, 3
    match.remaining_batter_indices.discard(provisional)
    match._bring_new_batter('striker', provisional)
    provisional_name = match.batting_team[provisional]['name']
    selected_name = match.batting_team[selected]['name']
    assert match.batsman_stats[provisional_name]['did_bat'] is True
    match._create_next_batter_decision('Dismissed batter', provisional, [provisional, selected])
    _response, status = match.submit_pending_decision(selected)
    assert status == 200
    assert match.batsman_stats[provisional_name]['did_bat'] is False
    assert match.batsman_stats[selected_name]['did_bat'] is True


def test_final_ball_wicket_does_not_count_unneeded_next_batter(app, regular_user):
    match = _make_match(regular_user.id)
    match.current_over = match.overs
    match._bring_new_batter('striker', 2)
    assert match.batsman_stats[match.batting_team[2]['name']]['did_bat'] is False


def test_legacy_career_reversal_keeps_original_not_out_rule(app, regular_user, test_team):
    from match_archiver import reverse_player_aggregates
    player = Player.query.filter_by(team_id=test_team.id).first()
    player.not_outs = 2
    # Before participation markers, a zero-ball not-out did not contribute to
    # career not-outs. Reversing one must not subtract a different innings.
    card = MatchScorecard(player_id=player.id, did_bat=None, record_type='batting',
                          runs=1, balls=0, fours=0, sixes=0, is_out=False)
    reverse_player_aggregates([card])
    assert player.not_outs == 2
    card.balls = 1
    reverse_player_aggregates([card])
    assert player.not_outs == 1


def test_archive_exports_include_zero_not_out_and_leave_dnb_empty(app, regular_user, tmp_path):
    import csv
    match = _make_match(regular_user.id)
    arch = MatchArchiver(match.match_data, match)
    arch.archive_path = tmp_path
    lineup = [{'name': 'Unfaced participant'}, {'name': 'Unused batter'}]
    stats = {p['name']: LiveMatch._new_batting_stats(p) for p in lineup}
    stats[lineup[0]['name']]['did_bat'] = True
    text = arch._create_batting_table(stats)
    assert 'Unfaced participant' in text and 'not out' in text
    assert 'Unused batter' not in text
    arch._create_batting_csv('batting.csv', stats, 'Home', lineup)
    with (tmp_path / 'batting.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert (rows[0]['Runs'], rows[0]['Balls'], rows[0]['Status'], rows[0]['Strike Rate']) == ('0', '0', 'not out', '-')
    assert rows[1]['Runs'] == rows[1]['Status'] == ''
