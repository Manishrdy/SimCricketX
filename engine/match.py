import random
from engine.ball_outcome import calculate_outcome
from engine.super_over_outcome import calculate_super_over_outcome
from match_archiver import MatchArchiver, find_original_json_file

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

    def _create_match_archive_with_frontend_commentary(self):
        """Alternative method called when frontend commentary is captured"""
        return self._create_match_archive()
            

    def _check_for_rain(self):
        """Simple probability-based rain check - only once per match"""
        print(f"🌧️  RAIN CHECK: Over {self.current_over + 1}, Innings {self.innings}")
        
        if self.rain_occurred:  # Rain already happened
            print(f"🌧️  Rain already occurred - skipping")
            return False
            
        # Only check after 5 overs in 1st innings, or any time in 2nd innings
        if self.innings == 1 and self.current_over < 5:
            print(f"🌧️  Too early for rain (over {self.current_over + 1} < 5)")
            return False
        
        rain_roll = random.random()
        will_rain = rain_roll < self.rain_probability
        print(f"🌧️  Rain roll: {rain_roll:.3f} < {self.rain_probability} = {will_rain}")
            
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

    def _pick_death_overs_bowler(self):
        """
        PRE-CALCULATED death overs bowler selection (Overs 18-20)
        Ignores all filters and uses pure mathematical distribution
        """
        print(f"\n🎯 === DEATH OVERS PRE-CALCULATION - Over {self.current_over + 1} ===")
        
        # Get all bowlers and their remaining overs
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        bowler_quota = {}
        
        for bowler in all_bowlers:
            overs_bowled = self.bowler_history.get(bowler["name"], 0)
            overs_remaining = max(0, 4 - overs_bowled)
            bowler_quota[bowler["name"]] = {
                'bowler': bowler,
                'overs_remaining': overs_remaining,
                'overs_bowled': overs_bowled
            }
            print(f"  {bowler['name']}: {overs_bowled}/4 bowled, {overs_remaining} remaining")
        
        # Calculate death overs plan
        death_plan = self._calculate_death_overs_plan(bowler_quota)
        
        # Get current over position in death overs (18=0, 19=1, 20=2)
        death_over_index = self.current_over - 17
        over_names = ["18th", "19th", "20th"]
        
        selected_bowler_name = death_plan[death_over_index]
        selected_bowler = bowler_quota[selected_bowler_name]['bowler']
        
        print(f"🎯 DEATH PLAN SELECTION: {over_names[death_over_index]} over → {selected_bowler_name}")
        print(f"📋 Complete Death Plan: 18th→{death_plan[0]}, 19th→{death_plan[1]}, 20th→{death_plan[2]}")
        
        # Update tracking (since we bypass normal tracking)
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
            print(f"📊 Initialized stats for {selected_bowler['name']}")
        
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
            print(f"  🚨 MATHEMATICAL CONSTRAINT VIOLATION DETECTED!")
            print(f"    {bowler_2_overs} has 2 overs left but bowled over 17 (consecutive issue)")
            print(f"    This scenario should be prevented by better distribution in overs 1-17")
            
            # Emergency resolution: Allow consecutive for mathematical necessity
            return [bowler_1_over, bowler_2_overs, bowler_2_overs]
        
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
        """Emergency plan when mathematical constraints are violated"""
        print(f"  🚨 EMERGENCY DEATH PLAN: filtered remaining_bowlers={remaining_bowlers}")

        # ——— OVERRIDE GUARD ———
        if not remaining_bowlers:
            print("  🔄 No bowlers passed filters; overriding selection protocols.")
            remaining_bowlers = {
                name: info.get("overs_remaining", 0)
                for name, info in self.bowler_quota.items()
                if info.get("overs_remaining", 0) > 0
            }
            print(f"  🔄 After first fallback, remaining_bowlers={remaining_bowlers}")
            if not remaining_bowlers:
                print("  ⚠️ Fallback still empty; using full quota map.")
                remaining_bowlers = {
                    name: info.get("overs_remaining", 0)
                    for name, info in self.bowler_quota.items()
                }
                print(f"  🔄 After full fallback, remaining_bowlers={remaining_bowlers}")
        # ————————————————

        # Just distribute available overs as best as possible
        available_overs = []
        for name, overs in remaining_bowlers.items():
            print(f"  ⚙️ Adding {overs} slots for bowler '{name}'")
            available_overs.extend([name] * overs)
        print(f"  ⚙️ Total available_overs slots={len(available_overs)}")

        # Pad with last bowler if not enough overs (shouldn't happen)
        while len(available_overs) < 3:
            print(f"  🛠 Padding: current slots={len(available_overs)}")
            if available_overs:
                dup = available_overs[-1]
                available_overs.append(dup)
                print(f"  🛠 Appended duplicate of '{dup}'")
            else:
                print("  ❌ No available_overs; selecting first bowler from remaining_bowlers")
                first_bowler = list(remaining_bowlers.keys())[0]
                available_overs.append(first_bowler)
                print(f"  🛠 Appended '{first_bowler}'")

        death_plan = available_overs[:3]
        print(f"  ⚠️ Emergency Plan: over18→{death_plan[0]}, over19→{death_plan[1]}, over20→{death_plan[2]}")
        return death_plan


    def _validate_death_plan(self, death_plan, bowler_quota):
        """Enhanced validation with mathematical constraint checking"""
        print(f"  ✅ Validating Death Plan:")
        
        # Count assignments
        death_assignments = {}
        for bowler_name in death_plan:
            death_assignments[bowler_name] = death_assignments.get(bowler_name, 0) + 1
        
        # Validate quota constraints
        all_valid = True
        for bowler_name, death_overs in death_assignments.items():
            available = bowler_quota[bowler_name]['overs_remaining']
            current_bowled = bowler_quota[bowler_name]['overs_bowled']
            total_after = current_bowled + death_overs
            
            if death_overs > available:
                print(f"  🚨 QUOTA VIOLATION: {bowler_name} assigned {death_overs}, has {available}")
                all_valid = False
            elif total_after > 4:
                print(f"  🚨 TOTAL VIOLATION: {bowler_name} would bowl {total_after}/4 total")
                all_valid = False
            else:
                print(f"  ✅ {bowler_name}: {current_bowled} + {death_overs} = {total_after}/4")
        
        # Check consecutive constraints
        previous_bowler = self.current_bowler["name"] if self.current_bowler else None
        if previous_bowler == death_plan[0]:
            print(f"  ⚠️  CONSECUTIVE: {previous_bowler} bowls over 17→18")
            all_valid = False
        
        for i in range(len(death_plan) - 1):
            if death_plan[i] == death_plan[i + 1]:
                print(f"  ⚠️  CONSECUTIVE: {death_plan[i]} bowls over {18+i}→{18+i+1}")
                all_valid = False
        
        if all_valid:
            print(f"  ✅ Death plan passes all constraints")
        else:
            print(f"  ⚠️  Death plan has constraint violations (may be mathematically necessary)")
        
        return all_valid

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

    def _find_least_overrun_bowlers(self, all_bowlers, quota_analysis):
        """Find bowlers with minimum quota overrun for emergency selection"""
        print(f"    🔍 Finding least overrun bowlers:")
        
        # Find bowlers with minimum overs bowled (even if 4+)
        min_overs = min(quota_analysis[b["name"]]['overs_bowled'] for b in all_bowlers)
        least_overrun = [b for b in all_bowlers if quota_analysis[b["name"]]['overs_bowled'] == min_overs]
        
        print(f"    Minimum overs bowled: {min_overs}")
        print(f"    Bowlers at minimum: {[b['name'] for b in least_overrun]}")
        
        return least_overrun

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

    def _apply_full_strategy_suite(self, constraint_eligible):
        """Apply full strategy suite in normal mode"""
        print(f"  ✅ FULL STRATEGY SUITE:")
        current_eligible = constraint_eligible.copy()
        
        # Phase 2A: Approach 1 Strategy (Middle overs risk management)
        if 6 <= self.current_over < 16:
            print(f"  📊 Applying Approach 1 (Middle overs strategy):")
            approach1_result = self._apply_approach_1_strategy(current_eligible)
            if approach1_result != current_eligible:
                current_eligible = approach1_result
                print(f"    Modified by Approach 1: {[b['name'] for b in current_eligible]}")
            else:
                print(f"    No Approach 1 changes needed")
        
        # Phase 2B: Pattern Strategy
        print(f"  🎯 Applying Pattern Strategy:")
        pattern_result = self._apply_pattern_strategy(current_eligible, self._get_preferred_bowler_type(self.current_over))
        print(f"    After pattern filter: {[b['name'] for b in pattern_result]}")
        current_eligible = pattern_result
        
        # Phase 2C: Secondary Filters (Form, Matchup)
        print(f"  🔧 Applying Secondary Filters:")
        secondary_result = self._apply_secondary_filters(current_eligible)
        print(f"    After secondary filters: {[b['name'] for b in secondary_result]}")
        
        return secondary_result

    
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
        """Apply pattern strategy with comprehensive debugging"""
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
        
        # Safe fallback
        if pattern_bowlers:
            print(f"  ✅ Pattern filter successful")
            return pattern_bowlers
        else:
            print(f"  ⚠️  No bowlers match pattern - using all eligible")
            return eligible_bowlers

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

    def _apply_approach_1_strategy(self, eligible_bowlers):
        """
        Approach 1: Risk-aware middle overs strategy with comprehensive debugging
        """
        print(f"  📊 Approach 1 Analysis:")
        
        # Identify death specialists
        death_specialists = self._identify_death_specialists(eligible_bowlers)
        print(f"  Death specialists available: {[b['name'] for b in death_specialists]}")
        
        # Calculate risk level
        risk_level = self._calculate_death_overs_risk(death_specialists)
        print(f"  Risk level: {risk_level}")
        
        if risk_level == "HIGH_RISK":
            print(f"  🚨 HIGH RISK: Forcing non-death specialists")
            non_specialists = [b for b in eligible_bowlers if not self._is_death_specialist(b)]
            print(f"  Non-specialists: {[b['name'] for b in non_specialists]}")
            
            if non_specialists:
                print(f"  ✅ Returning non-specialists only")
                return non_specialists
            else:
                print(f"  ⚠️  No non-specialists available - using all eligible")
                return eligible_bowlers
        
        elif risk_level == "MEDIUM_RISK":
            print(f"  ⚖️  MEDIUM RISK: Limiting specialist usage")
            specialists_used = self._count_specialists_used_in_middle()
            max_usage = 1
            print(f"  Specialists used in middle: {specialists_used}/{max_usage}")
            
            if specialists_used >= max_usage:
                print(f"  🔒 Quota reached - forcing non-specialists")
                non_specialists = [b for b in eligible_bowlers if not self._is_death_specialist(b)]
                if non_specialists:
                    return non_specialists
            else:
                print(f"  ✅ Can still use specialists")
        
        else:  # LOW_RISK
            print(f"  ✅ LOW RISK: Normal selection allowed")
        
        return eligible_bowlers

    def _identify_death_specialists(self, bowlers):
        """Identify death overs specialists with debugging"""
        specialists = []
        
        print(f"    🔍 Analyzing death specialist criteria:")
        for bowler in bowlers:
            is_fast = self._categorize_bowler(bowler) == "fast"
            high_rating = bowler["bowling_rating"] >= 75
            fast_type = bowler["bowling_type"] in ["Fast", "Fast-medium", "Medium-fast"]
            
            print(f"    {bowler['name']}: fast={is_fast}, rating≥75={high_rating}, fast_type={fast_type}")
            
            if is_fast and high_rating and fast_type:
                specialists.append(bowler)
                print(f"    ✅ {bowler['name']} qualified as death specialist")
        
        return specialists

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

    def _apply_strict_consecutive_policy(self, quota_eligible, risk_assessment):
        """Strictly enforce no-consecutive-overs policy"""
        print(f"  🔒 No-Consecutive Policy Enforcement:")
        
        if not self.current_bowler:
            print(f"    ✅ No previous bowler - all quota-eligible bowlers available")
            return quota_eligible
        
        previous_name = self.current_bowler["name"]
        print(f"    Previous bowler: {previous_name}")
        
        consecutive_eligible = []
        
        for bowler in quota_eligible:
            if bowler["name"] != previous_name:
                consecutive_eligible.append(bowler)
                print(f"    ✅ {bowler['name']}: Available (not consecutive)")
            else:
                print(f"    ❌ {bowler['name']}: BLOCKED (would be consecutive)")
        
        print(f"  Non-consecutive eligible: {len(consecutive_eligible)}/{len(quota_eligible)}")
        
        # Special handling for high-risk scenarios
        if not consecutive_eligible and risk_assessment['emergency_mode']:
            print(f"    🚨 EMERGENCY: No non-consecutive bowlers in high-risk scenario")
            print(f"    📋 Will be handled in emergency constraint resolution")
        
        return consecutive_eligible

    def _handle_constraint_emergency(self, all_bowlers, quota_analysis, risk_assessment):
        """Handle emergency with ABSOLUTE constraint enforcement - NO EXCEPTIONS"""
        print(f"  🚨 CONSTRAINT EMERGENCY HANDLING:")
        print(f"  Risk Level: {risk_assessment['risk_level']}")
        
        # ABSOLUTE RULE: Never allow > 4 overs per bowler
        # ABSOLUTE RULE: Never allow consecutive overs
        
        # Step 1: Find bowlers with < 4 overs (STRICT)
        quota_eligible = []
        for bowler in all_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            if bowler_data['overs_bowled'] < 4:  # STRICT: Must be < 4, not <= 4
                quota_eligible.append(bowler)
        
        print(f"  Bowlers with < 4 overs: {[b['name'] for b in quota_eligible]}")
        
        # Step 2: Remove previous bowler (STRICT)
        if self.current_bowler:
            previous_name = self.current_bowler["name"]
            quota_eligible = [b for b in quota_eligible if b["name"] != previous_name]
            print(f"  After removing consecutive ({previous_name}): {[b['name'] for b in quota_eligible]}")
        
        # Step 3: If we have valid options, return them
        if quota_eligible:
            print(f"  ✅ EMERGENCY RESOLVED: Found valid bowlers")
            return quota_eligible
        
        # Step 4: CRITICAL EMERGENCY - Game cannot continue properly
        print(f"  🚨 CRITICAL: NO VALID BOWLERS - MATCH CONSTRAINT VIOLATED")
        print(f"  📋 This should NEVER happen with proper constraint management")
        
        # Log critical violation
        self._log_constraint_violation("CRITICAL_SYSTEM_FAILURE", "No valid bowlers available")
        
        # EMERGENCY: Return any bowler with exactly 4 overs (least violation)
        emergency_bowlers = [b for b in all_bowlers if quota_analysis[b["name"]]['overs_bowled'] == 4]
        if emergency_bowlers:
            print(f"  🔧 LAST RESORT: Using bowler with exactly 4 overs")
            return emergency_bowlers[:1]  # Return only one option
        
        # ULTIMATE FALLBACK (should never reach here)
        print(f"  💥 SYSTEM FAILURE: Returning first available bowler")
        return all_bowlers[:1]


    def pick_bowler(self):
        """
        Production-level bowler selection with DUAL HIGH-PRIORITY constraints:
        Priority 1A: Strict 4-overs policy (no bowler exceeds 4 overs)
        Priority 1B: No consecutive overs (no bowler bowls back-to-back)
        Priority 2: Strategy optimization (pattern, approach 1, etc.)
        """

        # ================ DEATH OVERS SPECIAL HANDLING ================
        if self.current_over >= 17:  # Overs 18, 19, 20
            print(f"\n🎯 === SWITCHING TO DEATH OVERS MODE ===")
            return self._pick_death_overs_bowler()
        
        # ================ DEBUG: INITIALIZATION ================
        print(f"\n🎳 === BOWLER SELECTION DEBUG - Over {self.current_over + 1} ===")
        print(f"Previous bowler: {self.current_bowler['name'] if self.current_bowler else 'None'}")
        print(f"Match phase: {self._get_match_phase()}")
        
        # Get all available bowlers
        all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        print(f"All bowlers marked will_bowl: {[b['name'] for b in all_bowlers]}")
        
        # ================ QUOTA TRACKING & ANALYSIS ================
        quota_analysis = self._analyze_quota_status(all_bowlers)
        print(f"\n📊 QUOTA ANALYSIS:")
        for bowler_name, data in quota_analysis.items():
            print(f"  {bowler_name}: {data['overs_bowled']}/4 overs ({data['percentage']:.1f}%) - {data['status']}")
        
        # ================ RISK ASSESSMENT ================
        risk_assessment = self._assess_constraint_risk(all_bowlers, quota_analysis)
        print(f"\n⚠️  RISK ASSESSMENT:")
        print(f"  Constraint Risk Level: {risk_assessment['risk_level']}")
        print(f"  Risk Factors: {risk_assessment['risk_factors']}")
        print(f"  Emergency Mode: {risk_assessment['emergency_mode']}")
        
        # ================ PHASE 1: DUAL CONSTRAINT ENFORCEMENT ================
        print(f"\n--- PHASE 1: DUAL CONSTRAINT ENFORCEMENT ---")
        
        # Sub-phase 1A: 4-Overs Policy Enforcement
        quota_eligible = self._apply_strict_quota_policy(all_bowlers, quota_analysis)
        print(f"After 4-overs filter: {[b['name'] for b in quota_eligible]}")
        
        # Sub-phase 1B: No Consecutive Policy Enforcement
        constraint_eligible = self._apply_strict_consecutive_policy(quota_eligible, risk_assessment)
        print(f"After no-consecutive filter: {[b['name'] for b in constraint_eligible]}")
        
        # ================ EMERGENCY CONSTRAINT HANDLING ================
        if not constraint_eligible:
            print(f"\n🚨 EMERGENCY: No bowlers meet both constraints!")
            constraint_eligible = self._handle_constraint_emergency(all_bowlers, quota_analysis, risk_assessment)
            print(f"Emergency resolution: {[b['name'] for b in constraint_eligible]}")
        
        # ================ PHASE 2: STRATEGIC OPTIMIZATION ================
        print(f"\n--- PHASE 2: STRATEGIC OPTIMIZATION ---")
        
        # Check if we should override strategy due to high risk
        if risk_assessment['emergency_mode']:
            print(f"🚨 EMERGENCY MODE: Minimal strategy override only")
            strategic_eligible = self._apply_minimal_strategy_override(constraint_eligible, risk_assessment)
        else:
            print(f"✅ NORMAL MODE: Full strategy application")
            strategic_eligible = self._apply_full_strategy_suite(constraint_eligible)
        
        print(f"After strategic filters: {[b['name'] for b in strategic_eligible]}")
        
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
        return selected_bowler


    def rotate_strike(self):
        self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
        self.batter_idx.reverse()


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
                scorecard_data = self._generate_detailed_scorecard()
                self.first_innings_score = self.score
                self.target = self.score + 1
                required_rr = self.target / self.overs
                chasing_team = self.data["team_away"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_home"].split("_")[0]
                scorecard_data["target_info"] = f"{chasing_team} needs {self.target} runs from {self.overs} overs at {required_rr:.2f} runs per over"
                
                # ADD THIS LINE HERE (before resetting stats):
                self._save_first_innings_stats()

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

                return {
                    "Test": "Manish2",
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

                    # balls_left = (self.overs - self.current_over) * 6 - self.current_ball
                    # overs_left = balls_left / 6

                    # total_balls_in_innings = self.overs * 6  # 120 balls for 20 overs
                    # balls_played_including_this_ball = self.current_over * 6 + self.current_ball  # Now includes the match-winning ball
                    # balls_left = total_balls_in_innings - balls_played_including_this_ball
                    # overs_left = balls_left / 6

                    self.result = f"{winner_code} won by {wkts_left} wicket(s) with {overs_left:.1f} overs remaining."
                else:
                    # Check for tie
                    # if self.score == self.target - 1:
                    if self.score >= self.target - 1:
                        self.result = "Match Tied"
                        # Set up super over
                        self.innings = 4  # Super over mode
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
            print(f"🌧️  Checking for rain...")
            if self._check_for_rain():
                print(f"🌧️  RAIN DETECTED! Handling rain event...")
                rain_result = self._handle_rain_event()
                print(f"🐛 RAIN RESULT: {rain_result}")
                print(f"🐛 RAIN COMMENTARY: {rain_result.get('commentary', 'NO COMMENTARY FOUND')}")
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

        outcome = calculate_outcome(
            batter=self.current_striker,
            bowler=self.current_bowler,
            pitch=self.pitch,
            streak={},
            over_number=self.current_over,
            batter_runs=self.batsman_stats[self.current_striker["name"]]["runs"]
        )

        ball_number = f"{self.current_over}.{self.current_ball + 1}"
        runs, wicket, extra = outcome["runs"], outcome["batter_out"], outcome["is_extra"]
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
            
            fielder_name = None
            
            self.batsman_stats[self.current_striker["name"]]["wicket_type"] = wicket_type
            self.batsman_stats[self.current_striker["name"]]["bowler_out"] = self.current_bowler["name"]
            
            if wicket_type in ["Caught", "Run Out"]:
                fielder_name = self._select_fielder_for_wicket(wicket_type)
                self.batsman_stats[self.current_striker["name"]]["fielder_out"] = fielder_name
            
            commentary_line += self._generate_wicket_commentary(outcome, fielder_name)
            self.commentary.append(commentary_line)

            self.batter_idx[0] = max(self.batter_idx) + 1

            if self.batter_idx[0] >= len(self.batting_team):
                scorecard_data = self._generate_detailed_scorecard()
                self.commentary.append("<br><strong>All Out!</strong>")

                self._save_second_innings_stats()
                self._create_match_archive()

                return {
                    "Test": "Manish5",
                    "match_over": True,
                    "scorecard_data": scorecard_data,
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": f"All out for {self.score}",
                    "commentary": "<br>".join(self.commentary[-2:])  # Include last ball and all-out message
                }

            self.current_striker = self.batting_team[self.batter_idx[0]]
            if self.current_striker["name"] not in self.batsman_stats:
                self.batsman_stats[self.current_striker["name"]] = {
                    "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0, "threes": 0, "dots": 0,
                    "wicket_type": "", "bowler_out": "", "fielder_out": ""
                }
            commentary_line += f"<br><strong>New batsman:</strong> {self.current_striker['name']}<br>"
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

        if not extra:
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


    def _generate_end_of_over_stats(self):
        """Generate end of over statistics display"""
        stats_lines = []
        
        # Team score with run rate
        balls_played = self.current_over * 6 + self.current_ball
        if balls_played > 0:
            current_rr = (self.score * 6) / balls_played
            stats_lines.append(f"<strong>End of over {self.current_over + 1}</strong> (Score: {self.score}/{self.wickets}, RR: {current_rr:.2f})")
        else:
            stats_lines.append(f"<strong>End of over {self.current_over + 1}</strong> (Score: {self.score}/{self.wickets})")
        
        # Required run rate for 2nd innings
        if self.innings == 2:
            balls_remaining = (self.overs - self.current_over - 1) * 6 + (6 - self.current_ball)
            if balls_remaining > 0:
                required_rr = ((self.target - self.score) * 6) / balls_remaining
                stats_lines.append(f"Required: {self.target - self.score} runs from {balls_remaining} balls (RRR: {required_rr:.2f})")
        
        # Current batsmen stats
        striker_stats = self.batsman_stats[self.current_striker["name"]]
        non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
        
        stats_lines.append(f"{self.current_striker['name']}\t\t{striker_stats['runs']}({striker_stats['balls']}b) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]")
        stats_lines.append(f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]")
        
        # Current bowler stats
        bowler_stats = self.bowler_stats[self.current_bowler["name"]]
        overs_bowled = bowler_stats["overs"] + (bowler_stats["balls_bowled"] % 6) / 10
        stats_lines.append(f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}")
        
        return "<br>" + "<br>".join(stats_lines) + "<br>"


    def _generate_end_of_innings_stats(self):
        """Generate end of innings comprehensive statistics"""
        stats_lines = []
        stats_lines.append("<br><strong>=== INNINGS SUMMARY ===</strong>")
        
        # Current batsmen (last pair)
        striker_stats = self.batsman_stats[self.current_striker["name"]]
        non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
        
        stats_lines.append(f"{self.current_striker['name']}\t\t{striker_stats['runs']}({striker_stats['balls']}b) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]")
        stats_lines.append(f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]")
        
        # Last bowler
        if self.current_bowler:
            bowler_stats = self.bowler_stats[self.current_bowler["name"]]
            overs_bowled = bowler_stats["overs"] + (bowler_stats["balls_bowled"] % 6) / 10
            

            # Add extras display for innings end
            extras_str = ""
            if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                extras_parts = []
                if bowler_stats["wides"] > 0:
                    extras_parts.append(f"{bowler_stats['wides']}w")
                if bowler_stats["noballs"] > 0:
                    extras_parts.append(f"{bowler_stats['noballs']}nb")
                if extras_parts:
                    extras_str = f" ({', '.join(extras_parts)})"

            stats_lines.append(f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}")

        return "<br>" + "<br>".join(stats_lines) + "<br>"


    def _generate_detailed_scorecard(self):
        """Generate detailed cricbuzz-style scorecard"""
        
        if self.batting_team == self.home_xi:
            team_name = self.data["team_home"].split("_")[0]
        else:
            team_name = self.data["team_away"].split("_")[0]
        
        players = []

        # Loop through ALL players in batting order, not just those who batted
        for player in self.batting_team:
            player_name = player["name"]
            
            if player_name in self.batsman_stats:
                stats = self.batsman_stats[player_name]
                
                # Check if player actually batted (faced balls or got out)
                if stats["balls"] > 0 or stats["wicket_type"]:
                    strike_rate = (stats["runs"] * 100) / stats["balls"] if stats["balls"] > 0 else 0
                    status = stats["wicket_type"] if stats["wicket_type"] else "not out"
                    
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
                        "status": "-",
                        "runs": "",
                        "balls": "",
                        "fours": "",
                        "sixes": "",
                        "strike_rate": "",
                        "bowler_out": "",
                        "fielder_out": ""
                    })
            else:
                # Player not in stats at all - didn't bat
                players.append({
                    "name": player_name,
                    "status": "-",
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

    def start_super_over(self, first_batting_team):
        """Start the super over with selected team batting first"""
        self.super_over_innings = 1
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
        
        return {
            "super_over_started": True,
            "innings": self.super_over_innings,
            "batting_team": first_batting_team,
            "batsmen": [p["name"] for p in self.super_over_batsmen],
            "bowler": self.super_over_bowler["name"],
            "commentary": f"<br><strong>SUPER OVER {self.super_over_innings}</strong><br>" +
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
            # Start second innings
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
                "target": target,
                "first_innings_score": self.super_over_scores[team_key],
                "batting_team": "away" if team_key == "home" else "home",
                "commentary": f"<br><strong>End of Super Over Innings 1</strong><br>" +
                            f"Target: {target} runs<br>" +
                            f"<strong>SUPER OVER INNINGS 2</strong><br>" +
                            f"Batsmen: {self.super_over_batsmen[0]['name']}, {self.super_over_batsmen[1]['name']}<br>" +
                            f"Bowler: {self.super_over_bowler['name']}<br>"
            }
        else:
            # Determine super over winner
            home_score = self.super_over_scores["home"]
            away_score = self.super_over_scores["away"]
            
            if home_score > away_score:
                winner = self.data["team_home"].split("_")[0]
                margin = home_score - away_score
                result = f"{winner} won the Super Over by {margin} run(s)"
            elif away_score > home_score:
                winner = self.data["team_away"].split("_")[0]
                margin = away_score - home_score
                result = f"{winner} won the Super Over by {margin} run(s)"
            else:
                result = "Super Over also tied! (In real cricket, this would go to another Super Over)"
            
            self.result = result
            self.innings = 5  # Super over complete
            
            self._save_second_innings_stats()
            self._create_match_archive()
             
            return {
                "super_over_complete": True,
                "match_over": True,
                "result": result,
                "home_score": home_score,
                "away_score": away_score,
                "commentary": f"<br><strong>SUPER OVER COMPLETE!</strong><br>{result}"
            }