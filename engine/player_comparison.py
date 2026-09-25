"""Read-only cross-format comparisons; pool links are identity, never ownership."""
from collections import defaultdict
from statistics import median

from sqlalchemy import and_, or_
from database import db
from database.models import Player, Team, UserPlayer, Match, MatchScorecard, Tournament

from engine.format_catalog import SUPPORTED_FORMATS
FORMATS = SUPPORTED_FORMATS


def identities(user_id):
    rows = (db.session.query(Player, Team, UserPlayer)
            .join(Team, Player.team_id == Team.id)
            .outerjoin(UserPlayer, and_(Player.user_player_id == UserPlayer.id,
                                       UserPlayer.user_id == user_id))
            .filter(Team.user_id == user_id)
            .filter(or_(Team.is_placeholder.is_(False), Team.is_placeholder.is_(None)))
            .order_by(Player.id).all())
    groups = {}
    for player, team, custom in rows:
        master_id = player.master_player_id or (custom.master_player_id if custom else None)
        key = (f'master:{master_id}' if master_id else
               f'user:{custom.id}' if custom else f'player:{player.id}')
        group = groups.setdefault(key, {
            'id': key, 'name': player.name, 'linked': not key.startswith('player:'),
            'player_ids': [], 'teams': [],
        })
        group['player_ids'].append(player.id)
        if team.name not in group['teams']:
            group['teams'].append(team.name)
    return list(groups.values())


def add_insights(players, formats):
    rules = [('batting', 'strike_rate', 'Scoring pace', True),
             ('batting', 'average', 'Runs per dismissal', True),
             ('bowling', 'economy', 'Run control', False),
             ('bowling', 'strike_rate', 'Wicket-taking frequency', False),
             ('bowling', 'average', 'Cost per wicket', False)]
    for fmt in formats:
        for discipline, metric, label, higher in rules:
            eligible = []
            for player in players:
                data = player['formats'][fmt]
                stats = data[discipline]
                if data['incomplete'] or stats.get(metric) is None:
                    continue
                if stats.get('innings', 0) < 5:
                    continue
                if stats.get('balls', 0) < (100 if discipline == 'batting' else 120):
                    continue
                if discipline == 'batting' and metric == 'average' and stats['innings'] - stats['not_outs'] < 3:
                    continue
                if discipline == 'bowling' and metric != 'economy' and stats['wickets'] < 5:
                    continue
                eligible.append((player, stats[metric]))
            if len(eligible) < 2:
                continue
            values = [value for _, value in eligible]
            midpoint = median(values)
            # A relative percentage difference is undefined at a zero median.
            if midpoint <= 0:
                continue
            for player, value in eligible:
                if values.count(value) != 1 or abs(value - midpoint) / midpoint < .1:
                    continue
                if value not in (min(values), max(values)):
                    continue
                strong = (value > midpoint) == higher
                kind = 'strengths' if strong else 'weaknesses'
                data = player['formats'][fmt]
                if len(data['insights'][kind]) >= 2:
                    continue
                stats = data[discipline]
                data['insights'][kind].append({
                    'label': label, 'discipline': discipline, 'metric': metric,
                    'value': value, 'median': midpoint, 'eligible_players': len(values),
                    'innings': stats['innings'], 'balls': stats['balls'],
                    'direction': 'highest' if value == max(values) else 'lowest',
                })


def compare(service, user_id, identity_ids, tournament_id=None, scheduled_overs=None):
    if len(identity_ids) != len(set(identity_ids)):
        raise ValueError('Select distinct player identities')
    if not 2 <= len(identity_ids) <= 6:
        raise ValueError('Select 2–6 players to compare')
    available = {p['id']: p for p in identities(user_id)}
    if any(key not in available for key in identity_ids):
        raise ValueError('One or more selected players are unavailable')
    formats = list(FORMATS)
    if tournament_id is not None:
        tournament = Tournament.query.filter_by(id=tournament_id, user_id=user_id).first()
        if not tournament:
            raise ValueError('Tournament not found')
        formats = [tournament.format_type]
    players = [dict(available[key], formats={}) for key in identity_ids]
    owners = {pid: p['id'] for p in players for pid in p['player_ids']}
    query = (db.session.query(MatchScorecard, Match)
             .join(Match, MatchScorecard.match_id == Match.id)
             .filter(MatchScorecard.player_id.in_(owners), Match.user_id == user_id,
                     Match.match_format.in_(formats), MatchScorecard.is_super_over.isnot(True)))
    if tournament_id is not None:
        query = query.filter(Match.tournament_id == tournament_id)
    rows = service._filter_length(query, scheduled_overs).order_by(Match.date, Match.id, MatchScorecard.innings_number, MatchScorecard.id).all()
    buckets = defaultdict(list)
    for card, match in rows:
        buckets[(owners[card.player_id], match.match_format)].append((card, match))
    for player in players:
        for fmt in formats:
            batting, bowling, match_ids = [], [], set()
            fielding = dict(catches=0, run_outs=0, stumpings=0)
            incomplete = False
            for card, match in buckets[(player['id'], fmt)]:
                match_ids.add(match.id)
                incomplete = incomplete or bool(match.stats_incomplete)
                meta = {'match_id': match.id, 'date': match.date.isoformat() if match.date else None,
                        'innings_number': card.innings_number}
                if card.record_type == 'batting' and ((card.balls or 0) > 0 or (card.runs or 0) > 0 or card.is_out):
                    batting.append(dict(meta, runs=card.runs or 0, balls=card.balls or 0,
                                        is_out=bool(card.is_out), fours=card.fours or 0, sixes=card.sixes or 0))
                if card.record_type == 'bowling' and (card.balls_bowled or 0) > 0:
                    bowling.append(dict(meta, wickets=card.wickets or 0,
                                        runs=card.runs_conceded or 0, balls=card.balls_bowled))
                for key in fielding:
                    fielding[key] += getattr(card, key) or 0
            bat = service._calculate_batting_metrics(batting)
            bowl = service._calculate_bowling_metrics(bowling, fmt)
            fielding['total_dismissals'] = sum(fielding.values())
            four_runs, six_runs = bat.get('fours', 0) * 4, bat.get('sixes', 0) * 6
            total = bat.get('runs', 0)
            valid_composition = four_runs + six_runs <= total
            player['formats'][fmt] = {
                'matches': len(match_ids), 'has_data': bool(match_ids), 'incomplete': incomplete,
                'batting': bat, 'bowling': bowl, 'fielding': fielding if match_ids else {},
                'recent': {'batting': batting[-10:], 'bowling': bowling[-10:]},
                'composition': {'fours': four_runs, 'sixes': six_runs, 'other': total-four_runs-six_runs,
                                'boundary_percentage': round(100*(four_runs+six_runs)/total, 2) if total else None}
                               if batting and valid_composition else None,
                'insights': {'strengths': [], 'weaknesses': []},
                'sample': {'batting': len(batting) >= 5 and bat.get('balls', 0) >= 100,
                           'bowling': len(bowling) >= 5 and bowl.get('balls', 0) >= 120},
            }
    add_insights(players, formats)
    return {'players': players, 'formats': formats, 'tournament_id': tournament_id}
