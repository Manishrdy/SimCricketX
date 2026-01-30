# -*- coding: utf-8 -*-
"""
Statistics Service Module
Handles all statistics calculations and queries for the SimCricketX application.
"""

from sqlalchemy import func
from datetime import datetime
from database.models import Match, MatchScorecard, Tournament, Player, Team
from database import db
from collections import defaultdict
import csv
import io
from tabulate import tabulate


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
    
    def get_overall_stats(self, user_id):
        """
        Get overall statistics for a user (all tournaments + individual matches).
        
        Args:
            user_id (str): User ID
            
        Returns:
            dict: Statistics dictionary with batting, bowling, fielding, and leaderboards
        """
        self._log(f"Fetching overall stats for user {user_id}")
        
        # Query all match scorecards for this user's teams
        query = (
            db.session.query(MatchScorecard, Match, Player, Team)
            .join(Match, MatchScorecard.match_id == Match.id)
            .join(Player, MatchScorecard.player_id == Player.id)
            .join(Team, Player.team_id == Team.id)
            .filter(Team.user_id == user_id)
        )
        
        records = query.all()
        self._log(f"Found {len(records)} scorecard records for user {user_id}")
        
        if not records:
            return self._empty_stats()
        
        return self._calculate_stats_from_records(records)
    
    def get_tournament_stats(self, user_id, tournament_id):
        """
        Get statistics for a specific tournament.
        
        Args:
            user_id (str): User ID
            tournament_id (int): Tournament ID
            
        Returns:
            dict: Statistics dictionary with batting, bowling, fielding, and leaderboards
        """
        self._log(f"Fetching tournament stats for user {user_id}, tournament {tournament_id}")
        
        # Query scorecards for specific tournament
        query = (
            db.session.query(MatchScorecard, Match, Player, Team)
            .join(Match, MatchScorecard.match_id == Match.id)
            .join(Player, MatchScorecard.player_id == Player.id)
            .join(Team, Player.team_id == Team.id)
            .filter(Match.tournament_id == tournament_id)
            .filter(Team.user_id == user_id)
        )
        
        records = query.all()
        self._log(f"Found {len(records)} scorecard records for tournament {tournament_id}")
        
        if not records:
            return self._empty_stats()
        
        return self._calculate_stats_from_records(records)

    def get_insights(self, user_id, tournament_id=None):
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

        def _top_form(form_map, key, limit=3):
            items = []
            for pid, data in form_map.items():
                if not data["series"]:
                    continue
                total = sum(v for _, v in data["series"])
                series_sorted = sorted(data["series"], key=lambda x: x[0], reverse=True)[:5]
                series_sorted.reverse()
                items.append({
                    "player": data["player"],
                    "team": data["team"],
                    "series": [v for _, v in series_sorted],
                    "total": total
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
            'matches': set()  # Track unique match IDs
        })
        
        # Process each record
        for card, match, player, team in records:
            pid = player.id
            player_data[pid]['name'] = player.name
            player_data[pid]['team'] = team.name
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
            
            # Calculate average
            outs = innings - not_outs
            average = runs / outs if outs > 0 else runs
            
            # Calculate strike rate
            strike_rate = (runs * 100 / balls) if balls > 0 else 0
            
            # Calculate milestones
            zeros = sum(1 for inn in innings_data if inn['runs'] == 0 and inn['is_out'])
            thirties = sum(1 for inn in innings_data if 30 <= inn['runs'] <= 49)
            fifties = sum(1 for inn in innings_data if 50 <= inn['runs'] < 100)
            hundreds = sum(1 for inn in innings_data if inn['runs'] >= 100)
            
            batting_stats.append({
                'player': data['name'],
                'team': data['team'],
                'matches': matches,
                'innings': innings,
                'runs': runs,
                'balls': balls,
                'not_outs': not_outs,
                'strike_rate': round(strike_rate, 2),
                'average': round(average, 2),
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
            
            # Calculate average
            average = runs / wickets if wickets > 0 else 0
            
            bowling_stats.append({
                'team': data['team'],
                'player': data['name'],
                'matches': matches,
                'innings': innings,
                'overs': round(overs, 1),
                'runs': runs,
                'wickets': wickets,
                'best': f"{data['bowl_best'][0]}/{data['bowl_best'][1]}" if data['bowl_best'][0] > 0 else '-',
                'average': round(average, 2),
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
            
            self._log(f"Player {data['name']}: matches={matches}, catches={catches}, run_outs={run_outs}")
            
            # Include all players who have played matches
            # This gives a complete view of fielding performance
            if matches > 0:
                fielding_stats.append({
                    'player': data['name'],
                    'team': data['team'],
                    'matches': matches,
                    'catches': catches,
                    'run_outs': run_outs
                })
        
        self._log(f"Generated {len(fielding_stats)} fielding stat entries")
        
        # Sort by total dismissals (descending), then by matches
        fielding_stats.sort(key=lambda x: (x['catches'] + x['run_outs'], x['matches']), reverse=True)
        return fielding_stats
    
    def _calculate_leaderboards(self, batting_stats, bowling_stats):
        """Calculate leaderboard data for dashboard widgets"""
        leaderboards = {
            'most_runs': [],
            'most_wickets': [],
            'highest_sr': [],
            'best_average': []
        }
        
        # Top 5 run scorers
        leaderboards['most_runs'] = [
            {
                'player': b['player'],
                'team': b['team'],
                'runs': b['runs']
            }
            for b in batting_stats[:5]
        ]
        
        # Top 5 wicket takers
        leaderboards['most_wickets'] = [
            {
                'player': b['player'],
                'team': b['team'],
                'wickets': b['wickets']
            }
            for b in bowling_stats[:5]
        ]
        
        # Top 5 strike rates (minimum 50 balls faced)
        sr_qualified = [b for b in batting_stats if b['balls'] >= 50]
        sr_sorted = sorted(sr_qualified, key=lambda x: x['strike_rate'], reverse=True)
        leaderboards['highest_sr'] = [
            {
                'player': b['player'],
                'team': b['team'],
                'sr': b['strike_rate']
            }
            for b in sr_sorted[:5]
        ]
        
        # Top 5 averages (minimum 3 innings)
        avg_qualified = [b for b in batting_stats if b['innings'] >= 3]
        avg_sorted = sorted(avg_qualified, key=lambda x: x['average'], reverse=True)
        leaderboards['best_average'] = [
            {
                'player': b['player'],
                'team': b['team'],
                'average': b['average']
            }
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
        
        writer = csv.DictWriter(output, fieldnames=fieldnames)
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
    
    def get_bowling_figures_leaderboard(self, user_id, tournament_id=None, limit=10):
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
                return match.away_team.name if hasattr(match, 'away_team') else 'N/A'
            else:
                return match.home_team.name if hasattr(match, 'home_team') else 'N/A'
        except:
            return 'N/A'
    
    # ============================================================================
    # NEW FEATURE: Player Comparison Tool
    # ============================================================================
    
    def compare_players(self, user_id, player_ids, tournament_id=None):
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
                player_stats = self._get_player_detailed_stats(player_id, user_id, tournament_id)
                if player_stats:
                    comparison['players'].append(player_stats)
            
            # Build comparison tables
            if comparison['players']:
                comparison['batting_comparison'] = self._build_batting_comparison(comparison['players'])
                comparison['bowling_comparison'] = self._build_bowling_comparison(comparison['players'])
                comparison['fielding_comparison'] = self._build_fielding_comparison(comparison['players'])
            
            return comparison
            
        except Exception as e:
            self._log(f"Error in player comparison: {e}", level='error')
            return {'error': str(e)}
    
    def _get_player_detailed_stats(self, player_id, user_id, tournament_id=None):
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
            
            # Query scorecard records
            query = (
                db.session.query(MatchScorecard, Match)
                .join(Match, MatchScorecard.match_id == Match.id)
                .filter(MatchScorecard.player_id == player_id)
            )
            
            if tournament_id:
                query = query.filter(Match.tournament_id == tournament_id)
            
            records = query.all()
            
            # Aggregate stats
            batting_data = []
            bowling_data = []
            catches = 0
            run_outs = 0
            matches = set()
            
            for card, match in records:
                matches.add(match.id)
                
                if card.record_type == 'batting' and (card.balls or 0) > 0:
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
                    'total_dismissals': catches + run_outs
                }
            }
            
        except Exception as e:
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
        
        avg = total_runs / outs if outs > 0 else total_runs
        sr = (total_runs * 100.0 / total_balls) if total_balls > 0 else 0.0
        high_score = max(i['runs'] for i in innings_list) if innings_list else 0
        
        return {
            'innings': innings,
            'runs': total_runs,
            'balls': total_balls,
            'average': round(avg, 2),
            'strike_rate': round(sr, 2),
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
        
        avg = total_runs / total_wickets if total_wickets > 0 else 0.0
        economy = (total_runs * 6.0 / total_balls) if total_balls > 0 else 0.0
        sr = total_balls / total_wickets if total_wickets > 0 else 0.0
        
        best = max(bowling_list, key=lambda x: (x['wickets'], -x['runs'])) if bowling_list else None
        
        return {
            'innings': innings,
            'wickets': total_wickets,
            'runs': total_runs,
            'balls': total_balls,
            'average': round(avg, 2),
            'economy': round(economy, 2),
            'strike_rate': round(sr, 1),
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
    
    def get_player_partnership_stats(self, player_id, user_id, tournament_id=None):
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
    
    def get_tournament_partnership_leaderboard(self, user_id, tournament_id, limit=10):
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
            
            # Query partnerships from tournament matches
            partnerships = (
                db.session.query(MatchPartnership, Match, Player, Player, Team)
                .join(Match, MatchPartnership.match_id == Match.id)
                .join(Player, MatchPartnership.batsman1_id == Player.id)
                .join(Team, Player.team_id == Team.id)
                .filter(Match.tournament_id == tournament_id)
                .filter(Team.user_id == user_id)
                .order_by(MatchPartnership.runs.desc())
                .limit(limit)
                .all()
            )
            
            leaderboard = []
            for partnership, match, batsman1, batsman2_placeholder, team in partnerships:
                # Get second batsman
                batsman2 = Player.query.get(partnership.batsman2_id)
                
                opponent = self._get_opponent_name(match, team.id)
                
                leaderboard.append({
                    'batsman1': batsman1.name,
                    'batsman2': batsman2.name if batsman2 else 'Unknown',
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
            self._log(f"Error fetching partnership leaderboard: {e}", level='error')
            return []
