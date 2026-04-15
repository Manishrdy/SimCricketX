# -*- coding: utf-8 -*-
"""
Statistics Service Module
Handles all statistics calculations and queries for the SimCricketX application.
"""

from sqlalchemy import func
from sqlalchemy.orm import aliased
from datetime import datetime
from database.models import Match, MatchScorecard, Tournament, Player, Team, TeamProfile, TournamentPlayerStatsCache
from database import db
from collections import defaultdict
import csv
import io
from tabulate import tabulate

from utils.exception_tracker import log_exception


class StatsService:
    """Service class for calculating and exporting cricket statistics"""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def _log(self, message, level='info'):
        """Safely log messages if logger is available"""
        if self.logger:
            if level == 'error':
                self.logger.error(message)
            elif level == 'warning':
                self.logger.warning(message)
            else:
                self.logger.info(message)
    
    def get_overall_stats(self, user_id, match_format=None):
        """
        Get overall statistics for a user (all tournaments + individual matches).

        Args:
            user_id (str): User ID
            match_format (str, optional): Filter by format — 'T20', 'ListA'

        Returns:
            dict: Statistics dictionary with batting, bowling, fielding, and leaderboards
        """
        self._log(f"Fetching overall stats for user {user_id}, format={match_format}")

        # Query all match scorecards for this user's teams
        query = (
            db.session.query(MatchScorecard, Match, Player, Team)
            .join(Match, MatchScorecard.match_id == Match.id)
            .join(Player, MatchScorecard.player_id == Player.id)
            .join(Team, Player.team_id == Team.id)
            .filter(Team.user_id == user_id)
        )

        if match_format:
            query = query.filter(Match.match_format == match_format)

        records = query.all()
        self._log(f"Found {len(records)} scorecard records for user {user_id}")
        
        if not records:
            return self._empty_stats()
        
        return self._calculate_stats_from_records(records)
    
    def get_tournament_stats(self, user_id, tournament_id, match_format=None):
        """
        Get statistics for a specific tournament.

        Uses TournamentPlayerStatsCache when available (and no format filter
        is applied, since the cache is format-agnostic). Falls back to full
        scorecard computation otherwise.

        Args:
            user_id (str): User ID
            tournament_id (int): Tournament ID
            match_format (str, optional): Filter by format — 'T20', 'ListA'

        Returns:
            dict: Statistics dictionary with batting, bowling, fielding, and leaderboards
        """
        self._log(f"Fetching tournament stats for user {user_id}, tournament {tournament_id}, format={match_format}")

        # Tournaments are single-format (enforced at creation + match save).
        # When the requested format doesn't match the tournament's format,
        # there are zero matches to aggregate — short-circuit to empty.
        # When it does match, the cache is correct by construction.
        tournament = db.session.get(Tournament, tournament_id)
        if tournament and match_format and tournament.format_type != match_format:
            self._log(
                f"Tournament {tournament_id} format={tournament.format_type} "
                f"does not match requested format={match_format}; returning empty"
            )
            return self._empty_stats()

        cached = self._try_cache_tournament_stats(tournament_id, user_id)
        if cached:
            return cached

        # Full computation fallback
        query = (
            db.session.query(MatchScorecard, Match, Player, Team)
            .join(Match, MatchScorecard.match_id == Match.id)
            .join(Player, MatchScorecard.player_id == Player.id)
            .join(Team, Player.team_id == Team.id)
            .filter(Match.tournament_id == tournament_id)
            .filter(Team.user_id == user_id)
        )

        if match_format:
            query = query.filter(Match.match_format == match_format)

        records = query.all()
        self._log(f"Found {len(records)} scorecard records for tournament {tournament_id}")

        if not records:
            return self._empty_stats()

        return self._calculate_stats_from_records(records)

    def _try_cache_tournament_stats(self, tournament_id, user_id):
        """Attempt to serve tournament stats from TournamentPlayerStatsCache.

        Returns the same dict shape as _calculate_stats_from_records() on
        cache hit, or None on cache miss so the caller can fall back to the
        full computation path.
        """
        cached = (
            db.session.query(TournamentPlayerStatsCache, Player, Team)
            .join(Player, TournamentPlayerStatsCache.player_id == Player.id)
            .join(Team, TournamentPlayerStatsCache.team_id == Team.id)
            .filter(TournamentPlayerStatsCache.tournament_id == tournament_id)
            .filter(Team.user_id == user_id)
            .all()
        )
        if not cached:
            return None

        self._log(f"Serving tournament {tournament_id} stats from cache ({len(cached)} players)")

        batting_stats = []
        bowling_stats = []
        fielding_stats = []

        for cache, player, team in cached:
            matches = cache.matches_played or 0
            if matches == 0:
                continue

            # Batting
            innings = cache.innings_batted or 0
            if innings > 0:
                batting_stats.append({
                    'player': player.name, 'team': team.name,
                    'matches': matches, 'innings': innings,
                    'runs': cache.runs_scored or 0,
                    'balls': cache.balls_faced or 0,
                    'not_outs': cache.not_outs or 0,
                    'strike_rate': cache.batting_strike_rate,
                    'average': cache.batting_average,
                    'zeros': 0, 'ones': 0, 'twos': 0, 'threes': 0,
                    'fours': cache.fours or 0,
                    'sixes': cache.sixes or 0,
                    'thirties': 0,
                    'fifties': cache.fifties or 0,
                    'hundreds': cache.centuries or 0,
                })

            # Bowling
            bowl_innings = cache.innings_bowled or 0
            if bowl_innings > 0 or (cache.wickets_taken or 0) > 0:
                best_w = cache.best_bowling_wickets or 0
                best_r = cache.best_bowling_runs or 0
                bowling_stats.append({
                    'team': team.name, 'player': player.name,
                    'matches': matches, 'innings': bowl_innings,
                    'overs': cache.overs_bowled or '0.0',
                    'runs': cache.runs_conceded or 0,
                    'wickets': cache.wickets_taken or 0,
                    'best': f"{best_w}/{best_r}" if best_w > 0 else '-',
                    'average': cache.bowling_average,
                    'economy': cache.bowling_economy or 0.0,
                    'dots': 0, 'bowled': 0, 'lbw': 0,
                    'byes': 0, 'leg_byes': 0,
                    'wides': 0, 'no_balls': 0,
                })

            # Fielding
            fielding_stats.append({
                'player': player.name, 'team': team.name,
                'matches': matches,
                'catches': cache.catches or 0,
                'run_outs': cache.run_outs or 0,
                'stumpings': cache.stumpings or 0,
            })

        batting_stats.sort(key=lambda x: x['runs'], reverse=True)
        bowling_stats.sort(key=lambda x: (-x['wickets'], x['economy']))
        fielding_stats.sort(
            key=lambda x: (x['catches'] + x['run_outs'] + x.get('stumpings', 0), x['matches']),
            reverse=True
        )

        leaderboards = self._calculate_leaderboards(batting_stats, bowling_stats)
        return {
            'batting': batting_stats,
            'bowling': bowling_stats,
            'fielding': fielding_stats,
            'leaderboards': leaderboards,
        }

    def get_insights(self, user_id, tournament_id=None, match_format=None):
        """
        Build advanced insights for the Statistics Hub.

        Returns:
            dict: {
                "impact": [ {player, team, impact, runs, wickets, catches, run_outs} ],
                "form": { "batting": [ {player, team, series} ], "bowling": [ ... ] },
                "conditions": { "venues": [ {label, avg_runs, avg_wkts, matches} ],
                                "pitches": [ {label, avg_runs, avg_wkts, matches} ] }
            }
        """
        insights = {
            "impact": [],
            "form": {"batting": [], "bowling": []},
            "conditions": {"venues": [], "pitches": []}
        }

        # Match-level conditions (venue, pitch)
        match_query = Match.query.filter(Match.user_id == user_id)
        if tournament_id:
            match_query = match_query.filter(Match.tournament_id == tournament_id)
        if match_format:
            match_query = match_query.filter(Match.match_format == match_format)

        venue_agg = {}
        pitch_agg = {}

        for match in match_query.all():
            if match.home_team_score is None or match.away_team_score is None:
                continue
            total_runs = (match.home_team_score or 0) + (match.away_team_score or 0)
            total_wkts = (match.home_team_wickets or 0) + (match.away_team_wickets or 0)

            venue_key = (match.venue or "Unknown Venue").strip()
            venue_stats = venue_agg.setdefault(venue_key, {"runs": 0, "wkts": 0, "matches": 0})
            venue_stats["runs"] += total_runs
            venue_stats["wkts"] += total_wkts
            venue_stats["matches"] += 1

            pitch_key = (match.pitch_type or "Unknown Pitch").strip()
            pitch_stats = pitch_agg.setdefault(pitch_key, {"runs": 0, "wkts": 0, "matches": 0})
            pitch_stats["runs"] += total_runs
            pitch_stats["wkts"] += total_wkts
            pitch_stats["matches"] += 1

        def _top_conditions(agg_map, limit=4):
            items = []
            for label, data in agg_map.items():
                matches = data["matches"] or 1
                items.append({
                    "label": label,
                    "avg_runs": round(data["runs"] / matches, 1),
                    "avg_wkts": round(data["wkts"] / matches, 1),
                    "matches": data["matches"]
                })
            items.sort(key=lambda x: (x["avg_runs"], x["matches"]), reverse=True)
            return items[:limit]

        insights["conditions"]["venues"] = _top_conditions(venue_agg, limit=4)
        insights["conditions"]["pitches"] = _top_conditions(pitch_agg, limit=4)

        # Player-level data for impact + form
        record_query = (
            db.session.query(MatchScorecard, Match, Player, Team)
            .join(Match, MatchScorecard.match_id == Match.id)
            .join(Player, MatchScorecard.player_id == Player.id)
            .join(Team, Player.team_id == Team.id)
            .filter(Team.user_id == user_id)
        )
        if tournament_id:
            record_query = record_query.filter(Match.tournament_id == tournament_id)
        if match_format:
            record_query = record_query.filter(Match.match_format == match_format)

        impact = {}
        batting_forms = {}
        bowling_forms = {}

        for card, match, player, team in record_query.all():
            pid = player.id
            match_date = match.date or datetime.min
            entry = impact.setdefault(pid, {
                "player": player.name,
                "team": team.name,
                "runs": 0,
                "wickets": 0,
                "catches": 0,
                "run_outs": 0
            })

            entry["catches"] += card.catches or 0
            entry["run_outs"] += card.run_outs or 0

            if card.record_type == "batting":
                faced = (card.balls or 0) > 0 or (card.runs or 0) > 0 or bool(card.is_out)
                if faced:
                    entry["runs"] += card.runs or 0
                    batting_forms.setdefault(pid, {
                        "player": player.name,
                        "team": team.name,
                        "series": []
                    })["series"].append((match_date, card.runs or 0))

            if card.record_type == "bowling":
                balls = card.balls_bowled or 0
                if balls > 0 or (card.overs or 0) > 0:
                    entry["wickets"] += card.wickets or 0
                    bowling_forms.setdefault(pid, {
                        "player": player.name,
                        "team": team.name,
                        "series": []
                    })["series"].append((match_date, card.wickets or 0))

        # Impact Index
        impact_list = []
        for data in impact.values():
            impact_score = (
                (data["runs"] or 0)
                + (data["wickets"] or 0) * 20
                + (data["catches"] or 0) * 8
                + (data["run_outs"] or 0) * 10
            )
            impact_list.append({
                "player": data["player"],
                "team": data["team"],
                "impact": impact_score,
                "runs": data["runs"],
                "wickets": data["wickets"],
                "catches": data["catches"],
                "run_outs": data["run_outs"]
            })
        impact_list.sort(key=lambda x: x["impact"], reverse=True)
        insights["impact"] = impact_list[:5]

        def _calc_trend(values):
            """Return 'up', 'down', or 'stable' comparing recent vs older half."""
            if len(values) < 2:
                return "stable"
            mid = len(values) // 2
            older = values[:mid]
            recent = values[mid:]
            avg_old = sum(older) / len(older)
            avg_new = sum(recent) / len(recent)
            # Need >15 % change to count as a trend
            if avg_old == 0:
                return "up" if avg_new > 0 else "stable"
            pct = (avg_new - avg_old) / avg_old
            if pct > 0.15:
                return "up"
            elif pct < -0.15:
                return "down"
            return "stable"

        def _top_form(form_map, key, limit=3):
            items = []
            for pid, data in form_map.items():
                if not data["series"]:
                    continue
                total = sum(v for _, v in data["series"])
                series_sorted = sorted(data["series"], key=lambda x: x[0], reverse=True)[:5]
                series_sorted.reverse()
                values = [v for _, v in series_sorted]
                trend = _calc_trend(values)
                items.append({
                    "player": data["player"],
                    "team": data["team"],
                    "series": values,
                    "total": total,
                    "trend": trend,
                    "recent_avg": round(sum(values[-3:]) / min(3, len(values)), 1) if values else 0,
                })
            items.sort(key=lambda x: x["total"], reverse=True)
            return items[:limit]

        insights["form"]["batting"] = _top_form(batting_forms, "runs", limit=3)
        insights["form"]["bowling"] = _top_form(bowling_forms, "wickets", limit=3)

        return insights
    
    def _empty_stats(self):
        """Return empty statistics structure"""
        return {
            'batting': [],
            'bowling': [],
            'fielding': [],
            'leaderboards': {
                'most_runs': [],
                'most_wickets': [],
                'highest_sr': [],
                'best_average': []
            }
        }
    
    def _calculate_stats_from_records(self, records):
        """
        Calculate statistics from scorecard records.
        
        Args:
            records: List of tuples (MatchScorecard, Match, Player, Team)
            
        Returns:
            dict: Statistics dictionary
        """
        # Aggregate data by player
        player_data = defaultdict(lambda: {
            'name': '',
            'team': '',
            'role': '',
            'player_id': 0,
            'bat_innings_data': [],  # List of individual innings
            'bat_runs': 0,
            'bat_balls': 0,
            'bat_not_outs': 0,
            'bat_fours': 0,
            'bat_sixes': 0,
            'bat_ones': 0,
            'bat_twos': 0,
            'bat_threes': 0,
            'bat_dots': 0,
            'bowl_innings': 0,
            'bowl_balls': 0,
            'bowl_runs': 0,
            'bowl_wickets': 0,
            'bowl_maidens': 0,
            'bowl_dots': 0,
            'bowl_wides': 0,
            'bowl_noballs': 0,
            'bowl_wickets_bowled': 0,
            'bowl_wickets_lbw': 0,
            'bowl_best': (0, 9999),  # (wickets, runs)
            'catches': 0,
            'run_outs': 0,
            'stumpings': 0,
            'matches': set()  # Track unique match IDs
        })
        
        # Process each record
        for card, match, player, team in records:
            pid = player.id
            player_data[pid]['name'] = player.name
            player_data[pid]['team'] = team.name
            player_data[pid]['role'] = player.role or ''
            player_data[pid]['player_id'] = player.id
            player_data[pid]['matches'].add(match.id)
            
            # Batting stats
            if card.record_type == 'batting':
                # Check if player actually faced balls
                faced = (card.balls or 0) > 0 or (card.runs or 0) > 0 or bool(card.is_out)
                
                if faced:
                    # Store individual innings data for milestone calculations
                    player_data[pid]['bat_innings_data'].append({
                        'runs': card.runs or 0,
                        'balls': card.balls or 0,
                        'is_out': card.is_out
                    })
                    
                    if not card.is_out:
                        player_data[pid]['bat_not_outs'] += 1
                
                # Aggregate stats
                player_data[pid]['bat_runs'] += card.runs or 0
                player_data[pid]['bat_balls'] += card.balls or 0
                player_data[pid]['bat_fours'] += card.fours or 0
                player_data[pid]['bat_sixes'] += card.sixes or 0
                player_data[pid]['bat_ones'] += card.ones or 0
                player_data[pid]['bat_twos'] += card.twos or 0
                player_data[pid]['bat_threes'] += card.threes or 0
                player_data[pid]['bat_dots'] += card.dot_balls or 0
            
            # Bowling stats
            if card.record_type == 'bowling':
                balls = card.balls_bowled or 0
                if balls > 0 or (card.overs or 0) > 0:
                    player_data[pid]['bowl_innings'] += 1
                
                player_data[pid]['bowl_balls'] += balls
                player_data[pid]['bowl_runs'] += card.runs_conceded or 0
                player_data[pid]['bowl_wickets'] += card.wickets or 0
                player_data[pid]['bowl_maidens'] += card.maidens or 0
                player_data[pid]['bowl_dots'] += card.dot_balls_bowled or 0
                player_data[pid]['bowl_wides'] += card.wides or 0
                player_data[pid]['bowl_noballs'] += card.noballs or 0
                player_data[pid]['bowl_wickets_bowled'] += card.wickets_bowled or 0
                player_data[pid]['bowl_wickets_lbw'] += card.wickets_lbw or 0
                best_w, best_r = player_data[pid]['bowl_best']
                if (card.wickets or 0) > best_w or ((card.wickets or 0) == best_w and (card.runs_conceded or 0) < best_r):
                    player_data[pid]['bowl_best'] = (card.wickets or 0, card.runs_conceded or 0)
            
            # Fielding stats (recorded in all record types)
            player_data[pid]['catches'] += card.catches or 0
            player_data[pid]['run_outs'] += card.run_outs or 0
            player_data[pid]['stumpings'] += card.stumpings or 0
        
        # Calculate final statistics
        batting_stats = self._calculate_batting_stats(player_data)
        bowling_stats = self._calculate_bowling_stats(player_data)
        fielding_stats = self._calculate_fielding_stats(player_data)
        leaderboards = self._calculate_leaderboards(batting_stats, bowling_stats)
        
        return {
            'batting': batting_stats,
            'bowling': bowling_stats,
            'fielding': fielding_stats,
            'leaderboards': leaderboards
        }
    
    def _calculate_batting_stats(self, player_data):
        """Calculate batting statistics with all required fields"""
        batting_stats = []
        
        for pid, data in player_data.items():
            innings_data = data['bat_innings_data']
            innings = len(innings_data)
            
            if innings == 0:
                continue  # Skip players who haven't batted
            
            matches = len(data['matches'])
            runs = data['bat_runs']
            balls = data['bat_balls']
            not_outs = data['bat_not_outs']
            
            # Calculate average — undefined when player has zero dismissals.
            # Standard cricket convention: shown as '-', not as total runs.
            outs = innings - not_outs
            average = round(runs / outs, 2) if outs > 0 else None

            # Calculate strike rate — undefined when player faced no balls.
            strike_rate = round(runs * 100 / balls, 2) if balls > 0 else None
            
            # Calculate milestones
            zeros = sum(1 for inn in innings_data if inn['runs'] == 0 and inn['is_out'])
            thirties = sum(1 for inn in innings_data if 30 <= inn['runs'] <= 49)
            fifties = sum(1 for inn in innings_data if 50 <= inn['runs'] < 100)
            hundreds = sum(1 for inn in innings_data if inn['runs'] >= 100)
            
            batting_stats.append({
                'player': data['name'],
                'team': data['team'],
                'role': data['role'],
                'player_id': data['player_id'],
                'matches': matches,
                'innings': innings,
                'runs': runs,
                'balls': balls,
                'not_outs': not_outs,
                'strike_rate': strike_rate,
                'average': average,
                'zeros': zeros,
                'ones': data['bat_ones'],
                'twos': data['bat_twos'],
                'threes': data['bat_threes'],
                'fours': data['bat_fours'],
                'sixes': data['bat_sixes'],
                'thirties': thirties,
                'fifties': fifties,
                'hundreds': hundreds
            })
        
        # Sort by runs (descending)
        batting_stats.sort(key=lambda x: x['runs'], reverse=True)
        return batting_stats
    
    def _calculate_bowling_stats(self, player_data):
        """Calculate bowling statistics with all required fields"""
        bowling_stats = []
        
        for pid, data in player_data.items():
            if data['bowl_balls'] == 0 and data['bowl_wickets'] == 0:
                continue  # Skip players who haven't bowled
            
            matches = len(data['matches'])
            innings = data['bowl_innings']
            balls = data['bowl_balls']
            runs = data['bowl_runs']
            wickets = data['bowl_wickets']
            
            # Calculate overs (proper cricket format: 3.2 means 3 overs and 2 balls)
            overs = (balls // 6) + (balls % 6) / 10.0
            
            # Calculate economy rate
            economy = runs / (balls / 6) if balls > 0 else 0
            
            # Calculate average — undefined when bowler has zero wickets.
            # Standard cricket convention: shown as '-', not as 0.
            average = round(runs / wickets, 2) if wickets > 0 else None

            bowling_stats.append({
                'team': data['team'],
                'player': data['name'],
                'role': data['role'],
                'player_id': data['player_id'],
                'matches': matches,
                'innings': innings,
                'overs': round(overs, 1),
                'runs': runs,
                'wickets': wickets,
                'best': f"{data['bowl_best'][0]}/{data['bowl_best'][1]}" if data['bowl_best'][0] > 0 else '-',
                'average': average,
                'economy': round(economy, 2),
                'dots': data['bowl_dots'],
                'bowled': data['bowl_wickets_bowled'],
                'lbw': data['bowl_wickets_lbw'],
                'byes': 0,  # Not tracked in current schema
                'leg_byes': 0,  # Not tracked in current schema
                'wides': data['bowl_wides'],
                'no_balls': data['bowl_noballs']
            })
        
        # Sort by wickets (descending), then by economy (ascending)
        bowling_stats.sort(key=lambda x: (-x['wickets'], x['economy']))
        return bowling_stats
    
    def _calculate_fielding_stats(self, player_data):
        """Calculate fielding statistics"""
        fielding_stats = []
        
        self._log(f"Processing fielding stats for {len(player_data)} players")
        
        for pid, data in player_data.items():
            matches = len(data['matches'])
            catches = data['catches']
            run_outs = data['run_outs']
            stumpings = data['stumpings']

            self._log(f"Player {data['name']}: matches={matches}, catches={catches}, run_outs={run_outs}, stumpings={stumpings}")

            # Include all players who have played matches
            # This gives a complete view of fielding performance
            if matches > 0:
                fielding_stats.append({
                    'player': data['name'],
                    'team': data['team'],
                    'role': data['role'],
                    'player_id': data['player_id'],
                    'matches': matches,
                    'catches': catches,
                    'run_outs': run_outs,
                    'stumpings': stumpings,
                })
        
        self._log(f"Generated {len(fielding_stats)} fielding stat entries")
        
        # Sort by total dismissals (descending), then by matches
        fielding_stats.sort(
            key=lambda x: (x['catches'] + x['run_outs'] + x.get('stumpings', 0), x['matches']),
            reverse=True
        )
        return fielding_stats
    
    def _calculate_leaderboards(self, batting_stats, bowling_stats):
        """Calculate leaderboard data for dashboard widgets.

        Qualification thresholds scale with the dataset so that small
        tournaments still produce meaningful leaderboards:
          - SR: min balls = max(10, median_balls * 0.5), capped at 50
          - Avg: min innings = max(1, total_players // 4), capped at 3
        """
        leaderboards = {
            'most_runs': [],
            'most_wickets': [],
            'highest_sr': [],
            'best_average': []
        }

        # Top 5 run scorers
        leaderboards['most_runs'] = [
            {'player': b['player'], 'team': b['team'], 'runs': b['runs']}
            for b in batting_stats[:5]
        ]

        # Top 5 wicket takers
        leaderboards['most_wickets'] = [
            {'player': b['player'], 'team': b['team'], 'wickets': b['wickets']}
            for b in bowling_stats[:5]
        ]

        # Dynamic SR threshold
        if batting_stats:
            sorted_balls = sorted(b['balls'] for b in batting_stats if b['balls'] > 0)
            median_balls = sorted_balls[len(sorted_balls) // 2] if sorted_balls else 0
            min_balls = max(10, int(median_balls * 0.5))
            min_balls = min(min_balls, 50)
        else:
            min_balls = 50

        # 0-ball players already excluded via min_balls; the `is not None`
        # guard is belt-and-braces and prevents TypeError on undefined SR.
        sr_qualified = [
            b for b in batting_stats
            if b['balls'] >= min_balls and b['strike_rate'] is not None
        ]
        sr_sorted = sorted(sr_qualified, key=lambda x: x['strike_rate'], reverse=True)
        leaderboards['highest_sr'] = [
            {'player': b['player'], 'team': b['team'], 'sr': b['strike_rate']}
            for b in sr_sorted[:5]
        ]

        # Dynamic average threshold
        num_batsmen = len(batting_stats)
        min_innings = max(1, num_batsmen // 4)
        min_innings = min(min_innings, 3)

        # Best Average: a player with zero dismissals has an undefined average,
        # not "infinity". Filter them out so the leaderboard isn't dominated by
        # tail-enders who happened to remain not-out across small sample sizes.
        avg_qualified = [
            b for b in batting_stats
            if b['innings'] >= min_innings and b['average'] is not None
        ]
        avg_sorted = sorted(avg_qualified, key=lambda x: x['average'], reverse=True)
        leaderboards['best_average'] = [
            {'player': b['player'], 'team': b['team'], 'average': b['average']}
            for b in avg_sorted[:5]
        ]

        return leaderboards
    
    def export_to_csv(self, data, stat_type):
        """
        Export statistics to CSV format.
        
        Args:
            data (list): List of statistics dictionaries
            stat_type (str): 'batting', 'bowling', or 'fielding'
            
        Returns:
            str: CSV content as string
        """
        if not data:
            return ""
        
        output = io.StringIO()
        
        if stat_type == 'batting':
            fieldnames = ['player', 'team', 'matches', 'innings', 'runs', 'balls', 
                         'not_outs', 'strike_rate', 'average', 'zeros', 'ones', 
                         'twos', 'threes', 'fours', 'sixes', 'thirties', 'fifties', 'hundreds']
        elif stat_type == 'bowling':
            fieldnames = ['team', 'player', 'matches', 'innings', 'overs', 'runs', 
                         'wickets', 'best', 'average', 'economy', 'dots', 'bowled', 'lbw', 
                         'byes', 'leg_byes', 'wides', 'no_balls']
        else:  # fielding
            fieldnames = ['player', 'team', 'matches', 'catches', 'run_outs']
        
        # Stats rows include extra internal keys (e.g. role/player_id). Ignore
        # unknown keys so exports remain stable across response-shape evolution.
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)
        
        return output.getvalue()
    
    def export_to_txt(self, data, stat_type):
        """
        Export statistics to formatted text table.
        
        Args:
            data (list): List of statistics dictionaries
            stat_type (str): 'batting', 'bowling', or 'fielding'
            
        Returns:
            str: Formatted text table
        """
        if not data:
            return "No data available"
        
        if stat_type == 'batting':
            headers = ['Player', 'Team', 'Mat', 'Inn', 'Runs', 'Balls', 'NO', 
                      'SR', 'Avg', '0s', '1s', '2s', '3s', '4s', '6s', '30s', '50s', '100s']
            rows = [
                [
                    d['player'], d['team'], d['matches'], d['innings'], d['runs'],
                    d['balls'], d['not_outs'], d['strike_rate'], d['average'],
                    d['zeros'], d['ones'], d['twos'], d['threes'], d['fours'],
                    d['sixes'], d['thirties'], d['fifties'], d['hundreds']
                ]
                for d in data
            ]
        elif stat_type == 'bowling':
            headers = ['Team', 'Player', 'Mat', 'Inn', 'Overs', 'Runs', 'Wkts', 
                      'Best', 'Avg', 'Econ', 'Dots', 'Bwld', 'LBW', 'Byes', 'LB', 'Wd', 'NB']
            rows = [
                [
                    d['team'], d['player'], d['matches'], d['innings'], d['overs'],
                    d['runs'], d['wickets'], d['best'], d['average'], d['economy'], d['dots'],
                    d['bowled'], d['lbw'], d['byes'], d['leg_byes'], d['wides'], d['no_balls']
                ]
                for d in data
            ]
        else:  # fielding
            headers = ['Player', 'Team', 'Matches', 'Catches', 'Run Outs']
            rows = [
                [d['player'], d['team'], d['matches'], d['catches'], d['run_outs']]
                for d in data
            ]
        
        return tabulate(rows, headers=headers, tablefmt='grid')
    
    # ============================================================================
    # NEW FEATURE: Best Bowling Figures Tracking
    # ============================================================================
    
    def get_bowling_figures_leaderboard(self, user_id, tournament_id=None, limit=10, match_format=None):
        """
        Get best bowling figures (wickets/runs) leaderboard.
        
        Args:
            user_id (str): User ID
            tournament_id (int, optional): Filter by tournament
            limit (int): Maximum number of entries to return
            
        Returns:
            list: List of bowling figure records sorted by wickets (desc), then runs (asc)
        """
        self._log(f"Fetching bowling figures leaderboard for user {user_id}, tournament {tournament_id}")
        
        try:
            # Build query for bowling records
            query = (
                db.session.query(MatchScorecard, Match, Player, Team)
                .join(Match, MatchScorecard.match_id == Match.id)
                .join(Player, MatchScorecard.player_id == Player.id)
                .join(Team, Player.team_id == Team.id)
                .filter(Team.user_id == user_id)
                .filter(MatchScorecard.record_type == 'bowling')
                .filter(MatchScorecard.wickets > 0)  # Only include actual wickets
            )
            
            # Apply tournament filter if specified
            if tournament_id:
                query = query.filter(Match.tournament_id == tournament_id)
            if match_format:
                query = query.filter(Match.match_format == match_format)

            records = query.all()
            self._log(f"Found {len(records)} bowling records with wickets")
            
            if not records:
                return []
            
            # Convert to list of dicts for easier sorting
            bowling_figures = []
            for card, match, player, team in records:
                # Calculate economy rate
                economy = 0.0
                if card.balls_bowled and card.balls_bowled > 0:
                    economy = (card.runs_conceded * 6.0) / card.balls_bowled
                
                # Determine opponent team
                opponent_name = self._get_opponent_name(match, team.id)
                
                bowling_figures.append({
                    'player': player.name,
                    'team': team.name,
                    'figures': f"{card.wickets}/{card.runs_conceded}",
                    'wickets': card.wickets,
                    'runs': card.runs_conceded,
                    'overs': round(card.overs, 1) if card.overs else 0.0,
                    'economy': round(economy, 2),
                    'maidens': card.maidens or 0,
                    'match_id': match.id,
                    'opponent': opponent_name,
                    'match_date': match.date.strftime('%Y-%m-%d') if match.date else 'N/A',
                    'venue': match.venue or 'N/A',
                    # Wicket type breakdown
                    'bowled': card.wickets_bowled or 0,
                    'caught': card.wickets_caught or 0,
                    'lbw': card.wickets_lbw or 0
                })
            
            # Sort: higher wickets first, then lower runs (better figures)
            bowling_figures.sort(key=lambda x: (-x['wickets'], x['runs']))
            
            # Return top N
            return bowling_figures[:limit]
            
        except Exception as e:
            log_exception(e)
            self._log(f"Error fetching bowling figures: {e}", level='error')
            return []
    
    def _get_opponent_name(self, match, team_id):
        """
        Helper to get opponent team name from a match.
        
        Args:
            match: Match object
            team_id: ID of the team whose opponent we want
            
        Returns:
            str: Opponent team name
        """
        try:
            if match.home_team_id == team_id:
                return match.away_team.name if getattr(match, 'away_team', None) is not None else 'N/A'
            else:
                return match.home_team.name if getattr(match, 'home_team', None) is not None else 'N/A'
        except:
            log_exception(source="backend")
            return 'N/A'
    
    # ============================================================================
    # NEW FEATURE: Player Comparison Tool
    # ============================================================================
    
    def compare_players(self, user_id, player_ids, tournament_id=None, match_format=None):
        """
        Compare multiple players across all metrics.
        
        Args:
            user_id (str): User ID
            player_ids (list): List of player IDs to compare (2-4 recommended)
            tournament_id (int, optional): Filter by tournament
            
        Returns:
            dict: Comprehensive comparison data with stats for each player
        """
        self._log(f"Comparing {len(player_ids)} players for user {user_id}")
        
        if not player_ids or len(player_ids) < 2:
            self._log("Need at least 2 players to compare", level='warning')
            return {'error': 'Select at least 2 players to compare'}
        
        comparison = {
            'players': [],
            'batting_comparison': [],
            'bowling_comparison': [],
            'fielding_comparison': [],
            'head_to_head_matches': []
        }
        
        try:
            for player_id in player_ids:
                player_stats = self._get_player_detailed_stats(player_id, user_id, tournament_id, match_format)
                if player_stats:
                    comparison['players'].append(player_stats)
            
            # Build comparison tables
            if comparison['players']:
                comparison['batting_comparison'] = self._build_batting_comparison(comparison['players'])
                comparison['bowling_comparison'] = self._build_bowling_comparison(comparison['players'])
                comparison['fielding_comparison'] = self._build_fielding_comparison(comparison['players'])
            
            return comparison
            
        except Exception as e:
            log_exception(e)
            self._log(f"Error in player comparison: {e}", level='error')
            return {'error': str(e)}
    
    def _get_player_detailed_stats(self, player_id, user_id, tournament_id=None, match_format=None):
        """
        Get comprehensive stats for a single player.
        
        Args:
            player_id (int): Player ID
            user_id (str): User ID
            tournament_id (int, optional): Filter by tournament
            
        Returns:
            dict: Detailed player statistics
        """
        try:
            # Get player info
            player = Player.query.get(player_id)
            if not player:
                return None
            
            team = Team.query.get(player.team_id)
            if not team or team.user_id != user_id:
                return None
            
            # A Player row is bound to a single TeamProfile (one format). Scope
            # scorecard queries to that format so any stray cross-format records
            # cannot bleed into a single-profile player view.
            profile_format = None
            if player.profile_id:
                profile = TeamProfile.query.get(player.profile_id)
                if profile:
                    profile_format = profile.format_type
            effective_format = match_format or profile_format

            # Query scorecard records
            query = (
                db.session.query(MatchScorecard, Match)
                .join(Match, MatchScorecard.match_id == Match.id)
                .filter(MatchScorecard.player_id == player_id)
                .filter(Match.user_id == user_id)  # Ensure we only get current user's matches
            )

            if tournament_id:
                query = query.filter(Match.tournament_id == tournament_id)
            if effective_format:
                query = query.filter(Match.match_format == effective_format)

            records = query.all()

            # Aggregate stats
            batting_data = []
            bowling_data = []
            catches = 0
            run_outs = 0
            stumpings = 0
            matches = set()
            
            for card, match in records:
                matches.add(match.id)
                
                if card.record_type == 'batting' and ((card.balls or 0) > 0 or (card.runs or 0) > 0 or bool(card.is_out)):
                    batting_data.append({
                        'runs': card.runs or 0,
                        'balls': card.balls or 0,
                        'is_out': card.is_out,
                        'fours': card.fours or 0,
                        'sixes': card.sixes or 0
                    })
                
                if card.record_type == 'bowling' and (card.balls_bowled or 0) > 0:
                    bowling_data.append({
                        'wickets': card.wickets or 0,
                        'runs': card.runs_conceded or 0,
                        'balls': card.balls_bowled or 0
                    })
                
                catches += card.catches or 0
                run_outs += card.run_outs or 0
                stumpings += card.stumpings or 0

            # Calculate batting stats
            batting_stats = self._calculate_batting_metrics(batting_data)
            bowling_stats = self._calculate_bowling_metrics(bowling_data)

            return {
                'player_id': player_id,
                'player_name': player.name,
                'team_name': team.name,
                'matches': len(matches),
                'batting': batting_stats,
                'bowling': bowling_stats,
                'fielding': {
                    'catches': catches,
                    'run_outs': run_outs,
                    'stumpings': stumpings,
                    'total_dismissals': catches + run_outs + stumpings,
                }
            }
            
        except Exception as e:
            log_exception(e)
            self._log(f"Error getting player stats for {player_id}: {e}", level='error')
            return None
    
    def _calculate_batting_metrics(self, innings_list):
        """Calculate batting metrics from innings list"""
        if not innings_list:
            return {}
        
        total_runs = sum(i['runs'] for i in innings_list)
        total_balls = sum(i['balls'] for i in innings_list)
        innings = len(innings_list)
        not_outs = sum(1 for i in innings_list if not i['is_out'])
        outs = innings - not_outs
        
        # Average and SR undefined when respective denominator is 0.
        avg = round(total_runs / outs, 2) if outs > 0 else None
        sr = round(total_runs * 100.0 / total_balls, 2) if total_balls > 0 else None
        high_score = max(i['runs'] for i in innings_list) if innings_list else 0

        return {
            'innings': innings,
            'runs': total_runs,
            'balls': total_balls,
            'average': avg,
            'strike_rate': sr,
            'high_score': high_score,
            'not_outs': not_outs,
            'fours': sum(i['fours'] for i in innings_list),
            'sixes': sum(i['sixes'] for i in innings_list),
            'fifties': sum(1 for i in innings_list if 50 <= i['runs'] < 100),
            'hundreds': sum(1 for i in innings_list if i['runs'] >= 100)
        }
    
    def _calculate_bowling_metrics(self, bowling_list):
        """Calculate bowling metrics from bowling performances"""
        if not bowling_list:
            return {}
        
        total_wickets = sum(b['wickets'] for b in bowling_list)
        total_runs = sum(b['runs'] for b in bowling_list)
        total_balls = sum(b['balls'] for b in bowling_list)
        innings = len(bowling_list)
        
        # Bowling average and strike rate are undefined when bowler has zero
        # wickets (cricket convention — cannot be 0, that would imply best ever).
        avg = round(total_runs / total_wickets, 2) if total_wickets > 0 else None
        economy = (total_runs * 6.0 / total_balls) if total_balls > 0 else 0.0
        sr = round(total_balls / total_wickets, 1) if total_wickets > 0 else None

        best = max(bowling_list, key=lambda x: (x['wickets'], -x['runs'])) if bowling_list else None

        return {
            'innings': innings,
            'wickets': total_wickets,
            'runs': total_runs,
            'balls': total_balls,
            'average': avg,
            'economy': round(economy, 2),
            'strike_rate': sr,
            'best_figures': f"{best['wickets']}/{best['runs']}" if best else 'N/A'
        }
    
    def _build_batting_comparison(self, players):
        """Build batting comparison table"""
        return [{
            'player': p['player_name'],
            'team': p['team_name'],
            'innings': p['batting'].get('innings', 0),
            'runs': p['batting'].get('runs', 0),
            'average': p['batting'].get('average', 0),
            'strike_rate': p['batting'].get('strike_rate', 0),
            'high_score': p['batting'].get('high_score', 0),
            'fifties': p['batting'].get('fifties', 0),
            'hundreds': p['batting'].get('hundreds', 0)
        } for p in players if p.get('batting')]
    
    def _build_bowling_comparison(self, players):
        """Build bowling comparison table"""
        return [{
            'player': p['player_name'],
            'team': p['team_name'],
            'innings': p['bowling'].get('innings', 0),
            'wickets': p['bowling'].get('wickets', 0),
            'average': p['bowling'].get('average', 0),
            'economy': p['bowling'].get('economy', 0),
            'strike_rate': p['bowling'].get('strike_rate', 0),
            'best_figures': p['bowling'].get('best_figures', 'N/A')
        } for p in players if p.get('bowling')]
    
    def _build_fielding_comparison(self, players):
        """Build fielding comparison table"""
        return [{
            'player': p['player_name'],
            'team': p['team_name'],
            'catches': p['fielding'].get('catches', 0),
            'run_outs': p['fielding'].get('run_outs', 0),
            'total_dismissals': p['fielding'].get('total_dismissals', 0)
        } for p in players if p.get('fielding')]
    
    # ============================================================================
    # NEW FEATURE: Partnership Statistics
    # ============================================================================
    
    def get_player_partnership_stats(self, player_id, user_id, tournament_id=None, match_format=None):
        """
        Get comprehensive partnership statistics for a specific player.
        
        Args:
            player_id (int): Player ID
            user_id (str): User ID
            tournament_id (int, optional): Filter by tournament
            
        Returns:
            dict: Partnership statistics including best partnerships, frequent partners, etc.
        """
        self._log(f"Fetching partnership stats for player {player_id}")
        
        try:
            from database.models import MatchPartnership
            
            # Verify player belongs to user
            player = Player.query.get(player_id)
            if not player:
                return {'error': 'Player not found'}
            
            team = Team.query.get(player.team_id)
            if not team or team.user_id != user_id:
                return {'error': 'Unauthorized'}
            
            # Query partnerships where this player is involved
            query = (
                db.session.query(MatchPartnership, Match)
                .join(Match, MatchPartnership.match_id == Match.id)
                .filter(
                    (MatchPartnership.batsman1_id == player_id) | 
                    (MatchPartnership.batsman2_id == player_id)
                )
            )
            
            if tournament_id:
                query = query.filter(Match.tournament_id == tournament_id)
            if match_format:
                query = query.filter(Match.match_format == match_format)

            partnerships = query.all()
            
            if not partnerships:
                return {
                    'player_name': player.name,
                    'total_partnerships': 0,
                    'best_partnerships': [],
                    'partners': [],
                    'milestones': {'50+': 0, '100+': 0, '150+': 0}
                }
            
            return self._analyze_partnerships(player_id, player.name, partnerships)
            
        except Exception as e:
            log_exception(e)
            self._log(f"Error fetching partnership stats: {e}", level='error')
            return {'error': str(e)}
    
    def _analyze_partnerships(self, player_id, player_name, partnerships):
        """
        Analyze partnership data for insights.
        
        Args:
            player_id (int): Player ID
            player_name (str): Player name
            partnerships (list): List of (MatchPartnership, Match) tuples
            
        Returns:
            dict: Analyzed partnership statistics
        """
        stats = {
            'player_name': player_name,
            'total_partnerships': len(partnerships),
            'best_partnerships': [],
            'partners': {},
            'by_position': {},
            'milestones': {'50+': 0, '100+': 0, '150+': 0, '200+': 0},
            'total_runs': 0,
            'average_partnership': 0
        }
        
        total_runs = 0
        
        for partnership, match in partnerships:
            # Determine partner
            is_batsman1 = (partnership.batsman1_id == player_id)
            partner_id = partnership.batsman2_id if is_batsman1 else partnership.batsman1_id
            
            # Get partner info
            partner = Player.query.get(partner_id)
            if not partner:
                continue
            
            partner_name = partner.name
            player_contribution = (partnership.batsman1_contribution if is_batsman1 
                                 else partnership.batsman2_contribution)
            
            # Track by partner
            if partner_id not in stats['partners']:
                stats['partners'][partner_id] = {
                    'name': partner_name,
                    'count': 0,
                    'total_runs': 0,
                    'best': 0,
                    'avg_runs': 0
                }
            
            stats['partners'][partner_id]['count'] += 1
            stats['partners'][partner_id]['total_runs'] += partnership.runs
            stats['partners'][partner_id]['best'] = max(
                stats['partners'][partner_id]['best'], 
                partnership.runs
            )
            
            # Track by wicket position
            wicket = partnership.wicket_number
            if wicket not in stats['by_position']:
                stats['by_position'][wicket] = {'count': 0, 'runs': 0}
            stats['by_position'][wicket]['count'] += 1
            stats['by_position'][wicket]['runs'] += partnership.runs
            
            # Track milestones
            if partnership.runs >= 50:
                stats['milestones']['50+'] += 1
            if partnership.runs >= 100:
                stats['milestones']['100+'] += 1
            if partnership.runs >= 150:
                stats['milestones']['150+'] += 1
            if partnership.runs >= 200:
                stats['milestones']['200+'] += 1
            
            total_runs += partnership.runs
            
            # Determine opponent
            opponent = self._get_opponent_name(match, partner.team_id)
            
            # Add to best partnerships list
            stats['best_partnerships'].append({
                'partner': partner_name,
                'runs': partnership.runs,
                'balls': partnership.balls,
                'player_contribution': player_contribution,
                'partner_contribution': (partnership.batsman2_contribution if is_batsman1 
                                        else partnership.batsman1_contribution),
                'wicket': wicket,
                'match_id': match.id,
                'opponent': opponent,
                'date': match.date.strftime('%Y-%m-%d') if match.date else 'N/A'
            })
        
        # Calculate averages
        for partner_stats in stats['partners'].values():
            partner_stats['avg_runs'] = round(
                partner_stats['total_runs'] / partner_stats['count'], 1
            )
        
        # Sort best partnerships
        stats['best_partnerships'].sort(key=lambda x: x['runs'], reverse=True)
        
        # Calculate overall averages
        stats['total_runs'] = total_runs
        stats['average_partnership'] = round(total_runs / len(partnerships), 1) if partnerships else 0
        
        # Convert partners dict to sorted list
        stats['partners'] = sorted(
            stats['partners'].values(), 
            key=lambda x: x['total_runs'], 
            reverse=True
        )
        
        return stats
    
    def get_tournament_partnership_leaderboard(self, user_id, tournament_id, limit=10, match_format=None):
        """
        Get best partnerships in a tournament.
        
        Args:
            user_id (str): User ID
            tournament_id (int): Tournament ID
            limit (int): Maximum number of entries
            
        Returns:
            list: Top partnerships sorted by runs
        """
        self._log(f"Fetching tournament partnership leaderboard for tournament {tournament_id}")
        
        try:
            from database.models import MatchPartnership
            
            # Query partnerships from tournament matches with both batsmen joined
            Batsman2 = aliased(Player)
            partnerships = (
                db.session.query(MatchPartnership, Match, Player, Batsman2, Team)
                .join(Match, MatchPartnership.match_id == Match.id)
                .join(Player, MatchPartnership.batsman1_id == Player.id)
                .join(Batsman2, MatchPartnership.batsman2_id == Batsman2.id)
                .join(Team, Player.team_id == Team.id)
                .filter(Match.tournament_id == tournament_id)
                .filter(Team.user_id == user_id)
            )
            if match_format:
                partnerships = partnerships.filter(Match.match_format == match_format)
            partnerships = (
                partnerships
                .order_by(MatchPartnership.runs.desc())
                .limit(limit)
                .all()
            )

            leaderboard = []
            for partnership, match, batsman1, batsman2, team in partnerships:
                
                opponent = self._get_opponent_name(match, team.id)
                
                leaderboard.append({
                    'batsman1': batsman1.name,
                    'batsman2': batsman2.name,
                    'team': team.name,
                    'runs': partnership.runs,
                    'balls': partnership.balls,
                    'batsman1_contribution': partnership.batsman1_contribution,
                    'batsman2_contribution': partnership.batsman2_contribution,
                    'wicket': partnership.wicket_number,
                    'opponent': opponent,
                    'match_id': match.id
                })
            
            return leaderboard
            
        except Exception as e:
            log_exception(e)
            self._log(f"Error fetching partnership leaderboard: {e}", level='error')
            return []

    # ========================================================================
    # Head-to-Head Team Comparison
    # ========================================================================

    def get_head_to_head(self, user_id, team1_id, team2_id, match_format=None):
        """Compare two teams' records against each other."""
        try:
            team1 = Team.query.get(team1_id)
            team2 = Team.query.get(team2_id)
            if not team1 or not team2:
                return {"error": "One or both teams not found"}
            if team1.user_id != user_id or team2.user_id != user_id:
                return {"error": "Unauthorized"}

            query = Match.query.filter(
                Match.user_id == user_id,
                db.or_(
                    db.and_(Match.home_team_id == team1_id, Match.away_team_id == team2_id),
                    db.and_(Match.home_team_id == team2_id, Match.away_team_id == team1_id),
                ),
            )
            if match_format:
                query = query.filter(Match.match_format == match_format)

            matches = query.order_by(Match.date.desc()).all()
            if not matches:
                return {
                    "team1": team1.name, "team2": team2.name,
                    "matches": [], "summary": {"played": 0, "team1_wins": 0, "team2_wins": 0, "ties": 0},
                    "top_performers": {"team1": [], "team2": []},
                }

            t1_wins = t2_wins = ties = 0
            match_list = []
            match_ids = []
            for m in matches:
                match_ids.append(m.id)
                if m.winner_team_id == team1_id:
                    t1_wins += 1
                elif m.winner_team_id == team2_id:
                    t2_wins += 1
                else:
                    ties += 1
                match_list.append({
                    "match_id": m.id,
                    "date": m.date.strftime("%Y-%m-%d") if m.date else "",
                    "venue": m.venue or "",
                    "home": team1.name if m.home_team_id == team1_id else team2.name,
                    "away": team2.name if m.home_team_id == team1_id else team1.name,
                    "result": m.result_description or "",
                    "home_score": f"{m.home_team_score or 0}/{m.home_team_wickets or 0}",
                    "away_score": f"{m.away_team_score or 0}/{m.away_team_wickets or 0}",
                    "format": m.match_format or "T20",
                })

            # Top performers in these H2H matches
            def _top_performers(team_id):
                cards = (
                    db.session.query(
                        Player.name,
                        func.sum(db.case((MatchScorecard.record_type == "batting", MatchScorecard.runs), else_=0)).label("runs"),
                        func.sum(db.case((MatchScorecard.record_type == "bowling", MatchScorecard.wickets), else_=0)).label("wkts"),
                        func.count(func.distinct(MatchScorecard.match_id)).label("mat"),
                    )
                    .join(Player, MatchScorecard.player_id == Player.id)
                    .filter(
                        MatchScorecard.match_id.in_(match_ids),
                        MatchScorecard.team_id == team_id,
                    )
                    .group_by(Player.name)
                    .all()
                )
                perfs = []
                for name, runs, wkts, mat in cards:
                    impact = (runs or 0) + (wkts or 0) * 20
                    perfs.append({"name": name, "runs": runs or 0, "wickets": wkts or 0, "matches": mat, "impact": impact})
                perfs.sort(key=lambda x: x["impact"], reverse=True)
                return perfs[:5]

            return {
                "team1": team1.name, "team2": team2.name,
                "team1_id": team1_id, "team2_id": team2_id,
                "matches": match_list,
                "summary": {"played": len(matches), "team1_wins": t1_wins, "team2_wins": t2_wins, "ties": ties},
                "top_performers": {"team1": _top_performers(team1_id), "team2": _top_performers(team2_id)},
            }
        except Exception as e:
            log_exception(e)
            self._log(f"Error in head-to-head: {e}", level="error")
            return {"error": str(e)}

    # ========================================================================
    # Player Profile
    # ========================================================================

    def get_player_profile(self, player_id, user_id, match_format=None):
        """Get full career stats + match log for a single player."""
        try:
            player = Player.query.get(player_id)
            if not player:
                return {"error": "Player not found"}
            team = Team.query.get(player.team_id)
            if not team or team.user_id != user_id:
                return {"error": "Unauthorized"}

            # A Player row is bound to a TeamProfile (one format). Constrain the
            # scorecard query to that profile's format so legacy or mis-archived
            # cross-format records cannot bleed into a single-profile player view.
            profile_format = None
            if player.profile_id:
                profile = TeamProfile.query.get(player.profile_id)
                if profile:
                    profile_format = profile.format_type

            effective_format = match_format or profile_format

            query = (
                db.session.query(MatchScorecard, Match)
                .join(Match, MatchScorecard.match_id == Match.id)
                .filter(MatchScorecard.player_id == player_id, Match.user_id == user_id)
            )
            if effective_format:
                query = query.filter(Match.match_format == effective_format)

            records = query.order_by(Match.date.desc()).all()

            batting_innings = []
            bowling_innings = []
            catches = run_outs = stumpings = 0
            match_set = set()
            match_log = {}

            for card, match in records:
                match_set.add(match.id)
                ml = match_log.setdefault(match.id, {
                    "match_id": match.id,
                    "date": match.date.strftime("%Y-%m-%d") if match.date else "",
                    "venue": match.venue or "",
                    "format": match.match_format or "T20",
                    "result": match.result_description or "",
                    "bat_runs": None, "bat_balls": None, "bat_out": None,
                    "bowl_wkts": None, "bowl_runs": None, "bowl_overs": None,
                    "catches": 0, "run_outs": 0, "stumpings": 0,
                })
                ml["catches"] += card.catches or 0
                ml["run_outs"] += card.run_outs or 0
                ml["stumpings"] += card.stumpings or 0
                catches += card.catches or 0
                run_outs += card.run_outs or 0
                stumpings += card.stumpings or 0

                if card.record_type == "batting" and ((card.balls or 0) > 0 or (card.runs or 0) > 0 or card.is_out):
                    batting_innings.append({
                        "runs": card.runs or 0, "balls": card.balls or 0,
                        "is_out": card.is_out, "fours": card.fours or 0, "sixes": card.sixes or 0,
                    })
                    ml["bat_runs"] = card.runs or 0
                    ml["bat_balls"] = card.balls or 0
                    ml["bat_out"] = card.is_out

                if card.record_type == "bowling" and (card.balls_bowled or 0) > 0:
                    bowling_innings.append({
                        "wickets": card.wickets or 0, "runs": card.runs_conceded or 0,
                        "balls": card.balls_bowled or 0,
                    })
                    ml["bowl_wkts"] = card.wickets or 0
                    ml["bowl_runs"] = card.runs_conceded or 0
                    balls = card.balls_bowled or 0
                    ml["bowl_overs"] = f"{balls // 6}.{balls % 6}"

            batting = self._calculate_batting_metrics(batting_innings)
            bowling = self._calculate_bowling_metrics(bowling_innings)

            # Milestones
            milestones = []
            if batting.get("hundreds", 0) > 0:
                milestones.append(f"{batting['hundreds']} centuries")
            if batting.get("fifties", 0) > 0:
                milestones.append(f"{batting['fifties']} half-centuries")
            if bowling.get("wickets", 0) >= 50:
                milestones.append(f"{bowling['wickets']} career wickets")
            if batting.get("runs", 0) >= 500:
                milestones.append(f"{batting['runs']} career runs")

            return {
                "player_id": player.id,
                "player": player.name,
                "team": team.name,
                "team_id": team.id,
                "role": player.role or "Unknown",
                "batting_hand": player.batting_hand or "",
                "bowling_type": player.bowling_type or "",
                "is_captain": player.is_captain,
                "is_wicketkeeper": player.is_wicketkeeper,
                "matches": len(match_set),
                "batting": batting,
                "bowling": bowling,
                "fielding": {"catches": catches, "run_outs": run_outs, "stumpings": stumpings, "total": catches + run_outs + stumpings},
                "milestones": milestones,
                "match_log": sorted(match_log.values(), key=lambda x: x["date"], reverse=True),
            }
        except Exception as e:
            log_exception(e)
            self._log(f"Error in player profile: {e}", level="error")
            return {"error": str(e)}

    # ========================================================================
    # Team Statistics Dashboard
    # ========================================================================

    def get_team_stats(self, user_id, team_id, match_format=None):
        """Get aggregate team-level statistics."""
        try:
            team = Team.query.get(team_id)
            if not team or team.user_id != user_id:
                return {"error": "Team not found or unauthorized"}

            query = Match.query.filter(
                Match.user_id == user_id,
                db.or_(Match.home_team_id == team_id, Match.away_team_id == team_id),
            )
            if match_format:
                query = query.filter(Match.match_format == match_format)

            matches = query.order_by(Match.date.desc()).all()
            if not matches:
                return {"team": team.name, "matches": 0, "summary": {}, "recent": [], "batting_first": {}, "chasing": {}}

            wins = losses = ties = 0
            bat_first_wins = bat_first_total = 0
            chase_wins = chase_total = 0
            total_scored = total_conceded = 0
            recent = []

            for m in matches:
                is_home = m.home_team_id == team_id
                team_score = m.home_team_score if is_home else m.away_team_score
                opp_score = m.away_team_score if is_home else m.home_team_score
                total_scored += team_score or 0
                total_conceded += opp_score or 0

                if m.winner_team_id == team_id:
                    wins += 1
                elif m.winner_team_id is not None:
                    losses += 1
                else:
                    ties += 1

                # Determine if team batted first
                first_bat_home = True  # default assumption
                if m.toss_winner_team_id and m.toss_decision:
                    toss_is_home = m.toss_winner_team_id == m.home_team_id
                    chose_bat = m.toss_decision.lower() == "bat"
                    first_bat_home = (toss_is_home and chose_bat) or (not toss_is_home and not chose_bat)

                team_batted_first = (is_home and first_bat_home) or (not is_home and not first_bat_home)
                if team_batted_first:
                    bat_first_total += 1
                    if m.winner_team_id == team_id:
                        bat_first_wins += 1
                else:
                    chase_total += 1
                    if m.winner_team_id == team_id:
                        chase_wins += 1

                opp_team = Team.query.get(m.away_team_id if is_home else m.home_team_id)
                if len(recent) < 10:
                    team_wickets = (m.home_team_wickets if is_home else m.away_team_wickets) or 0
                    opp_wickets = (m.away_team_wickets if is_home else m.home_team_wickets) or 0
                    recent.append({
                        "date": m.date.strftime("%Y-%m-%d") if m.date else "",
                        "opponent": opp_team.name if opp_team else "Unknown",
                        "result": "W" if m.winner_team_id == team_id else ("L" if m.winner_team_id else "T"),
                        "score": f"{team_score or 0}/{team_wickets}",
                        "opp_score": f"{opp_score or 0}/{opp_wickets}",
                        "venue": m.venue or "",
                        "format": m.match_format or "T20",
                    })

            played = len(matches)
            avg_scored = round(total_scored / played, 1) if played else 0
            avg_conceded = round(total_conceded / played, 1) if played else 0

            return {
                "team": team.name,
                "team_id": team_id,
                "team_color": team.team_color or "#6366f1",
                "matches": played,
                "summary": {
                    "played": played, "won": wins, "lost": losses, "tied": ties,
                    "win_pct": round(wins * 100 / played, 1) if played else 0,
                    "avg_scored": avg_scored,
                    "avg_conceded": avg_conceded,
                },
                "batting_first": {
                    "played": bat_first_total,
                    "won": bat_first_wins,
                    "win_pct": round(bat_first_wins * 100 / bat_first_total, 1) if bat_first_total else 0,
                },
                "chasing": {
                    "played": chase_total,
                    "won": chase_wins,
                    "win_pct": round(chase_wins * 100 / chase_total, 1) if chase_total else 0,
                },
                "recent": recent,
            }
        except Exception as e:
            log_exception(e)
            self._log(f"Error in team stats: {e}", level="error")
            return {"error": str(e)}

    # ========================================================================
    # Milestone Detection (used during match archival)
    # ========================================================================

    @staticmethod
    def detect_milestones(player_id, deltas=None):
        """Check if a player has reached any career milestones.

        Args:
            player_id: Player whose aggregates were just updated.
            deltas: Optional dict with this match's contribution
                ``{"runs": int, "wickets": int, "catches": int}``.
                When supplied, the "previous total" is computed exactly as
                ``current - delta``, so multi-wicket / multi-run jumps that
                straddle a milestone (e.g. 24 → 26 wickets crosses 25) fire
                correctly. When omitted, falls back to a coarse delta=1
                heuristic that may miss multi-event matches.

        Returns a list of milestone strings (empty if none reached).
        Called after aggregates are updated during match archival.
        """
        player = Player.query.get(player_id)
        if not player:
            return []

        d = deltas or {}
        runs_delta = max(int(d.get("runs", 1) or 0), 1)
        wickets_delta = max(int(d.get("wickets", 1) or 0), 1)
        catches_delta = max(int(d.get("catches", 1) or 0), 1)

        milestones = []
        run_marks = [500, 1000, 2000, 3000, 5000]
        for mark in run_marks:
            if player.total_runs >= mark and (player.total_runs - runs_delta) < mark:
                milestones.append(f"{player.name} reached {mark} career runs!")

        wkt_marks = [25, 50, 100, 150, 200]
        for mark in wkt_marks:
            if player.total_wickets >= mark and (player.total_wickets - wickets_delta) < mark:
                milestones.append(f"{player.name} reached {mark} career wickets!")

        catch_total = (
            db.session.query(func.coalesce(func.sum(MatchScorecard.catches), 0))
            .filter(MatchScorecard.player_id == player_id)
            .scalar()
        ) or 0
        for mark in [10, 25, 50]:
            if catch_total >= mark and (catch_total - catches_delta) < mark:
                milestones.append(f"{player.name} reached {mark} career catches!")

        return milestones
