import builtins
import logging
import random
from engine.ball_outcome import calculate_outcome
from engine.super_over_outcome import calculate_super_over_outcome
from match_archiver import MatchArchiver, find_original_json_file
from engine.pressure_engine import PressureEngine

logger = logging.getLogger(__name__)

# Guard console output on Windows consoles that choke on emoji/unicode.
def safe_print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except OSError:
        sanitized = []
        for arg in args:
            if isinstance(arg, str):
                sanitized.append(arg.encode("ascii", "ignore").decode())
            else:
                sanitized.append(arg)
        builtins.print(*sanitized, **kwargs)

# Override module-level print to the safe version
print = safe_print


class Match:
    def __init__(self, match_data):
        self.innings = 1
        self.first_innings_score = None
        self.target = None
        self.data = match_data
        self.pitch = match_data["pitch"]
        self.stadium = match_data["stadium"]
        self.home_xi = match_data["playing_xi"]["home"]
        self.away_xi = match_data["playing_xi"]["away"]

        # Load substitutes, defaulting to empty lists if not present
        self.home_substitutes = match_data.get("substitutes", {}).get("home", [])
        self.away_substitutes = match_data.get("substitutes", {}).get("away", [])
        
        self.toss_winner = match_data.get("toss_winner")
        self.toss_decision = match_data.get("toss_decision")

        team_home = match_data["team_home"].split("_")[0]
        team_away = match_data["team_away"].split("_")[0]

        # Correct toss logic clearly defined
        if self.toss_winner == team_home:
            if self.toss_decision == "Bat":
                self.batting_team, self.bowling_team = self.home_xi, self.away_xi
            else:  # toss_decision == "Bowl"
                self.batting_team, self.bowling_team = self.away_xi, self.home_xi
        else:  # toss_winner == team_away
            if self.toss_decision == "Bat":
                self.batting_team, self.bowling_team = self.away_xi, self.home_xi
            else:  # toss_decision == "Bowl"
                self.batting_team, self.bowling_team = self.home_xi, self.away_xi

        self.bowler_history = {}
        self.overs = 20
        self.current_over = 0
        self.current_ball = 0
        self.batter_idx = [0, 1]
        self.score = 0
        self.wickets = 0
        self.commentary = []

        # Initialize comprehensive stats
        self.batsman_stats = {p["name"]: {"runs":0,"balls":0,"fours":0,"sixes":0,"ones":0,"twos":0,"threes":0,"dots":0,"wicket_type":"","bowler_out":"","fielder_out":""} for p in self.batting_team}
        self.current_over_runs = 0
        self.bowler_stats = {p["name"]: {"runs":0,"fours":0,"sixes":0,"wickets":0,"overs":0,"maidens":0,"balls_bowled":0,"wides":0,"noballs":0,"byes":0,"legbyes":0} for p in self.bowling_team if p["will_bowl"]}

        self.current_striker = self.batting_team[0]
        self.current_non_striker = self.batting_team[1]
        self.current_bowler = None
        # Add bowling pattern detection
        self.bowling_pattern = self._detect_bowling_pattern()

        # ✅ ADD THIS - Initialize over tracking for fatigue management
        self.over_bowler_log = {}
        self.prev_delivery_was_extra = False

        # ===== RAIN & DLS SYSTEM =====
        self.rain_affected = False
        self.rain_occurred = False  # Flag to prevent multiple rain events
        self.original_overs = 20
        self.rain_probability = match_data.get("rain_probability", 0.0)  # GET FROM MATCH DATA

        # ===== NEW ARCHIVING VARIABLES =====
        self.match_data = match_data  # Store original match data
        
        # Explicitly store all 4 innings stats
        self.first_innings_batting_stats = {}   # First batting team's batting stats
        self.first_innings_bowling_stats = {}   # First bowling team's bowling stats  
        self.second_innings_batting_stats = {}  # Second batting team's batting stats
        self.second_innings_bowling_stats = {}  # Second bowling team's bowling stats
        
        # Track which team batted first for correct CSV naming
        self.first_batting_team_name = ""  # e.g., "CSK" 
        self.first_bowling_team_name = ""  # e.g., "DC"
        
        self.result = ""  # Store final match result

        # Add super over tracking variables
        self.super_over_round = 0  # Track which super over we're on
        self.super_over_history = []  # Track scores from each super over

        # Initialize pressure engine
        self.pressure_engine = PressureEngine()

        # Track partnership for pressure calculation
        self.current_partnership_balls = 0
        self.current_partnership_runs = 0
        self.recent_wickets_count = 0  # Track wickets in last few balls
        self.recent_wickets_tracker = []  # Track when wickets fell


    def _calculate_current_match_state(self):
        """Calculate current match state for pressure calculation"""
        total_balls = self.current_over * 6 + self.current_ball
        current_rr = (self.score * 6) / total_balls if total_balls > 0 else 0
        
        state = {
            'innings': self.innings,
            'current_over': self.current_over,
            'current_run_rate': current_rr,
            'wickets': self.wickets,
            'score': self.score,
            'pitch': self.pitch,
            'current_partnership_balls': self.current_partnership_balls
        }
        
        if self.innings == 2:
            overs_played = self.current_over + (self.current_ball / 6)
            overs_remaining = self.overs - overs_played
            runs_needed = self.target - self.score
            required_rr = (runs_needed * 6) / (overs_remaining * 6) if overs_remaining > 0 else 0
            
            state.update({
                'overs_remaining': overs_remaining,
                'runs_needed': runs_needed,
                'required_run_rate': required_rr
            })
        
        return state

    def _update_partnership_tracking(self, outcome):
        """Update partnership tracking for pressure calculation"""
        if outcome.get('batter_out'):
            # Partnership broken
            self.current_partnership_balls = 0
            self.current_partnership_runs = 0
        else:
            # Continue partnership
            if not outcome.get('is_extra'):
                self.current_partnership_balls += 1
            self.current_partnership_runs += outcome.get('runs', 0)

    def _save_first_innings_stats(self):
        """Save first innings stats before resetting for second innings"""
        import copy
        self.first_innings_batting_stats = copy.deepcopy(self.batsman_stats)
        self.first_innings_bowling_stats = copy.deepcopy(self.bowler_stats)
        
        # Track which teams played in first innings
        if self.batting_team == self.home_xi:
            self.first_batting_team_name = self.match_data["team_home"].split("_")[0] 
            self.first_bowling_team_name = self.match_data["team_away"].split("_")[0]
        else:
            self.first_batting_team_name = self.match_data["team_away"].split("_")[0]
            self.first_bowling_team_name = self.match_data["team_home"].split("_")[0]
        
        print(f"✅ Saved first innings stats - {self.first_batting_team_name} batting: {len(self.first_innings_batting_stats)} players, {self.first_bowling_team_name} bowling: {len(self.first_innings_bowling_stats)} bowlers")

    def _save_second_innings_stats(self):
        """Save second innings stats at match completion"""
        import copy
        self.second_innings_batting_stats = copy.deepcopy(self.batsman_stats)
        self.second_innings_bowling_stats = copy.deepcopy(self.bowler_stats)
        
        # Determine second innings teams (opposite of first)
        if self.batting_team == self.home_xi:
            second_batting_team = self.match_data["team_home"].split("_")[0]
            second_bowling_team = self.match_data["team_away"].split("_")[0]
        else:
            second_batting_team = self.match_data["team_away"].split("_")[0]
            second_bowling_team = self.match_data["team_home"].split("_")[0]
        
        print(f"✅ Saved second innings stats - {second_batting_team} batting: {len(self.second_innings_batting_stats)} players, {second_bowling_team} bowling: {len(self.second_innings_bowling_stats)} bowlers")

    def set_frontend_commentary(self, frontend_commentary):
        """Set the frontend commentary for archiving"""
        self.frontend_commentary_captured = frontend_commentary
        print(f"📺 Frontend commentary set: {len(frontend_commentary)} items")


    def _create_match_archive(self):
        """Create complete match archive when match ends"""
        try:
            # Find original JSON file
            original_json_path = find_original_json_file(self.match_data['match_id'])
            
            if not original_json_path:
                print(f"⚠️ Could not find original JSON file for match {self.match_data['match_id']}")
                return False
            
            # Use frontend commentary if captured, otherwise use backend commentary
            commentary_to_archive = getattr(self, 'frontend_commentary_captured', self.commentary)
            
            if hasattr(self, 'frontend_commentary_captured'):
                print(f"📺 Using frontend commentary ({len(commentary_to_archive)} items)")
            else:
                print(f"🔧 Using backend commentary ({len(commentary_to_archive)} items)")
            
            # Create archiver and generate archive
            archiver = MatchArchiver(self.match_data, self)
            success = archiver.create_archive(original_json_path, commentary_to_archive)
            
            if success:
                print(f"🎉 Match archive created successfully!")
                return True
            else:
                print(f"❌ Failed to create match archive")
                return False
                
        except Exception as e:
            print(f"❌ Error creating match archive: {e}")
            return False

    # def _create_match_archive_with_frontend_commentary(self):
    #     """Alternative method called when frontend commentary is captured"""
    #     return self._create_match_archive()
            

    def _check_for_rain(self):
        """Simple probability-based rain check - only once per match"""
        logger.debug(f"RAIN CHECK: Over {self.current_over + 1}, Innings {self.innings}")

        if self.rain_occurred:  # Rain already happened
            logger.debug(f"Rain already occurred - skipping")
            return False

        # Only check after 5 overs in 1st innings, or any time in 2nd innings
        if self.innings == 1 and self.current_over < 5:
            logger.debug(f"Too early for rain (over {self.current_over + 1} < 5)")
            return False

        rain_roll = random.random()
        will_rain = rain_roll < self.rain_probability
        logger.debug(f"Rain roll: {rain_roll:.3f} < {self.rain_probability} = {will_rain}")
            
        return will_rain


    def _calculate_simple_dls(self, first_innings_score, first_innings_overs, second_innings_overs):
        """
        Simple DLS calculation using proportional method
        Formula: Target = (First_Score * Second_Overs / First_Overs) + 1
        
        Args:
            first_innings_score: Runs scored in first innings
            first_innings_overs: Overs played in first innings  
            second_innings_overs: Overs available for second innings
        
        Returns:
            int: DLS target for second innings
        """
        if first_innings_overs <= 0:
            return first_innings_score + 1
            
        target = int((first_innings_score * second_innings_overs / first_innings_overs) + 1)
        return target

    def _handle_rain_event(self):
        """Handle rain for both scenarios"""
        self.rain_occurred = True  # Set flag to prevent future rain
        self.rain_affected = True
        
        if self.innings == 1:
            return self._handle_first_innings_rain()
        elif self.innings == 2:
            return self._handle_second_innings_rain()
        
    # from the _handle_first_innings_rain function
    def _handle_first_innings_rain(self):
        """Scenario 1: Rain during 1st innings - stop innings and calculate DLS"""
        current_score = self.score
        current_overs = self.current_over
        
        # Ensure minimum 5 overs for valid match
        if current_overs < 5:
            return {
                "match_abandoned": True,
                "commentary": "<br><strong>MATCH ABANDONED!</strong><br>Rain came before minimum 5 overs completed.",
                "result": "Match abandoned due to rain"
            }
        
        # Calculate reduced overs (same for both teams)
        reduced_overs = current_overs
        
        # Calculate DLS target for 2nd innings
        dls_target = self._calculate_simple_dls(current_score, current_overs, reduced_overs)
        
        # Update match parameters
        self.first_innings_score = current_score
        self.target = dls_target
        self.overs = reduced_overs  # This will limit 2nd innings automatically
        
        # PRODUCTION FIX: Save the first innings stats before resetting for the second innings.
        self._save_first_innings_stats()
        
        # Generate scorecard for 1st innings
        scorecard_data = self._generate_detailed_scorecard()
        scorecard_data["target_info"] = f"DLS Target: {dls_target} runs from {reduced_overs} overs"
        
        # Reset for 2nd innings
        self.innings = 2
        self.batting_team, self.bowling_team = self.bowling_team, self.batting_team
        self.score = 0

        self.wickets = 0
        self.current_over = 0
        self.current_ball = 0
        self.batter_idx = [0, 1]
        self.current_striker = self.batting_team[0]
        self.current_non_striker = self.batting_team[1]
        self.batsman_stats = {p["name"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0, "threes": 0, "dots": 0, "wicket_type": "", "bowler_out": "", "fielder_out": ""} for p in self.batting_team}
        self.bowler_history = {}
        self.bowler_stats = {p["name"]: {"runs": 0, "fours": 0, "sixes": 0, "wickets": 0, "overs": 0, "maidens": 0, "balls_bowled": 0, "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0} for p in self.bowling_team if p["will_bowl"]}
        self._reset_innings_state()
        
        # Commentary with DLS details
        commentary = f"<br><strong>🌧️ RAIN INTERRUPTION!</strong><br>"
        commentary += f"End of 1st Innings: {current_score}/{10 if self.wickets >= 10 else self.wickets} in {current_overs} overs<br>"
        commentary += f"Match reduced to {reduced_overs} overs per side<br>"
        commentary += f"<strong>DLS Target: {dls_target} runs from {reduced_overs} overs</strong><br>"
        commentary += f"Required Rate: {dls_target/reduced_overs:.2f} runs per over<br><br>"
        
        return {
            "rain_first_innings": True,
            "innings_end": True,
            "innings_number": 1,
            "match_over": False,
            "scorecard_data": scorecard_data,
            "score": 0,
            "wickets": 0,
            "over": 0,
            "ball": 0,
            "commentary": commentary,
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": "",
            "dls_target": dls_target,
            "reduced_overs": reduced_overs
        }
    
    def _handle_second_innings_rain(self):
        """Scenario 2: Rain during 2nd innings - revise target and continue"""
        current_score = self.score
        overs_played = self.current_over
        original_target = self.target
        
        # Calculate minimum overs needed for valid match
        min_overs_needed = 5
        
        # Determine reduced overs (ensure at least current overs + 2 more, minimum 5)
        reduced_overs = max(min_overs_needed, overs_played + 2)
        
        # If we've already played more than the reduced overs, match ends now
        if overs_played >= reduced_overs:
            # Match ends - check if target achieved
            if current_score >= original_target:
                winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                wkts_left = 10 - self.wickets
                self.result = f"{winner_code} won by {wkts_left} wicket(s) (DLS Method)"
            else:
                winner_code = self.data["team_home"].split("_")[0] if self.bowling_team is self.home_xi else self.data["team_away"].split("_")[0]
                run_diff = original_target - current_score - 1
                self.result = f"{winner_code} won by {run_diff} run(s) (DLS Method)"
            
            scorecard_data = self._generate_detailed_scorecard()
            scorecard_data["target_info"] = self.result
            
            self._save_second_innings_stats()
            self._create_match_archive()

            return {
                "rain_match_ended": True,
                "match_over": True,
                "scorecard_data": scorecard_data,
                "final_score": current_score,
                "wickets": self.wickets,
                "result": self.result,
                "commentary": f"<br><strong>🌧️ RAIN STOPS PLAY!</strong><br><strong>Match Over!</strong> {self.result}"
            }
        
        # Calculate revised DLS target based on new overs available
        revised_target = self._calculate_simple_dls(
            self.first_innings_score,
            self.original_overs,
            reduced_overs
        )
        
        # Update match parameters MID-MATCH
        self.overs = reduced_overs
        self.target = revised_target
        
        # Commentary with revised target
        runs_needed = revised_target - current_score
        overs_left = reduced_overs - overs_played
        required_rr = runs_needed / overs_left if overs_left > 0 else 0
        
        commentary = f"<br><strong>🌧️ RAIN INTERRUPTION!</strong><br>"
        commentary += f"Match reduced to {reduced_overs} overs per side<br>"
        commentary += f"<strong>Revised DLS Target: {revised_target} runs</strong><br>"
        commentary += f"Required: {runs_needed} runs from {overs_left} overs<br>"
        commentary += f"Required Rate: {required_rr:.2f} runs per over<br>"
        
        return {
            "rain_second_innings": True,
            "match_over": False,
            "score": current_score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "commentary": commentary,
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": self.current_bowler["name"] if self.current_bowler else "",
            "revised_target": revised_target,
            "reduced_overs": reduced_overs,
            "original_target": original_target
        }

    def _validate_death_overs_plan(self, death_plan, remaining_bowlers):
        """Validate death overs plan to ensure no violations"""
        print(f" 🔍 DEATH OVERS PLAN VALIDATION:")
        print(f" Plan: {death_plan}")
        
        # Check 1: No consecutive bowling
        for i in range(len(death_plan) - 1):
            if death_plan[i] == death_plan[i + 1]:
                print(f" ❌ CONSECUTIVE VIOLATION: {death_plan[i]} in positions {i+1} and {i+2}")
                return False
        
        # Check 2: Quota compliance
        usage_count = {}
        for bowler_name in death_plan:
            usage_count[bowler_name] = usage_count.get(bowler_name, 0) + 1
        
        for bowler_name, used_overs in usage_count.items():
            available_overs = remaining_bowlers.get(bowler_name, 0)
            if used_overs > available_overs:
                print(f" ❌ QUOTA VIOLATION: {bowler_name} uses {used_overs} but has {available_overs}")
                return False
        
        print(f" ✅ DEATH OVERS PLAN VALIDATED")
        return True


    def _handle_3_bowler_death_scenario_safe(self, remaining_bowlers, previous_bowler):
        """
        Handle: A(1 over), B(1 over), C(1 over)
        ENFORCES NO CONSECUTIVE CONSTRAINT
        """
        print(f"  📋 3-Bowler Scenario (Consecutive-Safe):")
        
        bowler_names = list(remaining_bowlers.keys())
        print(f"    Available: {bowler_names}")
        print(f"    Previous bowler: {previous_bowler}")
        
        # Remove previous bowler from first position to avoid consecutive
        if previous_bowler in bowler_names:
            non_previous = [b for b in bowler_names if b != previous_bowler]
            bowler_18 = non_previous[0]
            remaining_after_18 = [b for b in bowler_names if b != bowler_18]
            bowler_19 = remaining_after_18[0]
            bowler_20 = remaining_after_18[1]
        else:
            # Previous bowler not in remaining (normal case)
            bowler_18 = bowler_names[0]
            bowler_19 = bowler_names[1] 
            bowler_20 = bowler_names[2]
        
        death_plan = [bowler_18, bowler_19, bowler_20]
        print(f"  ✅ Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        
        return death_plan

    def _handle_complex_death_scenario_safe(self, remaining_bowlers, previous_bowler):
        """
        Handle complex scenarios (4+ bowlers or unusual distributions)
        ENFORCES NO CONSECUTIVE CONSTRAINT
        """
        print(f"  📋 Complex Scenario ({len(remaining_bowlers)} bowlers, Consecutive-Safe):")
        
        available_bowlers = list(remaining_bowlers.keys())
        death_plan = []
        used_in_plan = {}
        
        # Initialize usage tracking
        for name in available_bowlers:
            used_in_plan[name] = 0
        
        # Plan each over ensuring no consecutive bowling
        last_bowler = previous_bowler
        
        for over_idx in range(3):  # overs 18, 19, 20
            print(f"    Planning over {18 + over_idx}, last bowler: {last_bowler}")
            
            # Find eligible bowlers for this over
            eligible = []
            for name in available_bowlers:
                # Check if bowler has overs remaining
                remaining_quota = remaining_bowlers[name] - used_in_plan[name]
                # Check if not consecutive
                is_consecutive = (name == last_bowler)
                
                if remaining_quota > 0 and not is_consecutive:
                    eligible.append(name)
            
            if not eligible:
                # Emergency: use any bowler with quota (allow consecutive if necessary)
                print(f"    🚨 No non-consecutive bowlers available!")
                for name in available_bowlers:
                    remaining_quota = remaining_bowlers[name] - used_in_plan[name]
                    if remaining_quota > 0:
                        eligible.append(name)
                        break
            
            if eligible:
                # Select bowler (prefer those with more remaining overs)
                selected = max(eligible, key=lambda x: remaining_bowlers[x] - used_in_plan[x])
                death_plan.append(selected)
                used_in_plan[selected] += 1
                last_bowler = selected
                print(f"    Selected: {selected}")
            else:
                print(f"    🚨 CRITICAL: No bowlers available!")
                break
        
        # Ensure we have exactly 3 bowlers
        while len(death_plan) < 3:
            death_plan.append(available_bowlers[0])  # Emergency fallback
        
        print(f"  ✅ Complex Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        return death_plan[:3]

    def _emergency_death_plan_safe(self, remaining_bowlers, previous_bowler):
        """
        Emergency plan when mathematical constraints are violated
        RESPECTS CONSECUTIVE CONSTRAINT EVEN IN EMERGENCIES
        FIXED: Better handles impossible 2-bowler scenarios
        """
        print(f"  🚨 EMERGENCY DEATH PLAN (Consecutive-Safe - FIXED): {remaining_bowlers}")
        
        if not remaining_bowlers:
            print(f"  💥 CRITICAL: No bowlers with remaining overs!")
            # Use any bowler as absolute last resort
            all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
            if all_bowlers:
                emergency_bowler = all_bowlers[0]["name"]
                return [emergency_bowler, emergency_bowler, emergency_bowler]
            return ["Emergency_Bowler", "Emergency_Bowler", "Emergency_Bowler"]
        
        available_bowlers = list(remaining_bowlers.keys())
        death_plan = []
        
        # Create a working copy of remaining bowlers to modify
        working_quota = remaining_bowlers.copy()
        
        # Build plan ensuring no consecutive overs
        last_bowler = previous_bowler
        
        for over_num in range(3):  # overs 18, 19, 20
            print(f"    Planning over {18 + over_num}, last bowler: {last_bowler}")
            print(f"    Available quota: {working_quota}")
            
            # Find bowler who didn't bowl previous over and has quota
            selected_bowler = None
            
            # Priority 1: Non-consecutive bowlers with quota
            for bowler_name in available_bowlers:
                if bowler_name != last_bowler and working_quota.get(bowler_name, 0) > 0:
                    selected_bowler = bowler_name
                    break
            
            # Priority 2: If no non-consecutive bowler available, ALLOW QUOTA VIOLATION
            # but still prefer non-consecutive if possible
            if not selected_bowler:
                print(f"    🚨 No non-consecutive bowlers with quota!")
                
                # Try to find any non-consecutive bowler (even with 0 quota)
                for bowler_name in available_bowlers:
                    if bowler_name != last_bowler:
                        selected_bowler = bowler_name
                        print(f"    ⚠️ QUOTA VIOLATION: Using {bowler_name} with {working_quota.get(bowler_name, 0)} quota")
                        break
            
            # Priority 3: Absolute emergency - allow consecutive if necessary
            if not selected_bowler:
                print(f"    💥 ABSOLUTE EMERGENCY: Allowing consecutive bowling")
                for bowler_name in available_bowlers:
                    if working_quota.get(bowler_name, 0) > 0:
                        selected_bowler = bowler_name
                        print(f"    💥 CONSECUTIVE VIOLATION: Using {bowler_name}")
                        break
                
                # If still no bowler, use first available
                if not selected_bowler:
                    selected_bowler = available_bowlers[0]
                    print(f"    💥 LAST RESORT: Using {selected_bowler}")
            
            # Add to plan and update tracking
            death_plan.append(selected_bowler)
            if working_quota.get(selected_bowler, 0) > 0:
                working_quota[selected_bowler] -= 1
            last_bowler = selected_bowler
            
            print(f"    Selected: {selected_bowler}")
        
        print(f"  ⚠️ Emergency Plan (Consecutive-Safe): 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        
        # Log any violations for monitoring
        violation_count = 0
        for i in range(len(death_plan) - 1):
            if death_plan[i] == death_plan[i + 1]:
                violation_count += 1
                print(f"  🚨 EMERGENCY CONSECUTIVE VIOLATION: {death_plan[i]} in positions {i+1} and {i+2}")
        
        if violation_count > 0:
            self._log_constraint_violation("EMERGENCY_CONSECUTIVE_VIOLATION", 
                                        f"Emergency plan forced {violation_count} consecutive bowling instances")
        
        return death_plan

    def _calculate_death_overs_plan_safe(self, bowler_quota):
        """
        DEBUG VERSION: Calculate optimal 3-over distribution for overs 18-20
        """
        print(f"  🐛 DEBUG _calculate_death_overs_plan_safe:")
        print(f"     Input bowler_quota: {list(bowler_quota.keys())}")
        
        # Get bowlers with remaining overs
        remaining_bowlers = {}
        total_remaining = 0
        
        for name, data in bowler_quota.items():
            if data['overs_remaining'] > 0:
                remaining_bowlers[name] = data['overs_remaining']
                total_remaining += data['overs_remaining']
                print(f"    {name}: {data['overs_remaining']} overs remaining")
        
        print(f"  🐛 Remaining bowlers: {remaining_bowlers}")
        print(f"  🐛 Total remaining: {total_remaining}")
        
        # Get previous bowler
        previous_bowler = self.current_bowler["name"] if self.current_bowler else None
        print(f"  🐛 Previous bowler: {previous_bowler}")
        
        # Check scenario
        num_bowlers = len(remaining_bowlers)
        print(f"  🐛 Number of bowlers with quota: {num_bowlers}")
        
        death_plan = None
        
        if num_bowlers == 2:
            print(f"  🐛 CASE 1: 2-bowler scenario")
            death_plan = self._handle_2_bowler_death_scenario_safe(remaining_bowlers, previous_bowler)
            if death_plan is None:
                print(f"  🐛 2-bowler scenario returned None - using emergency")
                death_plan = self._emergency_death_plan_safe(remaining_bowlers, previous_bowler)
        elif num_bowlers == 3:
            print(f"  🐛 CASE 2: 3-bowler scenario")
            death_plan = self._handle_3_bowler_death_scenario_safe(remaining_bowlers, previous_bowler)
        else:
            print(f"  🐛 CASE 3: Complex scenario ({num_bowlers} bowlers)")
            death_plan = self._handle_complex_death_scenario_safe(remaining_bowlers, previous_bowler)
        
        print(f"  🐛 Final death plan: {death_plan}")
        
        # Validate plan
        if death_plan:
            validation_copy = remaining_bowlers.copy()
            if self._validate_death_overs_plan(death_plan, validation_copy):
                print(f"  🐛 Death plan PASSED validation")
                return death_plan
            else:
                print(f"  🐛 Death plan FAILED validation - using emergency")
                return self._emergency_death_plan_safe(remaining_bowlers, previous_bowler)
        else:
            print(f"  🐛 Death plan is None - using emergency")
            return self._emergency_death_plan_safe(remaining_bowlers, previous_bowler)

    def _handle_2_bowler_death_scenario_safe(self, remaining_bowlers, previous_bowler):
        """
        Handle: A(X overs left), B(Y overs left) where X+Y = 3
        ENFORCES NO CONSECUTIVE CONSTRAINT + OPTIMAL DISTRIBUTION
        FIXED: Prevents consecutive bowling in ALL scenarios
        """
        print(f" 📋 Enhanced 2-Bowler Death Scenario (Consecutive-Safe - FIXED):")
        
        bowler_names = list(remaining_bowlers.keys())
        bowler_1_name = bowler_names[0]
        bowler_2_name = bowler_names[1]
        overs_1 = remaining_bowlers[bowler_1_name]
        overs_2 = remaining_bowlers[bowler_2_name]
        
        print(f" {bowler_1_name}: {overs_1} overs, {bowler_2_name}: {overs_2} overs")
        print(f" Previous bowler: {previous_bowler}")
        
        # Determine who has more overs
        if overs_1 > overs_2:
            bowler_more_overs = bowler_1_name
            bowler_fewer_overs = bowler_2_name
            overs_more = overs_1
            overs_fewer = overs_2
        elif overs_2 > overs_1:
            bowler_more_overs = bowler_2_name
            bowler_fewer_overs = bowler_1_name
            overs_more = overs_2
            overs_fewer = overs_1
        else:
            # Equal overs - choose arbitrarily but still apply consecutive logic
            bowler_more_overs = bowler_1_name
            bowler_fewer_overs = bowler_2_name
            overs_more = overs_1
            overs_fewer = overs_2
        
        print(f" More overs: {bowler_more_overs} ({overs_more})")
        print(f" Fewer overs: {bowler_fewer_overs} ({overs_fewer})")
        
        # CRITICAL CHECK: Can we create a valid plan without consecutive violations?
        
        if previous_bowler == bowler_more_overs:
            # Can't start with bowler who has more overs
            print(f" 🚨 CONSECUTIVE CONSTRAINT: Can't start with {bowler_more_overs}")
            
            # Check if we can create a valid plan starting with fewer-overs bowler
            if overs_more == 2 and overs_fewer == 1:
                # Only possible plan: [fewer, more, ???]
                # But "more" can't bowl again after position 2
                # This creates an impossible scenario: we need "more" to bowl 2 overs
                # but can't have consecutive, and "fewer" only has 1 over
                print(f" 🚨 MATHEMATICAL IMPOSSIBILITY: Can't distribute 2-1 without consecutive")
                print(f"    Required: {bowler_more_overs} needs 2 overs but can't be consecutive")
                print(f"    Available: {bowler_fewer_overs} only has 1 over")
                return None  # Signal that this scenario is impossible
            else:
                # For other distributions (like 1-2 which shouldn't happen, or edge cases)
                # Try: [fewer, more, fewer] if fewer has enough overs
                if overs_fewer >= 2:
                    death_plan = [bowler_fewer_overs, bowler_more_overs, bowler_fewer_overs]
                    print(f" ✅ Alternative Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
                else:
                    print(f" 🚨 IMPOSSIBLE: {bowler_fewer_overs} doesn't have enough overs for alternative")
                    return None
        
        elif previous_bowler == bowler_fewer_overs:
            # Perfect - can start with bowler who has more overs
            print(f" ✅ OPTIMAL: Starting with {bowler_more_overs} (more overs)")
            
            if overs_more == 2 and overs_fewer == 1:
                # Standard case: [more, fewer, more]
                death_plan = [bowler_more_overs, bowler_fewer_overs, bowler_more_overs]
                print(f" ✅ Optimal Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
            else:
                # Handle other distributions
                death_plan = [bowler_more_overs, bowler_fewer_overs, bowler_more_overs]
                print(f" ✅ Standard Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        
        else:
            # Neither bowled previous over - use optimal distribution
            print(f" ✅ NO CONSECUTIVE ISSUE: Using optimal distribution")
            
            if overs_more == 2 and overs_fewer == 1:
                # Optimal: [more, fewer, more] - no consecutive issues
                death_plan = [bowler_more_overs, bowler_fewer_overs, bowler_more_overs]
                print(f" ✅ Optimal Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
            else:
                # Handle equal or other distributions
                death_plan = [bowler_more_overs, bowler_fewer_overs, bowler_more_overs]
                print(f" ✅ Standard Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        
        # Validate the plan if we created one
        if 'death_plan' not in locals():
            print(f" 🚨 NO VALID PLAN CREATED - returning None")
            return None
        
        # Final validation - ensure no consecutive bowling
        for i in range(len(death_plan) - 1):
            if death_plan[i] == death_plan[i + 1]:
                print(f" 🚨 FINAL VALIDATION FAILED: {death_plan[i]} in consecutive positions {i+1} and {i+2}")
                return None
        
        # Validate quota compliance
        usage_count = {}
        for bowler_name in death_plan:
            usage_count[bowler_name] = usage_count.get(bowler_name, 0) + 1
        
        for bowler_name, used_overs in usage_count.items():
            available_overs = remaining_bowlers.get(bowler_name, 0)
            if used_overs > available_overs:
                print(f" 🚨 QUOTA VALIDATION FAILED: {bowler_name} uses {used_overs} but has {available_overs}")
                return None
        
        print(f" ✅ PLAN VALIDATED: No consecutive bowling, quota compliance verified")
        return death_plan


    def _emergency_single_bowler_selection(self):
        """Emergency bowler selection for death overs"""
        print(f"🚨 EMERGENCY SINGLE BOWLER SELECTION")
        
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        
        # Find any bowler who didn't bowl previous over and has quota
        for bowler in all_bowlers:
            overs_bowled = self.bowler_history.get(bowler["name"], 0)
            is_consecutive = self.current_bowler and bowler["name"] == self.current_bowler["name"]
            
            if overs_bowled < 4 and not is_consecutive:
                print(f"🆘 Emergency selection: {bowler['name']}")
                return bowler
        
        # If no non-consecutive bowler with quota, allow quota violation but prevent consecutive
        for bowler in all_bowlers:
            is_consecutive = self.current_bowler and bowler["name"] == self.current_bowler["name"]
            if not is_consecutive:
                print(f"🆘 Emergency quota violation: {bowler['name']}")
                return bowler
        
        # Absolute last resort
        print(f"🆘 Absolute emergency: {all_bowlers[0]['name']}")
        return all_bowlers[0]

    def _pick_death_overs_bowler(self):
        """
        FIXED: PRE-CALCULATED death overs bowler selection (Overs 18-20)  
        Calculates plan ONCE at over 18, then uses stored plan for 19 & 20
        """
        print(f"\n🎯 === DEATH OVERS SELECTION - Over {self.current_over + 1} ===")
        
        # ================ CHECK IF WE NEED TO CALCULATE NEW PLAN ================
        # Only calculate plan at the START of death overs (over 18)
        if self.current_over == 17:  # Over 18 (0-indexed)
            print(f"🔥 CALCULATING NEW DEATH PLAN FOR OVERS 18-20")
            
            # Get all bowlers and their current quota
            all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
            
            # Build quota dictionary with CURRENT state
            bowler_quota = {}
            for bowler in all_bowlers:
                overs_bowled = self.bowler_history.get(bowler["name"], 0)
                overs_remaining = max(0, 4 - overs_bowled)
                if overs_remaining > 0:
                    bowler_quota[bowler["name"]] = {
                        'bowler': bowler,
                        'overs_remaining': overs_remaining,
                        'overs_bowled': overs_bowled
                    }
                    print(f"  {bowler['name']}: {overs_bowled}/4 bowled, {overs_remaining} remaining")
            
            # Calculate complete death plan for all 3 overs
            self.death_overs_plan = self._calculate_death_overs_plan_safe(bowler_quota)
            self.death_overs_bowler_objects = {}
            
            # Store bowler objects for quick lookup
            for bowler_name in self.death_overs_plan:
                self.death_overs_bowler_objects[bowler_name] = bowler_quota[bowler_name]['bowler']
            
            print(f"📋 STORED DEATH PLAN: 18→{self.death_overs_plan[0]}, 19→{self.death_overs_plan[1]}, 20→{self.death_overs_plan[2]}")
        
        # ================ USE STORED PLAN ================
        elif hasattr(self, 'death_overs_plan') and self.death_overs_plan:
            print(f"♻️  USING STORED DEATH PLAN: {self.death_overs_plan}")
        
        else:
            print(f"🚨 ERROR: No death plan available for over {self.current_over + 1}")
            # Emergency fallback - should not happen
            return self._emergency_single_bowler_selection()
        
        # ================ GET BOWLER FOR CURRENT OVER ================
        death_over_index = self.current_over - 17  # 17→0, 18→1, 19→2
        over_names = ["18th", "19th", "20th"]
        
        if death_over_index >= len(self.death_overs_plan):
            print(f"🚨 ERROR: Death over index {death_over_index} out of range")
            return self._emergency_single_bowler_selection()
        
        selected_bowler_name = self.death_overs_plan[death_over_index]
        selected_bowler = self.death_overs_bowler_objects[selected_bowler_name]
        
        print(f"🎯 DEATH PLAN SELECTION: {over_names[death_over_index]} over → {selected_bowler_name}")
        
        # ================ SAFETY CHECK ================
        if self.current_bowler and selected_bowler["name"] == self.current_bowler["name"]:
            print(f"🚨 CONSECUTIVE VIOLATION IN STORED PLAN!")
            print(f"   Previous: {self.current_bowler['name']}")
            print(f"   Selected: {selected_bowler['name']}")
            print(f"   This indicates a bug in the death plan calculation!")
            # Use emergency fallback
            return self._emergency_single_bowler_selection()
        
        # ================ UPDATE TRACKING ================
        old_count = self.bowler_history.get(selected_bowler["name"], 0)
        new_count = old_count + 1
        self.bowler_history[selected_bowler["name"]] = new_count
        print(f"📝 Updated {selected_bowler['name']} quota: {old_count}/4 → {new_count}/4")
        
        # Initialize bowler stats if needed
        if selected_bowler["name"] not in self.bowler_stats:
            self.bowler_stats[selected_bowler["name"]] = {
                "runs": 0, "fours": 0, "sixes": 0, "wickets": 0, 
                "overs": 0, "maidens": 0, "balls_bowled": 0,
                "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0
            }
        
        print(f"🏁 === DEATH OVERS SELECTION COMPLETE ===\n")
        return selected_bowler

    def _calculate_death_overs_plan(self, bowler_quota):
        """
        Calculate optimal 3-over distribution for overs 18-20
        MATHEMATICALLY GUARANTEED to work with perfect 20-over distribution
        """
        print(f"  📊 Calculating Death Overs Plan:")
        
        # Get bowlers with remaining overs
        remaining_bowlers = {}
        total_remaining = 0
        
        for name, data in bowler_quota.items():
            if data['overs_remaining'] > 0:
                remaining_bowlers[name] = data['overs_remaining']
                total_remaining += data['overs_remaining']
                print(f"    {name}: {data['overs_remaining']} overs remaining")
        
        print(f"  Total remaining quota: {total_remaining} (must be 3)")
        
        # Mathematical validation
        if total_remaining != 3:
            print(f"  🚨 MATHEMATICAL ERROR: Expected 3 remaining overs, got {total_remaining}")
            return self._emergency_death_plan(remaining_bowlers)
        
        # Get previous bowler
        previous_bowler = self.current_bowler["name"] if self.current_bowler else None
        print(f"  Previous bowler: {previous_bowler}")
        
        # CASE 1: One bowler has 2 overs, one has 1 over
        if len(remaining_bowlers) == 2:
            return self._handle_2_bowler_death_scenario(remaining_bowlers, previous_bowler)
        
        # CASE 2: Three bowlers each have 1 over
        elif len(remaining_bowlers) == 3:
            return self._handle_3_bowler_death_scenario(remaining_bowlers, previous_bowler)
        
        else:
            print(f"  🚨 UNEXPECTED SCENARIO: {len(remaining_bowlers)} bowlers with remaining overs")
            return self._emergency_death_plan(remaining_bowlers)

    def _handle_2_bowler_death_scenario(self, remaining_bowlers, previous_bowler):
        """
        Handle: A(2 overs left), B(1 over left)
        Plan: A→18, B→19, A→20 (if A didn't bowl over 17)
        """
        print(f"  📋 2-Bowler Scenario:")
        
        # Identify who has 2 overs and who has 1
        bowler_2_overs = None
        bowler_1_over = None
        
        for name, overs in remaining_bowlers.items():
            if overs == 2:
                bowler_2_overs = name
            elif overs == 1:
                bowler_1_over = name
        
        print(f"    {bowler_2_overs}: 2 overs, {bowler_1_over}: 1 over")
        print(f"    Previous bowler: {previous_bowler}")
        
        # MATHEMATICAL CONSTRAINT: If bowler with 2 overs bowled over 17, 
        # they CAN'T bowl over 18 (consecutive). But they MUST bowl 2 of the 3 remaining overs.
        # This creates a mathematical impossibility that shouldn't occur with proper distribution.
        
        if previous_bowler == bowler_2_overs:
            print(f"  🚨 CONSECUTIVE CONSTRAINT - FORCING ALTERNATIVE PLAN")
            print(f"    {bowler_2_overs} has 2 overs left but bowled over 17")
            print(f"    Using alternative: {bowler_1_over} bowls 2 overs instead")
            
            # Force non-consecutive plan: bowler with 1 over gets extra over
            return [bowler_1_over, bowler_2_overs, bowler_1_over]
        
        # Normal case: bowler with 2 overs didn't bowl over 17
        death_plan = [bowler_2_overs, bowler_1_over, bowler_2_overs]
        print(f"  ✅ Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        
        return death_plan

    def _handle_3_bowler_death_scenario(self, remaining_bowlers, previous_bowler):
        """
        Handle: A(1 over), B(1 over), C(1 over)
        Plan: Non-previous→18, Different→19, Remaining→20
        """
        print(f"  📋 3-Bowler Scenario:")
        
        bowler_names = list(remaining_bowlers.keys())
        print(f"    Available: {bowler_names}")
        print(f"    Previous bowler: {previous_bowler}")
        
        # Remove previous bowler from first position if possible
        if previous_bowler in bowler_names:
            non_previous = [b for b in bowler_names if b != previous_bowler]
            bowler_18 = non_previous[0]
            remaining_after_18 = [b for b in bowler_names if b != bowler_18]
            bowler_19 = remaining_after_18[0]
            bowler_20 = remaining_after_18[1]
        else:
            # Previous bowler not in remaining (normal case)
            bowler_18 = bowler_names[0]
            bowler_19 = bowler_names[1] 
            bowler_20 = bowler_names[2]
        
        death_plan = [bowler_18, bowler_19, bowler_20]
        print(f"  ✅ Plan: 18→{death_plan[0]}, 19→{death_plan[1]}, 20→{death_plan[2]}")
        
        return death_plan

    def _emergency_death_plan(self, remaining_bowlers):
        """Emergency plan when mathematical constraints are violated - RESPECTS CONSECUTIVE RULE"""
        print(f"  🚨 EMERGENCY DEATH PLAN: filtered remaining_bowlers={remaining_bowlers}")
        
        previous_bowler = self.current_bowler["name"] if self.current_bowler else None
        available_bowlers = list(remaining_bowlers.keys())
        
        # Remove previous bowler from first position to avoid consecutive
        if previous_bowler in available_bowlers:
            available_bowlers.remove(previous_bowler)
            # Add back at end for later overs
            available_bowlers.append(previous_bowler)
        
        # Build plan ensuring no consecutive overs
        death_plan = []
        used_bowlers = []
        
        for over_num in range(3):  # overs 18, 19, 20
            # Find bowler who didn't bowl previous over
            last_bowler = death_plan[-1] if death_plan else previous_bowler
            
            for bowler_name in available_bowlers:
                if bowler_name != last_bowler and remaining_bowlers[bowler_name] > 0:
                    death_plan.append(bowler_name)
                    remaining_bowlers[bowler_name] -= 1
                    if remaining_bowlers[bowler_name] == 0:
                        available_bowlers.remove(bowler_name)
                    break
        
        # If we couldn't fill all 3 slots, use any available bowler
        while len(death_plan) < 3:
            for bowler_name in remaining_bowlers:
                if remaining_bowlers[bowler_name] > 0:
                    death_plan.append(bowler_name)
                    remaining_bowlers[bowler_name] -= 1
                    break
        
        print(f"  ⚠️ Emergency Plan (No Consecutive): over18→{death_plan[0]}, over19→{death_plan[1]}, over20→{death_plan[2]}")
        return death_plan[:3]


    def _log_constraint_violation(self, violation_type, reason):
        """Log constraint violations for monitoring and analysis"""
        violation_msg = f"⚠️ CONSTRAINT VIOLATION - {violation_type}: {reason} (Over {self.current_over + 1})"
        print(f"  📝 {violation_msg}")
        
        # Add to commentary for visibility
        self.commentary.append(f"<strong>{violation_msg}</strong>")
        
        # Track violations for post-match analysis
        if not hasattr(self, 'constraint_violations'):
            self.constraint_violations = []
        
        self.constraint_violations.append({
            'over': self.current_over + 1,
            'type': violation_type,
            'reason': reason,
            'timestamp': self.current_over
        })


    def _get_match_phase(self):
        """Determine current match phase for context"""
        if self.current_over < 6:
            return "POWERPLAY"
        elif self.current_over < 16:
            return "MIDDLE_OVERS"
        else:
            return "DEATH_OVERS"

    def _select_optimal_bowler(self, eligible_bowlers, risk_assessment):
        """Select optimal bowler from eligible pool with smart selection logic"""
        print(f"  🎯 Optimal Selection Logic:")
        print(f"  Eligible pool: {[b['name'] for b in eligible_bowlers]}")
        
        if len(eligible_bowlers) == 1:
            print(f"  ✅ Single option: {eligible_bowlers[0]['name']}")
            return eligible_bowlers[0]
        
        # Multi-criteria selection in normal mode
        if not risk_assessment['emergency_mode']:
            # Prefer higher-rated bowlers in crucial overs
            crucial_overs = [0, 5] + list(range(16, 20))
            if self.current_over in crucial_overs:
                best_rated = max(eligible_bowlers, key=lambda b: b['bowling_rating'])
                print(f"  ⭐ Crucial over: Selected highest rated {best_rated['name']} (rating: {best_rated['bowling_rating']})")
                return best_rated
        
        # Random selection from eligible pool
        selected = random.choice(eligible_bowlers)
        print(f"  🎲 Random selection: {selected['name']}")
        return selected

    def _validate_selection(self, selected_bowler, all_bowlers, quota_analysis):
        """ABSOLUTE validation - NO exceptions allowed"""
        print(f"  ✅ ABSOLUTE SELECTION VALIDATION:")
        
        bowler_data = quota_analysis[selected_bowler["name"]]
        overs_bowled = bowler_data['overs_bowled']
        
        # ABSOLUTE CHECK 1: 4-overs policy (STRICT)
        if overs_bowled >= 4:
            print(f"  🚨 ABSOLUTE VIOLATION: {selected_bowler['name']} has {overs_bowled} overs (limit: 4)")
            return {
                'valid': False,
                'reason': f"ABSOLUTE 4-overs violation: {selected_bowler['name']} has {overs_bowled}/4 overs",
                'critical': True
            }
        
        # ABSOLUTE CHECK 2: Consecutive policy (STRICT)
        if self.current_bowler and selected_bowler["name"] == self.current_bowler["name"]:
            print(f"  🚨 ABSOLUTE VIOLATION: {selected_bowler['name']} bowled previous over")
            return {
                'valid': False,
                'reason': f"ABSOLUTE consecutive violation: {selected_bowler['name']} bowled previous over",
                'critical': True
            }
        
        print(f"  ✅ ABSOLUTE VALIDATION PASSED: {selected_bowler['name']} ({overs_bowled}/4 overs)")
        return {'valid': True, 'reason': 'All constraints met', 'critical': False}

    def _force_valid_selection(self, all_bowlers, quota_analysis):
        """Force valid selection with ABSOLUTE constraints - NO compromises"""
        print(f"  🔧 ABSOLUTE FORCE VALID SELECTION:")
        
        # ABSOLUTE RULE: Find ANY bowler with < 4 overs who didn't bowl previous over
        for bowler in all_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            is_consecutive = self.current_bowler and bowler["name"] == self.current_bowler["name"]
            overs_bowled = bowler_data['overs_bowled']
            
            if overs_bowled < 4 and not is_consecutive:
                print(f"  ✅ ABSOLUTE VALID: {bowler['name']} ({overs_bowled}/4 overs, not consecutive)")
                return bowler
        
        # If we reach here, there's a systematic error in constraint management
        print(f"  💥 ABSOLUTE CONSTRAINT FAILURE - SYSTEM ERROR")
        print(f"  📋 Match state is invalid - this should never happen")
        
        # Critical system failure - log and halt
        self._log_constraint_violation("ABSOLUTE_SYSTEM_FAILURE", "No valid bowlers exist")
        
        # Return first bowler (system is broken at this point)
        print(f"  ⚠️  SYSTEM BROKEN: Returning emergency bowler")
        return all_bowlers[0]

    def _update_bowler_tracking(self, selected_bowler):
        """Update all tracking systems"""
        print(f"  📝 TRACKING UPDATE:")
        
        # Update over history
        self._log_bowler_for_over(selected_bowler)
        
        # Update quota tracking
        old_count = self.bowler_history.get(selected_bowler["name"], 0)
        new_count = old_count + 1
        self.bowler_history[selected_bowler["name"]] = new_count
        
        print(f"    {selected_bowler['name']} quota: {old_count}/4 → {new_count}/4")
        
        # Initialize/update bowler stats
        if selected_bowler["name"] not in self.bowler_stats:
            self.bowler_stats[selected_bowler["name"]] = {
                "runs": 0, "fours": 0, "sixes": 0, "wickets": 0, 
                "overs": 0, "maidens": 0, "balls_bowled": 0,
                "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0
            }
            print(f"    Initialized stats for {selected_bowler['name']}")
        
        # Restore any temporary rating modifications
        self._restore_bowler_ratings()

    def _project_future_constraints(self, selected_bowler, all_bowlers):
        """Project future constraint implications"""
        remaining_overs = 20 - (self.current_over + 1)
        
        # Calculate post-selection availability
        available_next_over = 0
        for bowler in all_bowlers:
            future_overs = self.bowler_history.get(bowler["name"], 0)
            if bowler["name"] == selected_bowler["name"]:
                future_overs += 1
            
            if future_overs < 4 and bowler["name"] != selected_bowler["name"]:
                available_next_over += 1
        
        # Assess next over risk
        if available_next_over == 0:
            next_over_risk = "CRITICAL"
        elif available_next_over <= 1:
            next_over_risk = "HIGH"
        elif available_next_over <= 2:
            next_over_risk = "MEDIUM"
        else:
            next_over_risk = "LOW"
        
        return {
            'available_count': available_next_over,
            'next_over_risk': next_over_risk,
            'remaining_overs': remaining_overs
        }

    def _apply_minimal_strategy_override(self, constraint_eligible, risk_assessment):
        """Apply minimal strategy in emergency mode - only essential filters"""
        print(f"  🚨 MINIMAL STRATEGY (Emergency Mode):")
        print(f"  Input bowlers: {[b['name'] for b in constraint_eligible]}")
        
        # In emergency mode, only apply critical strategy elements
        
        # 1. Death overs specialization (if in death overs and specialists available)
        if self.current_over >= 16:
            death_specialists = [b for b in constraint_eligible if self._is_death_specialist(b)]
            if death_specialists:
                print(f"  💀 Death overs: Using specialists {[b['name'] for b in death_specialists]}")
                return death_specialists
            else:
                print(f"  💀 Death overs: No specialists available - using all eligible")
        
        # 2. Minimal pattern preference (only if multiple bowlers available)
        if len(constraint_eligible) > 1:
            preferred_type = self._get_preferred_bowler_type(self.current_over)
            pattern_bowlers = [b for b in constraint_eligible if self._categorize_bowler(b) == preferred_type]
            
            if pattern_bowlers:
                print(f"  🎯 Minimal pattern filter: {preferred_type} → {[b['name'] for b in pattern_bowlers]}")
                return pattern_bowlers
            else:
                print(f"  🎯 No {preferred_type} bowlers available")
        
        print(f"  ✅ Emergency mode: Using all constraint-eligible bowlers")
        return constraint_eligible

    
    def _reset_innings_state(self):
        """Reset all innings-specific state for clean 2nd innings"""
        self.bowling_pattern = self._detect_bowling_pattern()
        self.over_bowler_log = {}
        # Restore any modified bowler ratings
        self._restore_bowler_ratings()

    def _restore_bowler_ratings(self):
        """Restore original bowling ratings after matchup bonuses"""
        for player in self.bowling_team:
            if hasattr(player, 'original_bowling_rating'):
                player['bowling_rating'] = player['original_bowling_rating']
                del player['original_bowling_rating']


    def _log_bowler_for_over(self, bowler):
        """Track which bowler bowled which over for fatigue management"""
        if not hasattr(self, 'over_bowler_log'):
            self.over_bowler_log = {}
        self.over_bowler_log[self.current_over] = bowler["name"]

    def _detect_bowling_pattern(self):
        """Automatically detect the best bowling pattern based on team composition and pitch"""
        bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        
        # Categorize bowlers
        fast_bowlers = [b for b in bowlers if b["bowling_type"] in ["Fast", "Fast-medium", "Medium-fast"]]
        spin_bowlers = [b for b in bowlers if b["bowling_type"] in ["Off spin", "Leg spin", "Finger spin", "Wrist spin"]]
        
        # Pattern selection logic
        if len(fast_bowlers) >= 4:
            return "fast_heavy"  # 4+ fast bowlers
        elif len(spin_bowlers) >= 3 and self.pitch in ["Dry"]:
            return "spin_heavy"  # 3+ spinners on spin-friendly pitch
        else:
            return "traditional"  # Balanced approach


    def _get_preferred_bowler_type(self, over_number):
        """Get the preferred bowler type for a specific over based on pattern"""
        pattern = self.bowling_pattern
        
        if pattern == "traditional":
            if over_number < 6:  # Powerplay (overs 1-6)
                return "fast"
            elif over_number < 16:  # Middle overs (7-16)
                return "spin"
            else:  # Death overs (17-20)
                return "fast"
        
        elif pattern == "fast_heavy":
            if over_number < 6:  # Powerplay
                return "fast"
            elif over_number < 14:  # Middle overs with some spin
                return "mixed"  # Allow both, but prefer fast
            else:  # Death overs
                return "fast"
        
        elif pattern == "spin_heavy":
            if over_number < 3:  # Early overs
                return "fast"
            elif over_number < 17:  # Long spin phase
                return "spin"
            else:  # Death overs
                return "fast"
        
        return "mixed"  # Fallback

    def _categorize_bowler(self, bowler):
        """Categorize a bowler as fast, spin, or medium"""
        bowling_type = bowler["bowling_type"]
        if bowling_type in ["Fast", "Fast-medium", "Medium-fast"]:
            return "fast"
        elif bowling_type in ["Off spin", "Leg spin", "Finger spin", "Wrist spin"]:
            return "spin"
        else:
            return "medium"  # Medium pacers

    def _select_fielder_for_wicket(self, wicket_type):
        """Select a fielder based on fielding ratings and wicket type"""
        
        # For wicket keeper dismissals (common in caught behind, stumpings)
        wicket_keeper = next((p for p in self.bowling_team if p["role"] == "Wicketkeeper"), None)
        
        # Weight-based selection based on fielding ratings
        fielders = []
        weights = []
        
        for player in self.bowling_team:
            # Skip the current bowler for caught dismissals (fielder can't be bowler)
            if wicket_type == "Caught" and player["name"] == self.current_bowler["name"]:
                continue
                
            fielders.append(player)
            
            # Weight calculation based on fielding rating and position
            base_weight = player["fielding_rating"]
            
            # Wicket keeper gets higher weight for catches
            if player["role"] == "Wicketkeeper" and wicket_type == "Caught":
                base_weight *= 1.5
            
            # All-rounders and good fielders get slight boost
            if player["role"] in ["All-rounder"] and player["fielding_rating"] > 70:
                base_weight *= 1.2
                
            weights.append(base_weight)
        
        # Random selection based on weights
        if fielders and weights:
            selected_fielder = random.choices(fielders, weights=weights)[0]
            return selected_fielder["name"]
        
        # Fallback to any fielder
        return random.choice(self.bowling_team)["name"]
    
    def _generate_wicket_commentary(self, outcome, fielder_name=None):
        """Generate enhanced commentary for wickets including fielder details"""
        wicket_type = outcome["wicket_type"]
        bowler_name = self.current_bowler["name"]
        batsman_name = self.current_striker["name"]
        
        if wicket_type == "Caught":
            if fielder_name:
                return f"Wicket! {batsman_name} caught by {fielder_name} off {bowler_name}! Excellent catch!"
            else:
                return f"Wicket! {batsman_name} caught! {outcome['description']}"
                
        elif wicket_type == "Bowled":
            return f"Wicket! {batsman_name} bowled by {bowler_name}! {outcome['description']}"
            
        elif wicket_type == "LBW":
            return f"Wicket! {batsman_name} LBW to {bowler_name}! {outcome['description']}"
            
        elif wicket_type == "Run Out":
            if fielder_name:
                return f"Wicket! {batsman_name} run out by {fielder_name}! Brilliant fielding!"
            else:
                return f"Wicket! {batsman_name} run out! {outcome['description']}"
        
        # Fallback
        return f"Wicket! {outcome['description']}"
    

    def _apply_pattern_strategy(self, eligible_bowlers, preferred_type):
        """Apply pattern strategy with RATING-WEIGHTED selection"""
        print(f"  🎯 Pattern Strategy Analysis:")
        print(f"  Input bowlers: {[b['name'] for b in eligible_bowlers]}")
        print(f"  Preferred type: {preferred_type}")
        
        if preferred_type == "fast":
            pattern_bowlers = [b for b in eligible_bowlers if self._categorize_bowler(b) == "fast"]
            print(f"  Fast bowlers found: {[b['name'] for b in pattern_bowlers]}")
        elif preferred_type == "spin":
            pattern_bowlers = [b for b in eligible_bowlers if self._categorize_bowler(b) == "spin"]
            print(f"  Spin bowlers found: {[b['name'] for b in pattern_bowlers]}")
        else:
            pattern_bowlers = eligible_bowlers
            print(f"  Mixed/All types allowed")
        
        # Enhanced: Role and rating-weighted selection within type
        if pattern_bowlers:
            pattern_bowlers = self._sort_by_rating_and_role(pattern_bowlers)
            print(f"  ✅ Pattern filter successful with role and rating priority")
            print(f"  Role-Rating order: {[(b['name'], b['role'], b['bowling_rating']) for b in pattern_bowlers]}")
            return pattern_bowlers
        else:
            print(f"  ⚠️  No bowlers match pattern - using all eligible with role priority")
            # Apply role-based sorting even when no pattern match
            sorted_eligible = self._sort_by_rating_and_role(eligible_bowlers)
            return sorted_eligible

    def _sort_by_rating_and_role(self, bowlers):
        """
        Enhanced: Sort bowlers with strong role-based priority
        1. Pure Bowlers (any rating) > All-rounders (any rating) > Others
        2. Within each role group, sort by rating (highest first)
        3. Alphabetical for ties
        """
        return sorted(bowlers, key=lambda b: (
            # Primary sort: Role priority (lower number = higher priority)
            0 if b['role'] == 'Bowler' else (1 if b['role'] == 'All-rounder' else 2),
            # Secondary sort: Higher rating first (negative for descending)
            -b['bowling_rating'],
            # Tertiary sort: Alphabetical for ties
            b['name']
        ))
    
    def _apply_all_rounder_bowling_limits(self, eligible_bowlers, quota_analysis):
        """
        NEW: Limit All-rounder bowling to 1-2 overs when 5+ bowlers available
        Only applies when there are sufficient pure bowlers available
        """
        print(f"  🎯 All-rounder Bowling Limits Check:")
        
        # Count total bowlers marked will_bowl
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        total_bowlers = len(all_bowlers)
        
        print(f"    Total bowlers available: {total_bowlers}")
        
        # Only apply limits when we have 6+ bowlers (more than minimum 5)
        if total_bowlers < 6:
            print(f"    ✅ Only {total_bowlers} bowlers - no All-rounder limits applied")
            return eligible_bowlers
        
        # Separate pure bowlers and all-rounders
        pure_bowlers = [b for b in eligible_bowlers if b['role'] == 'Bowler']
        all_rounders = [b for b in eligible_bowlers if b['role'] == 'All-rounder']
        other_bowlers = [b for b in eligible_bowlers if b['role'] not in ['Bowler', 'All-rounder']]
        
        print(f"    Pure bowlers available: {[b['name'] for b in pure_bowlers]}")
        print(f"    All-rounders available: {[b['name'] for b in all_rounders]}")
        
        # Apply 2-over limit to all-rounders
        limited_all_rounders = []
        for ar in all_rounders:
            overs_bowled = quota_analysis[ar['name']]['overs_bowled']
            if overs_bowled < 2:  # Allow up to 2 overs
                limited_all_rounders.append(ar)
                print(f"    ✅ {ar['name']}: {overs_bowled}/2 overs - Available")
            else:
                print(f"    🚫 {ar['name']}: {overs_bowled}/2 overs - Limit reached")
        
        # Combine filtered bowlers with pure bowlers prioritized
        filtered_bowlers = pure_bowlers + limited_all_rounders + other_bowlers
        
        # If we have no eligible bowlers after filtering, allow all-rounders to exceed limit
        if not filtered_bowlers:
            print(f"    ⚠️  No eligible bowlers after limits - allowing All-rounder override")
            return eligible_bowlers
        
        print(f"    Final filtered pool: {[b['name'] for b in filtered_bowlers]}")
        return filtered_bowlers

    def _apply_secondary_filters(self, eligible_bowlers):
        """Apply form, matchup, and other filters with debugging"""
        print(f"  🔧 Secondary Filters:")
        print(f"  Input: {[b['name'] for b in eligible_bowlers]}")
        
        # Form consideration
        form_filtered = self._apply_form_consideration_debug(eligible_bowlers)
        print(f"  After form filter: {[b['name'] for b in form_filtered]}")
        
        # Matchup strategy  
        matchup_filtered = self._apply_matchup_strategy_debug(form_filtered)
        print(f"  After matchup filter: {[b['name'] for b in matchup_filtered]}")
        
        # Safety check
        if matchup_filtered:
            return matchup_filtered
        else:
            print(f"  ⚠️  No bowlers after secondary filters - reverting to input")
            return eligible_bowlers

    def _apply_form_consideration_debug(self, eligible_bowlers):
        """Form consideration with debugging"""
        crucial_overs = [0, 5] + list(range(16, 20))  # Overs 1, 6, 17-20
        is_crucial = self.current_over in crucial_overs
        
        print(f"    📈 Form Filter - Crucial over: {is_crucial}")
        
        if is_crucial:
            # Sort by rating and take top 50%
            sorted_bowlers = sorted(eligible_bowlers, key=lambda b: b["bowling_rating"], reverse=True)
            crucial_count = max(1, len(sorted_bowlers) // 2)
            form_filtered = sorted_bowlers[:crucial_count]
            
            print(f"    Ratings: {[(b['name'], b['bowling_rating']) for b in sorted_bowlers]}")
            print(f"    Top {crucial_count} selected: {[b['name'] for b in form_filtered]}")
            return form_filtered
        else:
            print(f"    Non-crucial over - no form filtering")
            return eligible_bowlers

    def _apply_matchup_strategy_debug(self, eligible_bowlers):
        """Matchup strategy with debugging"""
        striker_hand = self.current_striker.get("batting_hand", "Right")
        print(f"    🥊 Matchup - Striker batting hand: {striker_hand}")
        
        if striker_hand == "Right":
            left_arm_bowlers = [b for b in eligible_bowlers if b.get("bowling_hand", "Right") == "Left"]
            print(f"    Left-arm bowlers available: {[b['name'] for b in left_arm_bowlers]}")
            
            if left_arm_bowlers:
                print(f"    ✅ Favorable matchup found - boosting left-arm bowlers")
                # Apply temporary rating bonus
                for bowler in left_arm_bowlers:
                    if not hasattr(bowler, 'original_bowling_rating'):
                        bowler['original_bowling_rating'] = bowler['bowling_rating']
                        boosted_rating = min(100, int(bowler['bowling_rating'] * 1.1))
                        bowler['bowling_rating'] = boosted_rating
                        print(f"    {bowler['name']} rating boosted: {bowler['original_bowling_rating']} → {boosted_rating}")
                
                return left_arm_bowlers
        
        print(f"    No favorable matchups - using all eligible")
        return eligible_bowlers

    def _is_death_specialist(self, bowler):
        """Check if bowler is a death specialist (optimized version)"""
        return (self._categorize_bowler(bowler) == "fast" and 
                bowler["bowling_rating"] >= 75 and
                bowler["bowling_type"] in ["Fast", "Fast-medium", "Medium-fast"])

    def _calculate_death_overs_risk(self, death_specialists):
        """Calculate risk level for death overs coverage with detailed logging"""
        print(f"    💡 Death overs risk calculation:")
        
        total_remaining_overs = 0
        for specialist in death_specialists:
            bowled = self.bowler_history.get(specialist["name"], 0)
            remaining = 4 - bowled
            total_remaining_overs += remaining
            print(f"    {specialist['name']}: bowled={bowled}, remaining={remaining}")
        
        death_overs_needed = 4  # overs 17-20
        print(f"    Total specialist overs remaining: {total_remaining_overs}")
        print(f"    Death overs needed: {death_overs_needed}")
        
        if total_remaining_overs < death_overs_needed:
            risk = "HIGH_RISK"
            print(f"    ⚠️  SHORTAGE: {death_overs_needed - total_remaining_overs} overs short")
        elif total_remaining_overs == death_overs_needed:
            risk = "MEDIUM_RISK"
            print(f"    ⚖️  EXACT: Just enough specialist overs")
        else:
            risk = "LOW_RISK"
            surplus = total_remaining_overs - death_overs_needed
            print(f"    ✅ SURPLUS: {surplus} extra specialist overs available")
        
        return risk

    def _count_specialists_used_in_middle(self):
        """
        Count death specialists used in middle overs (7-16)
        TODO: Implement detailed tracking - for now return conservative estimate
        """
        # Conservative implementation - can be enhanced with detailed over-by-over tracking
        middle_overs_completed = max(0, self.current_over - 6)
        specialists_in_team = len([b for b in self.bowling_team if self._is_death_specialist(b)])
        
        # Rough estimate: assume some specialist usage if many middle overs completed
        if middle_overs_completed > 6 and specialists_in_team >= 2:
            return 1  # Conservative estimate
        return 0


    def _analyze_quota_status(self, all_bowlers):
        """Comprehensive quota analysis with detailed tracking"""
        quota_analysis = {}
        
        print(f"  🔍 Detailed Quota Analysis:")
        
        for bowler in all_bowlers:
            overs_bowled = self.bowler_history.get(bowler["name"], 0)
            overs_remaining = max(0, 4 - overs_bowled)  # Never negative
            percentage = (overs_bowled / 4) * 100
            
            # STRICT STATUS DETERMINATION
            if overs_bowled >= 4:
                status = "EXHAUSTED"
                exhausted = True
            elif overs_bowled >= 3:
                status = "CRITICAL (75%+)"
                exhausted = False
            elif overs_bowled >= 2:
                status = "WARNING (50%+)"
                exhausted = False
            else:
                status = "SAFE"
                exhausted = False
            
            quota_analysis[bowler["name"]] = {
                'bowler': bowler,
                'overs_bowled': overs_bowled,
                'overs_remaining': overs_remaining,
                'percentage': percentage,
                'status': status,
                'exhausted': exhausted  # STRICT: True only if >= 4 overs
            }
            
            print(f"    {bowler['name']}: {overs_bowled}/4 ({percentage:.1f}%) - {status}")
        
        return quota_analysis

    def _assess_constraint_risk(self, all_bowlers, quota_analysis):
        """Assess risk level for constraint violations"""
        print(f"  🔍 Constraint Risk Assessment:")
        
        # Calculate key metrics
        total_overs_remaining = 20 - (self.current_over + 1)
        exhausted_bowlers = sum(1 for data in quota_analysis.values() if data['exhausted'])
        critical_bowlers = sum(1 for data in quota_analysis.values() if data['overs_bowled'] >= 3)
        available_bowlers = len(all_bowlers) - exhausted_bowlers
        
        print(f"    Total overs remaining: {total_overs_remaining}")
        print(f"    Exhausted bowlers: {exhausted_bowlers}/5")
        print(f"    Critical bowlers (3+ overs): {critical_bowlers}/5")
        print(f"    Available bowlers: {available_bowlers}/5")
        
        # Calculate remaining overs pool
        total_overs_pool = sum(data['overs_remaining'] for data in quota_analysis.values())
        print(f"    Total overs pool remaining: {total_overs_pool}")
        
        # Risk factor analysis
        risk_factors = []
        
        if total_overs_pool < total_overs_remaining:
            risk_factors.append("MATHEMATICAL_IMPOSSIBILITY")
        elif total_overs_pool == total_overs_remaining:
            risk_factors.append("PERFECT_FIT_REQUIRED")
        elif available_bowlers <= 2:
            risk_factors.append("LIMITED_BOWLER_POOL")
        elif critical_bowlers >= 3:
            risk_factors.append("HIGH_QUOTA_PRESSURE")
        
        if self.current_over >= 16 and available_bowlers <= 3:
            risk_factors.append("DEATH_OVERS_CONSTRAINT")
        
        # Determine risk level
        if "MATHEMATICAL_IMPOSSIBILITY" in risk_factors:
            risk_level = "CRITICAL"
            emergency_mode = True
        elif "PERFECT_FIT_REQUIRED" in risk_factors or len(risk_factors) >= 2:
            risk_level = "HIGH"
            emergency_mode = True
        elif len(risk_factors) >= 1:
            risk_level = "MEDIUM"
            emergency_mode = False
        else:
            risk_level = "LOW"
            emergency_mode = False
        
        print(f"    Risk Level: {risk_level}")
        print(f"    Emergency Mode: {emergency_mode}")
        
        return {
            'risk_level': risk_level,
            'risk_factors': risk_factors,
            'emergency_mode': emergency_mode,
            'available_bowlers': available_bowlers,
            'total_overs_pool': total_overs_pool,
            'total_overs_remaining': total_overs_remaining
        }
    
    def _prevent_over_utilization(self, eligible_bowlers, quota_analysis):
        """Prevent any bowler from bowling more than 2 overs in first 10 overs"""
        print(f"  🎯 Over-Utilization Prevention (Over {self.current_over + 1}):")
        
        if self.current_over >= 10:
            print(f"    After over 10 - no over-utilization limits")
            return eligible_bowlers
        
        balanced_bowlers = []
        for bowler in eligible_bowlers:
            overs_bowled = quota_analysis[bowler['name']]['overs_bowled']
            if overs_bowled < 2:
                balanced_bowlers.append(bowler)
                print(f"    ✅ {bowler['name']}: {overs_bowled}/2 overs - Available")
            else:
                print(f"    🚫 {bowler['name']}: {overs_bowled}/2 overs - Over-utilized")
        
        if not balanced_bowlers:
            print(f"    ⚠️  No fresh bowlers - allowing 2-over bowlers")
            balanced_bowlers = [b for b in eligible_bowlers if quota_analysis[b['name']]['overs_bowled'] <= 2]
        
        if not balanced_bowlers:
            balanced_bowlers = eligible_bowlers
        
        return balanced_bowlers

    def _apply_star_preservation_strategy(self, eligible_bowlers, bowler_tiers, quota_analysis):
        """Save star bowlers for crucial phases"""
        print(f"  ⭐ Star Preservation Strategy (Over {self.current_over + 1}):")
        
        if self.current_over < 6:  # Powerplay
            return eligible_bowlers
        elif self.current_over < 16:  # Middle overs - prefer regulars
            regulars = [b for b in eligible_bowlers if b in bowler_tiers['regular']]
            support = [b for b in eligible_bowlers if b in bowler_tiers['support']]
            non_stars = regulars + support
            
            if non_stars:
                print(f"    ✅ Middle overs: Using regular bowlers to save stars")
                return non_stars
            else:
                print(f"    ⚠️  No regular bowlers - using stars")
                return eligible_bowlers
        else:  # Death overs
            return eligible_bowlers

    def _apply_variety_enforcement(self, eligible_bowlers, quota_analysis):
        """Prevent same bowler from bowling too frequently"""
        print(f"  🔄 Variety Enforcement:")
        
        if len(eligible_bowlers) <= 2:
            return eligible_bowlers
        
        # Check last 3 overs
        recent_overs = max(0, self.current_over - 2)
        recent_bowlers = []
        for over in range(recent_overs, self.current_over):
            if over in self.over_bowler_log:
                recent_bowlers.append(self.over_bowler_log[over])
        
        variety_preferred = []
        for bowler in eligible_bowlers:
            recent_count = recent_bowlers.count(bowler['name'])
            if recent_count < 2:  # Hasn't bowled 2 of last 3 overs
                variety_preferred.append(bowler)
        
        return variety_preferred if variety_preferred else eligible_bowlers

    def _get_bowling_phase(self):
        """Get current bowling phase"""
        if self.current_over < 6:
            return "POWERPLAY"
        elif self.current_over < 16:
            return "MIDDLE_OVERS"
        else:
            return "DEATH_OVERS"

    def _apply_strict_quota_policy(self, all_bowlers, quota_analysis):
        """Strictly enforce 4-overs-per-bowler policy"""
        print(f"  🔒 4-Overs Policy Enforcement:")
        
        quota_eligible = []
        
        for bowler in all_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            
            if not bowler_data['exhausted']:
                quota_eligible.append(bowler)
                print(f"    ✅ {bowler['name']}: {bowler_data['overs_remaining']} overs remaining")
            else:
                print(f"    ❌ {bowler['name']}: EXHAUSTED (4/4 overs)")
        
        print(f"  Quota-eligible bowlers: {len(quota_eligible)}/{len(all_bowlers)}")
        return quota_eligible

    def _absolute_consecutive_validation(self, selected_bowler):
        """Final validation to ensure no consecutive bowling - PRODUCTION SAFETY NET"""
        if not self.current_bowler:
            return True  # No previous bowler, so no consecutive issue
        
        if selected_bowler["name"] == self.current_bowler["name"]:
            print(f" 🚨 PRODUCTION SAFETY VIOLATION: {selected_bowler['name']} would bowl consecutive!")
            print(f" 🚨 This should NEVER reach this point - constraint system failed!")
            
            # Log critical violation
            self._log_constraint_violation("PRODUCTION_SAFETY_VIOLATION", 
                                        f"Consecutive bowling detected at final validation: {selected_bowler['name']}")
            
            # ABSOLUTELY DO NOT ALLOW - Force system halt
            raise Exception(f"PRODUCTION SAFETY: Consecutive bowling prevented for {selected_bowler['name']}")
        
        return True


    def _apply_strict_consecutive_policy(self, quota_eligible, risk_assessment):
        """Strictly enforce no-consecutive-overs policy - ABSOLUTE NO EXCEPTIONS"""
        print(f" 🔒 ABSOLUTE No-Consecutive Policy Enforcement:")
        
        if not self.current_bowler:
            print(f" ✅ No previous bowler - all quota-eligible bowlers available")
            return quota_eligible
        
        previous_name = self.current_bowler["name"]
        print(f" Previous bowler (FORBIDDEN): {previous_name}")
        
        consecutive_eligible = []
        
        for bowler in quota_eligible:
            if bowler["name"] != previous_name:
                consecutive_eligible.append(bowler)
                print(f" ✅ {bowler['name']}: Available (not consecutive)")
            else:
                print(f" 🚫 {bowler['name']}: ABSOLUTELY BLOCKED (would be consecutive)")
        
        print(f" Non-consecutive eligible: {len(consecutive_eligible)}/{len(quota_eligible)}")
        
        # PRODUCTION FIX: Never return empty list if there are bowlers available
        # If quota_eligible had bowlers but consecutive filtering removes all,
        # this indicates a constraint management error that should be caught early
        
        if not consecutive_eligible and quota_eligible:
            print(f" 🚨 CRITICAL ERROR: All quota-eligible bowlers would be consecutive!")
            print(f" 🚨 This indicates poor constraint planning - should never happen")
            self._log_constraint_violation("CONSECUTIVE_CONSTRAINT_VIOLATION", 
                                        f"All quota-eligible bowlers would bowl consecutive to {previous_name}")
            
            # Force emergency resolution through proper channels
            # Don't return empty list - let emergency handler deal with it properly
            
        return consecutive_eligible


    def _handle_constraint_emergency(self, all_bowlers, quota_analysis, risk_assessment):
        """Handle emergency with ABSOLUTE consecutive constraint enforcement - NO EXCEPTIONS EVER"""
        print(f" 🚨 CONSTRAINT EMERGENCY HANDLING:")
        print(f" Risk Level: {risk_assessment['risk_level']}")
        
        # ABSOLUTE RULE 1: Never allow consecutive overs (HIGHEST PRIORITY)
        # ABSOLUTE RULE 2: Prefer bowlers with < 4 overs, but allow 4+ overs if needed to prevent consecutive
        
        previous_bowler_name = self.current_bowler["name"] if self.current_bowler else None
        print(f" Previous bowler (MUST BE AVOIDED): {previous_bowler_name}")
        
        # Step 1: Find ALL non-consecutive bowlers first (regardless of quota)
        non_consecutive_bowlers = []
        for bowler in all_bowlers:
            if bowler["name"] != previous_bowler_name:
                non_consecutive_bowlers.append(bowler)
        
        print(f" All non-consecutive bowlers: {[b['name'] for b in non_consecutive_bowlers]}")
        
        if not non_consecutive_bowlers:
            # IMPOSSIBLE SCENARIO: Only one bowler in team (should never happen in T20)
            print(f" 💥 CRITICAL SYSTEM ERROR: Only one bowler available - match cannot continue")
            self._log_constraint_violation("IMPOSSIBLE_SCENARIO", "Only one bowler in team")
            # Force match abandonment rather than allow consecutive
            raise Exception("Match cannot continue: Insufficient bowlers to prevent consecutive overs")
        
        # Step 2: Among non-consecutive bowlers, prefer those with < 4 overs
        preferred_bowlers = []
        fallback_bowlers = []
        
        for bowler in non_consecutive_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            if bowler_data['overs_bowled'] < 4:
                preferred_bowlers.append(bowler)
            else:
                fallback_bowlers.append(bowler)
        
        print(f" Preferred (< 4 overs): {[b['name'] for b in preferred_bowlers]}")
        print(f" Fallback (4+ overs): {[b['name'] for b in fallback_bowlers]}")
        
        # Step 3: Return preferred bowlers if available, otherwise use fallback
        if preferred_bowlers:
            print(f" ✅ EMERGENCY RESOLVED: Using preferred non-consecutive bowlers")
            return preferred_bowlers
        else:
            # Allow quota violation but NEVER consecutive bowling
            print(f" ⚠️ QUOTA VIOLATION ALLOWED: Using 4+ over bowlers to prevent consecutive")
            print(f" 🔒 CONSECUTIVE CONSTRAINT MAINTAINED: Never allowing consecutive overs")
            self._log_constraint_violation("QUOTA_VIOLATION_FOR_CONSECUTIVE_PREVENTION", 
                                        f"Using {fallback_bowlers[0]['name']} with 4+ overs to prevent consecutive")
            return fallback_bowlers


    def _classify_bowlers_by_tier(self, all_bowlers):
        """
        NEW: Classify bowlers into performance tiers for strategic selection
        """
        print(f"\n🏷️  === BOWLER CLASSIFICATION ===")
        
        tiers = {
            'star': [],      # 85+ rating
            'regular': [],   # 70-84 rating  
            'support': [],   # 50-69 rating
            'filler': []     # <50 rating
        }
        
        for bowler in all_bowlers:
            rating = bowler['bowling_rating']
            role = bowler['role']
            
            if rating >= 85:
                tiers['star'].append(bowler)
                print(f"  ⭐ STAR: {bowler['name']} ({rating}, {role})")
            elif rating >= 70:
                tiers['regular'].append(bowler)
                print(f"  🔷 REGULAR: {bowler['name']} ({rating}, {role})")
            elif rating >= 50:
                tiers['support'].append(bowler)
                print(f"  🔹 SUPPORT: {bowler['name']} ({rating}, {role})")
            else:
                tiers['filler'].append(bowler)
                print(f"  ⚪ FILLER: {bowler['name']} ({rating}, {role})")
        
        return tiers

    def _try_early_overs_fast_selection(self, bowler_tiers, quota_analysis):
        """
        NEW: Force top-rated fast bowlers in early overs (1-4)
        """
        print(f"\n🚀 === EARLY OVERS FAST SELECTION ===")
        
        # Get ALL fast bowlers from all tiers, not just stars
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        fast_bowlers = [
            b for b in all_bowlers 
            if self._is_fast_bowler(b) and self._is_powerplay_eligible(b, quota_analysis)
        ]
        
        if not fast_bowlers:
            print(f"  ❌ No fast bowlers available for early overs")
            return None
        
        # Sort by rating first (highest to lowest), then by role (pure bowlers > all-rounders)
        fast_bowlers.sort(key=lambda b: (
            b['bowling_rating'], 
            0 if b['role'] == 'Bowler' else 1
        ), reverse=True)
        
        selected = fast_bowlers[0]
        overs_bowled = quota_analysis[selected['name']]['overs_bowled']
        
        print(f"  ✅ EARLY OVERS FAST: {selected['name']} (Rating: {selected['bowling_rating']}, Overs: {overs_bowled}/4)")
        return selected

    def _prevent_star_neglect(self, bowler_tiers, quota_analysis):
        """
        NEW: Prevent star bowlers from sitting idle too long
        """
        print(f"\n⚡ === STAR NEGLECT PREVENTION ===")
        
        # Find star bowlers who haven't bowled enough
        neglected_stars = []
        for star in bowler_tiers['star']:
            overs_bowled = quota_analysis[star['name']]['overs_bowled'] 
            
            # Star bowler neglect criteria
            if self.current_over >= 10 and overs_bowled == 0:
                neglected_stars.append((star, 'zero_overs'))
                print(f"  🚨 CRITICAL NEGLECT: {star['name']} (0 overs by over {self.current_over + 1})")
            elif self.current_over >= 14 and overs_bowled <= 1:
                neglected_stars.append((star, 'under_bowled'))
                print(f"  ⚠️  MODERATE NEGLECT: {star['name']} ({overs_bowled} overs by over {self.current_over + 1})")
        
        if not neglected_stars:
            print(f"  ✅ No star bowler neglect detected")
            return None
        
        # Prioritize critical neglect, then by rating
        neglected_stars.sort(key=lambda x: (
            0 if x[1] == 'zero_overs' else 1,  # Critical first
            -x[0]['bowling_rating']             # Higher rating first
        ))
        
        # Check if top neglected star is eligible
        candidate = neglected_stars[0][0]
        if self._is_constraint_eligible(candidate, quota_analysis):
            print(f"  🎯 NEGLECT OVERRIDE: Selecting {candidate['name']}")
            return candidate
        
        print(f"  ❌ Neglected star {candidate['name']} not constraint-eligible")
        return None

    def _is_fast_bowler(self, bowler):
        """Check if bowler is fast/fast-medium type"""
        return bowler['bowling_type'] in ['Fast', 'Fast-medium', 'Medium-fast']

    def _is_powerplay_eligible(self, bowler, quota_analysis):
        """Check if bowler is eligible for powerplay selection"""
        bowler_data = quota_analysis[bowler['name']]
        
        # Must have overs remaining
        if bowler_data['overs_bowled'] >= 4:
            return False
        
        # Must not have bowled previous over (consecutive check)
        if self.current_bowler and bowler['name'] == self.current_bowler['name']:
            return False
            
        return True

    def _is_constraint_eligible(self, bowler, quota_analysis):
        """Check if bowler meets basic constraint requirements"""
        return self._is_powerplay_eligible(bowler, quota_analysis)  # Same logic for now

    def _try_low_rated_bowler_usage(self, bowler_tiers, quota_analysis):
        """
        NEW: Strategic usage of low-rated bowlers (support/filler) for 1-3 overs when beneficial
        """
        print(f"\n🎯 === LOW-RATED BOWLER STRATEGIC USAGE ===")
        
        # Combine support and filler bowlers
        low_rated_bowlers = bowler_tiers['support'] + bowler_tiers['filler']
        
        if not low_rated_bowlers:
            print(f"  ❌ No low-rated bowlers available")
            return None
        
        # Filter for eligible bowlers (constraint-safe)
        eligible_low_rated = []
        for bowler in low_rated_bowlers:
            if self._is_constraint_eligible(bowler, quota_analysis):
                overs_bowled = quota_analysis[bowler['name']]['overs_bowled']
                if overs_bowled <= 2:  # Max 3 overs for low-rated bowlers
                    eligible_low_rated.append(bowler)
        
        if not eligible_low_rated:
            print(f"  ❌ No eligible low-rated bowlers (constraint or over-limit)")
            return None
        
        # Determine if we should use low-rated bowler based on strategy
        should_use = False
        reason = ""
        
        # Strategy 1: Save premium bowlers for death overs (overs 11-16)
        if 11 <= self.current_over < 16:
            star_remaining_overs = sum(
                quota_analysis[star['name']]['overs_remaining'] 
                for star in bowler_tiers['star']
            )
            death_overs_needed = 4  # overs 17-20
            if star_remaining_overs >= death_overs_needed:
                should_use = True
                reason = "Saving stars for death overs"
        
        # Strategy 2: Balance workload in middle overs (overs 8-14)
        elif 8 <= self.current_over < 15:
            regular_bowlers_used = sum(
                1 for regular in bowler_tiers['regular'] 
                if quota_analysis[regular['name']]['overs_bowled'] >= 2
            )
            if regular_bowlers_used >= 2:  # If 2+ regulars have bowled 2+ overs
                should_use = True
                reason = "Balancing workload among regulars"
        
        # Strategy 3: Fresh bowler injection (any over after 7)
        elif self.current_over >= 7:
            unused_bowlers = sum(
                1 for bowler in eligible_low_rated
                if quota_analysis[bowler['name']]['overs_bowled'] == 0
            )
            if unused_bowlers > 0 and random.random() < 0.3:  # 30% chance
                should_use = True
                reason = "Fresh bowler injection for variation"
        
        if not should_use:
            print(f"  ⏸️  Strategic conditions not met for low-rated usage")
            return None
        
        # Select best available low-rated bowler
        eligible_low_rated.sort(key=lambda b: (
            quota_analysis[b['name']]['overs_bowled'],  # Prefer less used
            -b['bowling_rating']  # Then by rating (descending)
        ))
        
        selected = eligible_low_rated[0]
        overs_bowled = quota_analysis[selected['name']]['overs_bowled']
        
        print(f"  ✅ LOW-RATED STRATEGIC: {selected['name']} (Rating: {selected['bowling_rating']}, Overs: {overs_bowled}/3)")
        print(f"  📋 Reason: {reason}")
        
        return selected

    def _check_critical_2_bowler_scenario(self):
        """
        CRITICAL: Check for 2-bowler scenario that could lead to consecutive bowling
        Applies to overs 16+ to prevent impossible situations in death overs
        """
        print(f"\n🚨 === CRITICAL 2-BOWLER SCENARIO CHECK (Over {self.current_over + 1}) ===")
        
        # Get all available bowlers
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        quota_analysis = self._analyze_quota_status(all_bowlers)
        
        # Count bowlers with overs remaining
        available_bowlers = {}
        for bowler in all_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            if bowler_data['overs_remaining'] > 0:
                available_bowlers[bowler["name"]] = bowler_data['overs_remaining']
        
        print(f" Available bowlers: {available_bowlers}")
        
        # Check if exactly 2 bowlers remain with total overs = remaining match overs
        remaining_match_overs = 20 - (self.current_over + 1)
        total_available_overs = sum(available_bowlers.values())
        
        print(f" Remaining match overs: {remaining_match_overs}")
        print(f" Total available overs: {total_available_overs}")
        
        # CRITICAL SCENARIO: Exactly 2 bowlers AND tight quota situation
        if (len(available_bowlers) == 2 and 
            total_available_overs <= remaining_match_overs + 1):  # Allow 1 over buffer
            
            print(f" 🚨 CRITICAL 2-BOWLER SCENARIO DETECTED!")
            
            bowler_names = list(available_bowlers.keys())
            bowler_1_name = bowler_names[0]
            bowler_2_name = bowler_names[1]
            overs_1 = available_bowlers[bowler_1_name]
            overs_2 = available_bowlers[bowler_2_name]
            
            print(f" {bowler_1_name}: {overs_1} overs, {bowler_2_name}: {overs_2} overs")
            
            # Find bowler objects
            bowler_1 = next(b for b in all_bowlers if b["name"] == bowler_1_name)
            bowler_2 = next(b for b in all_bowlers if b["name"] == bowler_2_name)
            
            # Apply SMART SELECTION: Choose bowler with MORE overs to avoid future consecutive
            # This prevents the scenario where we pick the wrong bowler now and create impossible situation later
            
            previous_bowler = self.current_bowler["name"] if self.current_bowler else None
            
            # Rule 1: Never allow consecutive
            if previous_bowler == bowler_1_name:
                if overs_2 > 0:  # bowler_2 is available and not consecutive
                    selected_bowler = bowler_2
                    print(f" ✅ CRITICAL: Selected {bowler_2_name} (not consecutive)")
                else:
                    print(f" ❌ IMPOSSIBLE: Both bowlers create consecutive or quota violations")
                    return None
            elif previous_bowler == bowler_2_name:
                if overs_1 > 0:  # bowler_1 is available and not consecutive
                    selected_bowler = bowler_1
                    print(f" ✅ CRITICAL: Selected {bowler_1_name} (not consecutive)")
                else:
                    print(f" ❌ IMPOSSIBLE: Both bowlers create consecutive or quota violations")
                    return None
            else:
                # Neither bowled previous over - apply SMART SELECTION
                # Choose bowler with MORE remaining overs to better distribute workload
                if overs_1 > overs_2:
                    selected_bowler = bowler_1
                    print(f" ✅ CRITICAL: Selected {bowler_1_name} (more overs: {overs_1} vs {overs_2})")
                elif overs_2 > overs_1:
                    selected_bowler = bowler_2
                    print(f" ✅ CRITICAL: Selected {bowler_2_name} (more overs: {overs_2} vs {overs_1})")
                else:
                    # Equal overs - choose based on rating
                    if bowler_1["bowling_rating"] >= bowler_2["bowling_rating"]:
                        selected_bowler = bowler_1
                        print(f" ✅ CRITICAL: Selected {bowler_1_name} (equal overs, higher rating)")
                    else:
                        selected_bowler = bowler_2
                        print(f" ✅ CRITICAL: Selected {bowler_2_name} (equal overs, higher rating)")
            
            print(f"🎯 CRITICAL 2-BOWLER INTERVENTION: {selected_bowler['name']}")
            return selected_bowler
        
        print(f" ✅ No critical 2-bowler scenario detected")
        return None


    def pick_bowler(self):
        """
        Production-level bowler selection with ENHANCED POWERPLAY + STAR PRIORITY:
        Priority 1A: Strict 4-overs policy (no bowler exceeds 4 overs)
        Priority 1B: No consecutive overs (no bowler bowls back-to-back)  
        Priority 1C: Powerplay star selection (NEW)
        Priority 1D: Star bowler utilization tracking (NEW)
        Priority 2: Strategy optimization (pattern, approach 1, etc.)
        """

        print(f"\n🐛 PICK_BOWLER DEBUG: Over {self.current_over + 1}")
        print(f"🐛 Current over >= 17? {self.current_over >= 17}")
        print(f"🐛 Previous bowler: {self.current_bowler['name'] if self.current_bowler else 'None'}")

        # ================ DEATH OVERS SPECIAL HANDLING ================
        if self.current_over >= 17:  # Overs 18, 19, 20
            print(f"\n🎯 === SWITCHING TO DEATH OVERS MODE ===")
            return self._pick_death_overs_bowler()
        
        # ================ CRITICAL 2-BOWLER SCENARIO PRE-CHECK ================
        # Check for 2-bowler scenario even before death overs (overs 16-17)
        if self.current_over >= 15:  # Start checking from over 16
            critical_2_bowler_result = self._check_critical_2_bowler_scenario()
            if critical_2_bowler_result:
                print(f"🚨 CRITICAL 2-BOWLER SCENARIO DETECTED - EARLY INTERVENTION")
                return critical_2_bowler_result
        
        # ================ DEBUG: INITIALIZATION ================
        print(f"\n🎳 === BOWLER SELECTION DEBUG - Over {self.current_over + 1} ===")
        print(f"Previous bowler: {self.current_bowler['name'] if self.current_bowler else 'None'}")
        print(f"Match phase: {self._get_match_phase()}")
        
        # Get all available bowlers
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        print(f"All bowlers marked will_bowl: {[b['name'] for b in all_bowlers]}")
        
        # ================ NEW: BOWLER CLASSIFICATION ================
        bowler_tiers = self._classify_bowlers_by_tier(all_bowlers)
        print(f"🌟 Star bowlers: {[b['name'] for b in bowler_tiers['star']]}")
        print(f"⭐ Regular bowlers: {[b['name'] for b in bowler_tiers['regular']]}")
        
        # ================ QUOTA TRACKING & ANALYSIS ================
        quota_analysis = self._analyze_quota_status(all_bowlers)
        
        # ================ NEW: EARLY OVERS FAST BOWLER OVERRIDE ================
        if self.current_over < 4:  # Early overs 1-4 only
            early_overs_result = self._try_early_overs_fast_selection(bowler_tiers, quota_analysis)
            if early_overs_result:
                print(f"🚀 EARLY OVERS FAST OVERRIDE: Selected {early_overs_result['name']}")
                self._update_bowler_tracking(early_overs_result)
                return early_overs_result

        # ================ NEW: STAR NEGLECT PREVENTION ================  
        if self.current_over >= 10:  # After over 10
            neglect_result = self._prevent_star_neglect(bowler_tiers, quota_analysis)
            if neglect_result:
                print(f"⚡ STAR NEGLECT PREVENTION: Selected {neglect_result['name']}")
                self._update_bowler_tracking(neglect_result)
                return neglect_result

        # ================ NEW: LOW-RATED BOWLER STRATEGIC USAGE ================
        if self.current_over >= 5:  # After early overs
            low_rated_result = self._try_low_rated_bowler_usage(bowler_tiers, quota_analysis)
            if low_rated_result:
                print(f"🎯 LOW-RATED STRATEGIC: Selected {low_rated_result['name']}")
                self._update_bowler_tracking(low_rated_result)
                return low_rated_result

        # ================ RISK ASSESSMENT ================
        risk_assessment = self._assess_constraint_risk(all_bowlers, quota_analysis)
        print(f"\n⚠️  RISK ASSESSMENT:")
        print(f"  Constraint Risk Level: {risk_assessment['risk_level']}")
        print(f"  Risk Factors: {risk_assessment['risk_factors']}")
        print(f"  Emergency Mode: {risk_assessment['emergency_mode']}")
        
                # ================ PHASE 1: DUAL CONSTRAINT ENFORCEMENT ================
                # ================ PHASE 1: DUAL CONSTRAINT ENFORCEMENT ================
        print(f"\n--- PHASE 1: DUAL CONSTRAINT ENFORCEMENT ---")

        # Sub-phase 1A: 4-Overs Policy Enforcement
        quota_eligible = self._apply_strict_quota_policy(all_bowlers, quota_analysis)
        print(f"After 4-overs filter: {[b['name'] for b in quota_eligible]}")

        # Sub-phase 1B: No Consecutive Policy Enforcement
        constraint_eligible = self._apply_strict_consecutive_policy(quota_eligible, risk_assessment)
        print(f"After no-consecutive filter: {[b['name'] for b in constraint_eligible]}")

        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
        # SPECIAL HANDLING: DEAD or FLAT PITCH → force Spinner/Medium-fast/Medium,
        # then boost their bowling_rating by 10%.
        if self.pitch in ("Dead", "Flat"):
            # 1. Keep only Spinner / Medium-fast / Medium among constraint_eligible
            filtered = [
                b for b in constraint_eligible
                if b["bowling_type"] in ("Spinner", "Medium-fast", "Medium")
            ]
            if filtered:
                # 2. Replace constraint_eligible with that filtered list
                constraint_eligible = filtered

                # 3. Temporarily boost each bowler’s bowling_rating by 10% (capped at 100)
                for bowler in constraint_eligible:
                    if bowler.get("_orig_boiling_rating") is None:
                        bowler["_orig_boiling_rating"] = bowler["bowling_rating"]
                    bowler["bowling_rating"] = int(
                        min(bowler["_orig_boiling_rating"] * 1.1, 100)
                    )
        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
        # NEW: Ensure every ‘will_bowl=True’ bowler bowls at least 1 over.
        # If the number of remaining overs equals the count of fresh bowlers,
        # force selection from those who haven’t yet bowled.
        remaining_overs = 20 - (self.current_over + 1)
        # “Fresh” means: marked will_bowl AND overs_bowled == 0
        fresh_bowlers = [
            b for b in constraint_eligible
            if b.get("will_bowl", False)
               and self.bowler_history.get(b["name"], 0) == 0
        ]
        # If exactly as many fresh bowlers as there are overs left, they all must bowl once.
        if fresh_bowlers and len(fresh_bowlers) == remaining_overs:
            constraint_eligible = fresh_bowlers
        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

        
        # ================ EMERGENCY CONSTRAINT HANDLING ================
        if not constraint_eligible:
            print(f"\n🚨 EMERGENCY: No bowlers meet both constraints!")
            constraint_eligible = self._handle_constraint_emergency(all_bowlers, quota_analysis, risk_assessment)
            print(f"Emergency resolution: {[b['name'] for b in constraint_eligible]}")
        
        # ================ PHASE 2: BALANCED STRATEGIC DISTRIBUTION ================
        print(f"\n--- PHASE 2: BALANCED STRATEGIC DISTRIBUTION ---")

        # 2A: Prevent over-utilization (max 2 overs in first 10)
        balanced_eligible = self._prevent_over_utilization(constraint_eligible, quota_analysis)

        # 2B: NEW - Apply All-rounder bowling limits when 6+ bowlers available
        role_limited_eligible = self._apply_all_rounder_bowling_limits(balanced_eligible, quota_analysis)

        # 2C: Star preservation strategy  
        preserved_eligible = self._apply_star_preservation_strategy(role_limited_eligible, bowler_tiers, quota_analysis)

        # 2D: Variety enforcement
        variety_eligible = self._apply_variety_enforcement(preserved_eligible, quota_analysis)

        print(f"After balanced strategy: {[b['name'] for b in variety_eligible]}")

        # ================ PHASE 3: PATTERN OPTIMIZATION ================
        print(f"\n--- PHASE 3: PATTERN OPTIMIZATION ---")

        # Apply existing pattern strategy to final pool
        strategic_eligible = self._apply_pattern_strategy(variety_eligible, self._get_preferred_bowler_type(self.current_over))

        print(f"After pattern filters: {[b['name'] for b in strategic_eligible]}")
        
        # ================ FINAL SELECTION & VALIDATION ================
        print(f"\n--- FINAL SELECTION & VALIDATION ---")
        
        if not strategic_eligible:
            print(f"⚠️  No bowlers after strategy - reverting to constraint-safe pool")
            strategic_eligible = constraint_eligible
        
        selected_bowler = self._select_optimal_bowler(strategic_eligible, risk_assessment)
        print(f"🎯 SELECTED: {selected_bowler['name']} ({selected_bowler['bowling_type']}, rating: {selected_bowler['bowling_rating']})")
        
        # ================ CRITICAL VALIDATION ================
        validation_result = self._validate_selection(selected_bowler, all_bowlers, quota_analysis)
        
        if not validation_result['valid']:
            print(f"🚨 VALIDATION FAILED: {validation_result['reason']}")
            # Force emergency correction
            selected_bowler = self._force_valid_selection(all_bowlers, quota_analysis)
            print(f"🔧 CORRECTED SELECTION: {selected_bowler['name']}")
        else:
            print(f"✅ VALIDATION PASSED: All constraints satisfied")
        
                # ================ TRACKING & PROJECTION ================
        print(f"\n--- TRACKING & PROJECTION ---")

        # Update tracking
        self._update_bowler_tracking(selected_bowler)

        # Project future implications
        future_projection = self._project_future_constraints(selected_bowler, all_bowlers)
        print(f"📈 FUTURE PROJECTION:")
        print(f"  Remaining overs: {20 - (self.current_over + 1)}")
        print(f"  Available bowlers after this over: {future_projection['available_count']}")
        print(f"  Potential risk next over: {future_projection['next_over_risk']}")

        print(f"\n🏁 === BOWLER SELECTION COMPLETE ===\n")

        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
        # If we had boosted any Spinner/Medium-fast/Medium earlier, revert their rating now
        for bowler in self.bowling_team:
            if bowler.get("_orig_boiling_rating") is not None:
                bowler["bowling_rating"] = bowler["_orig_boiling_rating"]
                del bowler["_orig_boiling_rating"]
        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

        # Add this right before "return selected_bowler" in pick_bowler()
        # ================ PRODUCTION SAFETY NET ================
        print(f"\n--- PRODUCTION CONSECUTIVE VALIDATION ---")
        self._absolute_consecutive_validation(selected_bowler)
        print(f"✅ CONSECUTIVE VALIDATION PASSED: {selected_bowler['name']} is safe to bowl")

        return selected_bowler


    def rotate_strike(self):
        self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
        self.batter_idx.reverse()

    def _generate_risk_commentary(self, risk_effects):
        """Generate commentary for risk-based cricket"""
        if not risk_effects or not risk_effects.get('risk_active'):
            return None
        
        mode = risk_effects['mode']
        risk_factor = risk_effects['risk_factor']
        
        if mode == 'DEATH_OR_GLORY':
            return random.choice([
                f"<strong>💀 DEATH OR GLORY!</strong> Risk factor {risk_factor:.1f}x - It's boundaries or bust!",
                f"<strong>💀 FINAL ASSAULT!</strong> Throwing everything at it now!",
                f"<strong>💀 LAST STAND!</strong> No tomorrow cricket!"
            ])
        elif mode == 'ALL_OUT_ATTACK':
            return random.choice([
                f"<strong>🔥 ALL-OUT ATTACK!</strong> High-risk cricket in full flow!",
                f"<strong>🔥 AGGRESSIVE MODE!</strong> Calculated risks being taken!",
                f"<strong>🔥 POWER SURGE!</strong> Going for broke!"
            ])
        elif mode == 'HIGH_RISK_CRICKET':
            return random.choice([
                f"<strong>⚡ HIGH-RISK CRICKET!</strong> Batsmen taking chances!",
                f"<strong>⚡ PRESSURE COOKER!</strong> Big shots needed!",
                f"<strong>⚡ AGGRESSIVE INTENT!</strong> No safe options left!"
            ])
        else:  # AGGRESSIVE_CRICKET
            return random.choice([
                f"<strong>🎯 AGGRESSIVE CRICKET!</strong> Taking calculated risks!",
                f"<strong>🎯 STEPPING UP!</strong> Need boundaries to stay alive!"
            ])

    def _generate_pressure_commentary(self, pressure_score, match_state):
        """Generate contextual pressure commentary based on match situation"""
        
        # Only show pressure commentary occasionally to avoid spam
        if random.random() > 0.3:  # 30% chance to show
            return None
        
        # Only show for medium-high pressure
        if pressure_score < 40:
            return None
        
        commentary = ""
        
        if self.innings == 1:
            # First innings pressure commentary
            if pressure_score >= 70:
                if self.current_over < 6:
                    commentary = random.choice([
                        f"<strong>Pressure Building!</strong> {self.data['team_home'].split('_')[0] if self.batting_team == self.home_xi else self.data['team_away'].split('_')[0]} struggling to get going in the powerplay...",
                        f"The run rate is concerning early on - need to accelerate soon!",
                        f"Dot balls piling up - the asking rate keeps climbing!",
                        f"Early wickets have put the brakes on - need a partnership here."
                    ])
                elif self.current_over >= 15:
                    commentary = random.choice([
                        f"<strong>Death Overs Pressure!</strong> Need to find the boundary - every ball is crucial now!",
                        f"The total is looking under par - desperate need for some big hits!",
                        f"Clock is ticking! Can they accelerate in these final overs?",
                        f"Pressure of setting a competitive total weighing heavily..."
                    ])
            elif pressure_score >= 50:
                commentary = random.choice([
                    f"Building some pressure here - need to rotate the strike...",
                    f"Bowlers have tightened the screws - batsmen feeling the heat!",
                    f"Partnership under pressure - one big shot could release it..."
                ])
        
        else:  # Second innings
            runs_needed = match_state.get('runs_needed', 0)
            overs_remaining = match_state.get('overs_remaining', 0)
            required_rr = match_state.get('required_run_rate', 0)
            
            if pressure_score >= 70:
                if overs_remaining <= 5:
                    commentary = random.choice([
                        f"<strong>Crunch Time!</strong> {runs_needed} needed from {overs_remaining:.1f} overs - RRR: {required_rr:.2f}",
                        f"Nerves jangling in the dressing room! This is where champions are made!",
                        f"The pressure is immense! Every run, every ball matters now!",
                        f"Heart-stopping cricket! Can they hold their nerve?",
                        f"The crowd is on its feet - this is nail-biting stuff!",
                        f"Pressure cooker situation! One boundary could change everything!"
                    ])
                else:
                    commentary = random.choice([
                        f"Required rate climbing dangerously - {required_rr:.1f} runs per over needed!",
                        f"The chase is getting away from them - need a big over soon!",
                        f"Wickets falling at the wrong time - pressure mounting!",
                        f"Running out of recognized batsmen - dangerous situation!"
                    ])
            elif pressure_score >= 50:
                commentary = random.choice([
                    f"Chase getting tighter - need to find gaps and rotate strike...",
                    f"Bowlers applying the squeeze - batsmen need to be smart here!",
                    f"Asking rate creeping up - time to take calculated risks!",
                    f"Partnership needs to weather this storm..."
                ])
        
        # Add pitch-specific pressure elements
        if self.pitch in ['Green', 'Dusty'] and pressure_score >= 60:
            pitch_commentary = random.choice([
                f"This {self.pitch.lower()} pitch is making life difficult for the batsmen!",
                f"Conditions favoring the bowlers - tough to score freely!"
            ])
            if commentary:
                commentary += f"<br>{pitch_commentary}"
            else:
                commentary = pitch_commentary
        
        # Add momentum-specific commentary
        recent_events = getattr(self.pressure_engine, 'recent_events', [])
        if len(recent_events) >= 3:
            recent_dots = sum(1 for event in recent_events[-3:] if event.get('runs') == 0 and not event.get('extra'))
            if recent_dots >= 2 and pressure_score >= 55:
                momentum_commentary = random.choice([
                    "Three dot balls building pressure!",
                    "Bowler right on top - batsmen struggling to get away!",
                    "Maiden over building? Pressure mounting with every dot ball!"
                ])
                if commentary:
                    commentary += f"<br>{momentum_commentary}"
                else:
                    commentary = momentum_commentary
        
        return f"<em>{commentary}</em>" if commentary else None

    def next_ball(self):
        if self.innings == 3:

            self._save_second_innings_stats()
            self._create_match_archive()

            return {
                "test": "Manish1",
                "match_over": True,
                "final_score": self.score,
                "wickets": self.wickets,
                "result": self.result
            }


        if self.current_over >= self.overs or self.wickets >= 10:
            if self.innings == 1:
                # 🔧 USER REQUEST: Print team stats when all out
                if self.wickets >= 10:
                    print(f"\n🛑 All Out! {self.first_batting_team_name} {self.score}/10 in {self.current_over}.{self.current_ball} overs")
                    not_out_batter = self.current_striker["name"] if not self.batsman_stats[self.current_striker["name"]]["wicket_type"] else self.current_non_striker["name"]
                    not_out_score = self.batsman_stats[not_out_batter]["runs"]
                    not_out_balls = self.batsman_stats[not_out_batter]["balls"]
                    print(f"   Not Out: {not_out_batter} {not_out_score}*({not_out_balls})")
                    print(f"   Total Score: {self.score}")

                scorecard_data = self._generate_detailed_scorecard()
                self.first_innings_score = self.score
                self.target = self.score + 1
                required_rr = self.target / self.overs
                chasing_team_code = self.data["team_away"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_home"].split("_")[0]
                scorecard_data["target_info"] = f"{chasing_team_code} needs {self.target} runs from {self.overs} overs at {required_rr:.2f} runs per over"
                
                self._save_first_innings_stats()

                self.innings = 2
                
                # Reload lineups from data to ensure impact player/reordering changes are applied
                if hasattr(self, 'data') and self.data.get('impact_players_swapped'):
                    print("🔄 [Innings Change] Applying impact player and reordering changes.")
                    self.home_xi = self.data["playing_xi"]["home"]
                    self.away_xi = self.data["playing_xi"]["away"]
                    
                # --- START FIX ---
                # Re-determine batting and bowling teams based on original toss decision and the UPDATED XIs
                team_home_code = self.match_data["team_home"].split("_")[0]
                if self.toss_winner == team_home_code:
                    if self.toss_decision == "Bat":
                        # Home batted first, so Away bats second
                        self.batting_team, self.bowling_team = self.away_xi, self.home_xi
                    else:  # Home bowled first, so Home bats second
                        self.batting_team, self.bowling_team = self.home_xi, self.away_xi
                else:  # Away won toss
                    if self.toss_decision == "Bat":
                        # Away batted first, so Home bats second
                        self.batting_team, self.bowling_team = self.home_xi, self.away_xi
                    else:  # Away bowled first, so Away bats second
                        self.batting_team, self.bowling_team = self.away_xi, self.home_xi
                # --- END FIX ---

                # Reset all innings-specific state
                self.score = 0
                self.wickets = 0
                self.current_over = 0
                self.current_ball = 0
                self.batter_idx = [0, 1]
                self.current_striker = self.batting_team[0]
                self.current_non_striker = self.batting_team[1]

                print(f"🏏 Second innings batting order (Post-Fix):")
                for i, player in enumerate(self.batting_team):
                    print(f"   {i+1}. {player['name']}")

                self.batsman_stats = {p["name"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0, "threes": 0, "dots": 0, "wicket_type": "", "bowler_out": "", "fielder_out": ""} for p in self.batting_team}
                self.bowler_history = {}
                self.bowler_stats = {p["name"]: {"runs": 0, "fours": 0, "sixes": 0, "wickets": 0, "overs": 0, "maidens": 0, "balls_bowled": 0, "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0} for p in self.bowling_team if p.get("will_bowl")}
                self._reset_innings_state()

                return {
                    "innings_end": True,
                    "innings_number": 1,
                    "match_over": False,
                    "scorecard_data": scorecard_data,
                    "score": 0,
                    "wickets": 0,
                    "over": 0,
                    "ball": 0,
                    "commentary": f"End of 1st Innings: {self.first_innings_score}/{10 if self.wickets >= 10 else self.wickets}. Target: {self.target}",
                    "striker": self.current_striker["name"],
                    "non_striker": self.current_non_striker["name"],
                    "bowler": ""
                }

            else:
                scorecard_data = self._generate_detailed_scorecard()
                if self.score >= self.target:
                    winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                    wkts_left = 10 - self.wickets

                    total_balls_in_innings = self.overs * 6  # 120 balls for 20 overs
                    balls_played_including_this_ball = self.current_over * 6 + self.current_ball  # Now includes the match-winning ball
                    balls_left = total_balls_in_innings - balls_played_including_this_ball
                    overs_left = balls_left / 6

                    print("Check points: {}".format({
                        "current_over": self.current_over,
                        "current_ball": self.current_ball
                    }))

                    self.result = f"{winner_code} won by {wkts_left} wicket(s) with {overs_left:.1f} overs remaining."
                else:
                    # Check for tie
                    if self.score == self.target - 1:
                        self.result = "Match Tied"
                        
                        # ✅ Save main match state
                        self._save_second_innings_stats()
                        self._create_match_archive()
                        
                        # ✅ Store original scorecard for later display
                        self.original_scorecard = self._generate_detailed_scorecard()
                        self.original_scorecard["target_info"] = "Match Tied"
                        
                        # ✅ Set up super over (this won't affect main match stats)
                        self.innings = 4
                        return self._setup_super_over()
                    else:
                        winner_code = self.data["team_home"].split("_")[0] if self.bowling_team is self.home_xi else self.data["team_away"].split("_")[0]
                        run_diff = self.target - self.score - 1
                        self.result = f"{winner_code} won by {run_diff} run(s)."

                striker_stats = self.batsman_stats[self.current_striker["name"]]
                non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
                bowler_stats = self.bowler_stats[self.current_bowler["name"]]
                overs_bowled = bowler_stats["overs"] + (bowler_stats["balls_bowled"] % 6) / 10
                
                extras_str = ""
                if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                    extras_parts = []
                    if bowler_stats["wides"] > 0:
                        extras_parts.append(f"{bowler_stats['wides']}w")
                    if bowler_stats["noballs"] > 0:
                        extras_parts.append(f"{bowler_stats['noballs']}nb")
                    if extras_parts:
                        extras_str = f" ({', '.join(extras_parts)})"
                
                final_commentary = f"<br><strong>Match Over!</strong> {self.result}<br><br>"
                final_commentary += f"<strong>Final Stats:</strong><br>"
                final_commentary += f"{self.current_striker['name']}\t\t{striker_stats['runs']}({striker_stats['balls']}b) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}"

                self.innings = 3
                scorecard_data["target_info"] = self.result

                self._save_second_innings_stats()
                self._create_match_archive()

                return {
                    "Test": "Manish4",
                    "innings_end": True,
                    "innings_number": 2,
                    "match_over": True,
                    "scorecard_data": scorecard_data,
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": self.result,
                    "commentary": final_commentary
                }

        if self.current_ball == 0 and not getattr(self, "prev_delivery_was_extra", False):

            # ===== 🌧️ RAIN CHECK =====
            logger.debug(f"Checking for rain...")
            if self._check_for_rain():
                logger.info(f"RAIN DETECTED! Handling rain event...")
                rain_result = self._handle_rain_event()
                logger.debug(f"RAIN RESULT: {rain_result}")
                logger.debug(f"RAIN COMMENTARY: {rain_result.get('commentary', 'NO COMMENTARY FOUND')}")
                return rain_result
            # ===== END RAIN CHECK =====

            self.current_bowler = self.pick_bowler()
            self.commentary.append(
                f"<strong>The New bowler is</strong> {self.current_bowler['name']}<br>"
            )
            if self.current_over == 0:
                self.commentary.append(
                    f"🧢 <strong>Striker:</strong> {self.current_striker['name']}"
                )
                self.commentary.append(
                    f"🎯 <strong>Non-striker:</strong> {self.current_non_striker['name']}<br>"
                )

        # Calculate pressure and effects
        match_state = self._calculate_current_match_state()
        pressure_score = self.pressure_engine.calculate_pressure(match_state)

        # Get base pressure effects (now fair)
        pressure_effects = self.pressure_engine.get_pressure_effects(
            pressure_score, 
            self.current_striker['batting_rating'],
            self.current_bowler['bowling_rating'],
            self.pitch
        )

        # 🔧 ADD CHASING ADVANTAGE
        chasing_advantage = self.pressure_engine.get_chasing_advantage(match_state)
        if chasing_advantage:
            pressure_effects['boundary_modifier'] *= chasing_advantage['boundary_boost']
            pressure_effects['wicket_modifier'] *= chasing_advantage['wicket_reduction']
            print(f"🎯 CHASING ADVANTAGE: {chasing_advantage['boundary_boost']:.2f}x boundaries, {chasing_advantage['wicket_reduction']:.2f}x wickets")

        # Check for defensive mode first
        defensive_effects = self.pressure_engine.calculate_defensive_factor(match_state)

        if defensive_effects and defensive_effects['defensive_active']:
            # Defensive mode (many wickets down)
            pressure_effects['boundary_modifier'] *= (1 - defensive_effects['boundary_reduction'])
            pressure_effects['wicket_modifier'] *= (1 - defensive_effects['wicket_reduction'])
            pressure_effects['dot_bonus'] += defensive_effects['dot_increase']
            pressure_effects['single_boost'] = defensive_effects['single_boost']
            logger.info(f"{defensive_effects['mode']}: Playing defensively!")

        else:
            # Apply fair risk-based effects
            risk_effects = self.pressure_engine.get_risk_based_effects(match_state)
            
            if risk_effects and risk_effects['risk_active']:
                # Check wicket cluster
                recent_wickets = getattr(self, 'recent_wickets_count', 0)
                cluster_trigger = self.pressure_engine.should_trigger_wicket_cluster(
                    match_state, recent_wickets
                )
                
                if cluster_trigger:
                    pressure_effects['wicket_modifier'] *= 1.3  # Reduced from 1.5
                    logger.info(f"WICKET CLUSTER: 1.3x additional wicket boost!")
                
                # Apply effects
                pressure_effects['boundary_modifier'] *= risk_effects['boundary_boost']
                pressure_effects['dot_bonus'] += risk_effects['dot_increase']
                pressure_effects['wicket_modifier'] *= risk_effects['wicket_boost']
                pressure_effects['strike_rotation_penalty'] = risk_effects['strike_rotation_penalty']
                pressure_effects['single_floor'] = risk_effects['single_floor']

        outcome = calculate_outcome(
            batter=self.current_striker,
            bowler=self.current_bowler,
            pitch=self.pitch,
            streak={},
            over_number=self.current_over,
            batter_runs=self.batsman_stats[self.current_striker["name"]]["runs"],
            innings=self.innings,
            pressure_effects=pressure_effects
        )

        # Update pressure engine with outcome
        self.pressure_engine.update_recent_events(outcome)
        self._update_partnership_tracking(outcome)

        # ENHANCED DEBUG - Show ALL outcome details
        logger.debug(f"Ball {self.current_over}.{self.current_ball + 1} FULL OUTCOME:")
        logger.debug(f"   type: {outcome.get('type')}")
        logger.debug(f"   runs: {outcome.get('runs')}")
        logger.debug(f"   batter_out: {outcome.get('batter_out')}")
        logger.debug(f"   wicket_type: {outcome.get('wicket_type')}")
        logger.debug(f"   description: '{outcome.get('description')}'")
        logger.debug(f"   is_extra: {outcome.get('is_extra')}")

        # Debug wicket outcomes to catch future issues
        if outcome.get("batter_out", False):
            logger.debug(f"Ball {self.current_over}.{self.current_ball + 1} WICKET: type={outcome.get('wicket_type')}, desc='{outcome.get('description')}'")

        ball_number = f"{self.current_over}.{self.current_ball + 1}"
        runs, wicket, extra = outcome["runs"], outcome["batter_out"], outcome["is_extra"]

        # 🔧 NOW ADD WICKET TRACKING HERE (after wicket is defined)
        if wicket:
            # Update recent wickets tracking
            if not hasattr(self, 'recent_wickets_tracker'):
                self.recent_wickets_tracker = []
            
            self.recent_wickets_tracker.append(self.current_over * 6 + self.current_ball)
            # Keep only last 12 balls (2 overs)
            current_ball_number = self.current_over * 6 + self.current_ball
            self.recent_wickets_tracker = [w for w in self.recent_wickets_tracker
                                        if current_ball_number - w <= 12]
            self.recent_wickets_count = len(self.recent_wickets_tracker)
            logger.info(f"Wicket tracking: {self.recent_wickets_count} wickets in last 12 balls")

        
        self.prev_delivery_was_extra = extra

        if not hasattr(self, 'current_over_runs'):
            self.current_over_runs = 0
        if self.current_ball == 0:
            self.current_over_runs = 0

        commentary_line = f"{ball_number} {self.current_bowler['name']} to {self.current_striker['name']} - "

        if wicket:
            self.wickets += 1
            # self.bowler_stats[self.current_bowler["name"]]["wickets"] += 1
            
            wicket_type = outcome["wicket_type"]

            if wicket_type != "Run Out":
                self.bowler_stats[self.current_bowler["name"]]["wickets"] += 1
            
            # ─── NEW: credit this ball to the striker’s 'balls faced' counter ───
            if not extra:
                self.current_ball += 1
                self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1
                self.batsman_stats[self.current_striker["name"]]["balls"] += 1
            
            fielder_name = None
            
            self.batsman_stats[self.current_striker["name"]]["wicket_type"] = wicket_type
            self.batsman_stats[self.current_striker["name"]]["bowler_out"] = self.current_bowler["name"]
            
            if wicket_type in ["Caught", "Run Out"]:
                fielder_name = self._select_fielder_for_wicket(wicket_type)
                self.batsman_stats[self.current_striker["name"]]["fielder_out"] = fielder_name
            
            commentary_line += self._generate_wicket_commentary(outcome, fielder_name)
            self.commentary.append(commentary_line)

            self.batter_idx[0] = max(self.batter_idx) + 1

            # In match.py, replace the existing "all out" logic in next_ball() method:
            if self.batter_idx[0] >= len(self.batting_team):
                scorecard_data = self._generate_detailed_scorecard()

                # ✅ BUILD ENHANCED ALL-OUT COMMENTARY
                enhanced_commentary_parts = []

                # 1. Add the wicket ball commentary (already built)
                enhanced_commentary_parts.append(commentary_line)

                 # 2. Add current bowler's final stats (like end of over)
                bowler_stats = self.bowler_stats[self.current_bowler["name"]]
                balls_bowled_this_over = bowler_stats["balls_bowled"] % 6
                overs_bowled = bowler_stats["overs"] + (balls_bowled_this_over / 10) if balls_bowled_this_over > 0 else bowler_stats["overs"]
                
                # Build extras string
                extras_str = ""
                if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                    extras_parts = []
                    if bowler_stats["wides"] > 0:
                        extras_parts.append(f"{bowler_stats['wides']}w")
                    if bowler_stats["noballs"] > 0:
                        extras_parts.append(f"{bowler_stats['noballs']}nb")
                    if extras_parts:
                        extras_str = f" ({', '.join(extras_parts)})"


                enhanced_commentary_parts.append(f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}")
    
                # 3. Add "All Out!" message
                enhanced_commentary_parts.append("<br><strong>All Out!</strong>")

                # 4. Combine all parts
                all_out_commentary = "<br>".join(enhanced_commentary_parts)
                
                if self.innings == 1:
                    # ✅ FIRST INNINGS ALL OUT - Transition to second innings
                    self.first_innings_score = self.score
                    self.target = self.score + 1
                    required_rr = self.target / self.overs
                    chasing_team = self.data["team_away"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_home"].split("_")[0]
                    scorecard_data["target_info"] = f"{chasing_team} needs {self.target} runs from {self.overs} overs at {required_rr:.2f} runs per over"
                    
                    # Save first innings stats
                    self._save_first_innings_stats()

                    # Reset for 2nd innings (same as time-based transition)
                    self.innings = 2
                    # ✅ CRITICAL: Update lineups if impact player swaps occurred
                    if hasattr(self, 'data') and self.data.get('impact_players_swapped'):
                        print(f"🔄 Applying impact player changes for second innings...")
                        # Use the updated playing XI from match data
                        self.home_xi = self.data["playing_xi"]["home"]
                        self.away_xi = self.data["playing_xi"]["away"]
                        print(f"   Updated Home XI: {[p['name'] for p in self.home_xi]}")
                        print(f"   Updated Away XI: {[p['name'] for p in self.away_xi]}")

                    self.batting_team, self.bowling_team = self.bowling_team, self.batting_team
                    self.score = 0
                    self.wickets = 0
                    self.current_over = 0
                    self.current_ball = 0
                    self.batter_idx = [0, 1]
                    self.current_striker = self.batting_team[0]
                    self.current_non_striker = self.batting_team[1]
                    self.batsman_stats = {p["name"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0, "threes": 0, "dots": 0, "wicket_type": "", "bowler_out": "", "fielder_out": ""} for p in self.batting_team}
                    self.bowler_history = {}
                    self.bowler_stats = {p["name"]: {"runs": 0, "fours": 0, "sixes": 0, "wickets": 0, "overs": 0, "maidens": 0, "balls_bowled": 0, "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0} for p in self.bowling_team if p["will_bowl"]}
                    self._reset_innings_state()

                    return {
                        "Test": "AllOut_FirstInnings",
                        "innings_end": True,
                        "innings_number": 1,
                        "match_over": False,  # ✅ Keep match going
                        "scorecard_data": scorecard_data,
                        "score": 0,
                        "wickets": 0,
                        "over": 0,
                        "ball": 0,
                        "commentary": f"{all_out_commentary}!<br>End of 1st Innings: {self.first_innings_score}/10. Target: {self.target}",
                        "striker": self.current_striker["name"],
                        "non_striker": self.current_non_striker["name"],
                        "bowler": ""
                    }
                else:
                    # ✅ SECOND INNINGS ALL OUT - Match over
                    self._save_second_innings_stats()
                    self._create_match_archive()

                    #Include logic for all out result

                    # 3. Add striker dismissal line
                    out_name      = self.current_striker["name"]
                    stats         = self.batsman_stats[out_name]
                    runs_scored   = stats["runs"]
                    balls_faced   = stats["balls"]
                    fours_scored  = stats["fours"]
                    sixes_scored  = stats["sixes"]
                    extras = []

                    if fours_scored > 0:
                        extras.append(f"{fours_scored}x4")
                    if sixes_scored > 0:
                        extras.append(f"{sixes_scored}x6")
                    extra_str = f"[{', '.join(extras)}]" if extras else ""
                    dismissal_line = f"{out_name} {runs_scored}({balls_faced}b) {extra_str}"
                    enhanced_commentary_parts.append(dismissal_line)

                    # 4. Add non-striker stats
                    non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
                    enhanced_commentary_parts.append(
                        f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) "
                        f"[{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]"
                    )

                    # 5. Add bowler stats line
                    bowler_stats = self.bowler_stats[self.current_bowler["name"]]
                    extras_str = ""
                    if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                        extras_parts = []
                        if bowler_stats["wides"] > 0:
                            extras_parts.append(f"{bowler_stats['wides']}w")
                        if bowler_stats["noballs"] > 0:
                            extras_parts.append(f"{bowler_stats['noballs']}nb")
                        extras_str = f" ({', '.join(extras_parts)})"

                    balls_bowled_this_over = bowler_stats["balls_bowled"] % 6
                    overs_bowled = bowler_stats["overs"] + (balls_bowled_this_over / 10) if balls_bowled_this_over > 0 else bowler_stats["overs"]
                    enhanced_commentary_parts.append(
                        f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-"
                        f"{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}"
                    )

                    all_out_commentary = "<br>".join(enhanced_commentary_parts)
                    return {
                        "Test": "AllOut_SecondInnings", 
                        "match_over": True,
                        "scorecard_data": scorecard_data,
                        "final_score": self.score,
                        "wickets": self.wickets,
                        # "result": f"All out for {self.score}",
                        "commentary": f"{all_out_commentary}<br>Match Over! All out for {self.score}",
                        "result": f"{self.first_batting_team_name} won by {(self.target - 1) - self.score} runs!!"
                    }

            # 1) Gather the dismissed batsman’s stats:
            out_name      = self.current_striker["name"]
            stats         = self.batsman_stats[out_name]
            runs_scored   = stats["runs"]
            balls_faced   = stats["balls"]
            fours_scored  = stats["fours"]
            sixes_scored  = stats["sixes"]

            # 2) Choose fielder/bowler strings based on wicket type:
            wkt = outcome["wicket_type"]
            bowler_name = self.current_bowler["name"]
            fielder_part = ""
            bowler_part  = ""

            if wkt == "Caught":
                # caught: “c Fielder b Bowler”
                # fielder_name was already computed above as fielder_name
                fielder_part = f"c {fielder_name}"
                bowler_part  = f"b {bowler_name}"
            elif wkt == "Bowled":
                # bowled: “b Bowler”
                bowler_part = f"b {bowler_name}"
            elif wkt == "LBW":
                # lbw: “lbw b Bowler”
                bowler_part = f"lbw b {bowler_name}"
            elif wkt == "Run Out":
                # run out: “f Fielder” (no bowler)
                fielder_part = f"f {fielder_name}"

            # 3) Build the “[0x4, 1x6]” part:
            extras = []
            if fours_scored > 0:
                extras.append(f"{fours_scored}x4")
            if sixes_scored > 0:
                extras.append(f"{sixes_scored}x6")
            extra_str = f"[{', '.join(extras)}]" if extras else ""

            # 4) Combine into one dismissal‐line:
            dismissal_line = f"{out_name} "
            if fielder_part:
                dismissal_line += f"{fielder_part} "
            if bowler_part:
                dismissal_line += f"{bowler_part} "
            dismissal_line += f"{runs_scored}({balls_faced}b) {extra_str}"

            # 5) Append it before “New batsman…”
            print("dismissal_line", dismissal_line)
            commentary_line += "<br><br>" + dismissal_line + "<br>"

            
            self.current_striker = self.batting_team[self.batter_idx[0]]
            if self.current_striker["name"] not in self.batsman_stats:
                self.batsman_stats[self.current_striker["name"]] = {
                    "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0, "threes": 0, "dots": 0,
                    "wicket_type": "", "bowler_out": "", "fielder_out": ""
                }
            commentary_line += f"<br><strong>New batsman:</strong> {self.current_striker['name']}<br><br>"
            self.commentary.append(commentary_line)

        else:
            self.score += runs
            self.current_over_runs += runs
            self.bowler_stats[self.current_bowler["name"]]["runs"] += runs
            
            if not extra:
                self.batsman_stats[self.current_striker["name"]]["runs"] += runs
                self.batsman_stats[self.current_striker["name"]]["balls"] += 1
                
                if runs == 0:
                    self.batsman_stats[self.current_striker["name"]]["dots"] += 1
                elif runs == 1:
                    self.batsman_stats[self.current_striker["name"]]["ones"] += 1
                elif runs == 2:
                    self.batsman_stats[self.current_striker["name"]]["twos"] += 1
                elif runs == 3:
                    self.batsman_stats[self.current_striker["name"]]["threes"] += 1
                elif runs == 4:
                    self.batsman_stats[self.current_striker["name"]]["fours"] += 1
                elif runs == 6:
                    self.batsman_stats[self.current_striker["name"]]["sixes"] += 1

            commentary_line += f"{runs} run(s), {outcome['description']}"
            self.commentary.append(commentary_line)

            if runs in [1, 3] and not extra:
                self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
                self.batter_idx.reverse()

            if self.innings == 2 and self.score >= self.target:
                # ✅ UPDATE BOWLER STATS FOR THE MATCH-WINNING BALL
                if not extra:
                    self.current_ball += 1  # Increment ball count for this delivery
                    self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1

                # ✅ ADD THIS: Check if over completed with match-winning ball
                if self.current_ball == 6:
                    if self.current_over_runs == 0:
                        self.bowler_stats[self.current_bowler["name"]]["maidens"] += 1
                    self.bowler_stats[self.current_bowler["name"]]["overs"] += 1

                # Handle extras for bowler stats
                if extra:
                    if "Wide" in outcome['description']:
                        self.bowler_stats[self.current_bowler["name"]]["wides"] += 1
                    elif "No Ball" in outcome['description']:
                        self.bowler_stats[self.current_bowler["name"]]["noballs"] += 1
                    elif "Leg Bye" in outcome['description']:
                        self.bowler_stats[self.current_bowler["name"]]["legbyes"] += 1
                    elif "Byes" in outcome['description']:
                        self.bowler_stats[self.current_bowler["name"]]["byes"] += 1

                scorecard_data = self._generate_detailed_scorecard()
                winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                wkts_left = 10 - self.wickets
                
                # ✅ FIXED CALCULATION: Calculate remaining balls correctly
                total_balls_in_innings = self.overs * 6  # 120 balls for 20 overs
                balls_played_including_this_ball = self.current_over * 6 + self.current_ball  # Now includes the match-winning ball
                balls_left = total_balls_in_innings - balls_played_including_this_ball
                overs_left = balls_left // 6
                balls_left_in_over = balls_left % 6
                overs_left = float(f"{overs_left}.{balls_left_in_over}")

                
                print("Check point1: {}".format({
                        "current_over": self.current_over,
                        "current_ball": self.current_ball,
                        "balls_left": balls_left,
                        "overs_left": overs_left
                    }))
                
                self.result = f"{winner_code} won by {wkts_left} wicket(s) with {overs_left:.1f} overs remaining."
                
                striker_stats = self.batsman_stats[self.current_striker["name"]]
                non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
                bowler_stats = self.bowler_stats[self.current_bowler["name"]]
                
                # ✅ CORRECT OVERS CALCULATION INCLUDING THE MATCH-WINNING BALL
                overs_bowled = bowler_stats["overs"] + (bowler_stats["balls_bowled"] % 6) / 10
                
                extras_str = ""
                if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                    extras_parts = []
                    if bowler_stats["wides"] > 0:
                        extras_parts.append(f"{bowler_stats['wides']}w")
                    if bowler_stats["noballs"] > 0:
                        extras_parts.append(f"{bowler_stats['noballs']}nb")
                    if extras_parts:
                        extras_str = f" ({', '.join(extras_parts)})"

                final_commentary = f"{commentary_line}<br><strong>Match Over!</strong> {self.result}<br><br>"
                final_commentary += f"<strong>Final Stats:</strong><br>"
                final_commentary += f"{self.current_striker['name']}\t\t{striker_stats['runs']}({striker_stats['balls']}b) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}"

                self.innings = 3
                scorecard_data["target_info"] = self.result

                self._save_second_innings_stats()
                self._create_match_archive()

                return {
                    "Test": "Manish6",
                    "match_over": True,
                    "scorecard_data": scorecard_data,
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": self.result,
                    "commentary": final_commentary
                }

        if not extra and not wicket:
            self.current_ball += 1
            self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1

        if extra:
            if "Wide" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["wides"] += 1
            elif "No Ball" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["noballs"] += 1
            elif "Leg Bye" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["legbyes"] += 1
            elif "Byes" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["byes"] += 1

        all_commentary = [commentary_line]
        over_complete = self.current_ball == 6

        if over_complete:
            if self.current_over_runs == 0:
                self.bowler_stats[self.current_bowler["name"]]["maidens"] += 1
            self.bowler_stats[self.current_bowler["name"]]["overs"] += 1
            
            striker_stats = self.batsman_stats[self.current_striker["name"]]
            non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
            bowler_stats = self.bowler_stats[self.current_bowler["name"]]
            
            balls_played = (self.current_over + 1) * 6
            current_rr = (self.score * 6) / balls_played if balls_played > 0 else 0
            
            all_commentary.append(f"<br><strong>End of over {self.current_over + 1}</strong> (Score: {self.score}/{self.wickets}, RR: {current_rr:.2f})<br>")
            

            if self.innings == 2:
                balls_remaining = (self.overs - self.current_over - 1) * 6
                if balls_remaining > 0:
                    required_rr = ((self.target - self.score) * 6) / balls_remaining
                    all_commentary.append(f"Required: {self.target - self.score} runs from {balls_remaining} balls (RRR: {required_rr:.2f})")
            
            extras_str = ""
            if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                extras_parts = []
                if bowler_stats["wides"] > 0:
                    extras_parts.append(f"{bowler_stats['wides']}w")
                if bowler_stats["noballs"] > 0:
                    extras_parts.append(f"{bowler_stats['noballs']}nb")
                if extras_parts:
                    extras_str = f" ({', '.join(extras_parts)})"
            
            all_commentary.append(f"{self.current_striker['name']}\t\t{striker_stats['runs']}({striker_stats['balls']}b) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]")
            all_commentary.append(f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]")
            all_commentary.append(f"{self.current_bowler['name']}\t\t{bowler_stats['overs']:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}")
            all_commentary.append(f"<br>")

            self.current_ball = 0
            self.current_over += 1
            self.current_over_runs = 0
            self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
            self.batter_idx.reverse()

        return {
            "Test": "Manish7",
            "match_over": False,
            "score": self.score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "commentary": "<br>".join(all_commentary),
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": self.current_bowler["name"] if self.current_bowler else ""
        }

    def _generate_detailed_scorecard(self):
        """Generate detailed cricbuzz-style scorecard"""
        
        if self.batting_team == self.home_xi:
            team_name = self.data["team_home"].split("_")[0]
        else:
            team_name = self.data["team_away"].split("_")[0]
        
        players = []

        # Loop through ALL players in batting order, not just those who batted
        # from the _generate_detailed_scorecard function
        for player in self.batting_team:
            player_name = player["name"]
            
            if player_name in self.batsman_stats:
                stats = self.batsman_stats[player_name]
                
                # PRODUCTION FIX: Display full stats if the player has a dismissal type OR has faced balls.
                # This ensures 0-ball ducks are shown correctly.
                if stats.get("wicket_type") or stats.get("balls", 0) > 0:
                    strike_rate = (stats["runs"] * 100) / stats["balls"] if stats["balls"] > 0 else 0
                    status_raw = stats.get("wicket_type") if stats.get("wicket_type") else "not out"
                    status = status_raw
                    
                    if status_raw != "not out":
                        if status_raw == "Caught":
                            status = f"c {stats.get('fielder_out', '?')} b {stats.get('bowler_out', '?')}"
                        elif status_raw == "Bowled":
                            status = f"b {stats.get('bowler_out', '?')}"
                        elif status_raw == "LBW":
                            status = f"lbw b {stats.get('bowler_out', '?')}"
                        elif status_raw == "Run Out":
                            status = f"run out ({stats.get('fielder_out', '?')})"
                        elif status_raw == "Stumped":
                            status = f"st {stats.get('fielder_out', '?')} b {stats.get('bowler_out', '?')}"
                        elif status_raw == "Hit Wicket":
                             status = f"hit wicket b {stats.get('bowler_out', '?')}"
                    
                    players.append({
                        "name": player_name,
                        "status": status,
                        "runs": stats["runs"],
                        "balls": stats["balls"],
                        "fours": stats["fours"],
                        "sixes": stats["sixes"],
                        "strike_rate": f"{strike_rate:.1f}",
                        "bowler_out": stats["bowler_out"],
                        "fielder_out": stats["fielder_out"]
                    })
                else:
                    # Player didn't bat - show with empty stats
                    players.append({
                        "name": player_name,
                        "status": "",
                        "runs": "",
                        "balls": "",
                        "fours": "",
                        "sixes": "",
                        "strike_rate": "",
                        "bowler_out": "",
                        "fielder_out": ""
                    })
            else:
                # Player not in stats - did not bat
                players.append({
                    "name": player_name,
                    "status": "",
                    "runs": "",
                    "balls": "",
                    "fours": "",
                    "sixes": "",
                    "strike_rate": "",
                    "bowler_out": "",
                    "fielder_out": ""
                })

        # Generate bowler stats - all players marked will_bowl
        bowlers = []
        for player in self.bowling_team:
            if player.get("will_bowl", False):
                player_name = player["name"]
                
                if player_name in self.bowler_stats:
                    stats = self.bowler_stats[player_name]
                    
                    # Check if bowler actually bowled
                    if stats["balls_bowled"] > 0 or stats["overs"] > 0:
                        # Calculate economy rate
                        total_balls = stats["overs"] * 6 + (stats["balls_bowled"] % 6)
                        economy = (stats["runs"] * 6) / total_balls if total_balls > 0 else 0
                        overs_display = f"{stats['overs']}.{stats['balls_bowled'] % 6}" if stats['balls_bowled'] % 6 > 0 else str(stats['overs'])
                        
                        bowlers.append({
                            "name": player_name,
                            "overs": overs_display,
                            "maidens": stats["maidens"],
                            "runs": stats["runs"],
                            "wickets": stats["wickets"],
                            "noballs": stats["noballs"],
                            "wides": stats["wides"],
                            "economy": f"{economy:.2f}"
                        })
                    else:
                        # Bowler didn't bowl - empty stats
                        bowlers.append({
                            "name": player_name,
                            "overs": "",
                            "maidens": "",
                            "runs": "",
                            "wickets": "",
                            "noballs": "",
                            "wides": "",
                            "economy": ""
                        })
                else:
                    # Bowler not in stats - didn't bowl
                    bowlers.append({
                        "name": player_name,
                        "overs": "",
                        "maidens": "",
                        "runs": "",
                        "wickets": "",
                        "noballs": "",
                        "wides": "",
                        "economy": ""
                    })

        # Calculate extras
        individual_runs = sum(stats["runs"] for stats in self.batsman_stats.values())
        extras = self.score - individual_runs
        
        total_balls = self.current_over * 6 + self.current_ball
        overs_display = f"{self.current_over}.{self.current_ball}" if self.current_ball > 0 else str(self.current_over)
        run_rate = (self.score * 6) / total_balls if total_balls > 0 else 0

        # Determine target_info based on innings
        target_info_value = None
        if self.innings == 2 and hasattr(self, 'result') and self.result:
            # For 2nd innings end, show the match result
            target_info_value = self.result
        
        return {
            "team_name": team_name,
            "innings": "1st" if self.innings == 1 else "2nd",
            "players": players,
            "bowlers": bowlers,  # ← ADD THIS LINE
            "total_score": self.score,
            "wickets": self.wickets,
            "overs": overs_display,
            "run_rate": f"{run_rate:.2f}",
            "extras": extras,
            "target_info": target_info_value
        }
    
    def _setup_super_over(self):
        """Setup super over after a tie"""
        scorecard_data = self._generate_detailed_scorecard()
        scorecard_data["target_info"] = "Match Tied - Super Over Required!"
        
        return {
            "match_tied": True,
            "super_over_required": True,
            "scorecard_data": scorecard_data,
            "commentary": "<br><strong>MATCH TIED!</strong><br>Super Over Required to decide the winner!<br>",
            "home_team": self.data["team_home"].split("_")[0],
            "away_team": self.data["team_away"].split("_")[0]
        }

    # Modify the start_super_over method:
    def start_super_over(self, first_batting_team):
        """Start the super over with selected team batting first"""
        self.super_over_round += 1
        self.super_over_innings = 1
        
        # Reset scores for this round (but keep history)
        self.super_over_scores = {"home": 0, "away": 0}
        self.super_over_wickets = {"home": 0, "away": 0}
        
        # Determine teams
        if first_batting_team == "home":
            self.super_over_batting_team = self.home_xi
            self.super_over_bowling_team = self.away_xi
        else:
            self.super_over_batting_team = self.away_xi
            self.super_over_bowling_team = self.home_xi
        
        # Select players automatically
        self.super_over_batsmen = self._select_super_over_batsmen(self.super_over_batting_team)
        self.super_over_bowler = self._select_super_over_bowler(self.super_over_bowling_team)
        
        # Initialize super over state
        self.super_over_ball = 0
        self.super_over_current_striker = self.super_over_batsmen[0]
        self.super_over_current_non_striker = self.super_over_batsmen[1]
        self.super_over_batter_idx = [0, 1]
        
        # Stats
        self.super_over_batsman_stats = {
            p["name"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "wicket_type": "", "out": False}
            for p in self.super_over_batsmen
        }
        
        round_text = f"SUPER OVER {self.super_over_round}" if self.super_over_round == 1 else f"SUPER OVER {self.super_over_round} (Previous tied)"
        
        return {
            "super_over_started": True,
            "innings": self.super_over_innings,
            "round": self.super_over_round,
            "batting_team": first_batting_team,
            "batsmen": [p["name"] for p in self.super_over_batsmen],
            "bowler": self.super_over_bowler["name"],
            "commentary": f"<br><strong>{round_text}</strong><br>" +
                        f"Batsmen: {self.super_over_batsmen[0]['name']}, {self.super_over_batsmen[1]['name']}<br>" +
                        f"Bowler: {self.super_over_bowler['name']}<br>"
        }

    def _select_super_over_batsmen(self, team):
        """Select top 3 batsmen by rating, return top 2 for super over"""
        sorted_batsmen = sorted(team, key=lambda p: p["batting_rating"], reverse=True)
        return sorted_batsmen[:2]  # Return top 2 batsmen

    def _select_super_over_bowler(self, team):
        """Select best bowler by rating"""
        bowlers = [p for p in team if p.get("will_bowl", False)]
        return max(bowlers, key=lambda p: p["bowling_rating"])

    def next_super_over_ball(self):
        """Process next ball in super over"""
        if self.super_over_ball >= 6 or self.super_over_wickets[
            "home" if self.super_over_batting_team == self.home_xi else "away"
        ] >= 2:
            return self._end_super_over_innings()
        
        # Calculate outcome using super over logic
        outcome = calculate_super_over_outcome(
            batter=self.super_over_current_striker,
            bowler=self.super_over_bowler,
            pitch=self.pitch,
            streak={},
            over_number=0,
            batter_runs=self.super_over_batsman_stats[self.super_over_current_striker["name"]]["runs"]
        )
        
        runs, wicket, extra = outcome["runs"], outcome["batter_out"], outcome["is_extra"]
        team_key = "home" if self.super_over_batting_team == self.home_xi else "away"
        
        commentary_line = f"Ball {self.super_over_ball + 1}: {self.super_over_bowler['name']} to {self.super_over_current_striker['name']} - "
        
        if wicket:
            self.super_over_wickets[team_key] += 1
            self.super_over_batsman_stats[self.super_over_current_striker["name"]]["wicket_type"] = outcome["wicket_type"]
            self.super_over_batsman_stats[self.super_over_current_striker["name"]]["out"] = True
            
            commentary_line += f"WICKET! {outcome['description']}"
            
            # Check if 2 wickets down
            if self.super_over_wickets[team_key] >= 2:
                commentary_line += "<br><strong>Two wickets down! Super over innings complete!</strong>"
                return {
                    "super_over_ball_complete": True,
                    "wicket": True,
                    "runs": runs,
                    "commentary": commentary_line,
                    "score": self.super_over_scores[team_key],
                    "wickets": self.super_over_wickets[team_key],
                    "ball": self.super_over_ball + 1,
                    "innings_complete": True
                }
            
            # Bring in 3rd batsman
            if len([p for p in self.super_over_batsmen if not self.super_over_batsman_stats[p["name"]]["out"]]) > 0:
                # Get 3rd best batsman
                all_batsmen = sorted(self.super_over_batting_team, key=lambda p: p["batting_rating"], reverse=True)
                third_batsman = all_batsmen[2]  # 3rd best
                self.super_over_batsmen.append(third_batsman)
                self.super_over_batsman_stats[third_batsman["name"]] = {
                    "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "wicket_type": "", "out": False
                }
                self.super_over_current_striker = third_batsman
                commentary_line += f"<br>{third_batsman['name']} comes to bat."
        else:
            self.super_over_scores[team_key] += runs
            commentary_line += f"{runs} run(s). {outcome['description']}"
            
            if not extra:
                self.super_over_batsman_stats[self.super_over_current_striker["name"]]["runs"] += runs
                self.super_over_batsman_stats[self.super_over_current_striker["name"]]["balls"] += 1
                
                if runs == 4:
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["fours"] += 1
                elif runs == 6:
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["sixes"] += 1
                
                # Rotate strike on odd runs
                if runs in [1, 3]:
                    self.super_over_current_striker, self.super_over_current_non_striker = \
                        self.super_over_current_non_striker, self.super_over_current_striker
        
        if not extra:
            self.super_over_ball += 1
        
        # Check if over complete
        over_complete = self.super_over_ball >= 6
        
        return {
            "super_over_ball_complete": True,
            "wicket": wicket,
            "runs": runs,
            "commentary": commentary_line,
            "score": self.super_over_scores[team_key],
            "wickets": self.super_over_wickets[team_key],
            "ball": self.super_over_ball,
            "innings_complete": over_complete or self.super_over_wickets[team_key] >= 2
        }

    def _end_super_over_innings(self):
        """Handle end of super over innings"""
        team_key = "home" if self.super_over_batting_team == self.home_xi else "away"
        
        if self.super_over_innings == 1:
            # Start second innings of this super over
            self.super_over_innings = 2
            self.super_over_batting_team, self.super_over_bowling_team = \
                self.super_over_bowling_team, self.super_over_batting_team
            
            # Reset for second innings
            self.super_over_ball = 0
            self.super_over_batsmen = self._select_super_over_batsmen(self.super_over_batting_team)
            self.super_over_bowler = self._select_super_over_bowler(self.super_over_bowling_team)
            self.super_over_current_striker = self.super_over_batsmen[0]
            self.super_over_current_non_striker = self.super_over_batsmen[1]
            
            # Stats for new innings
            self.super_over_batsman_stats = {
                p["name"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "wicket_type": "", "out": False}
                for p in self.super_over_batsmen
            }
            
            target = self.super_over_scores["home" if team_key == "away" else "away"] + 1
            
            return {
                "super_over_innings_end": True,
                "innings": 2,
                "round": self.super_over_round,
                "target": target,
                "first_innings_score": self.super_over_scores[team_key],
                "batting_team": "away" if team_key == "home" else "home",
                "commentary": f"<br><strong>End of Super Over {self.super_over_round} Innings 1</strong><br>" +
                            f"Target: {target} runs<br>" +
                            f"<strong>SUPER OVER {self.super_over_round} INNINGS 2</strong><br>" +
                            f"Batsmen: {self.super_over_batsmen[0]['name']}, {self.super_over_batsmen[1]['name']}<br>" +
                            f"Bowler: {self.super_over_bowler['name']}<br>"
            }
        else:
            # End of second innings - determine winner or continue
            home_score = self.super_over_scores["home"]
            away_score = self.super_over_scores["away"]
            
            # Store this super over result in history
            self.super_over_history.append({
                "round": self.super_over_round,
                "home_score": home_score,
                "away_score": away_score
            })
            
            if home_score > away_score:
                winner = self.data["team_home"].split("_")[0]
                margin = home_score - away_score
                result = f"{winner} won Super Over {self.super_over_round} by {margin} run(s)"
                
                self.result = result
                self.innings = 5  # Super over complete

                # ✅ Update original scorecard with super over result
                self.original_scorecard["target_info"] = result
                
                return {
                    "super_over_complete": True,
                    "match_over": True,
                    "result": result,
                    "scorecard_data": self.original_scorecard,
                    "round": self.super_over_round,
                    "total_super_overs": self.super_over_round,
                    "home_score": home_score,
                    "away_score": away_score,
                    "commentary": f"<br><strong>SUPER OVER {self.super_over_round} COMPLETE!</strong><br>{result}"
                }
                
            elif away_score > home_score:
                winner = self.data["team_away"].split("_")[0]
                margin = away_score - home_score
                result = f"{winner} won Super Over {self.super_over_round} by {margin} run(s)"
                
                self.result = result
                self.innings = 5  # Super over complete
                
                self._save_second_innings_stats()
                self._create_match_archive()
                
                return {
                    "super_over_complete": True,
                    "match_over": True,
                    "result": result,
                    "round": self.super_over_round,
                    "total_super_overs": self.super_over_round,
                    "home_score": home_score,
                    "away_score": away_score,
                    "commentary": f"<br><strong>SUPER OVER {self.super_over_round} COMPLETE!</strong><br>{result}"
                }
            else:
                # Another tie! Set up next super over
                return {
                    "super_over_tied_again": True,
                    "match_over": False,
                    "round": self.super_over_round,
                    "home_score": home_score,
                    "away_score": away_score,
                    "home_team": self.data["team_home"].split("_")[0],
                    "away_team": self.data["team_away"].split("_")[0],
                    "commentary": f"<br><strong>SUPER OVER {self.super_over_round} TIED!</strong><br>" +
                                f"Score: {home_score}-{away_score}<br>" +
                                f"Another Super Over is required to decide the winner!<br>"
                }
