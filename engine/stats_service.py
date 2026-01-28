# -*- coding: utf-8 -*-
"""
Statistics Service Module
Handles all statistics calculations and queries for the SimCricketX application.
"""

from sqlalchemy import func
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
                         'wickets', 'average', 'economy', 'dots', 'bowled', 'lbw', 
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
                      'Avg', 'Econ', 'Dots', 'Bwld', 'LBW', 'Byes', 'LB', 'Wd', 'NB']
            rows = [
                [
                    d['team'], d['player'], d['matches'], d['innings'], d['overs'],
                    d['runs'], d['wickets'], d['average'], d['economy'], d['dots'],
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
