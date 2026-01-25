from database import db
from database.models import Tournament, TournamentTeam, TournamentFixture, Match
from datetime import datetime
import itertools
import logging

logger = logging.getLogger(__name__)


class TournamentEngine:
    """
    Engine for managing tournament operations including creation,
    fixture generation, and standings management.
    """

    # Points system configuration
    POINTS_WIN = 2
    POINTS_TIE = 1
    POINTS_NO_RESULT = 1
    POINTS_LOSS = 0

    def __init__(self):
        pass

    @staticmethod
    def overs_to_balls(overs: float) -> int:
        """
        Convert cricket overs (float) to total balls.
        E.g., 19.5 overs = 19*6 + 5 = 119 balls

        Handles floating point precision issues.
        """
        if overs is None or overs < 0:
            return 0
        whole_overs = int(overs)
        # Use round to handle float precision (e.g., 19.5 -> 5, not 4.999...)
        partial_balls = round((overs - whole_overs) * 10)
        # Validate: partial balls should be 0-5 (not 6+)
        if partial_balls > 5:
            logger.warning(f"Invalid overs format: {overs} - partial balls {partial_balls} > 5")
            partial_balls = 5
        return whole_overs * 6 + partial_balls

    @staticmethod
    def balls_to_overs(balls: int) -> float:
        """
        Convert total balls to cricket overs (float).
        E.g., 119 balls = 19.5 overs
        """
        if balls is None or balls < 0:
            return 0.0
        return balls // 6 + (balls % 6) / 10.0

    def create_tournament(self, name, user_id, team_ids):
        """
        Creates a new tournament, initializes stats for teams, and generates fixtures.

        Args:
            name: Tournament name
            user_id: Owner user ID
            team_ids: List of team IDs to include

        Returns:
            Tournament: The created tournament object

        Raises:
            ValueError: If less than 2 unique teams provided
        """
        # Validate and deduplicate team IDs
        team_ids = list(set(team_ids))
        if len(team_ids) < 2:
            raise ValueError("At least 2 unique teams are required to create a tournament.")

        try:
            # 1. Create Tournament Record
            tournament = Tournament(
                name=name.strip(),
                user_id=user_id,
                status='Active'
            )
            db.session.add(tournament)
            db.session.flush()  # Get ID for foreign keys

            # 2. Add Teams to Tournament with initialized stats
            for tid in team_ids:
                # Check for existing entry (defensive)
                existing = TournamentTeam.query.filter_by(
                    tournament_id=tournament.id,
                    team_id=tid
                ).first()
                if not existing:
                    tt = TournamentTeam(
                        tournament_id=tournament.id,
                        team_id=tid
                    )
                    db.session.add(tt)

            # 3. Generate Fixtures (Round Robin)
            self._generate_fixtures(tournament.id, team_ids)

            db.session.commit()
            logger.info(f"Tournament '{name}' created with {len(team_ids)} teams")
            return tournament

        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to create tournament: {e}")
            raise

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

    def update_standings(self, match, commit=True):
        """
        Updates the standings table based on a completed match.

        Args:
            match: DBMatch object with scores and winner_team_id populated
            commit: Whether to commit the transaction (default True)

        Returns:
            bool: True if standings were updated, False otherwise
        """
        if not match.tournament_id:
            logger.debug(f"Match {match.id} has no tournament_id, skipping standings update")
            return False

        # Find and update the Fixture Record status
        fixture = TournamentFixture.query.filter_by(match_id=match.id).first()
        if fixture:
            fixture.status = 'Completed'

        # Get team stats records
        home_team_stats = TournamentTeam.query.filter_by(
            tournament_id=match.tournament_id,
            team_id=match.home_team_id
        ).first()
        away_team_stats = TournamentTeam.query.filter_by(
            tournament_id=match.tournament_id,
            team_id=match.away_team_id
        ).first()

        if not home_team_stats or not away_team_stats:
            logger.error(f"Stats record not found for match {match.id} "
                         f"(home: {match.home_team_id}, away: {match.away_team_id})")
            return False

        # Update Played count
        home_team_stats.played += 1
        away_team_stats.played += 1

        # Determine Result and update W/L/T/NR and points
        winner_id = match.winner_team_id
        is_no_result = self._is_no_result(match)

        if is_no_result:
            # No Result - match abandoned/no play
            home_team_stats.no_result += 1
            away_team_stats.no_result += 1
            home_team_stats.points += self.POINTS_NO_RESULT
            away_team_stats.points += self.POINTS_NO_RESULT
            logger.info(f"Match {match.id}: No Result")
        elif winner_id == match.home_team_id:
            # Home team won
            home_team_stats.won += 1
            home_team_stats.points += self.POINTS_WIN
            away_team_stats.lost += 1
            away_team_stats.points += self.POINTS_LOSS
            logger.info(f"Match {match.id}: Home team {match.home_team_id} won")
        elif winner_id == match.away_team_id:
            # Away team won
            away_team_stats.won += 1
            away_team_stats.points += self.POINTS_WIN
            home_team_stats.lost += 1
            home_team_stats.points += self.POINTS_LOSS
            logger.info(f"Match {match.id}: Away team {match.away_team_id} won")
        else:
            # Tie - scores are equal, both teams batted
            home_team_stats.tied += 1
            away_team_stats.tied += 1
            home_team_stats.points += self.POINTS_TIE
            away_team_stats.points += self.POINTS_TIE
            logger.info(f"Match {match.id}: Tie")

        # Update NRR components (only if match was completed with valid scores)
        if not is_no_result:
            self._update_nrr_components(home_team_stats, away_team_stats, match)

        # Check if tournament is complete
        self._check_tournament_completion(match.tournament_id)

        if commit:
            db.session.commit()

        return True

    def _is_no_result(self, match) -> bool:
        """
        Determine if a match is a No Result (abandoned/no play).

        A No Result occurs when:
        - No winner and scores indicate match didn't complete normally
        - Both teams have 0 overs faced (match never started)
        - Result description contains 'abandoned' or 'no result'
        """
        if match.winner_team_id:
            return False

        # Check if match was abandoned
        result_desc = (match.result_description or '').lower()
        if 'abandoned' in result_desc or 'no result' in result_desc:
            return True

        # If both teams have zero overs, likely no result
        home_overs = match.home_team_overs or 0
        away_overs = match.away_team_overs or 0
        if home_overs == 0 and away_overs == 0:
            return True

        return False

    def _update_nrr_components(self, home_stats, away_stats, match):
        """
        Update Net Run Rate components for both teams.

        NRR = (Runs Scored / Overs Faced) - (Runs Conceded / Overs Bowled)
        """
        # Safely get scores (handle None values)
        home_score = match.home_team_score or 0
        away_score = match.away_team_score or 0
        home_overs = match.home_team_overs or 0.0
        away_overs = match.away_team_overs or 0.0

        # Home Batting / Away Bowling
        home_stats.runs_scored += home_score
        home_stats.overs_faced = self._add_overs(home_stats.overs_faced, home_overs)

        away_stats.runs_conceded += home_score
        away_stats.overs_bowled = self._add_overs(away_stats.overs_bowled, home_overs)

        # Away Batting / Home Bowling
        away_stats.runs_scored += away_score
        away_stats.overs_faced = self._add_overs(away_stats.overs_faced, away_overs)

        home_stats.runs_conceded += away_score
        home_stats.overs_bowled = self._add_overs(home_stats.overs_bowled, away_overs)

        # Recalculate NRR for both teams
        self._calculate_nrr(home_stats)
        self._calculate_nrr(away_stats)

    def _check_tournament_completion(self, tournament_id):
        """
        Check if all fixtures are completed and update tournament status.
        """
        pending_fixtures = TournamentFixture.query.filter_by(
            tournament_id=tournament_id,
            status='Scheduled'
        ).count()

        if pending_fixtures == 0:
            tournament = db.session.get(Tournament, tournament_id)
            if tournament and tournament.status != 'Completed':
                tournament.status = 'Completed'
                logger.info(f"Tournament {tournament_id} marked as Completed")

    def _add_overs(self, o1, o2):
        """
        Add two cricket overs values.

        Args:
            o1: First overs value (e.g., 10.4)
            o2: Second overs value (e.g., 2.3)

        Returns:
            float: Sum in cricket overs format (e.g., 13.1)
        """
        balls1 = self.overs_to_balls(o1 or 0.0)
        balls2 = self.overs_to_balls(o2 or 0.0)
        return self.balls_to_overs(balls1 + balls2)

    def _subtract_overs(self, total, sub):
        """
        Subtract cricket overs values.

        Args:
            total: Total overs value
            sub: Overs to subtract

        Returns:
            float: Difference in cricket overs format (minimum 0)
        """
        total_balls = self.overs_to_balls(total or 0.0)
        sub_balls = self.overs_to_balls(sub or 0.0)
        result_balls = max(0, total_balls - sub_balls)
        return self.balls_to_overs(result_balls)

    def _calculate_nrr(self, team_stats):
        """
        Calculate and update the net run rate for a team.

        NRR = (Runs Scored / Overs Faced) - (Runs Conceded / Overs Bowled)

        All rates are calculated per over (6 balls).
        """
        balls_faced = self.overs_to_balls(team_stats.overs_faced or 0.0)
        balls_bowled = self.overs_to_balls(team_stats.overs_bowled or 0.0)

        # Calculate run rate FOR (batting)
        if balls_faced > 0:
            overs_faced = balls_faced / 6.0
            run_rate_for = (team_stats.runs_scored or 0) / overs_faced
        else:
            run_rate_for = 0.0

        # Calculate run rate AGAINST (bowling)
        if balls_bowled > 0:
            overs_bowled = balls_bowled / 6.0
            run_rate_against = (team_stats.runs_conceded or 0) / overs_bowled
        else:
            run_rate_against = 0.0

        # NRR rounded to 3 decimal places
        team_stats.net_run_rate = round(run_rate_for - run_rate_against, 3)

    def reverse_standings(self, match, commit=False):
        """
        Reverses the stats update for a match (used for re-simulation).

        IMPORTANT: This method does NOT commit by default to allow for
        transactional safety when combined with other operations.

        Args:
            match: DBMatch object to reverse
            commit: Whether to commit changes (default False for transaction safety)

        Returns:
            bool: True if reversal was successful, False otherwise
        """
        if not match.tournament_id:
            logger.debug(f"Match {match.id} has no tournament_id, skipping reversal")
            return False

        home_team_stats = TournamentTeam.query.filter_by(
            tournament_id=match.tournament_id,
            team_id=match.home_team_id
        ).first()
        away_team_stats = TournamentTeam.query.filter_by(
            tournament_id=match.tournament_id,
            team_id=match.away_team_id
        ).first()

        if not home_team_stats or not away_team_stats:
            logger.error(f"Stats record not found for match {match.id} reversal")
            return False

        # 1. Decrement Played count
        home_team_stats.played = max(0, home_team_stats.played - 1)
        away_team_stats.played = max(0, away_team_stats.played - 1)

        # 2. Reverse Result based on match outcome
        winner_id = match.winner_team_id
        was_no_result = self._is_no_result(match)

        if was_no_result:
            # Reverse No Result
            home_team_stats.no_result = max(0, home_team_stats.no_result - 1)
            away_team_stats.no_result = max(0, away_team_stats.no_result - 1)
            home_team_stats.points = max(0, home_team_stats.points - self.POINTS_NO_RESULT)
            away_team_stats.points = max(0, away_team_stats.points - self.POINTS_NO_RESULT)
        elif winner_id == match.home_team_id:
            # Reverse home win
            home_team_stats.won = max(0, home_team_stats.won - 1)
            home_team_stats.points = max(0, home_team_stats.points - self.POINTS_WIN)
            away_team_stats.lost = max(0, away_team_stats.lost - 1)
        elif winner_id == match.away_team_id:
            # Reverse away win
            away_team_stats.won = max(0, away_team_stats.won - 1)
            away_team_stats.points = max(0, away_team_stats.points - self.POINTS_WIN)
            home_team_stats.lost = max(0, home_team_stats.lost - 1)
        else:
            # Reverse Tie
            home_team_stats.tied = max(0, home_team_stats.tied - 1)
            away_team_stats.tied = max(0, away_team_stats.tied - 1)
            home_team_stats.points = max(0, home_team_stats.points - self.POINTS_TIE)
            away_team_stats.points = max(0, away_team_stats.points - self.POINTS_TIE)

        # 3. Reverse NRR Components (only if match had valid scores)
        if not was_no_result:
            self._reverse_nrr_components(home_team_stats, away_team_stats, match)

        # 4. Recalculate NRR
        self._calculate_nrr(home_team_stats)
        self._calculate_nrr(away_team_stats)

        # 5. Reset tournament status if it was completed
        tournament = db.session.get(Tournament, match.tournament_id)
        if tournament and tournament.status == 'Completed':
            tournament.status = 'Active'

        if commit:
            db.session.commit()

        logger.info(f"Reversed standings for match {match.id}")
        return True

    def _reverse_nrr_components(self, home_stats, away_stats, match):
        """
        Reverse the NRR component updates from a match.
        """
        # Safely get scores (handle None values)
        home_score = match.home_team_score or 0
        away_score = match.away_team_score or 0
        home_overs = match.home_team_overs or 0.0
        away_overs = match.away_team_overs or 0.0

        # Reverse Home Batting / Away Bowling
        home_stats.runs_scored = max(0, (home_stats.runs_scored or 0) - home_score)
        home_stats.overs_faced = self._subtract_overs(home_stats.overs_faced, home_overs)

        away_stats.runs_conceded = max(0, (away_stats.runs_conceded or 0) - home_score)
        away_stats.overs_bowled = self._subtract_overs(away_stats.overs_bowled, home_overs)

        # Reverse Away Batting / Home Bowling
        away_stats.runs_scored = max(0, (away_stats.runs_scored or 0) - away_score)
        away_stats.overs_faced = self._subtract_overs(away_stats.overs_faced, away_overs)

        home_stats.runs_conceded = max(0, (home_stats.runs_conceded or 0) - away_score)
        home_stats.overs_bowled = self._subtract_overs(home_stats.overs_bowled, away_overs)
