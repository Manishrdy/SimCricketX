import random
from engine.ball_outcome import calculate_outcome

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

    def pick_bowler(self):
        # Get all bowlers who can bowl
        all_bowlers = [p for p in self.bowling_team if p["will_bowl"]]
        
        # Check if we're in the critical final overs with exactly 5 bowlers
        if len(all_bowlers) == 5 and self.current_over >= 18:
            return self._handle_critical_final_overs(all_bowlers)
        
        # Standard logic: get bowlers with overs left (<4 overs)
        eligible_bowlers = [
            p for p in all_bowlers 
            if self.bowler_history.get(p["name"], 0) < 4
        ]

        # Remove previous bowler to prevent consecutive overs (if possible)
        if self.current_bowler and len(eligible_bowlers) > 1:
            non_consecutive = [
                b for b in eligible_bowlers if b["name"] != self.current_bowler["name"]
            ]
            if non_consecutive:  # Only apply rule if we have alternatives
                eligible_bowlers = non_consecutive

        # Emergency fallback: if no eligible bowlers, allow anyone with overs left
        if not eligible_bowlers:
            eligible_bowlers = [
                p for p in all_bowlers
                if self.bowler_history.get(p["name"], 0) < 4
            ]
            
        # Critical fallback: if still no one (shouldn't happen with proper logic)
        if not eligible_bowlers:
            # Allow the bowler with minimum overs to bowl again
            min_overs = min(self.bowler_history.get(p["name"], 0) for p in all_bowlers)
            eligible_bowlers = [
                p for p in all_bowlers 
                if self.bowler_history.get(p["name"], 0) == min_overs
            ]

        # Prioritize fast bowlers in powerplay (first 6) and death overs (last 4)
        if self.current_over < 6 or self.current_over >= 16:
            fast_types = ["Fast", "Fast-medium", "Medium-fast"]
            fast_bowlers = [
                b for b in eligible_bowlers if b["bowling_type"] in fast_types
            ]
            if fast_bowlers:
                eligible_bowlers = fast_bowlers

        # Select randomly from eligible bowlers
        bowler = random.choice(eligible_bowlers)

        # Track overs
        self.bowler_history[bowler["name"]] = self.bowler_history.get(bowler["name"], 0) + 1

        # Initialize bowler stats if needed
        if bowler["name"] not in self.bowler_stats:
            self.bowler_stats[bowler["name"]] = {
            "runs": 0, "fours": 0, "sixes": 0,
            "wickets": 0, "overs": 0, "maidens": 0, "balls_bowled": 0,
            "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0
        }

        return bowler

    def _handle_critical_final_overs(self, all_bowlers):
        """Handle bowler selection for overs 19-20 when exactly 5 bowlers are available"""
        
        # Calculate remaining overs for each bowler
        remaining_overs = {}
        for bowler in all_bowlers:
            bowled = self.bowler_history.get(bowler["name"], 0)
            remaining_overs[bowler["name"]] = 4 - bowled
        
        # If we're at over 19 (second last over)
        if self.current_over == 18:  # 0-indexed, so 18 = over 19
            # Find bowlers who will have exactly 1 over left after this over
            # These should NOT bowl over 19, so they're available for over 20
            bowlers_for_final = [
                b for b in all_bowlers 
                if remaining_overs[b["name"]] == 1
            ]
            
            # Select from bowlers who have 2+ overs remaining (excluding previous bowler)
            eligible = [
                b for b in all_bowlers 
                if remaining_overs[b["name"]] >= 2 and 
                (not self.current_bowler or b["name"] != self.current_bowler["name"])
            ]
            
            # If no eligible bowlers (edge case), allow previous bowler
            if not eligible:
                eligible = [
                    b for b in all_bowlers 
                    if remaining_overs[b["name"]] >= 1
                ]
                
        else:  # Over 20 (final over)
            # Select from anyone with overs left, preferably not the previous bowler
            eligible = [
                b for b in all_bowlers 
                if remaining_overs[b["name"]] >= 1 and
                (not self.current_bowler or b["name"] != self.current_bowler["name"])
            ]
            
            # If only previous bowler available, allow them (emergency)
            if not eligible:
                eligible = [
                    b for b in all_bowlers 
                    if remaining_overs[b["name"]] >= 1
                ]
        
        # Prioritize fast bowlers for death overs
        fast_types = ["Fast", "Fast-medium", "Medium-fast"]
        fast_bowlers = [b for b in eligible if b["bowling_type"] in fast_types]
        
        return random.choice(fast_bowlers if fast_bowlers else eligible)


    def rotate_strike(self):
        self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
        self.batter_idx.reverse()

    def next_ball(self):
        if self.innings == 3:
            return {
                "test": "Manish1",
                "match_over": True,
                "final_score": self.score,
                "wickets": self.wickets,
                "result": self.result
            }

        if self.current_over >= self.overs or self.wickets >= 10:
            if self.innings == 1:
                # ✅ Generate scorecard data BEFORE resetting
                scorecard_data = self._generate_detailed_scorecard()
                
                # ✅ Set target
                self.first_innings_score = self.score
                self.target = self.score + 1

                # ✅ Calculate required run rate and add to scorecard
                required_rr = self.target / self.overs
                chasing_team = self.data["team_away"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_home"].split("_")[0]
                scorecard_data["target_info"] = f"{chasing_team} needs {self.target} runs from {self.overs} overs at {required_rr:.2f} runs per over"

                # ✅ Reset for 2nd innings
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

                # ✅ Return with scorecard data and innings_end flag
                return {
                    "Test": "Manish2",
                    "innings_end": True,  # New flag to indicate innings end
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
                
                # ✅ Calculate match result
                if self.score >= self.target:
                    winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                    wkts_left = 10 - self.wickets
                    balls_left = (self.overs - self.current_over) * 6 - self.current_ball
                    overs_left = balls_left / 6
                    self.result = f"{winner_code} won by {wkts_left} wicket(s) with {overs_left:.1f} overs remaining."
                else:
                    winner_code = self.data["team_home"].split("_")[0] if self.bowling_team is self.home_xi else self.data["team_away"].split("_")[0]
                    run_diff = self.target - self.score - 1
                    self.result = f"{winner_code} won by {run_diff} run(s)."

                # ✅ Generate final stats for commentary
                striker_stats = self.batsman_stats[self.current_striker["name"]]
                non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
                bowler_stats = self.bowler_stats[self.current_bowler["name"]]
                overs_bowled = bowler_stats["overs"] + (bowler_stats["balls_bowled"] % 6) / 10
                
                # Add extras display for final stats
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

                return {
                    "Test": "Manish4",
                    "innings_end": True,          # ← add this
                    "innings_number": 2,  
                    "match_over": True,
                    "scorecard_data": scorecard_data,  # ✅ SCORECARD INCLUDED
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": self.result,
                    "commentary": final_commentary  # ✅ FINAL STATS INCLUDED
                }

        if self.current_ball == 0:
            self.current_bowler = self.pick_bowler()
            self.commentary.append(
                f"<strong>The New bowler is</strong> {self.current_bowler['name']}<br>"
            )
            if self.current_over == 0:  # Only show striker/non-striker info for the very first over
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

        # Initialize current over runs if not exists
        if not hasattr(self, 'current_over_runs'):
            self.current_over_runs = 0
        if self.current_ball == 0:
            self.current_over_runs = 0

        if wicket:
            self.wickets += 1
            self.bowler_stats[self.current_bowler["name"]]["wickets"] += 1
            
            # ✅ NEW: Enhanced wicket handling with fielder selection
            wicket_type = outcome["wicket_type"]
            fielder_name = None
            
            # Update batsman wicket details
            self.batsman_stats[self.current_striker["name"]]["wicket_type"] = wicket_type
            self.batsman_stats[self.current_striker["name"]]["bowler_out"] = self.current_bowler["name"]
            
            # Select fielder based on wicket type
            if wicket_type in ["Caught", "Run Out"]:
                fielder_name = self._select_fielder_for_wicket(wicket_type)
                self.batsman_stats[self.current_striker["name"]]["fielder_out"] = fielder_name
            elif wicket_type in ["Bowled", "LBW"]:
                # Only bowler involved
                self.batsman_stats[self.current_striker["name"]]["fielder_out"] = ""
            
            # Generate enhanced commentary
            commentary_line = f"{ball_number} {self.current_bowler['name']} to {self.current_striker['name']} - "
            commentary_line += self._generate_wicket_commentary(outcome, fielder_name)

            self.batter_idx[0] = max(self.batter_idx) + 1

            #All out scenario
            if self.batter_idx[0] >= len(self.batting_team):
                # ✅ Generate scorecard for all out
                scorecard_data = self._generate_detailed_scorecard()
                
                self.commentary.append(commentary_line)
                self.commentary.append("<br><strong>All Out!</strong>")
                return {
                    "Test": "Manish5",
                    "match_over": True,
                    "scorecard_data": scorecard_data,  # ✅ ADD SCORECARD
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": f"All out for {self.score}"
                }

            self.current_striker = self.batting_team[self.batter_idx[0]]
            # Initialize new batsman stats
            if self.current_striker["name"] not in self.batsman_stats:
                self.batsman_stats[self.current_striker["name"]] = {
                    "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0, "threes": 0, "dots": 0,
                    "wicket_type": "", "bowler_out": "", "fielder_out": ""
                }
            commentary_line += f"<br><strong>New batsman:</strong> {self.current_striker['name']}"
            commentary_line += f"<br>"

        else:
            self.score += runs
            self.current_over_runs += runs
            self.bowler_stats[self.current_bowler["name"]]["runs"] += runs
            
            # Update batsman stats (only for legal deliveries)
            if not extra:
                self.batsman_stats[self.current_striker["name"]]["runs"] += runs
                self.batsman_stats[self.current_striker["name"]]["balls"] += 1
                
                # Update specific run counts
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

            commentary_line = f"{ball_number} {self.current_bowler['name']} to {self.current_striker['name']} - "
            commentary_line += f"{runs} run(s), {outcome['description']}"

            # Strike rotation logic (only for odd runs and legal deliveries)
            if runs in [1, 3] and not extra:
                self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
                self.batter_idx.reverse()

            if self.innings == 2 and self.score >= self.target:
                # ✅ Generate 2nd innings scorecard
                scorecard_data = self._generate_detailed_scorecard()
                
                # ✅ Calculate match result
                winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                wkts_left = 10 - self.wickets
                balls_left = (self.overs - self.current_over) * 6 - (self.current_ball + 1)
                overs_left = balls_left / 6
                self.result = f"{winner_code} won by {wkts_left} wicket(s) with {overs_left:.1f} overs remaining."
                
                # ✅ Generate final stats for commentary
                striker_stats = self.batsman_stats[self.current_striker["name"]]
                non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
                bowler_stats = self.bowler_stats[self.current_bowler["name"]]
                overs_bowled = bowler_stats["overs"] + (bowler_stats["balls_bowled"] % 6) / 10
                
                final_commentary = f"<br><strong>Match Over!</strong> {self.result}<br><br>"
                final_commentary += f"<strong>Final Stats:</strong><br>"
                final_commentary += f"{self.current_striker['name']}\t\t{striker_stats['runs']}({striker_stats['balls']}b) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_non_striker['name']}\t\t{non_striker_stats['runs']}({non_striker_stats['balls']}b) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]<br>"


                # Add extras display for final stats
                extras_str = ""
                if bowler_stats["wides"] > 0 or bowler_stats["noballs"] > 0:
                    extras_parts = []
                    if bowler_stats["wides"] > 0:
                        extras_parts.append(f"{bowler_stats['wides']}w")
                    if bowler_stats["noballs"] > 0:
                        extras_parts.append(f"{bowler_stats['noballs']}nb")
                    if extras_parts:
                        extras_str = f" ({', '.join(extras_parts)})"

                final_commentary += f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}"

                
                self.innings = 3
                return {
                    "Test": "Manish6",
                    "match_over": True,
                    "scorecard_data": scorecard_data,  # ✅ ADD SCORECARD
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": self.result,
                    "commentary": final_commentary  # ✅ ADD FINAL STATS
                }

        self.commentary.append(commentary_line)

        # Only increment ball count for legal deliveries
        if not extra:
            self.current_ball += 1
            self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1

        if extra:
            # Track different types of extras for bowlers
            if "Wide" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["wides"] += 1
            elif "No Ball" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["noballs"] += 1
            elif "Leg Bye" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["legbyes"] += 1
            elif "Byes" in outcome['description']:
                self.bowler_stats[self.current_bowler["name"]]["byes"] += 1

        # Collect all commentary for this ball
        all_commentary = [commentary_line]

        # Check if over is complete BEFORE resetting current_ball
        over_complete = self.current_ball == 6

        if over_complete:
            # Check for maiden over
            if self.current_over_runs == 0:
                self.bowler_stats[self.current_bowler["name"]]["maidens"] += 1
                
            # Update bowler overs
            self.bowler_stats[self.current_bowler["name"]]["overs"] += 1
            
            # Get current stats BEFORE rotating strike
            striker_stats = self.batsman_stats[self.current_striker["name"]]
            non_striker_stats = self.batsman_stats[self.current_non_striker["name"]]
            bowler_stats = self.bowler_stats[self.current_bowler["name"]]
            
            # Calculate bowler overs in decimal format
            overs_bowled = bowler_stats["overs"]
            
            # Add run rate calculation
            balls_played = (self.current_over + 1) * 6  # +1 because over just completed
            current_rr = (self.score * 6) / balls_played if balls_played > 0 else 0
            
            # Build end-of-over stats
            all_commentary.append(f"<br><strong>End of over {self.current_over + 1}</strong> (Score: {self.score}/{self.wickets}, RR: {current_rr:.2f})<br>")
            
            # Required run rate for 2nd innings
            if self.innings == 2:
                balls_remaining = (self.overs - self.current_over - 1) * 6
                if balls_remaining > 0:
                    required_rr = ((self.target - self.score) * 6) / balls_remaining
                    all_commentary.append(f"Required: {self.target - self.score} runs from {balls_remaining} balls (RRR: {required_rr:.2f})")
            
            # Add extras display for end-of-over
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
            all_commentary.append(f"{self.current_bowler['name']}\t\t{overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}")
            all_commentary.append(f"<br>")

            # Reset for next over
            self.current_ball = 0
            self.current_over += 1
            self.current_over_runs = 0
            self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
            self.batter_idx.reverse()

        return {
            "Test": "Manish7",
            "match_over": False,
            "score": self.score,  # ✅ ADD MISSING SCORE
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "commentary": "<br>".join(all_commentary),
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": self.current_bowler["name"] if self.current_bowler else ""
            # ✅ REMOVE scorecard_data from here - only for innings end
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
            "target_info": None
        }