from database import db
from database.models import Tournament, TournamentTeam, TournamentFixture, Match, Team
from datetime import datetime
import itertools
import logging
import json
import math

logger = logging.getLogger(__name__)


class TournamentEngine:
    """
    Engine for managing tournament operations including creation,
    fixture generation, and standings management.

    Supported Tournament Modes:
    - round_robin: Single round robin (each team plays every other team once)
    - double_round_robin: Double round robin (each team plays every other team twice - home & away)
    - knockout: Pure knockout/elimination tournament
    - round_robin_knockout: Round robin league + semi-finals + final
    - double_round_robin_knockout: Double round robin + semi-finals + final
    - ipl_style: League stage + IPL-style playoffs (Qualifier 1, Eliminator, Qualifier 2, Final)
    - custom_series: User-defined series of matches between 2 teams
    """

    # Points system configuration
    POINTS_WIN = 2
    POINTS_TIE = 1
    POINTS_NO_RESULT = 1
    POINTS_LOSS = 0

    # Tournament mode constants
    MODE_ROUND_ROBIN = 'round_robin'
    MODE_DOUBLE_ROUND_ROBIN = 'double_round_robin'
    MODE_KNOCKOUT = 'knockout'
    MODE_ROUND_ROBIN_KNOCKOUT = 'round_robin_knockout'
    MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT = 'double_round_robin_knockout'
    MODE_IPL_STYLE = 'ipl_style'
    MODE_CUSTOM_SERIES = 'custom_series'

    # Stage constants
    STAGE_LEAGUE = 'league'
    STAGE_QUALIFIER_1 = 'qualifier_1'
    STAGE_ELIMINATOR = 'eliminator'
    STAGE_QUALIFIER_2 = 'qualifier_2'
    STAGE_SEMIFINAL_1 = 'semifinal_1'
    STAGE_SEMIFINAL_2 = 'semifinal_2'
    STAGE_FINAL = 'final'
    STAGE_KNOCKOUT_R1 = 'knockout_r1'
    STAGE_KNOCKOUT_R2 = 'knockout_r2'
    STAGE_KNOCKOUT_QF = 'knockout_qf'
    STAGE_KNOCKOUT_SF = 'knockout_sf'

    # Minimum teams required for each mode
    MIN_TEAMS = {
        MODE_ROUND_ROBIN: 2,
        MODE_DOUBLE_ROUND_ROBIN: 2,
        MODE_KNOCKOUT: 2,
        MODE_ROUND_ROBIN_KNOCKOUT: 4,  # Need at least 4 for semis + final
        MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT: 4,
        MODE_IPL_STYLE: 4,  # IPL-style needs exactly 4 qualifiers from league
        MODE_CUSTOM_SERIES: 2,
    }

    def __init__(self):
        pass

    @staticmethod
    def overs_to_balls(overs: float) -> int:
        """
        Convert cricket overs (float) to total balls.
        E.g., 19.5 overs = 19*6 + 5 = 119 balls
        """
        if overs is None or overs < 0:
            return 0
        whole_overs = int(overs)
        partial_balls = round((overs - whole_overs) * 10)
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

    def get_available_modes(self, num_teams: int) -> list:
        """
        Get list of tournament modes available for given number of teams.

        Args:
            num_teams: Number of teams to participate

        Returns:
            List of (mode_id, mode_name, description) tuples
        """
        modes = []

        if num_teams >= 2:
            modes.append((self.MODE_ROUND_ROBIN, 'Round Robin',
                          f'Each team plays every other team once ({self._count_round_robin_matches(num_teams)} matches)'))
            modes.append((self.MODE_DOUBLE_ROUND_ROBIN, 'Double Round Robin',
                          f'Each team plays every other team twice - home & away ({self._count_round_robin_matches(num_teams) * 2} matches)'))
            modes.append((self.MODE_CUSTOM_SERIES, 'Custom Series',
                          'Design your own series between two teams'))

        if num_teams >= 2 and self._is_power_of_two(num_teams):
            modes.append((self.MODE_KNOCKOUT, 'Pure Knockout',
                          f'Single elimination tournament ({num_teams - 1} matches)'))
        elif num_teams >= 2:
            # Allow knockout with byes
            next_power = self._next_power_of_two(num_teams)
            byes = next_power - num_teams
            modes.append((self.MODE_KNOCKOUT, 'Pure Knockout',
                          f'Single elimination with {byes} bye(s) ({num_teams - 1} matches)'))

        if num_teams >= 4:
            rr_matches = self._count_round_robin_matches(num_teams)
            modes.append((self.MODE_ROUND_ROBIN_KNOCKOUT, 'League + Semis + Final',
                          f'Round robin league then top 4 play knockouts ({rr_matches + 3} matches)'))
            modes.append((self.MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT, 'Double League + Semis + Final',
                          f'Double round robin then top 4 play knockouts ({rr_matches * 2 + 3} matches)'))
            modes.append((self.MODE_IPL_STYLE, 'IPL Style Playoffs',
                          f'Round robin + IPL-style playoffs (Q1, Eliminator, Q2, Final) ({rr_matches + 4} matches)'))

        return modes

    def _count_round_robin_matches(self, n: int) -> int:
        """Calculate number of matches in single round robin: n*(n-1)/2"""
        return n * (n - 1) // 2

    def _is_power_of_two(self, n: int) -> bool:
        """Check if n is a power of 2"""
        return n > 0 and (n & (n - 1)) == 0

    def _next_power_of_two(self, n: int) -> int:
        """Get the next power of 2 >= n"""
        return 2 ** math.ceil(math.log2(n))

    def create_tournament(self, name: str, user_id: str, team_ids: list,
                          mode: str = MODE_ROUND_ROBIN, playoff_teams: int = 4,
                          series_config: dict = None) -> Tournament:
        """
        Creates a new tournament with specified mode.

        Args:
            name: Tournament name
            user_id: Owner user ID
            team_ids: List of team IDs to include
            mode: Tournament mode (default: round_robin)
            playoff_teams: Number of teams qualifying for playoffs (for modes with knockouts)
            series_config: Configuration for custom series mode

        Returns:
            Tournament: The created tournament object

        Raises:
            ValueError: If validation fails
        """
        # Validate and deduplicate team IDs (preserve order)
        team_ids = list(dict.fromkeys(team_ids))
        min_teams = self.MIN_TEAMS.get(mode, 2)

        if len(team_ids) < min_teams:
            raise ValueError(f"At least {min_teams} unique teams are required for {mode} mode.")

        # Validate mode-specific requirements
        if mode == self.MODE_CUSTOM_SERIES:
            if len(team_ids) != 2:
                raise ValueError("Custom series requires exactly 2 teams.")
            if not series_config or not series_config.get('matches'):
                raise ValueError("Custom series requires match configuration.")

        if mode in [self.MODE_IPL_STYLE] and len(team_ids) < 4:
            raise ValueError("IPL-style format requires at least 4 teams.")

        try:
            # 1. Create Tournament Record
            tournament = Tournament(
                name=name.strip(),
                user_id=user_id,
                status='Active',
                mode=mode,
                current_stage=self.STAGE_LEAGUE if mode != self.MODE_KNOCKOUT else self._get_knockout_round_name(self._next_power_of_two(len(team_ids)), 1),
                playoff_teams=min(playoff_teams, len(team_ids)),
                series_config=json.dumps(series_config) if series_config else None
            )
            db.session.add(tournament)
            db.session.flush()

            # 2. Add Teams to Tournament
            # Verify teams exist and belong to the user before adding.
            existing_teams = Team.query.filter(
                Team.id.in_(team_ids),
                Team.user_id == user_id
            ).all()
            if len(existing_teams) != len(team_ids):
                found_ids = {t.id for t in existing_teams}
                missing = set(team_ids) - found_ids
                raise ValueError(f"Teams with IDs {missing} are not available for this user.")

            for tid in team_ids:
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

            # 3. Generate Fixtures based on mode
            self._generate_fixtures_for_mode(tournament, team_ids, mode, series_config)

            db.session.commit()
            logger.info(f"Tournament '{name}' created with mode '{mode}' and {len(team_ids)} teams")
            return tournament

        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to create tournament: {e}")
            raise

    def _generate_fixtures_for_mode(self, tournament: Tournament, team_ids: list,
                                    mode: str, series_config: dict = None):
        """
        Generate fixtures based on tournament mode.
        """
        if mode == self.MODE_ROUND_ROBIN:
            self._generate_round_robin(tournament.id, team_ids, double=False)

        elif mode == self.MODE_DOUBLE_ROUND_ROBIN:
            self._generate_round_robin(tournament.id, team_ids, double=True)

        elif mode == self.MODE_KNOCKOUT:
            self._generate_knockout(tournament.id, team_ids)

        elif mode == self.MODE_ROUND_ROBIN_KNOCKOUT:
            self._generate_round_robin(tournament.id, team_ids, double=False)
            self._generate_semifinal_final_placeholders(tournament.id)

        elif mode == self.MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT:
            self._generate_round_robin(tournament.id, team_ids, double=True)
            self._generate_semifinal_final_placeholders(tournament.id)

        elif mode == self.MODE_IPL_STYLE:
            self._generate_round_robin(tournament.id, team_ids, double=False)
            self._generate_ipl_playoff_placeholders(tournament.id)

        elif mode == self.MODE_CUSTOM_SERIES:
            self._generate_custom_series(tournament.id, team_ids, series_config)

    def _generate_round_robin(self, tournament_id: int, team_ids: list, double: bool = False):
        """
        Generate Round Robin fixtures using Circle Method.

        Args:
            tournament_id: Tournament ID
            team_ids: List of team IDs
            double: If True, generates double round robin (home & away)
        """
        teams = team_ids[:]
        if len(teams) % 2 != 0:
            teams.append(None)  # Dummy team for bye

        n = len(teams)
        rounds = n - 1
        half = n // 2
        round_offset = 0

        # Number of passes (1 for single, 2 for double)
        passes = 2 if double else 1

        for pass_num in range(passes):
            teams_copy = team_ids[:]
            if len(teams_copy) % 2 != 0:
                teams_copy.append(None)

            for r in range(rounds):
                for i in range(half):
                    t1 = teams_copy[i]
                    t2 = teams_copy[n - 1 - i]

                    if t1 is not None and t2 is not None:
                        # For double round robin: flip home/away in second pass
                        if pass_num == 0:
                            if r % 2 == 0:
                                home, away = t1, t2
                            else:
                                home, away = t2, t1
                        else:
                            # Second pass: reverse home/away
                            if r % 2 == 0:
                                home, away = t2, t1
                            else:
                                home, away = t1, t2

                        fixture = TournamentFixture(
                            tournament_id=tournament_id,
                            home_team_id=home,
                            away_team_id=away,
                            round_number=round_offset + r + 1,
                            stage=self.STAGE_LEAGUE,
                            status='Scheduled'
                        )
                        db.session.add(fixture)

                # Rotate list (Circle method)
                teams_copy = [teams_copy[0]] + [teams_copy[-1]] + teams_copy[1:-1]

            round_offset += rounds

    def _generate_knockout(self, tournament_id: int, team_ids: list):
        """
        Generate Pure Knockout/Elimination tournament fixtures.
        Handles non-power-of-2 teams with byes.
        """
        bye_id = self._get_placeholder_team_id(tournament_id, "BYE")
        tbd_id = self._get_placeholder_team_id(tournament_id, "TBD")
        n = len(team_ids)
        next_power = self._next_power_of_two(n)
        
        # We model the tournament as a set of slots in a power-of-2 tree
        # For a tournament of 8 slots:
        # Round 1: slots 0-3 (matches 0, 1, 2, 3)
        # Round 2: slots 4-5 (matches 4, 5)
        # Round 3: slot 6 (match 6)
        
        # Randomize teams and distribute byes
        import random
        padded_teams = team_ids[:]
        num_byes = next_power - n
        padded_teams += [None] * num_byes
        random.shuffle(padded_teams)
        
        bracket_position = 0
        current_round = 1
        num_matches_current_round = next_power // 2
        round_matches = [] # List of (home, away) for the current round
        
        # Round 1
        round_name = self._get_knockout_round_name(next_power, current_round)
        for i in range(num_matches_current_round):
            t1 = padded_teams[2*i]
            t2 = padded_teams[2*i + 1]
            
            # If one is None, it's a bye. The team advances automatically.
            # We still create the fixture but mark it as Completed if it's a bye?
            # Actually, standard way is to create Scheduled for real matches,
            # and for byes, we can just populate the next round.
            
            if t1 is not None and t2 is not None:
                fixture = TournamentFixture(
                    tournament_id=tournament_id,
                    home_team_id=t1,
                    away_team_id=t2,
                    round_number=current_round,
                    stage=round_name,
                    stage_description=f"Winner advances to {self._get_knockout_round_name(next_power, current_round + 1)}",
                    bracket_position=bracket_position,
                    status='Scheduled'
                )
                db.session.add(fixture)
                db.session.flush()
            elif t1 is not None or t2 is not None:
                # Bye match. Create it as completed.
                winner = t1 if t1 is not None else t2

                home_id = t1 if t1 is not None else bye_id
                away_id = t2 if t2 is not None else bye_id

                fixture = TournamentFixture(
                    tournament_id=tournament_id,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    round_number=current_round,
                    stage=round_name,
                    status='Completed',
                    winner_team_id=winner,
                    bracket_position=bracket_position,
                    stage_description="Bye - Advances to next round"
                )
                db.session.add(fixture)
                db.session.flush()
            else:
                # Both are None (Phantom match)
                fixture = TournamentFixture(
                    tournament_id=tournament_id,
                    home_team_id=None,
                    away_team_id=None,
                    round_number=current_round,
                    stage=round_name,
                    status='Completed',
                    winner_team_id=None,
                    bracket_position=bracket_position,
                    stage_description="Phantom Match"
                )
                db.session.add(fixture)
                db.session.flush()
            
            bracket_position += 1
            
        # Subsequent rounds (Placeholders)
        current_round += 1
        num_matches_current_round //= 2
        
        while num_matches_current_round >= 1:
            round_name = self._get_knockout_round_name(next_power, current_round)
            for i in range(num_matches_current_round):
                fixture = TournamentFixture(
                    tournament_id=tournament_id,
                    home_team_id=None,
                    away_team_id=None,
                    round_number=current_round,
                    stage=round_name,
                    stage_description="Winner advances" if round_name != self.STAGE_FINAL else "Tournament Winner",
                    bracket_position=bracket_position,
                    status='Locked'
                )
                db.session.add(fixture)
                db.session.flush()
                bracket_position += 1
                
            num_matches_current_round //= 2
            current_round += 1
            
        # After generating all fixtures, we might need to auto-advance byes
        self._advance_bye_winners(tournament_id)

    def _get_knockout_round_name(self, total_teams: int, round_num: int) -> str:
        """Get the name of a knockout round based on remaining teams."""
        # Calculate teams remaining at start of this round
        teams_at_round = total_teams // (2 ** (round_num - 1))

        if teams_at_round == 2:
            return self.STAGE_FINAL
        elif teams_at_round == 4:
            return self.STAGE_KNOCKOUT_SF
        elif teams_at_round == 8:
            return self.STAGE_KNOCKOUT_QF
        elif teams_at_round == 16:
            return self.STAGE_KNOCKOUT_R2
        else:
            return f'round_{round_num}'

    def _generate_semifinal_final_placeholders(self, tournament_id: int):
        """
        Generate placeholder fixtures for Semi-finals and Final.
        Teams will be populated after league stage completes.
        """
        # Get the last round number from league fixtures
        last_league_round = db.session.query(db.func.max(TournamentFixture.round_number)).filter_by(
            tournament_id=tournament_id,
            stage=self.STAGE_LEAGUE
        ).scalar() or 0

        tbd_id = self._get_placeholder_team_id(tournament_id, "TBD")

        # Semi-final 1: 1st vs 4th
        sf1 = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 1,
            stage=self.STAGE_SEMIFINAL_1,
            stage_description="1st vs 4th - Winner to Final",
            bracket_position=1,
            status='Locked'
        )
        db.session.add(sf1)

        # Semi-final 2: 2nd vs 3rd
        sf2 = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 1,
            stage=self.STAGE_SEMIFINAL_2,
            stage_description="2nd vs 3rd - Winner to Final",
            bracket_position=2,
            status='Locked'
        )
        db.session.add(sf2)

        # Final
        final = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 2,
            stage=self.STAGE_FINAL,
            stage_description="Tournament Final",
            bracket_position=3,
            status='Locked'
        )
        db.session.add(final)

    def _generate_ipl_playoff_placeholders(self, tournament_id: int):
        """
        Generate IPL-style playoff fixtures:
        - Qualifier 1: 1st vs 2nd (Winner to Final)
        - Eliminator: 3rd vs 4th (Loser out)
        - Qualifier 2: Loser of Q1 vs Winner of Eliminator (Winner to Final)
        - Final: Winner of Q1 vs Winner of Q2
        """
        last_league_round = db.session.query(db.func.max(TournamentFixture.round_number)).filter_by(
            tournament_id=tournament_id,
            stage=self.STAGE_LEAGUE
        ).scalar() or 0

        tbd_id = self._get_placeholder_team_id(tournament_id, "TBD")

        # Qualifier 1: 1st vs 2nd
        q1 = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 1,
            stage=self.STAGE_QUALIFIER_1,
            stage_description="1st vs 2nd - Winner to Final, Loser to Qualifier 2",
            bracket_position=1,
            status='Locked'
        )
        db.session.add(q1)

        # Eliminator: 3rd vs 4th
        elim = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 1,
            stage=self.STAGE_ELIMINATOR,
            stage_description="3rd vs 4th - Winner to Qualifier 2, Loser Eliminated",
            bracket_position=2,
            status='Locked'
        )
        db.session.add(elim)

        # Qualifier 2: Loser Q1 vs Winner Eliminator
        q2 = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 2,
            stage=self.STAGE_QUALIFIER_2,
            stage_description="Loser Q1 vs Winner Eliminator - Winner to Final",
            bracket_position=3,
            status='Locked'
        )
        db.session.add(q2)

        # Final
        final = TournamentFixture(
            tournament_id=tournament_id,
            home_team_id=None,
            away_team_id=None,
            round_number=last_league_round + 3,
            stage=self.STAGE_FINAL,
            stage_description="Winner Q1 vs Winner Q2 - Tournament Champion",
            bracket_position=4,
            status='Locked'
        )
        db.session.add(final)

    def _generate_custom_series(self, tournament_id: int, team_ids: list, series_config: dict):
        """
        Generate fixtures for a custom series.

        series_config format:
        {
            "series_name": "Ashes 2026",
            "matches": [
                {"match_num": 1, "home": 0, "venue_name": "Lords"},
                {"match_num": 2, "home": 1, "venue_name": "MCG"},
                ...
            ]
        }

        'home' is the index (0 or 1) indicating which team is home for that match.
        """
        if len(team_ids) != 2:
            raise ValueError("Custom series requires exactly 2 teams")

        matches = series_config.get('matches', [])
        if not matches:
            raise ValueError("Custom series requires at least one match")

        for i, match_def in enumerate(matches):
            home_idx = match_def.get('home', 0)
            
            if home_idx not in [0, 1]:
                 logger.warning(f"Invalid home index {home_idx} in custom series. Defaulting to 0.")
                 home_idx = 0

            match_num = match_def.get('match_num', i + 1)

            home_team = team_ids[home_idx]
            away_team = team_ids[1 - home_idx]

            fixture = TournamentFixture(
                tournament_id=tournament_id,
                home_team_id=home_team,
                away_team_id=away_team,
                round_number=match_num,
                stage=self.STAGE_LEAGUE,
                stage_description=match_def.get('venue_name', f'Match {match_num}'),
                series_match_number=match_num,
                status='Scheduled'
            )
            db.session.add(fixture)

    def check_and_progress_tournament(self, tournament_id: int) -> bool:
        """
        Check if league stage is complete and progress to knockouts if applicable.

        This should be called after each match completion.

        Returns:
            bool: True if tournament progressed to next stage
        """
        tournament = db.session.get(Tournament, tournament_id)
        if not tournament:
            return False

        if tournament.mode not in [self.MODE_ROUND_ROBIN_KNOCKOUT,
                                   self.MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT,
                                   self.MODE_IPL_STYLE,
                                   self.MODE_KNOCKOUT]:
            return False

        # Check if current stage is complete
        if tournament.current_stage == self.STAGE_LEAGUE:
            return self._check_league_completion(tournament)
        elif tournament.mode == self.MODE_KNOCKOUT:
            return self._check_knockout_progression(tournament)
        elif tournament.current_stage in [self.STAGE_QUALIFIER_1, self.STAGE_ELIMINATOR,
                                          self.STAGE_SEMIFINAL_1, self.STAGE_SEMIFINAL_2]:
            return self._check_playoff_progression(tournament)
        elif tournament.current_stage == self.STAGE_QUALIFIER_2:
            return self._check_qualifier2_completion(tournament)

        return False

    def _check_league_completion(self, tournament: Tournament) -> bool:
        """Check if league stage is complete and populate playoff fixtures."""
        pending_league = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_LEAGUE,
            status='Scheduled'
        ).count()

        if pending_league > 0:
            return False

        # League complete - get standings
        standings = self.get_standings(tournament.id)
        if len(standings) < tournament.playoff_teams:
            logger.warning(f"Not enough teams in standings for playoffs: {len(standings)}")
            return False

        # Populate knockout fixtures based on mode
        if tournament.mode == self.MODE_IPL_STYLE:
            self._populate_ipl_playoffs(tournament, standings)
            tournament.current_stage = self.STAGE_QUALIFIER_1
        elif tournament.mode in [self.MODE_ROUND_ROBIN_KNOCKOUT, self.MODE_DOUBLE_ROUND_ROBIN_KNOCKOUT]:
            self._populate_semifinal_playoffs(tournament, standings)
            tournament.current_stage = self.STAGE_SEMIFINAL_1

        db.session.commit()
        logger.info(f"Tournament {tournament.id} progressed to {tournament.current_stage}")
        return True

    def _populate_ipl_playoffs(self, tournament: Tournament, standings: list):
        """Populate IPL-style playoff fixtures with qualified teams."""
        top4 = standings[:4]

        # Qualifier 1: 1st vs 2nd
        q1 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_QUALIFIER_1
        ).first()
        if q1:
            q1.home_team_id = top4[0].team_id
            q1.away_team_id = top4[1].team_id
            q1.status = 'Scheduled'

        # Eliminator: 3rd vs 4th
        elim = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_ELIMINATOR
        ).first()
        if elim:
            elim.home_team_id = top4[2].team_id
            elim.away_team_id = top4[3].team_id
            elim.status = 'Scheduled'

    def _populate_semifinal_playoffs(self, tournament: Tournament, standings: list):
        """Populate semi-final fixtures with qualified teams."""
        top4 = standings[:4]

        # SF1: 1st vs 4th
        sf1 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_SEMIFINAL_1
        ).first()
        if sf1:
            sf1.home_team_id = top4[0].team_id
            sf1.away_team_id = top4[3].team_id
            sf1.status = 'Scheduled'

        # SF2: 2nd vs 3rd
        sf2 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_SEMIFINAL_2
        ).first()
        if sf2:
            sf2.home_team_id = top4[1].team_id
            sf2.away_team_id = top4[2].team_id
            sf2.status = 'Scheduled'

    def _check_playoff_progression(self, tournament: Tournament) -> bool:
        """Check playoff stage completion and progress to next stage."""
        if tournament.mode == self.MODE_IPL_STYLE:
            return self._progress_ipl_playoffs(tournament)
        else:
            return self._progress_semifinal_playoffs(tournament)

    def _progress_ipl_playoffs(self, tournament: Tournament) -> bool:
        """Progress IPL-style playoffs."""
        # Check Q1 and Eliminator completion
        q1 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_QUALIFIER_1
        ).first()

        elim = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_ELIMINATOR
        ).first()

        if not q1 or not elim:
            return False

        # Both Q1 and Eliminator must be completed with winners to populate Q2
        if q1.status == 'Completed' and elim.status == 'Completed':
            if not q1.winner_team_id or not elim.winner_team_id:
                logger.warning(
                    "Tournament %s playoff progression blocked: missing winner in Q1/Eliminator",
                    tournament.id
                )
                return False
            q2 = TournamentFixture.query.filter_by(
                tournament_id=tournament.id,
                stage=self.STAGE_QUALIFIER_2
            ).first()

            if q2 and q2.status == 'Locked':
                # Q2: Loser of Q1 vs Winner of Eliminator
                q1_loser = q1.away_team_id if q1.winner_team_id == q1.home_team_id else q1.home_team_id
                elim_winner = elim.winner_team_id

                q2.home_team_id = q1_loser
                q2.away_team_id = elim_winner
                q2.status = 'Scheduled'
                tournament.current_stage = self.STAGE_QUALIFIER_2
                db.session.commit()
                logger.info(f"Tournament {tournament.id} Q2 populated")
                return True

        return False

    def _check_qualifier2_completion(self, tournament: Tournament) -> bool:
        """Check Q2 completion and populate Final."""
        q1 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_QUALIFIER_1
        ).first()

        q2 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_QUALIFIER_2
        ).first()

        if not q1 or not q2:
            return False

        if q2.status == 'Completed':
            if not q1.winner_team_id or not q2.winner_team_id:
                logger.warning(
                    "Tournament %s playoff progression blocked: missing winner in Q1/Q2",
                    tournament.id
                )
                return False
            final = TournamentFixture.query.filter_by(
                tournament_id=tournament.id,
                stage=self.STAGE_FINAL
            ).first()

            if final and final.status == 'Locked':
                # Final: Winner Q1 vs Winner Q2
                final.home_team_id = q1.winner_team_id
                final.away_team_id = q2.winner_team_id
                final.status = 'Scheduled'
                tournament.current_stage = self.STAGE_FINAL
                db.session.commit()
                logger.info(f"Tournament {tournament.id} Final populated")
                return True

        return False

    def _progress_semifinal_playoffs(self, tournament: Tournament) -> bool:
        """Progress semi-final based playoffs."""
        sf1 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_SEMIFINAL_1
        ).first()

        sf2 = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=self.STAGE_SEMIFINAL_2
        ).first()

        if not sf1 or not sf2:
            return False

        if sf1.status == 'Completed' and sf2.status == 'Completed':
            if not sf1.winner_team_id or not sf2.winner_team_id:
                logger.warning(
                    "Tournament %s playoff progression blocked: missing winner in semifinals",
                    tournament.id
                )
                return False
            final = TournamentFixture.query.filter_by(
                tournament_id=tournament.id,
                stage=self.STAGE_FINAL
            ).first()

            if final and final.status == 'Locked':
                final.home_team_id = sf1.winner_team_id
                final.away_team_id = sf2.winner_team_id
                final.status = 'Scheduled'
                tournament.current_stage = self.STAGE_FINAL
                db.session.commit()
                logger.info(f"Tournament {tournament.id} Final populated from SFs")
                return True

        return False

    def _check_knockout_progression(self, tournament: Tournament) -> bool:
        """
        Progress a pure knockout tournament based on completed matches.
        """
        current_stage = tournament.current_stage
        
        # Check if all matches in current stage are completed
        pending = TournamentFixture.query.filter(
            TournamentFixture.tournament_id == tournament.id,
            TournamentFixture.stage == current_stage,
            TournamentFixture.status != 'Completed'
        ).count()

        if pending > 0:
            return False
            
        # All matches in current stage done. Advance winners.
        # Logic: winner of bracket_position X and X+1 move to next round's bracket_position Y
        # where Y = offset + (X // 2)
        matches = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            stage=current_stage
        ).order_by(TournamentFixture.bracket_position).all()
        
        if not matches:
            return False
            
        # If this was the final, we are done
        if current_stage == self.STAGE_FINAL:
            return False # _check_tournament_completion will handle status
            
        # Identify next round fixtures
        num_teams = len(tournament.participating_teams)
        next_power = self._next_power_of_two(num_teams)
        
        # Find the first match of the next round
        # Bracket positions for round R (starting R=1):
        # R1: 0 to (N/2 - 1)
        # R2: N/2 to (N/2 + N/4 - 1)
        # ... and so on
        
        total_slots = next_power - 1
        current_round_start = 0
        matches_per_round = next_power // 2
        
        # Determine current round number and round start/count
        r = 1
        temp_start = 0
        temp_mpr = next_power // 2
        while temp_start < matches[0].bracket_position:
            temp_start += temp_mpr
            temp_mpr //= 2
            r += 1
        
        current_round_start = temp_start
        matches_per_round = temp_mpr
        next_round_start = current_round_start + matches_per_round
        
        # Advance winners (ensure winners are present)
        if any(match.winner_team_id is None for match in matches):
            logger.warning(
                "Tournament %s knockout progression blocked: missing winners in stage %s",
                tournament.id,
                current_stage
            )
            return False

        # Advance winners
        for i in range(0, len(matches), 2):
            if i + 1 >= len(matches):
                # Should not happen in a power-of-2 tree unless it's the final
                break
                
            m1 = matches[i]
            m2 = matches[i+1]
            
            next_bp = next_round_start + (i // 2)
            next_fixture = TournamentFixture.query.filter_by(
                tournament_id=tournament.id,
                bracket_position=next_bp
            ).first()
            
            if next_fixture:
                next_fixture.home_team_id = m1.winner_team_id
                next_fixture.away_team_id = m2.winner_team_id
                
                # If both teams are now known, unlock the match
                if next_fixture.home_team_id and next_fixture.away_team_id:
                    next_fixture.status = 'Scheduled'
                    
        # Update tournament current stage
        next_round_fixture = TournamentFixture.query.filter_by(
            tournament_id=tournament.id,
            bracket_position=next_round_start
        ).first()
        
        if next_round_fixture:
            tournament.current_stage = next_round_fixture.stage
            db.session.commit()
            logger.info(f"Tournament {tournament.id} progressed to {tournament.current_stage}")
            return True
            
        return False

    def _advance_bye_winners(self, tournament_id: int):
        """
        Automatically advance winners of bye matches in initial knockout round.
        """
        # Find all completed matches in Round 1 (byes)
        byes = TournamentFixture.query.filter_by(
            tournament_id=tournament_id,
            status='Completed',
            round_number=1
        ).all()
        
        if not byes:
            return
            
        tournament = db.session.get(Tournament, tournament_id)
        if not tournament:
            return
            
        num_teams = len(tournament.participating_teams)
        next_power = self._next_power_of_two(num_teams)
        next_round_start = next_power // 2
        
        for m in byes:
            # bracket_position X advances to next_round_start + (X // 2)
            next_bp = next_round_start + (m.bracket_position // 2)
            next_fixture = TournamentFixture.query.filter_by(
                tournament_id=tournament_id,
                bracket_position=next_bp
            ).first()
            
            if next_fixture:
                if m.bracket_position % 2 == 0:
                    next_fixture.home_team_id = m.winner_team_id
                else:
                    next_fixture.away_team_id = m.winner_team_id
                
                # If both teams are now known (or one is an advanced bye), unlock it
                if next_fixture.home_team_id and next_fixture.away_team_id:
                    next_fixture.status = 'Scheduled'
        
        db.session.commit()

    def get_standings(self, tournament_id: int) -> list:
        """
        Get tournament standings sorted by points and NRR.

        Returns:
            List of TournamentTeam objects sorted by:
            1. Points (descending)
            2. Net Run Rate (descending)
            3. Wins (descending)
        """
        return TournamentTeam.query.filter_by(
            tournament_id=tournament_id
        ).order_by(
            TournamentTeam.points.desc(),
            TournamentTeam.net_run_rate.desc(),
            TournamentTeam.won.desc(),
            TournamentTeam.runs_scored.desc()  # Deterministic tie-breaker
        ).all()

    def _ensure_team_stats(self, tournament_id: int, team_id: int):
        stats = TournamentTeam.query.filter_by(tournament_id=tournament_id, team_id=team_id).first()
        if stats:
            return stats
        stats = TournamentTeam(
            tournament_id=tournament_id,
            team_id=team_id,
            played=0,
            won=0,
            lost=0,
            tied=0,
            no_result=0,
            points=0,
            net_run_rate=0.0,
            runs_scored=0,
            runs_conceded=0,
            overs_faced=0.0,
            overs_bowled=0.0,
        )
        db.session.add(stats)
        return stats

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

        # Find and update the Fixture Record
        fixture = TournamentFixture.query.filter_by(match_id=match.id).first()
        if fixture:
            if fixture.standings_applied:
                logger.info(
                    "Standings already applied for fixture %s (match %s); skipping update.",
                    fixture.id,
                    match.id
                )
                return False
            fixture.status = 'Completed'
            fixture.winner_team_id = match.winner_team_id
            fixture.standings_applied = True

        # Get team stats records
        home_team_stats = self._ensure_team_stats(match.tournament_id, match.home_team_id)
        away_team_stats = self._ensure_team_stats(match.tournament_id, match.away_team_id)

        # Only update league standings (not knockout stats)
        if fixture and fixture.stage == self.STAGE_LEAGUE:
            # Update Played count
            home_team_stats.played += 1
            away_team_stats.played += 1

            # Determine Result
            winner_id = match.winner_team_id
            is_no_result = self._is_no_result(match)

            if is_no_result:
                home_team_stats.no_result += 1
                away_team_stats.no_result += 1
                home_team_stats.points += self.POINTS_NO_RESULT
                away_team_stats.points += self.POINTS_NO_RESULT
            elif winner_id == match.home_team_id:
                home_team_stats.won += 1
                home_team_stats.points += self.POINTS_WIN
                away_team_stats.lost += 1
            elif winner_id == match.away_team_id:
                away_team_stats.won += 1
                away_team_stats.points += self.POINTS_WIN
                home_team_stats.lost += 1
            else:
                home_team_stats.tied += 1
                away_team_stats.tied += 1
                home_team_stats.points += self.POINTS_TIE
                away_team_stats.points += self.POINTS_TIE

            # Update NRR components
            if not is_no_result:
                self._update_nrr_components(home_team_stats, away_team_stats, match)

        # Check tournament progression
        self._check_tournament_completion(match.tournament_id)

        # Check if we need to progress to next stage
        self.check_and_progress_tournament(match.tournament_id)

        if fixture:
            fixture.standings_applied = True

        if commit:
            db.session.commit()

        return True

    def _is_no_result(self, match) -> bool:
        """Determine if a match is a No Result."""
        if match.winner_team_id:
            return False

        result_desc = (match.result_description or '').lower()
        if 'abandoned' in result_desc or 'no result' in result_desc:
            return True

        home_overs = match.home_team_overs or 0
        away_overs = match.away_team_overs or 0
        if home_overs == 0 and away_overs == 0:
            return True

        return False

    def _update_nrr_components(self, home_stats, away_stats, match):
        """Update Net Run Rate components for both teams."""
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

        # Recalculate NRR
        self._calculate_nrr(home_stats)
        self._calculate_nrr(away_stats)

    def _check_tournament_completion(self, tournament_id):
        """Check if all fixtures are completed and update tournament status."""
        pending = TournamentFixture.query.filter(
            TournamentFixture.tournament_id == tournament_id,
            TournamentFixture.status.in_(['Scheduled', 'Locked'])
        ).count()

        if pending == 0:
            tournament = db.session.get(Tournament, tournament_id)
            if tournament and tournament.status != 'Completed':
                tournament.status = 'Completed'
                tournament.current_stage = 'completed'
                logger.info(f"Tournament {tournament_id} marked as Completed")

    def _add_overs(self, o1, o2):
        """Add two cricket overs values."""
        balls1 = self.overs_to_balls(o1 or 0.0)
        balls2 = self.overs_to_balls(o2 or 0.0)
        return self.balls_to_overs(balls1 + balls2)

    def _subtract_overs(self, total, sub):
        """Subtract cricket overs values."""
        total_balls = self.overs_to_balls(total or 0.0)
        sub_balls = self.overs_to_balls(sub or 0.0)
        result_balls = max(0, total_balls - sub_balls)
        return self.balls_to_overs(result_balls)

    def _calculate_nrr(self, team_stats):
        """Calculate and update the net run rate for a team."""
        balls_faced = self.overs_to_balls(team_stats.overs_faced or 0.0)
        balls_bowled = self.overs_to_balls(team_stats.overs_bowled or 0.0)

        if balls_faced > 0:
            run_rate_for = (team_stats.runs_scored or 0) / (balls_faced / 6.0)
        else:
            run_rate_for = 0.0

        if balls_bowled > 0:
            run_rate_against = (team_stats.runs_conceded or 0) / (balls_bowled / 6.0)
        else:
            run_rate_against = 0.0

        team_stats.net_run_rate = round(run_rate_for - run_rate_against, 3)

    def reverse_standings(self, match, commit=False):
        """
        Reverses the stats update for a match (used for re-simulation).
        """
        if not match.tournament_id:
            return False

        # Get fixture to check stage
        fixture = TournamentFixture.query.filter_by(match_id=match.id).first()
        if not fixture:
            logger.error("No fixture found for match %s; cannot reverse standings.", match.id)
            return False

        home_team_stats = self._ensure_team_stats(match.tournament_id, match.home_team_id)
        away_team_stats = self._ensure_team_stats(match.tournament_id, match.away_team_id)

        # Only reverse league standings
        if fixture and fixture.stage == self.STAGE_LEAGUE:
            home_team_stats.played = max(0, home_team_stats.played - 1)
            away_team_stats.played = max(0, away_team_stats.played - 1)

            winner_id = match.winner_team_id
            was_no_result = self._is_no_result(match)

            if was_no_result:
                home_team_stats.no_result = max(0, home_team_stats.no_result - 1)
                away_team_stats.no_result = max(0, away_team_stats.no_result - 1)
                home_team_stats.points = max(0, home_team_stats.points - self.POINTS_NO_RESULT)
                away_team_stats.points = max(0, away_team_stats.points - self.POINTS_NO_RESULT)
            elif winner_id == match.home_team_id:
                home_team_stats.won = max(0, home_team_stats.won - 1)
                home_team_stats.points = max(0, home_team_stats.points - self.POINTS_WIN)
                away_team_stats.lost = max(0, away_team_stats.lost - 1)
            elif winner_id == match.away_team_id:
                away_team_stats.won = max(0, away_team_stats.won - 1)
                away_team_stats.points = max(0, away_team_stats.points - self.POINTS_WIN)
                home_team_stats.lost = max(0, home_team_stats.lost - 1)
            else:
                home_team_stats.tied = max(0, home_team_stats.tied - 1)
                away_team_stats.tied = max(0, away_team_stats.tied - 1)
                home_team_stats.points = max(0, home_team_stats.points - self.POINTS_TIE)
                away_team_stats.points = max(0, away_team_stats.points - self.POINTS_TIE)

            if not was_no_result:
                self._reverse_nrr_components(home_team_stats, away_team_stats, match)

            self._calculate_nrr(home_team_stats)
            self._calculate_nrr(away_team_stats)

        # Reset fixture winner and standings state
        if fixture:
            fixture.winner_team_id = None
            fixture.status = 'Scheduled'
            fixture.standings_applied = False
            fixture.match_id = None

            if fixture.stage != self.STAGE_LEAGUE:
                self._reset_knockout_bracket(match.tournament_id, fixture.bracket_position)

        # Reset tournament status if it was completed
        tournament = db.session.get(Tournament, match.tournament_id)
        if tournament and tournament.status == 'Completed':
            tournament.status = 'Active'
        if tournament and fixture and fixture.stage != self.STAGE_LEAGUE:
            tournament.current_stage = fixture.stage

        if commit:
            db.session.commit()

        logger.info(f"Reversed standings for match {match.id}")
        return True

    def _reset_knockout_bracket(self, tournament_id: int, from_bracket_position: int):
        """
        Reset downstream knockout fixtures when a completed fixture is re-simulated.

        This clears dependent teams, match links, and winners, and locks fixtures
        that should be repopulated once earlier rounds are replayed.
        """
        tbd_id = self._get_placeholder_team_id(tournament_id, "TBD")
        downstream = TournamentFixture.query.filter(
            TournamentFixture.tournament_id == tournament_id,
            TournamentFixture.bracket_position != None,
            TournamentFixture.bracket_position > from_bracket_position
        ).all()

        for fixture in downstream:
            fixture.home_team_id = tbd_id
            fixture.away_team_id = tbd_id
            fixture.winner_team_id = None
            fixture.match_id = None
            fixture.status = 'Locked'
            fixture.standings_applied = False

    def _get_placeholder_team_id(self, tournament_id: int, label: str) -> int:
        """
        Ensure a per-user placeholder team (BYE/TBD) exists and return its ID.
        """
        tournament = db.session.get(Tournament, tournament_id)
        if not tournament:
            raise ValueError(f"Tournament {tournament_id} not found.")

        placeholder = Team.query.filter_by(user_id=tournament.user_id, name=label).first()
        if placeholder:
            return placeholder.id

        placeholder = Team(
            user_id=tournament.user_id,
            name=label,
            short_code=label
        )
        db.session.add(placeholder)
        db.session.flush()
        return placeholder.id

    def _reverse_nrr_components(self, home_stats, away_stats, match):
        """Reverse the NRR component updates from a match."""
        home_score = match.home_team_score or 0
        away_score = match.away_team_score or 0
        home_overs = match.home_team_overs or 0.0
        away_overs = match.away_team_overs or 0.0

        home_stats.runs_scored = max(0, (home_stats.runs_scored or 0) - home_score)
        home_stats.overs_faced = self._subtract_overs(home_stats.overs_faced, home_overs)
        away_stats.runs_conceded = max(0, (away_stats.runs_conceded or 0) - home_score)
        away_stats.overs_bowled = self._subtract_overs(away_stats.overs_bowled, home_overs)

        away_stats.runs_scored = max(0, (away_stats.runs_scored or 0) - away_score)
        away_stats.overs_faced = self._subtract_overs(away_stats.overs_faced, away_overs)
        home_stats.runs_conceded = max(0, (home_stats.runs_conceded or 0) - away_score)
        home_stats.overs_bowled = self._subtract_overs(home_stats.overs_bowled, away_overs)
