from database import db
from database.models import Tournament, TournamentTeam, TournamentFixture, Match
from datetime import datetime
import itertools

class TournamentEngine:
    def __init__(self):
        pass

    def create_tournament(self, name, user_id, team_ids):
        """
        Creates a new tournament, initializes stats for teams, and generates fixtures.
        """
        # 1. Create Tournament Record
        tournament = Tournament(
            name=name,
            user_id=user_id,
            status='Active'
        )
        db.session.add(tournament)
        db.session.flush() # Get ID

        # 2. Add Teams to Tournament
        # Verify unique teams
        team_ids = list(set(team_ids))
        if len(team_ids) < 2:
            raise ValueError("At least 2 teams required to create a tournament.")

        for tid in team_ids:
            tt = TournamentTeam(
                tournament_id=tournament.id,
                team_id=tid
            )
            db.session.add(tt)
        
        # 3. Generate Fixtures (Round Robin)
        self._generate_fixtures(tournament.id, team_ids)
        
        db.session.commit()
        return tournament

    def _generate_fixtures(self, tournament_id, team_ids):
        """
        Generates Single Round Robin schedule using Circle Method.
        """
        teams = team_ids[:]
        if len(teams) % 2 != 0:
            teams.append(None) # Dummy team for bye

        n = len(teams)
        rounds = n - 1
        half = n // 2

        # Round Robin Scheduling
        for r in range(rounds):
            round_matches = []
            for i in range(half):
                t1 = teams[i]
                t2 = teams[n - 1 - i]

                if t1 is not None and t2 is not None:
                    # Alternating home/away for fairness (basic)
                    if r % 2 == 0:
                        home, away = t1, t2
                    else:
                        home, away = t2, t1
                    
                    fixture = TournamentFixture(
                        tournament_id=tournament_id,
                        home_team_id=home,
                        away_team_id=away,
                        round_number=r + 1,
                        status='Scheduled'
                    )
                    db.session.add(fixture)

            # Rotate list (Circle method): Keep first element fixed, rotate rest
            teams = [teams[0]] + [teams[-1]] + teams[1:-1]

    def update_standings(self, match):
        """
        Updates the standings table based on a completed match.
        Matches can be linked via match.tournament_id
        """
        if not match.tournament_id:
            return

        # Find the Fixture Record to update its status
        # Assuming one fixture per match for now, but safer to query by match_id if it was already linked.
        # But actually, when we 'play' a fixture, we create a Match and link it.
        # So here we update the TournamentTeam stats.
        
        fixture = TournamentFixture.query.filter_by(match_id=match.id).first()
        if fixture:
            fixture.status = 'Completed'
        
        home_team_stats = TournamentTeam.query.filter_by(tournament_id=match.tournament_id, team_id=match.home_team_id).first()
        away_team_stats = TournamentTeam.query.filter_by(tournament_id=match.tournament_id, team_id=match.away_team_id).first()

        if not home_team_stats or not away_team_stats:
            print(f"[TournamentEngine] Stats record not found for match {match.id}")
            return

        # Update Played
        home_team_stats.played += 1
        away_team_stats.played += 1

        # Determine Result
        winner_id = match.winner_team_id
        
        if winner_id == match.home_team_id:
            home_team_stats.won += 1
            home_team_stats.points += 2
            away_team_stats.lost += 1
        elif winner_id == match.away_team_id:
            away_team_stats.won += 1
            away_team_stats.points += 2
            home_team_stats.lost += 1
        else:
            # Tie or No Result (Assuming Tie for now if run counts exist)
            home_team_stats.tied += 1
            away_team_stats.tied += 1
            home_team_stats.points += 1
            away_team_stats.points += 1

        # Update run aggregates for NRR
        # Note: Match stores overs as Float (e.g. 19.5). Need to convert properly?
        # NRR = (Total Runs / Total Overs) - (Total Runs Conceded / Total Overs Conceded)
        # We accumulate components.
        
        # Home Batting / Away Bowling
        home_team_stats.runs_scored += match.home_team_score
        home_team_stats.overs_faced = self._add_overs(home_team_stats.overs_faced, match.home_team_overs)
        
        away_team_stats.runs_conceded += match.home_team_score
        away_team_stats.overs_bowled = self._add_overs(away_team_stats.overs_bowled, match.home_team_overs)

        # Away Batting / Home Bowling
        away_team_stats.runs_scored += match.away_team_score
        away_team_stats.overs_faced = self._add_overs(away_team_stats.overs_faced, match.away_team_overs)

        home_team_stats.runs_conceded += match.away_team_score
        home_team_stats.overs_bowled = self._add_overs(home_team_stats.overs_bowled, match.away_team_overs)

        # Recalculate NRR
        self._calculate_nrr(home_team_stats)
        self._calculate_nrr(away_team_stats)

        db.session.commit()

    def _add_overs(self, o1, o2):
        """Adds two float overs (e.g. 10.4 + 2.3 = 13.1)"""
        import math
        balls1 = int(o1) * 6 + int(round((o1 % 1) * 10))
        balls2 = int(o2) * 6 + int(round((o2 % 1) * 10))
        total_balls = balls1 + balls2
        return total_balls // 6 + (total_balls % 6) / 10.0

    def _calculate_nrr(self, team_stats):
        """Updates the net_run_rate field."""
        def get_balls(overs):
            return int(overs) * 6 + int(round((overs % 1) * 10))

        balls_faced = get_balls(team_stats.overs_faced)
        balls_bowled = get_balls(team_stats.overs_bowled)

        if balls_faced > 0:
            run_rate_for = team_stats.runs_scored / (balls_faced / 6.0)
        else:
            run_rate_for = 0

        if balls_bowled > 0:
            run_rate_against = team_stats.runs_conceded / (balls_bowled / 6.0)
        else:
            run_rate_against = 0
            
        team_stats.net_run_rate = round(run_rate_for - run_rate_against, 3)

    def reverse_standings(self, match):
        """
        Reverses the stats update for a match (used for re-simulation).
        Subtracts points, wins, losses, and NRR components.
        """
        if not match.tournament_id:
            return

        home_team_stats = TournamentTeam.query.filter_by(tournament_id=match.tournament_id, team_id=match.home_team_id).first()
        away_team_stats = TournamentTeam.query.filter_by(tournament_id=match.tournament_id, team_id=match.away_team_id).first()

        if not home_team_stats or not away_team_stats:
            return

        # 1. Decrement Played
        home_team_stats.played = max(0, home_team_stats.played - 1)
        away_team_stats.played = max(0, away_team_stats.played - 1)

        # 2. Reverse Result
        winner_id = match.winner_team_id
        
        if winner_id == match.home_team_id:
            home_team_stats.won = max(0, home_team_stats.won - 1)
            home_team_stats.points = max(0, home_team_stats.points - 2)
            away_team_stats.lost = max(0, away_team_stats.lost - 1)
        elif winner_id == match.away_team_id:
            away_team_stats.won = max(0, away_team_stats.won - 1)
            away_team_stats.points = max(0, away_team_stats.points - 2)
            home_team_stats.lost = max(0, home_team_stats.lost - 1)
        else:
            # Tie or No Result
            home_team_stats.tied = max(0, home_team_stats.tied - 1)
            away_team_stats.tied = max(0, away_team_stats.tied - 1)
            home_team_stats.points = max(0, home_team_stats.points - 1)
            away_team_stats.points = max(0, away_team_stats.points - 1)

        # 3. Reverse NRR Components
        # Helper to subtract overs
        def subtract_overs(total, sub):
            # Convert both to balls
            total_balls = int(total) * 6 + int(round((total % 1) * 10))
            sub_balls = int(sub) * 6 + int(round((sub % 1) * 10))
            res_balls = max(0, total_balls - sub_balls)
            return res_balls // 6 + (res_balls % 6) / 10.0

        # Home Batting / Away Bowling removal
        home_team_stats.runs_scored = max(0, home_team_stats.runs_scored - match.home_team_score)
        home_team_stats.overs_faced = subtract_overs(home_team_stats.overs_faced, match.home_team_overs)
        
        away_team_stats.runs_conceded = max(0, away_team_stats.runs_conceded - match.home_team_score)
        away_team_stats.overs_bowled = subtract_overs(away_team_stats.overs_bowled, match.home_team_overs)

        # Away Batting / Home Bowling removal
        away_team_stats.runs_scored = max(0, away_team_stats.runs_scored - match.away_team_score)
        away_team_stats.overs_faced = subtract_overs(away_team_stats.overs_faced, match.away_team_overs)

        home_team_stats.runs_conceded = max(0, home_team_stats.runs_conceded - match.away_team_score)
        home_team_stats.overs_bowled = subtract_overs(home_team_stats.overs_bowled, match.away_team_overs)

        # 4. Recalculate NRR
        self._calculate_nrr(home_team_stats)
        self._calculate_nrr(away_team_stats)

        db.session.commit()
