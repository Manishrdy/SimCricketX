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

        self.batsman_stats = {p["name"]: {"runs":0,"balls":0,"fours":0,"sixes":0} for p in self.batting_team}
        self.bowler_stats = {}

        self.current_striker = self.batting_team[0]
        self.current_non_striker = self.batting_team[1]
        self.current_bowler = None


    def pick_bowler(self):
    # First, get all bowlers with overs left (<4 overs)
        eligible_bowlers = [
            p for p in self.bowling_team 
            if p["will_bowl"] and self.bowler_history.get(p["name"], 0) < 4
        ]

        # If exactly 5 bowlers, strictly ensure each bowls exactly 4 overs
        if len([p for p in self.bowling_team if p["will_bowl"]]) == 5:
            eligible_bowlers = [
                p for p in eligible_bowlers
                if self.bowler_history.get(p["name"], 0) < 4
            ]

        # If previous bowler bowled last over, remove him to prevent consecutive overs
        if self.current_bowler:
            eligible_bowlers = [
                b for b in eligible_bowlers if b["name"] != self.current_bowler["name"]
            ]

        # In the very rare scenario, if no one else available, fallback to anyone eligible
        if not eligible_bowlers:
            eligible_bowlers = [
                p for p in self.bowling_team
                if p["will_bowl"] and self.bowler_history.get(p["name"], 0) < 4
            ]

        # Prioritize fast bowlers in first 3 and last 3 overs
        if self.current_over < 3 or self.current_over >= 17:
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

        if bowler["name"] not in self.bowler_stats:
            self.bowler_stats[bowler["name"]] = {
                "runs": 0, "fours": 0, "sixes": 0,
                "wickets": 0, "overs": 0, "maidens": 0
            }

        return bowler


    def rotate_strike(self):
        self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
        self.batter_idx.reverse()

    def next_ball(self):
        if self.innings == 3:
            return {
                "match_over": True,
                "final_score": self.score,
                "wickets": self.wickets,
                "result": self.result
            }

        if self.current_over >= self.overs or self.wickets >= 10:
            if self.innings == 1:
                self.first_innings_score = self.score
                self.target = self.score + 1
                self.commentary.append(
                    f"<br><strong>End Innings 1:</strong> {self.score}/{self.wickets}. Target: {self.target}."
                )

                self.innings = 2
                self.batting_team, self.bowling_team = self.bowling_team, self.batting_team
                self.score = 0
                self.wickets = 0
                self.current_over = 0
                self.current_ball = 0
                self.batter_idx = [0, 1]

                self.current_striker = self.batting_team[0]
                self.current_non_striker = self.batting_team[1]

                self.batsman_stats = {p["name"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0} for p in self.batting_team}
                self.bowler_history = {}
                self.bowler_stats = {p["name"]: {"runs": 0, "fours": 0, "sixes": 0, "wickets": 0, "overs": 0, "maidens": 0} for p in self.bowling_team if p["will_bowl"]}

                self.commentary.append(f"<br><strong>2nd Innings Begins:</strong> Target is {self.target} runs.")

            else:
                if self.score >= self.target:
                    winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                    wkts_left = 10 - self.wickets
                    self.result = f"{winner_code} won by {wkts_left} wicket(s)."
                else:
                    winner_code = self.data["team_home"].split("_")[0] if self.bowling_team is self.home_xi else self.data["team_away"].split("_")[0]
                    run_diff = self.target - self.score - 1
                    self.result = f"{winner_code} won by {run_diff} run(s)."

                self.commentary.append(f"<br><strong>Match Over!</strong> {self.result}")
                self.innings = 3
                return {
                    "match_over": True,
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": self.result
                }

        if self.current_ball == 0:
            self.current_bowler = self.pick_bowler()
            self.commentary.append(
                f"🎳 <strong>The New bowler is</strong> {self.current_bowler['name']} ({self.current_bowler['bowling_type']})<br>"
            )
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

        commentary_line = f"{ball_number} {self.current_bowler['name']} to {self.current_striker['name']} - "

        if wicket:
            self.wickets += 1
            self.bowler_stats[self.current_bowler["name"]]["wickets"] += 1
            commentary_line += f"Wicket! {outcome['description']}"

            self.batter_idx[0] = max(self.batter_idx) + 1
            if self.batter_idx[0] >= len(self.batting_team):
                self.commentary.append(commentary_line)
                self.commentary.append("<br><strong>All Out!</strong>")
                return {"match_over": True, "final_score": self.score, "wickets": self.wickets, "result": f"All out for {self.score}"}

            self.current_striker = self.batting_team[self.batter_idx[0]]
            commentary_line += f"<br><strong>New batsman:</strong> {self.current_striker['name']}"

        else:
            self.score += runs
            self.bowler_stats[self.current_bowler["name"]]["runs"] += runs
            self.batsman_stats[self.current_striker["name"]]["runs"] += runs
            self.batsman_stats[self.current_striker["name"]]["balls"] += 1

            if runs == 4:
                self.batsman_stats[self.current_striker["name"]]["fours"] += 1
            if runs == 6:
                self.batsman_stats[self.current_striker["name"]]["sixes"] += 1

            commentary_line += f"{runs} run(s), {outcome['description']}"

            if runs in [1, 3]:
                self.rotate_strike()

            if self.innings == 2 and self.score >= self.target:
                winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                wkts_left = 10 - self.wickets
                self.result = f"{winner_code} won by {wkts_left} wicket(s)."
                self.commentary.append(f"<br><strong>Match Over!</strong> {self.result}")
                self.innings = 3
                return {"match_over": True, "final_score": self.score, "wickets": self.wickets, "result": self.result}

        self.commentary.append(commentary_line)
        self.current_ball += 1

        if self.current_ball == 6:
            # record that this bowler has now completed one full over
            self.bowler_stats[self.current_bowler["name"]]["overs"] += 1
            # reset for next over
            self.current_ball = 0
            self.current_over += 1
            self.rotate_strike()

        # if self.current_ball >= 6:
        #     self.current_ball = 0
        #     self.current_over += 1
        #     self.bowler_stats[self.current_bowler["name"]]["overs"] += 1
        #     self.rotate_strike()

        # If it's ball 1 of overs 1+, prefix the new‐bowler intro
        comment_out = commentary_line
        if self.current_ball == 1 and self.current_over > 0:
            intro = f"<strong>The New bowler is</strong> {self.current_bowler['name']}<br><br>"
            comment_out = intro + comment_out

        return {
            "match_over": False,
            "score": self.score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "commentary": comment_out,
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": self.current_bowler["name"]
        }
