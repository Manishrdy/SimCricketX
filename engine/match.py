import builtins
import copy
import dataclasses
import logging
import math
import os
import random
import sys
import time
from engine import dls
from engine import weather as weather_engine
from engine.ball_outcome import calculate_outcome
from engine.super_over_outcome import calculate_super_over_outcome
from engine.cricket_math import balls_to_overs_str
from match_archiver import MatchArchiver, find_original_json_file
from engine.pressure_engine import PressureEngine, FCPressureEngine
from engine.game_state_engine import (
    compute_game_state_vector,
    apply_game_state_to_probs,
    make_ball_event,
    get_par_score,
    BALL_HISTORY_WINDOW,
)
from engine.format_config import get_format, get_any_format
from engine.bowler_manager import BowlerManager
from engine.fc_bowler_workload import FCBowlerManager
from engine import fc_bowler_workload
from engine import fc_declaration
from engine import fc_weather
from engine.toss import innings_teams
from utils.exception_tracker import log_exception


logger = logging.getLogger(__name__)
_MATCH_DEBUG_PRINTS = os.getenv("SIMCRICKET_DEBUG_PRINTS", "").strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Feature 1: Phase-specific bowling effectiveness multipliers.
# Keys are normalised bowling-type groups: 'pace', 'medium', 'spin', 'default'.
# ---------------------------------------------------------------------------
_POWERPLAY_BOWLING_MULT: dict = {
    "pace":    1.12,   # Pacers excel with new ball in powerplay
    "medium":  1.05,
    "spin":    0.88,   # Spinners less effective in first 6
    "default": 1.00,
}
_DEATH_BOWLING_MULT: dict = {
    "pace":    1.10,   # Pacers effective at death with yorkers/bouncers
    "medium":  1.03,
    "spin":    0.90,   # Spinners more hittable at death
    "default": 1.00,
}
_MIDDLE_BOWLING_MULT: dict = {
    "spin":    1.08,   # Middle overs are the spinner's playground
    "medium":  1.03,
    "pace":    0.97,
    "default": 1.00,
}

# Feature 2: Bowler spell fatigue — diminishing effectiveness over overs bowled.
_FATIGUE_MULT: dict = {0: 1.00, 1: 1.00, 2: 0.99, 3: 0.97, 4: 0.94}

# Feature 7: Correct toss choice for each pitch type.
_CORRECT_TOSS_CHOICE: dict = {
    "Green": "bowl",   # Seam/swing friendly → bowl first
    "Dry":   "bat",    # Spin worsens with wear → bat first
    "Hard":  "bat",    # Good batting surface → bat first
    "Flat":  "bowl",   # Will be a run-fest, dew helps chaser → bowl first
    "Dead":  "bowl",   # Extreme batting pitch, chaser advantaged → bowl first
}

# Guard console output on Windows consoles that choke on emoji/unicode.
from engine.commentary_engine import CommentaryEngine

def safe_print(*args, **kwargs):
    # Suppress high-volume simulation debug output in production by default.
    # Enable with `SIMCRICKET_DEBUG_PRINTS=1` when deep tracing is needed.
    if not _MATCH_DEBUG_PRINTS:
        return
    try:
        builtins.print(*args, **kwargs)
    except OSError:
        log_exception(source="backend")
        sanitized = []
        for arg in args:
            if isinstance(arg, str):
                sanitized.append(arg.encode("ascii", "ignore").decode())
            else:
                sanitized.append(arg)
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("file", None)
        fallback_stream = getattr(sys, "__stderr__", None) or getattr(sys, "stderr", None)
        if fallback_stream is not None:
            fallback_kwargs["file"] = fallback_stream
        try:
            builtins.print(*sanitized, **fallback_kwargs)
        except OSError:
            try:
                builtins.print(*sanitized)
            except OSError:
                # Final fallback: avoid cascading failures from broken streams.
                pass

# Override module-level print to the safe version
print = safe_print


class Match:
    def __init__(self, match_data):
        created_at = match_data.get("created_at")
        if not isinstance(created_at, (int, float)):
            created_at = time.time()
            match_data["created_at"] = created_at
        self.created_at = created_at
        self.innings = 1
        self.first_innings_score = None
        self.target = None
        self.data = match_data
        self.simulation_mode = match_data.get("simulation_mode", "auto")
        self.pending_decision = None
        self.pitch = match_data["pitch"]
        self.stadium = match_data["stadium"]
        self.ground_config = match_data.get("ground_config")  # per-user snapshot; None for legacy matches
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
        self.batting_team, self.bowling_team = innings_teams(
            self.toss_winner, self.toss_decision, team_home,
            self.home_xi, self.away_xi, innings=1,
        )

        # Load format config (T20 by default for backward compatibility).
        # A per-match copy is taken because rain revisions mutate overs and
        # bowler quotas — the FORMAT_REGISTRY/MULTIDAY_FORMAT_REGISTRY
        # singletons are shared across every concurrent match and must
        # never be modified.
        self.fmt = get_any_format(match_data.get("match_format", "T20"), days=match_data.get("days"))
        # First-Class (multi-day) matches take an entirely separate code
        # path through next_ball() — see the is_fc branches below and in
        # _innings_should_end()/_transition_to_next_innings(). self.innings
        # (1/2/3/4/5) carries overloaded super-over semantics that FC must
        # never touch; FC's own innings counter is self.fc_innings (1-4).
        self.is_fc = (self.fmt.format_family == "multi_day")

        # Feature 7: compute toss × conditions advantage once at match start.
        # D/N matches: dew in the 2nd innings tilts the optimal choice towards
        # bowling first on almost every pitch.  Use the D/N override dict when
        # available; fall back to the standard pitch-only dict for day games.
        _is_dn = bool(match_data.get("is_day_night", False))
        _dn_choices = getattr(self.fmt, "correct_toss_choice_dn", None)
        if _is_dn and _dn_choices:
            _correct_choice = _dn_choices.get(self.pitch, "bowl")
        else:
            _correct_choice = self.fmt.correct_toss_choice.get(self.pitch, "bat")
        # toss_decision stored as 'Bat' or 'Bowl'; correct_choice is lowercase.
        self._toss_choice_correct = (self.toss_decision or "").lower() == _correct_choice
        # Track which XI won the toss (used in next_ball for per-innings check).
        if self.toss_winner == team_home:
            self._toss_winner_xi = self.home_xi
        else:
            self._toss_winner_xi = self.away_xi

        # BowlerManager owns quota + consecutive-over enforcement for
        # T20/ListA; FC has no quota at all (see FCBowlerManager's
        # docstring) so it gets a genuinely separate, simpler class rather
        # than a branch inside BowlerManager — BowlerManager's fatigue
        # table indexes by fmt.max_bowler_overs, which doesn't exist on
        # MultiDayFormatConfig and would KeyError on the very first lookup.
        if self.is_fc:
            self.bowler_manager = FCBowlerManager(self.bowling_team, self.fmt)
            self.bowler_history = self.bowler_manager._overs_this_innings
            # No fixed innings-overs concept in FC; a nominal ceiling
            # (days * overs_per_day) keeps any shared code that happens to
            # read self.overs (arithmetic/formatting) from crashing on
            # None. It is NOT the real innings-end gate for FC — that's
            # self._fc_innings_should_end() (day/match exhaustion,
            # declaration, all-out), which never reads self.overs.
            self.overs = self.fmt.days * self.fmt.overs_per_day
        else:
            self.bowler_manager = BowlerManager(self.bowling_team, self.fmt)
            # Keep bowler_history as a live alias into BowlerManager's quota
            # dict so existing read-only usages remain valid without a
            # large refactor.
            self.bowler_history = self.bowler_manager._quota
            self.overs = self.fmt.overs
        self.current_over = 0
        self.current_ball = 0

        # ── Rain / DLS state ─────────────────────────────────────────────
        # The weather script is rolled once (at match creation by the setup
        # route; fallback here for older payloads and tests) and stored in
        # match_data so a resumed match replays identically. engine/weather.py's
        # script generator is hardcoded to a 2-innings match and has no DLS
        # equivalent for FC, so self.weather_script stays an inert placeholder
        # for FC (no shared T20/ListA code path crashes if it happens to read
        # it) — FC's own day-aware weather model is the separate
        # fc_weather_script below (engine/fc_weather.py).
        self.original_overs = self.overs
        self.weather_forecast = match_data.get("weather_forecast", weather_engine.DEFAULT_FORECAST)
        if self.is_fc:
            match_data.setdefault("weather_script", {"forecast": self.weather_forecast, "events": []})
            if match_data.get("fc_weather_script") is None:
                match_data["fc_weather_script"] = fc_weather.generate_weather_script(
                    self.weather_forecast, self.fmt.days, self.fmt.overs_per_day,
                    min_overs_last_hour=self.fmt.min_overs_last_hour,
                )
            self.fc_weather_script = match_data["fc_weather_script"]
        elif match_data.get("weather_script") is None:
            match_data["weather_script"] = weather_engine.generate_weather_script(
                self.weather_forecast, self.fmt.overs, self.fmt.name
            )
        self.weather_script = match_data["weather_script"]
        self.weather_next_event = 0          # index of next unconsumed script event
        self.rain_affected = False
        self.rain_events_log = []            # applied interruptions (UI + archive)
        self._innings1_overs_bowled = None   # actual completed overs of innings 1
        self._pending_rain_info = None       # rain info to attach to next response
        self.dls_ledger_innings1 = None if self.is_fc else dls.ResourceLedger(self.fmt.overs)
        self.dls_ledger_innings2 = None      # created when the chase is set up
        self.batter_idx = [0, 1]
        self.score = 0
        self.wickets = 0
        self.commentary = []

        # Initialize comprehensive stats
        self.batsman_stats = {p["name"]: self._new_batting_stats(p) for p in self.batting_team}
        self.current_over_runs = 0
        self.current_over_outcomes = []
        self.bowler_stats = {p["name"]: self._new_bowling_stats(p) for p in self.bowling_team if p["will_bowl"]}

        self.current_striker = self.batting_team[0]
        self.current_non_striker = self.batting_team[1]
        self.current_bowler = None
        self.bowler_selected_for_over = -1
        self.remaining_batter_indices = set(range(2, len(self.batting_team)))
        # Add bowling pattern detection
        self.bowling_pattern = self._detect_bowling_pattern()

        self.over_bowler_log = {}
        # ListA-only bowling distribution plan (recomputed per innings/roster)
        self.lista_bowler_plan = {}
        self.lista_plan_innings = None
        self.lista_plan_roster = ()
        self.prev_delivery_was_extra = False
        self.current_over_maiden_invalid = False  # A2: only bat-runs, wides, no-balls invalidate maidens
        self.free_hit_active = False  # A5: free hit after no-ball
        
        # Bug Fix B1: Initialize wicket tracking variables (moved from after return in _format_scorecard_block)
        self.recent_wickets_count = 0  # Track wickets in last few balls
        self.recent_wickets_tracker = []  # Track when wickets fell

        # Streak tracking: consecutive boundaries per batter (activates boundary penalty + wicket boost)
        self.batter_streaks = {}  # {batter_name: {"boundaries": int}}

        # GSME: rolling 18-ball delivery history for game-state momentum engine
        self.ball_history: list = []  # list of dicts; max BALL_HISTORY_WINDOW entries

        # Feature 3: count of legal + extra deliveries bowled this innings
        # (used to compute pitch_wear = innings_balls_bowled / 120.0)
        self.innings_balls_bowled: int = 0

        # FC only: count of legal + extra deliveries bowled in the WHOLE
        # MATCH, never reset by _reset_innings_state() (unlike
        # innings_balls_bowled above) — this is the entire point of FC
        # pitch wear being continuous across all 4 innings instead of
        # resetting at each innings break.
        self.match_balls_bowled: int = 0

        # FC only (Phase 2): overs bowled with the CURRENT ball this
        # innings — independent of pitch wear/type. Resets at the start of
        # every innings (a new ball is always issued) and whenever the
        # bowling side takes the new ball at fmt.new_ball_overs. Drives
        # engine/ground_config.py's get_fc_ball_condition_factor() (new-ball
        # swing, then reverse-swing for genuine pace just before the next
        # new ball is due) via calculate_outcome()'s ball_overs_bowled param.
        self.fc_ball_overs_bowled: int = 0

        # Feature 8: runs conceded per over by each bowler (keyed by name)
        # populated at over completion; read in _get_effective_bowler_dict()
        self.bowler_prev_over_runs: dict = {}


        # ===== NEW ARCHIVING VARIABLES =====
        self.match_data = match_data  # Store original match data
        
        # Explicitly store all 4 innings stats
        self.first_innings_batting_stats = {}   # First batting team's batting stats
        self.first_innings_bowling_stats = {}   # First bowling team's bowling stats  
        self.second_innings_batting_stats = {}  # Second batting team's batting stats
        self.second_innings_bowling_stats = {}  # Second bowling team's bowling stats
        
        # Partnership storage
        self.first_innings_partnerships = []    # List of partnership dicts for 1st innings
        self.second_innings_partnerships = []   # List of partnership dicts for 2nd innings
        
        # Track which team batted first for correct CSV naming
        self.first_batting_team_name = ""  # e.g., "CSK" 
        self.first_bowling_team_name = ""  # e.g., "DC"
        
        self.result = ""  # Store final match result

        # Structured outcome, captured alongside self.result at the same
        # sites instead of being re-derived later by parsing that prose.
        # winner_is_home: True/False/None (None = tie/no-result/not yet decided)
        self.winner_is_home = None
        self.match_status = None    # 'completed'|'tied'|'no_result'|'aborted'
        self.margin_type = None     # 'runs'|'wickets'|'tie'|'boundary_count'
        self.margin_value = None

        self.pending_pre_ball_commentary = []
        self._second_innings_stats_saved = False
        self._archive_created = False

        # Add super over tracking variables
        self.super_over_round = 0  # Track which super over we're on
        self.super_over_history = []  # Track scores from each super over
        # Resume support: a phase string describing where the super over is, so a
        # page refresh can rebuild the correct UI (see get_super_over_resume_state).
        self.super_over_phase = None
        # Cumulative super-over data that must persist ACROSS rounds/innings:
        #   - team boundaries → 5-round tie boundary count-back
        #   - per-team/per-player batting & bowling → career-stat aggregation
        self.super_over_team_boundaries = {"home": 0, "away": 0}
        self.super_over_career_batting = {"home": {}, "away": {}}
        self.super_over_career_bowling = {"home": {}, "away": {}}
        # This round's own ball-by-ball history (micro-GSME momentum input)
        # and the pitch wear frozen from the main match at tie time.
        self.super_over_ball_history = []
        self.super_over_pitch_wear = 0.0
        self.constraint_violations = []  # Constraint violation log for post-match analysis

        # Initialize pressure engine (format_config wires phase thresholds + RRs).
        # FCPressureEngine is a genuinely separate class (session-survival +
        # lead-building pressure, not run-rate-chase pressure) — see
        # engine/pressure_engine.py's module docstring for the "why".
        self.pressure_engine = FCPressureEngine(format_config=self.fmt) if self.is_fc \
            else PressureEngine(format_config=self.fmt)

        # ── First-Class (FC) match state ─────────────────────────────────
        # fc_innings (1-4) is completely separate from self.innings, which
        # carries overloaded super-over semantics next_ball() must never
        # confuse with a real 3rd/4th innings — see is_fc dispatch above.
        self.fc_innings = 1
        self.fc_day = 1
        self.fc_day_overs_bowled_today = 0
        # Sessions: a first-class day is three sessions (morning to Lunch,
        # afternoon to Tea, evening to Stumps), not one flat 90-over block.
        # fc_sessions_taken_today counts the intervals ALREADY taken today
        # (0-2), so the session in progress is that + 1.
        self.fc_sessions_taken_today = 0
        self.fc_session_start = {"score": 0, "wickets": 0, "day_overs": 0,
                                 "fc_innings": 1}
        # None until first computed for the day in progress (the bowling
        # XI isn't settled yet at this point in __init__).
        self.fc_day_over_rate_adjust = None
        # One nightwatchman per innings — a captain does not keep promoting
        # bowlers every time a wicket falls late.
        self.fc_nightwatchman_used = False
        self.fc_nightwatchman_name = None
        # Maidens are commonplace in first-class cricket, so the commentary
        # engine remarks on a RUN of them rather than each one.
        self.fc_consecutive_maidens = 0
        self.fc_innings_declared = False
        # Per-innings time budget (overs) for time-forcing declaration
        # pressure — see fc_declaration.declaration_window_open/
        # should_declare. Captured once here for innings 1, since
        # _fc_start_next_innings() (which captures it for innings 2-4) is
        # never called for the first innings. None for non-FC matches,
        # where fmt has no days/overs_per_day and this is never read anyway.
        self.fc_innings_time_budget_overs = (
            fc_declaration.compute_innings_time_budget_overs(self._fc_overs_remaining_in_match())
            if self.is_fc else None
        )
        self.follow_on_enforced = None  # None until decided; then True/False
        # User-captained mode only (see _fc_check_declaration_and_follow_on):
        # the over the captain last declined to declare at, so the "ask
        # again" prompt doesn't re-fire every ball for the rest of that same
        # over boundary once answered. Reset by the over actually advancing.
        self._fc_declined_declare_over = None
        # Per-innings totals, keyed 1-4, populated at each innings break:
        # {"score": int, "wickets": int, "overs_str": str, "side": "home"|"away"}
        self.fc_innings_totals = {}
        # Accumulator the archiver loops over (2-4 entries depending on how
        # the match actually played out) — see match_archiver.py's
        # generalized innings_plan for FC, which replaces the hardcoded
        # 2-tuple T20/ListA use.
        self.fc_innings_stats = []
        # Partnerships keyed by fc_innings number (1-4) — see _save_partnership,
        # which routes here instead of first_/second_innings_partnerships
        # because self.innings stays frozen at 1 for the whole FC match.
        self.fc_innings_partnerships = {}

        # Track partnership for pressure calculation AND archiving
        self.current_partnership_balls = 0
        self.current_partnership_runs = 0
        self.current_partnership_start_over = 0.0
        self.current_partnership_contributions = {
            'batsman1': {'name': '', 'runs': 0, 'balls': 0},
            'batsman2': {'name': '', 'runs': 0, 'balls': 0}
        }

        # Initialize Commentary Engine
        self.commentary_engine = CommentaryEngine()

        # Initialize Scenario Engine (if scenario_mode is set)
        self.scenario_mode = match_data.get("scenario_mode", None)
        self.scenario_engine = None
        if self.scenario_mode:
            from engine.scenario_engine import create_scenario_engine
            self.scenario_engine = create_scenario_engine(self.scenario_mode, self)


    def _get_team_name(self, team_list):
        if team_list is self.home_xi:
            return self.data["team_home"].split("_")[0]
        return self.data["team_away"].split("_")[0]

    @staticmethod
    def _balls_to_overs_notation(total_balls):
        return balls_to_overs_str(total_balls)

    def _set_outcome(self, *, result_text, winner_is_home, match_status, margin_type, margin_value):
        """Set self.result plus the structured fields describing the same
        outcome, captured at the same call site instead of being re-derived
        later by parsing result_text. Does not compose result_text itself —
        callers already build it differently per branch (chase vs. defend
        vs. super over vs. boundary tiebreak)."""
        self.result = result_text
        self.winner_is_home = winner_is_home
        self.match_status = match_status
        self.margin_type = margin_type
        self.margin_value = margin_value

    @staticmethod
    def _new_batting_stats(player=None):
        return {
            "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "ones": 0, "twos": 0,
            "threes": 0, "dots": 0, "wicket_type": "", "bowler_out": "", "fielder_out": "",
            "form": 1.0, "id": player.get("id") if player else None,
        }

    @staticmethod
    def _new_bowling_stats(player=None):
        return {
            "runs": 0, "fours": 0, "sixes": 0, "wickets": 0, "overs": 0, "maidens": 0,
            "balls_bowled": 0, "wides": 0, "noballs": 0, "byes": 0, "legbyes": 0,
            "id": player.get("id") if player else None,
        }

    def _balls_left_in_innings(self):
        total_balls_in_innings = self.overs * 6
        balls_played = self.current_over * 6 + self.current_ball
        return max(0, total_balls_in_innings - balls_played)

    def _compute_pitch_wear(self):
        """
        FC: continuous match-long wear (never resets at an innings break —
        that's the entire point of FC pitch deterioration), normalized to
        the match's total ball capacity (days * overs_per_day * 6).
        T20/ListA: unchanged — per-innings wear, normalized to that
        format's fixed innings ball count.
        """
        if self.is_fc:
            _total_balls = self.fmt.days * self.fmt.overs_per_day * 6
            return min(1.0, self.match_balls_bowled / _total_balls)
        _total_balls = self.fmt.overs * 6
        return min(1.0, self.innings_balls_bowled / _total_balls)

    def _ball_outcome_token(self, outcome, wicket, runs, extra):
        if wicket:
            return "W"
        if extra:
            extra_type = outcome.get("extra_type", "")
            if extra_type == "Wide":
                return "Wd"
            if extra_type == "No Ball":
                bat_runs = outcome.get("bat_runs")
                nb_runs = bat_runs if bat_runs is not None else runs
                return f"Nb+{nb_runs}" if nb_runs else "Nb"
            if extra_type == "Byes":
                return f"B{runs}"
            if extra_type == "Leg Bye":
                return f"Lb{runs}"
        return "•" if runs == 0 else str(runs)

    def _format_over_summary(self, over_label):
        striker_stats = self.batsman_stats.get(self.current_striker["name"], {})
        non_striker_stats = self.batsman_stats.get(self.current_non_striker["name"], {})
        
        # Safe access to bowler stats - bowler might not exist during innings transition
        if self.current_bowler and self.current_bowler["name"] in self.bowler_stats:
            bowler_stats = self.bowler_stats[self.current_bowler["name"]]
        else:
            # Use default empty stats if bowler not found
            bowler_stats = {"runs":0,"fours":0,"sixes":0,"wickets":0,"overs":0,"maidens":0,"balls_bowled":0,"wides":0,"noballs":0,"byes":0,"legbyes":0}
        
        team_name = self._get_team_name(self.batting_team)
        balls_played = self.current_over * 6 + self.current_ball
        current_rr = (self.score * 6) / balls_played if balls_played > 0 else 0
        over_progress = bowler_stats.get("overs", 0) + (bowler_stats.get("balls_bowled", 0) % 6) / 10
        outcomes = " ".join(self.current_over_outcomes) if self.current_over_outcomes else "-"
        
        bowler_name = self.current_bowler["name"] if self.current_bowler else "Unknown"

        return (
            f"<strong>{over_label} - {outcomes} ({self.current_over_runs})</strong><br>"
            f"{team_name} {self.score}/{self.wickets}, RR: {current_rr:.2f}<br><br>"
            f"{self.current_striker['name']} {striker_stats.get('runs', 0)}({striker_stats.get('balls', 0)}) [{striker_stats.get('fours', 0)}x4, {striker_stats.get('sixes', 0)}x6]<br>"
            f"{self.current_non_striker['name']} {non_striker_stats.get('runs', 0)}({non_striker_stats.get('balls', 0)}) [{non_striker_stats.get('fours', 0)}x4, {non_striker_stats.get('sixes', 0)}x6]<br>"
            f"{bowler_name} {over_progress:.1f}-{bowler_stats.get('maidens', 0)}-{bowler_stats.get('runs', 0)}-{bowler_stats.get('wickets', 0)}"
        )

    def _format_innings_complete_summary(self, title="End of innings"):
        """Format a simple innings completion message"""
        if self.innings == 1:
            # End of first innings - show what second batting team needs
            second_batting_team_name = self._get_team_name(self.bowling_team)  # Bowling team becomes batting team
            runs_needed = self.target
            overs = self.overs
            return f"<strong>{title}</strong><br>{second_batting_team_name} need {runs_needed} runs from {overs} overs"
        else:
            # End of second innings - just show title
            return f"<strong>{title}</strong>"

    def _is_manual_mode(self):
        return str(self.simulation_mode).lower() == "manual"

    def _find_player_dict(self, player_name):
        """Best-effort lookup of the original player dict (with its DB id,
        if any) by name, for late-created stats entries."""
        for p in self.home_xi + self.away_xi:
            if p.get("name") == player_name:
                return p
        return None

    def _ensure_batsman_stats_entry(self, player_name):
        if player_name not in self.batsman_stats:
            self.batsman_stats[player_name] = self._new_batting_stats(self._find_player_dict(player_name))

    def _ensure_current_bowler_stats_entry(self):
        if not self.current_bowler:
            return
        bowler_name = self.current_bowler.get("name")
        if not bowler_name:
            return
        if bowler_name not in self.bowler_stats:
            self.bowler_stats[bowler_name] = self._new_bowling_stats(self.current_bowler)
            logger.warning("Created missing bowler_stats entry for '%s'", bowler_name)

    def _build_decision_required_response(self, decision, commentary="", ball_data=None):
        response = {
            "match_over": False,
            "decision_required": True,
            "decision_type": decision.get("type"),
            "decision_context": decision.get("context", {}),
            "decision_options": decision.get("options", []),
            "score": self.score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "striker": self.current_striker["name"] if self.current_striker else "",
            "non_striker": self.current_non_striker["name"] if self.current_non_striker else "",
            "bowler": self.current_bowler["name"] if self.current_bowler else "",
            "commentary": commentary
        }
        if ball_data:
            response["ball_data"] = ball_data
        return response

    def _get_manual_bowler_candidates(self):
        all_bowlers = [(i, p) for i, p in enumerate(self.bowling_team) if p.get("will_bowl", False)]
        previous_bowler = self.current_bowler["name"] if self.current_bowler else None
        non_consecutive = [(i, p) for i, p in all_bowlers if p["name"] != previous_bowler]

        # FC has no bowler-overs quota at all (Law 17.2 — no consecutive
        # overs — is the only eligibility rule; see engine/fc_bowler_workload.py),
        # so self.fmt.max_bowler_overs doesn't exist on MultiDayFormatConfig.
        # non_consecutive alone already IS the correct FC eligibility list.
        if self.is_fc:
            return non_consecutive

        strict = [
            (i, p) for i, p in all_bowlers
            if self.bowler_history.get(p["name"], 0) < self.fmt.max_bowler_overs and p["name"] != previous_bowler
        ]
        if strict:
            return strict

        if non_consecutive:
            return non_consecutive
        return []

    def _create_next_bowler_decision(self):
        candidates = self._get_manual_bowler_candidates()
        # FC has no fixed overs-per-bowler cap (see _get_manual_bowler_candidates)
        # — "overs_remaining" isn't a meaningful concept there, so sort by
        # bowling_rating alone instead of the T20/ListA overs-remaining-first
        # ordering, and never dereference the nonexistent max_bowler_overs.
        options = []
        for idx, bowler in candidates:
            overs_done = self.bowler_history.get(bowler["name"], 0)
            option = {
                "index": idx,
                "name": bowler["name"],
                "role": bowler.get("role", ""),
                "bowling_type": bowler.get("bowling_type", ""),
                "bowling_rating": bowler.get("bowling_rating", 0),
                "overs_bowled": overs_done,
            }
            if not self.is_fc:
                option["overs_remaining"] = max(0, self.fmt.max_bowler_overs - overs_done)
            options.append(option)

        if self.is_fc:
            options.sort(key=lambda b: b["bowling_rating"], reverse=True)
        else:
            options.sort(key=lambda b: (b["overs_remaining"], b["bowling_rating"]), reverse=True)
        decision = {
            "type": "next_bowler",
            "context": {
                "innings": self.fc_innings if self.is_fc else self.innings,
                "upcoming_over": self.current_over + 1,
                "score": self.score,
                "wickets": self.wickets
            },
            "options": options
        }
        self.pending_decision = decision
        return decision

    def _create_next_batter_decision(self, dismissed_name, provisional_index, candidate_indices):
        options = []
        for idx in candidate_indices:
            player = self.batting_team[idx]
            options.append({
                "index": idx,
                "name": player["name"],
                "role": player.get("role", ""),
                "batting_rating": player.get("batting_rating", 0)
            })
        options.sort(key=lambda b: b["batting_rating"], reverse=True)
        decision = {
            "type": "next_batter",
            "context": {
                "innings": self.fc_innings if self.is_fc else self.innings,
                "dismissed_batter": dismissed_name,
                "provisional_index": provisional_index,
                "provisional_batter": self.batting_team[provisional_index]["name"],
                "incoming_slot": "striker" if self.current_striker["name"] == self.batting_team[provisional_index]["name"] else "non_striker",
                "score": self.score,
                "wickets": self.wickets,
                "over": self.current_over,
                "ball": self.current_ball
            },
            "options": options
        }
        self.pending_decision = decision
        return decision

    # Overs left in the day inside which a captain would rather send a
    # bowler in than expose a specialist batter for a handful of deliveries.
    _FC_NIGHTWATCHMAN_OVERS = 6

    def _fc_pick_nightwatchman(self, next_index):
        """The classic end-of-day move: a wicket falls with minutes left, and
        rather than send a front-line batter out to survive an awkward few
        overs and start again in the morning, the captain pushes a bowler up
        to see out the day.

        Returns an index to promote, or None to bat in the normal order.
        """
        if not self.is_fc or self.fc_nightwatchman_used:
            return None
        # Only worth it to protect a specialist; from 7 down the next man in
        # is already a lower-order player.
        if next_index > 5:
            return None
        # Never with the last pair — there is nobody left to protect.
        if self.wickets >= 8:
            return None
        overs_left = self._fc_effective_overs_today() - self.fc_day_overs_bowled_today
        if not (0 < overs_left <= self._FC_NIGHTWATCHMAN_OVERS):
            return None

        # Best available defender among the bowlers below him — technique
        # first, since the job is to survive, not to score.
        candidates = [
            i for i in self.remaining_batter_indices
            if i > next_index and self.batting_team[i].get("will_bowl")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda i: (
            self.batting_team[i].get("technique_rating") or 0,
            self.batting_team[i].get("batting_rating") or 0,
        ))

    def _auto_pick_next_batter_index(self):
        if not self.remaining_batter_indices:
            return None
        next_index = min(self.remaining_batter_indices)
        watchman = self._fc_pick_nightwatchman(next_index)
        if watchman is not None:
            self.fc_nightwatchman_used = True
            self.fc_nightwatchman_name = self.batting_team[watchman]["name"]
            logger.info("FC NIGHTWATCHMAN: %s promoted ahead of %s",
                        self.batting_team[watchman]["name"],
                        self.batting_team[next_index]["name"])
            return watchman
        return next_index

    def _bring_new_batter(self, dismissed_end, selected_index):
        selected_player = self.batting_team[selected_index]
        self._ensure_batsman_stats_entry(selected_player["name"])

        if dismissed_end == "non_striker":
            self.batter_idx[1] = selected_index
            self.current_non_striker = self.current_striker
            self.current_striker = selected_player
            self.batter_idx.reverse()
        else:
            self.batter_idx[0] = selected_index
            self.current_striker = selected_player

    def submit_pending_decision(self, selected_index):
        if not self.pending_decision:
            return {"error": "No pending decision"}, 400

        decision = self.pending_decision
        option_indices = {int(opt["index"]) for opt in decision.get("options", [])}
        try:
            selected_index = int(selected_index)
        except Exception:
            log_exception(source="backend")
            return {"error": "Invalid selected index"}, 400

        if selected_index not in option_indices:
            return {"error": "Selected player is not a valid option"}, 400

        if decision["type"] == "next_bowler":
            selected_bowler = self.bowling_team[selected_index]
            previous_bowler = self.current_bowler["name"] if self.current_bowler else None
            if previous_bowler and selected_bowler["name"] == previous_bowler:
                return {"error": "Selected bowler cannot bowl consecutive overs"}, 400
            self.current_bowler = selected_bowler
            self.bowler_selected_for_over = self.current_over
            self._ensure_current_bowler_stats_entry()
            result = {
                "success": True,
                "applied": {
                    "type": "next_bowler",
                    "name": self.current_bowler["name"],
                    "index": selected_index
                }
            }
        elif decision["type"] == "next_batter":
            context = decision.get("context", {})
            provisional_index = int(context.get("provisional_index"))
            incoming_slot = context.get("incoming_slot", "striker")

            if selected_index != provisional_index:
                # Put provisional batter back in the queue and consume selected batter.
                self.remaining_batter_indices.add(provisional_index)
                self.remaining_batter_indices.discard(selected_index)
                selected_player = self.batting_team[selected_index]
                self._ensure_batsman_stats_entry(selected_player["name"])
                if incoming_slot == "striker":
                    self.current_striker = selected_player
                    self.batter_idx[0] = selected_index
                else:
                    self.current_non_striker = selected_player
                    self.batter_idx[1] = selected_index

            selected_player = self.batting_team[selected_index]
            result = {
                "success": True,
                "applied": {
                    "type": "next_batter",
                    "name": selected_player["name"],
                    "index": selected_index
                }
            }
        elif decision["type"] == "fc_declare":
            declared = selected_index == 1
            if declared:
                self.fc_innings_declared = True
            else:
                # Guards _fc_check_declaration_and_follow_on from re-asking
                # every ball for the rest of this over — see its docstring.
                self._fc_declined_declare_over = self.current_over
            result = {"success": True, "applied": {"type": "fc_declare", "declared": declared}}
        elif decision["type"] == "fc_follow_on":
            enforce_fo = selected_index == 1
            scorecard_data = getattr(self, "_fc_pending_innings_end_scorecard", None)
            self._fc_pending_innings_end_scorecard = None
            self.pending_decision = None
            transition_response = self._fc_apply_follow_on_decision(enforce_fo, scorecard_data)
            return {
                "success": True,
                "applied": {"type": "fc_follow_on", "enforce_fo": enforce_fo},
                "transition": transition_response,
            }, 200
        else:
            return {"error": "Unsupported decision type"}, 400

        self.pending_decision = None
        return result, 200

    def _format_scorecard_block(self, scorecard, title):
        if not scorecard:
            return ""
        total = scorecard.get("total_score", 0)
        wkts = scorecard.get("wickets", 0)
        overs = scorecard.get("overs", "0.0")

        def dismissal_text(player):
            wicket_type = (player.get("wicket_type") or "").strip()
            bowler_out = (player.get("bowler_out") or "").strip()
            fielder_out = (player.get("fielder_out") or "").strip()
            status = (player.get("status") or "").strip()
            runs = player.get("runs", "")
            balls = player.get("balls", "")

            # Player has not batted.
            if runs == "" and balls == "":
                return "DNB"

            # Not out batter at innings end.
            if not wicket_type:
                if status.lower() == "not out":
                    return "not out*"
                return status if status else "not out*"

            if wicket_type == "Caught":
                if fielder_out and bowler_out:
                    return f"c {fielder_out} b {bowler_out}"
                if bowler_out:
                    return f"c ? b {bowler_out}"
                return "c ? b ?"
            if wicket_type == "Bowled":
                return f"b {bowler_out}" if bowler_out else "b ?"
            if wicket_type == "LBW":
                return f"lbw b {bowler_out}" if bowler_out else "lbw b ?"
            if wicket_type == "Run Out":
                return f"run out ({fielder_out})" if fielder_out else "run out"
            if wicket_type == "Stumped":
                if fielder_out and bowler_out:
                    return f"st {fielder_out} b {bowler_out}"
                if bowler_out:
                    return f"st ? b {bowler_out}"
                return "st ? b ?"
            if wicket_type == "Hit Wicket":
                return f"hit wicket b {bowler_out}" if bowler_out else "hit wicket"

            # Fallback for any uncommon dismissal status.
            return status if status else wicket_type

        batting_rows = "".join(
            f"<tr><td>{p.get('name','')}</td><td>{dismissal_text(p)}</td><td>{p.get('runs',0)}</td><td>{p.get('balls',0)}</td><td>{p.get('fours',0)}</td><td>{p.get('sixes',0)}</td></tr>"
            for p in scorecard.get("players", [])
        )
        bowling_rows = "".join(
            f"<tr><td>{b.get('name','')}</td><td>{b.get('overs',0)}</td><td>{b.get('maidens',0)}</td><td>{b.get('runs',0)}</td><td>{b.get('wickets',0)}</td></tr>"
            for b in scorecard.get("bowlers", [])
        )
        return (
            f"<strong>{title}</strong><br>"
            f"Total: {total}/{wkts} ({overs} ov)<br>"
            f"<div style='margin-top:6px;font-weight:600;'>Batting</div>"
            f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem;'>"
            f"<thead><tr><th style='text-align:left;border-bottom:1px solid #444;'>Batter</th>"
            f"<th style='text-align:left;border-bottom:1px solid #444;'>Dismissal</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>R</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>B</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>4s</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>6s</th></tr></thead>"
            f"<tbody>{batting_rows}</tbody></table>"
            f"<div style='margin-top:8px;font-weight:600;'>Bowling</div>"
            f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem;'>"
            f"<thead><tr><th style='text-align:left;border-bottom:1px solid #444;'>Bowler</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>O</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>M</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>R</th>"
            f"<th style='text-align:right;border-bottom:1px solid #444;'>W</th></tr></thead>"
            f"<tbody>{bowling_rows}</tbody></table>"
        )


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
            'current_partnership_balls': self.current_partnership_balls,
            'current_partnership_runs': self.current_partnership_runs,
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

    # ── WIN PROBABILITY (2nd innings only) ────────────────────────────────────
    # DLS-inspired resource model: wickets-in-hand × balls remaining determine
    # how many runs can realistically be scored. A normal CDF approximation
    # converts the gap between expected and required runs into a probability.

    _WIN_PROB_WICKET_CAPACITY = {
        10: 1.00, 9: 0.95, 8: 0.89, 7: 0.81,
        6:  0.71, 5: 0.59, 4: 0.45, 3: 0.31,
        2:  0.17, 1: 0.08, 0: 0.00,
    }
    _WIN_PROB_PITCH_RPB = {
        "Green": 1.22, "Dry": 1.27, "Hard": 1.38,
        "Flat":  1.53, "Dead": 1.67,
    }

    @staticmethod
    def _normal_cdf(z: float) -> float:
        """Abramowitz & Stegun rational approximation of the standard normal CDF."""
        if z < -6.0:
            return 0.0
        if z > 6.0:
            return 1.0
        sign = 1 if z >= 0 else -1
        z = abs(z)
        t = 1.0 / (1.0 + 0.2316419 * z)
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
        cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z) * poly
        return 0.5 + sign * (cdf - 0.5)

    def _calculate_win_probability(self) -> float:
        """
        Return the chasing team's win probability (0–100) for the current ball.
        Only meaningful during innings 2; returns None for innings 1.
        """
        if self.innings != 2:
            return None

        runs_needed = self.target - self.score
        if runs_needed <= 0:
            return 100.0

        wickets_in_hand = 10 - self.wickets
        if wickets_in_hand <= 0:
            return 0.0

        balls_remaining = (self.overs - self.current_over) * 6 - self.current_ball
        if balls_remaining <= 0:
            return 0.0

        typical_rpb = self._WIN_PROB_PITCH_RPB.get(self.pitch, 1.38)
        capacity = self._WIN_PROB_WICKET_CAPACITY.get(wickets_in_hand, 0.50)

        expected_achievable = balls_remaining * typical_rpb * capacity
        std_dev = max(6.0, (balls_remaining ** 0.5) * 2.8 * (capacity ** 0.4))

        z = (expected_achievable - runs_needed) / std_dev
        win_prob = self._normal_cdf(z) * 100.0
        return round(max(2.0, min(98.0, win_prob)), 1)

    # ── PLAYER FORM SYSTEM ─────────────────────────────────────────────────────
    # In-match batter form: hot/cold streaks affect batting_rating per ball.
    # Updated after each delivery; stored in batsman_stats["form"].

    _FORM_DELTA = {
        "Six":    0.07,  "Four":   0.04,  "Three":  0.02,
        "Double": 0.01,  "Single": 0.005, "Dot":   -0.02,
    }
    _FORM_MILESTONE_25 = 1.15   # form floor after scoring 25 runs
    _FORM_MILESTONE_50 = 1.22   # form floor after scoring 50 runs
    _FORM_MIN, _FORM_MAX = 0.72, 1.30

    def _update_batter_form(self, striker_name: str, outcome: dict) -> None:
        """Update the in-match form multiplier for the batter after each delivery."""
        stats = self.batsman_stats.get(striker_name)
        if stats is None:
            return

        current_form = stats.get("form", 1.0)

        if outcome.get("batter_out"):
            # Wicket — form doesn't matter for this batter anymore
            stats["form"] = 1.0
            return

        outcome_label = outcome.get("label") or outcome.get("type", "")
        # Derive label from runs if not set (outcome dict may vary)
        if not outcome_label or outcome_label in ("run", "extra", "wicket"):
            runs = outcome.get("runs", 0)
            is_extra = outcome.get("is_extra", False)
            if is_extra:
                outcome_label = "Dot"
            elif runs == 0:
                outcome_label = "Dot"
            elif runs == 1:
                outcome_label = "Single"
            elif runs == 2:
                outcome_label = "Double"
            elif runs == 3:
                outcome_label = "Three"
            elif runs == 4:
                outcome_label = "Four"
            elif runs >= 6:
                outcome_label = "Six"
            else:
                outcome_label = "Dot"

        delta = self._FORM_DELTA.get(outcome_label, 0.0)
        new_form = current_form + delta

        # Milestone bonuses: ensure form doesn't dip below milestone floor.
        # ListA uses softer floors to prevent excessive set-batter snowballing.
        batter_runs = stats.get("runs", 0)
        floor_25 = 1.08 if self.fmt.name == "ListA" else self._FORM_MILESTONE_25
        floor_50 = 1.12 if self.fmt.name == "ListA" else self._FORM_MILESTONE_50
        if batter_runs >= 50:
            new_form = max(new_form, floor_50)
        elif batter_runs >= 25:
            new_form = max(new_form, floor_25)

        stats["form"] = max(self._FORM_MIN, min(self._FORM_MAX, new_form))

    def _update_partnership_tracking(self, outcome):
        """Update partnership tracking for pressure calculation (simpler version)"""
        if outcome.get('batter_out'):
            # Partnership broken - handled in next_ball via _save_partnership
            pass
        else:
            # Continue partnership
            # Legal deliveries: non-extras + byes/leg-byes (they ARE legal deliveries)
            is_legal = not outcome.get('is_extra')
            if outcome.get('is_extra') and outcome.get('extra_type', '') in ('Byes', 'Leg Bye'):
                is_legal = True
            if is_legal:
                self.current_partnership_balls += 1
            self.current_partnership_runs += outcome.get('runs', 0)

    def _credit_partnership_contribution(self, runs=0, balls=0):
        """Credit the current striker's contribution toward the running partnership.

        Lazily seeds batsman1/batsman2 slot names from the current striker and
        non-striker when both are empty (innings start, or first ball after a
        wicket), then matches by name so strike rotation is handled correctly.
        """
        if not runs and not balls:
            return
        contrib = self.current_partnership_contributions
        if contrib['batsman1']['name'] == '' and contrib['batsman2']['name'] == '':
            contrib['batsman1']['name'] = self.current_striker['name']
            contrib['batsman2']['name'] = self.current_non_striker['name']

        striker_name = self.current_striker['name']
        if contrib['batsman1']['name'] == striker_name:
            slot = contrib['batsman1']
        elif contrib['batsman2']['name'] == striker_name:
            slot = contrib['batsman2']
        else:
            # Striker name doesn't match either seeded slot — can happen after a
            # run-out with crossing, or an unexpected strike rotation edge-case.
            # Attempt to reseed into whichever slot is still empty; if both slots
            # are already occupied by other batters we cannot attribute safely and
            # log a warning instead of silently discarding the contribution.
            if contrib['batsman1']['name'] == '':
                contrib['batsman1']['name'] = striker_name
                slot = contrib['batsman1']
            elif contrib['batsman2']['name'] == '':
                contrib['batsman2']['name'] = striker_name
                slot = contrib['batsman2']
            else:
                print(f"⚠️ Partnership contribution dropped: striker '{striker_name}' "
                      f"not in slots ({contrib['batsman1']['name']!r}, "
                      f"{contrib['batsman2']['name']!r}). "
                      f"runs={runs}, balls={balls}")
                return

        slot['runs'] += runs
        slot['balls'] += balls

    def _save_partnership(self, wicket_type=None):
        """Save the current partnership and reset for next wicket"""
        contrib = self.current_partnership_contributions
        striker_name = self.current_striker["name"]
        non_striker_name = self.current_non_striker["name"]

        # Match contributions to the saved names (slots may be pegged to whoever
        # entered the pair first; strike rotation means current_striker != slot1).
        if contrib['batsman1']['name'] == striker_name:
            b1 = contrib['batsman1']
            b2 = contrib['batsman2']
        elif contrib['batsman2']['name'] == striker_name:
            b1 = contrib['batsman2']
            b2 = contrib['batsman1']
        else:
            # Fallback (shouldn't happen): assume slot order matches save order
            b1 = contrib['batsman1']
            b2 = contrib['batsman2']

        partnership_data = {
            "innings_number": self.fc_innings if self.is_fc else self.innings,
            "wicket_number": self.wickets,
            "batsman1_id": None,  # Will be resolved to player ID later
            "batsman1_name": striker_name,
            "batsman2_id": None,
            "batsman2_name": non_striker_name,
            "runs": self.current_partnership_runs,
            "balls": self.current_partnership_balls,
            "batsman1_contribution": b1['runs'],
            "batsman1_balls": b1['balls'],
            "batsman2_contribution": b2['runs'],
            "batsman2_balls": b2['balls'],
            "start_over": self.current_partnership_start_over,
            "end_over": self.current_over + (self.current_ball / 6),
        }
        
        if self.is_fc:
            self.fc_innings_partnerships.setdefault(self.fc_innings, []).append(partnership_data)
        elif self.innings == 1:
            self.first_innings_partnerships.append(partnership_data)
        else:
            self.second_innings_partnerships.append(partnership_data)
            
        print(f"🤝 Partnership Saved: {self.current_partnership_runs} runs off {self.current_partnership_balls} balls ({self.current_striker['name']} & {self.current_non_striker['name']})")
        
        # Reset tracking
        self.current_partnership_runs = 0
        self.current_partnership_balls = 0
        self.current_partnership_start_over = self.current_over + (self.current_ball / 6)
        
        # Reset contributions - one batter stays, one goes.
        # But for tracking simplicity, we'll reset both and accumulate fresh
        # The surviving batter's new partnership starts from 0 runs for THIS partnership
        self.current_partnership_contributions = {
            'batsman1': {'name': '', 'runs': 0, 'balls': 0},
            'batsman2': {'name': '', 'runs': 0, 'balls': 0}
        }

    def _save_first_innings_stats(self):
        """Save first innings stats before resetting for second innings"""
        self.first_innings_batting_stats = copy.deepcopy(self.batsman_stats)
        self.first_innings_bowling_stats = copy.deepcopy(self.bowler_stats)
        
        # Track which teams played in first innings
        if self.batting_team is self.home_xi:
            self.first_batting_team_name = self.match_data["team_home"].split("_")[0] 
            self.first_bowling_team_name = self.match_data["team_away"].split("_")[0]
        else:
            self.first_batting_team_name = self.match_data["team_away"].split("_")[0]
            self.first_bowling_team_name = self.match_data["team_home"].split("_")[0]
        
        print(f"✅ Saved first innings stats - {self.first_batting_team_name} batting: {len(self.first_innings_batting_stats)} players, {self.first_bowling_team_name} bowling: {len(self.first_innings_bowling_stats)} bowlers")

    def _save_second_innings_stats(self):
        """Save second innings stats at match completion"""
        if getattr(self, "_second_innings_stats_saved", False):
            return
        self.second_innings_batting_stats = copy.deepcopy(self.batsman_stats)
        self.second_innings_bowling_stats = copy.deepcopy(self.bowler_stats)
        
        # Determine second innings teams (opposite of first)
        if self.batting_team is self.home_xi:
            second_batting_team = self.match_data["team_home"].split("_")[0]
            second_bowling_team = self.match_data["team_away"].split("_")[0]
        else:
            second_batting_team = self.match_data["team_away"].split("_")[0]
            second_bowling_team = self.match_data["team_home"].split("_")[0]
        
        print(f"✅ Saved second innings stats - {second_batting_team} batting: {len(self.second_innings_batting_stats)} players, {second_bowling_team} bowling: {len(self.second_innings_bowling_stats)} bowlers")
        self._second_innings_stats_saved = True

    def set_frontend_commentary(self, frontend_commentary):
        """Set the frontend commentary for archiving"""
        self.frontend_commentary_captured = frontend_commentary
        print(f"📺 Frontend commentary set: {len(frontend_commentary)} items")


    def _create_match_archive(self):
        """Create complete match archive when match ends"""
        if getattr(self, "_archive_created", False):
            print("ℹ️ Match archive already created; skipping duplicate archive call.")
            return True
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
                self._archive_created = True
                # Clean up temp JSON — data is now in archive ZIP + DB
                try:
                    os.remove(original_json_path)
                    print(f"🧹 Cleaned up temp JSON: {original_json_path}")
                except Exception as cleanup_err:
                    log_exception(cleanup_err)
                    print(f"⚠️ JSON cleanup failed (non-critical): {cleanup_err}")
                return True
            else:
                print(f"❌ Failed to create match archive")
                return False
                
        except Exception as e:
            log_exception(e)
            print(f"❌ Error creating match archive: {e}")
            return False

    # def _create_match_archive_with_frontend_commentary(self):
    #     """Alternative method called when frontend commentary is captured"""
    #     return self._create_match_archive()
            


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
        
        # Remove previous bowler from first position to avoid consecutive.
        # Explicitly assign: pos 18 = first non-previous, pos 19 = second non-previous,
        # pos 20 = previous_bowler. This guarantees 17→18 and 19→20 are not consecutive
        # (all three bowlers are distinct, so 18→19 is safe by definition).
        if previous_bowler in bowler_names:
            non_previous = [b for b in bowler_names if b != previous_bowler]
            bowler_18 = non_previous[0]
            bowler_19 = non_previous[1]   # distinct from bowler_18, never previous_bowler
            bowler_20 = previous_bowler   # safe: bowler_19 != previous_bowler
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
        if not available_bowlers:
            raise Exception("No bowlers available for complex death-over plan")
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
            last_bowler = death_plan[-1] if death_plan else previous_bowler
            filler = next((name for name in available_bowlers if name != last_bowler), None)
            if not filler:
                raise Exception("No non-consecutive filler available for death-over plan")
            death_plan.append(filler)
        
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
            raise Exception("No bowlers with remaining quota for death-over plan")
        
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
            
            # Priority 3: no valid non-consecutive option exists.
            if not selected_bowler:
                raise Exception(
                    "No non-consecutive bowler available for emergency death-over plan"
                )
            
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
        Calculate a complete bowler sequence for the configured death phase.

        T20 death overs are configured as indexes 16-19 (human overs 17-20),
        so this planner must start at the format's death_phase.start rather
        than hard-coding a three-over 18-20 plan.
        """
        print(f"  🐛 DEBUG _calculate_death_overs_plan_safe:")
        print(f"     Input bowler_quota: {list(bowler_quota.keys())}")

        plan_length = self.fmt.overs - self.current_over
        previous_bowler = self.current_bowler["name"] if self.current_bowler else None
        remaining_bowlers = {
            name: max(0, data.get('overs_remaining', 0))
            for name, data in bowler_quota.items()
        }
        total_remaining = sum(remaining_bowlers.values())

        print(f"  🐛 Planning {plan_length} death overs from over {self.current_over + 1}")
        print(f"  🐛 Remaining bowlers: {remaining_bowlers}")
        print(f"  🐛 Total remaining: {total_remaining}")
        print(f"  🐛 Previous bowler: {previous_bowler}")

        def score_plan(plan, quota_violations):
            rating_total = 0
            specialist_count = 0
            for name in plan:
                bowler = bowler_quota[name]['bowler']
                rating_total += bowler.get("bowling_rating", 0)
                if self._is_death_specialist(bowler):
                    specialist_count += 1
            return (quota_violations, -specialist_count, -rating_total, tuple(plan))

        def find_plan(allow_quota_violation):
            best_plan = None
            best_score = None

            def backtrack(last_bowler, quota_left, plan, quota_violations):
                nonlocal best_plan, best_score

                if len(plan) == plan_length:
                    candidate_score = score_plan(plan, quota_violations)
                    if best_score is None or candidate_score < best_score:
                        best_score = candidate_score
                        best_plan = plan[:]
                    return

                candidates = []
                for name, data in bowler_quota.items():
                    if name == last_bowler:
                        continue
                    has_quota = quota_left.get(name, 0) > 0
                    if has_quota or allow_quota_violation:
                        candidates.append(name)

                candidates.sort(
                    key=lambda name: (
                        quota_left.get(name, 0) <= 0,
                        -quota_left.get(name, 0),
                        -int(self._is_death_specialist(bowler_quota[name]['bowler'])),
                        -bowler_quota[name]['bowler'].get("bowling_rating", 0),
                        name,
                    )
                )

                for name in candidates:
                    next_quota = quota_left.copy()
                    next_violations = quota_violations
                    if next_quota.get(name, 0) > 0:
                        next_quota[name] -= 1
                    else:
                        next_violations += 1
                    backtrack(name, next_quota, plan + [name], next_violations)

            backtrack(previous_bowler, remaining_bowlers.copy(), [], 0)
            return best_plan, best_score

        death_plan, death_score = find_plan(allow_quota_violation=False)
        if death_plan:
            print(f"  🐛 Death plan PASSED strict quota search: {death_plan}")
            return death_plan

        print(f"  🐛 Strict death plan unavailable - allowing quota violation fallback")
        death_plan, death_score = find_plan(allow_quota_violation=True)
        if death_plan:
            if death_score and death_score[0] > 0:
                self._log_constraint_violation(
                    "DEATH_PLAN_QUOTA_VIOLATION",
                    f"Death plan required {death_score[0]} quota violation(s) to preserve no-consecutive rule"
                )
            print(f"  🐛 Death plan with quota fallback: {death_plan}")
            return death_plan

        raise Exception("No non-consecutive bowler available for death-over plan")

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
        
        death_plan = None

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
        if death_plan is None:
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
            
            if overs_bowled < self.fmt.max_bowler_overs and not is_consecutive:
                print(f"🆘 Emergency selection: {bowler['name']}")
                return bowler
        
        # If no non-consecutive bowler with quota, allow quota violation but prevent consecutive
        for bowler in all_bowlers:
            is_consecutive = self.current_bowler and bowler["name"] == self.current_bowler["name"]
            if not is_consecutive:
                print(f"🆘 Emergency quota violation: {bowler['name']}")
                return bowler
        
        raise Exception("No non-consecutive bowler available for emergency selection")

    def _pick_death_overs_bowler(self):
        """
        Pre-calculated death overs bowler selection.
        Calculates the plan once at the configured death start, then consumes it
        over the remaining death overs.
        """
        print(f"\n🎯 === DEATH OVERS SELECTION - Over {self.current_over + 1} ===")
        
        # ================ CHECK IF WE NEED TO CALCULATE NEW PLAN ================
        death_start = self.fmt.death_phase.start
        existing_plan_start = getattr(self, 'death_overs_plan_start', None)
        needs_new_plan = (
            self.current_over == death_start
            or not getattr(self, 'death_overs_plan', None)
            or existing_plan_start != death_start
        )

        if needs_new_plan:
            plan_end = self.fmt.overs
            over_labels = list(range(self.current_over + 1, plan_end + 1))
            print(f"🔥 CALCULATING NEW DEATH PLAN FOR OVERS {over_labels}")
            
            # Get all bowlers and their current quota
            all_bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
            
            # Build quota dictionary with CURRENT state. Keep exhausted bowlers in
            # the map so the fallback planner can violate quota before it ever
            # violates the no-consecutive rule.
            bowler_quota = {}
            for bowler in all_bowlers:
                overs_bowled = self.bowler_history.get(bowler["name"], 0)
                overs_remaining = max(0, self.fmt.max_bowler_overs - overs_bowled)
                bowler_quota[bowler["name"]] = {
                    'bowler': bowler,
                    'overs_remaining': overs_remaining,
                    'overs_bowled': overs_bowled
                }
                print(f"  {bowler['name']}: {overs_bowled}/{self.fmt.max_bowler_overs} bowled, {overs_remaining} remaining")
            
            # Calculate complete death plan for all remaining configured death overs
            self.death_overs_plan = self._calculate_death_overs_plan_safe(bowler_quota)
            self.death_overs_plan_start = self.current_over
            self.death_overs_bowler_objects = {}
            
            # Store bowler objects for quick lookup
            for bowler_name in self.death_overs_plan:
                self.death_overs_bowler_objects[bowler_name] = bowler_quota[bowler_name]['bowler']
            
            plan_text = ", ".join(
                f"{over_label}→{bowler_name}"
                for over_label, bowler_name in zip(over_labels, self.death_overs_plan)
            )
            print(f"📋 STORED DEATH PLAN: {plan_text}")
        
        # ================ USE STORED PLAN ================
        elif hasattr(self, 'death_overs_plan') and self.death_overs_plan:
            print(f"♻️  USING STORED DEATH PLAN: {self.death_overs_plan}")
        
        else:
            print(f"🚨 ERROR: No death plan available for over {self.current_over + 1}")
            # Emergency fallback - should not happen
            return self._emergency_single_bowler_selection()
        
        # ================ GET BOWLER FOR CURRENT OVER ================
        death_over_index = self.current_over - getattr(self, 'death_overs_plan_start', death_start)
        
        if death_over_index >= len(self.death_overs_plan):
            print(f"🚨 ERROR: Death over index {death_over_index} out of range")
            return self._emergency_single_bowler_selection()
        
        selected_bowler_name = self.death_overs_plan[death_over_index]
        selected_bowler = self.death_overs_bowler_objects[selected_bowler_name]
        
        print(f"🎯 DEATH PLAN SELECTION: over {self.current_over + 1} → {selected_bowler_name}")
        
        # ================ SAFETY CHECK ================
        if self.current_bowler and selected_bowler["name"] == self.current_bowler["name"]:
            print(f"🚨 CONSECUTIVE VIOLATION IN STORED PLAN!")
            print(f"   Previous: {self.current_bowler['name']}")
            print(f"   Selected: {selected_bowler['name']}")
            print(f"   This indicates a bug in the death plan calculation!")
            # Use emergency fallback
            return self._emergency_single_bowler_selection()
        
        # D4: bowler_history increment moved to over completion
        print(f"📝 {selected_bowler['name']} current quota: {self.bowler_history.get(selected_bowler['name'], 0)}/{self.fmt.max_bowler_overs}")

        # Initialize bowler stats if needed
        if selected_bowler["name"] not in self.bowler_stats:
            self.bowler_stats[selected_bowler["name"]] = self._new_bowling_stats(selected_bowler)

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
        
        # Fix D9: Don't add debug messages to user-facing commentary
        # self.commentary.append(f"<strong>{violation_msg}</strong>")  # Removed
        
        # Track violations for post-match analysis
        self.constraint_violations.append({
            'over': self.current_over + 1,
            'type': violation_type,
            'reason': reason,
            'timestamp': self.current_over
        })


    def _get_match_phase(self):
        """Determine current match phase for context"""
        if self.fmt.is_powerplay(self.current_over):
            return "POWERPLAY"
        if self.fmt.is_death(self.current_over):
            return "DEATH_OVERS"
        return "MIDDLE_OVERS"

    def _is_lista_pure_bowler(self, bowler: dict) -> bool:
        """
        ListA classifier for "pure bowler" prioritization.
        Role labels vary by data source, so we treat any explicit Bowler role
        as pure and everyone else (all-rounders/batting options) as support.
        """
        role = str(bowler.get("role", "")).strip().lower()
        return role == "bowler"

    def _build_lista_bowler_plan(self) -> dict:
        """
        Build per-bowler target overs for ListA.
        Priority: high-rated pure bowlers > all-rounders, while respecting
        max 10 overs and keeping enough total quota to cover 50 overs.
        """
        bowlers = [p for p in self.bowling_team if p.get("will_bowl", False)]
        if not bowlers:
            return {}

        pure = [b for b in bowlers if self._is_lista_pure_bowler(b)]
        support = [b for b in bowlers if not self._is_lista_pure_bowler(b)]

        # If we have exactly 5 bowlers, all will end up near quota by necessity.
        # With 6+ options, this plan aggressively biases pure bowlers.
        weights = {}
        for b in bowlers:
            name = b["name"]
            rating = float(b.get("bowling_rating", 0))
            role_mult = 1.45 if self._is_lista_pure_bowler(b) else 0.90
            weights[name] = rating * role_mult

        allocation = {b["name"]: 0 for b in bowlers}
        caps = {b["name"]: (10 if self._is_lista_pure_bowler(b) else 7) for b in bowlers}
        overs_to_assign = self.fmt.overs

        # Greedy weighted allocation with diminishing returns.
        while overs_to_assign > 0:
            candidates = [b for b in bowlers if allocation[b["name"]] < caps[b["name"]]]
            if not candidates:
                break
            pick = max(
                candidates,
                key=lambda b: (
                    weights[b["name"]] - (allocation[b["name"]] * 3.0),
                    b.get("bowling_rating", 0),
                ),
            )
            allocation[pick["name"]] += 1
            overs_to_assign -= 1

        # If all-rounder caps were too strict to reach 50, relax to full quota.
        while overs_to_assign > 0:
            candidates = [b for b in bowlers if allocation[b["name"]] < 10]
            if not candidates:
                break
            pick = max(
                candidates,
                key=lambda b: (
                    weights[b["name"]] - (allocation[b["name"]] * 2.0),
                    b.get("bowling_rating", 0),
                ),
            )
            allocation[pick["name"]] += 1
            overs_to_assign -= 1

        # Enforce "high-rated pure > all-rounders" when mathematically possible.
        if pure and support:
            top_pure = sorted(
                pure,
                key=lambda b: b.get("bowling_rating", 0),
                reverse=True,
            )[: min(2, len(pure))]

            for p in top_pure:
                p_name = p["name"]
                while allocation[p_name] < 10:
                    support_max = max(allocation[s["name"]] for s in support)
                    if allocation[p_name] > support_max:
                        break
                    donor = max(support, key=lambda s: allocation[s["name"]])
                    donor_name = donor["name"]
                    if allocation[donor_name] <= 0:
                        break
                    allocation[donor_name] -= 1
                    allocation[p_name] += 1

        return allocation

    def _ensure_lista_bowler_plan(self) -> None:
        """Rebuild ListA bowler plan when innings or bowling roster changes."""
        if self.fmt.name != "ListA":
            return

        roster = tuple(
            sorted(p["name"] for p in self.bowling_team if p.get("will_bowl", False))
        )
        if (
            not self.lista_bowler_plan
            or self.lista_plan_innings != self.innings
            or self.lista_plan_roster != roster
        ):
            self.lista_bowler_plan = self._build_lista_bowler_plan()
            self.lista_plan_innings = self.innings
            self.lista_plan_roster = roster

    def _pick_bowler_lista(self):
        """
        ListA-only bowler selection:
        - quota and non-consecutive eligibility from BowlerManager
        - overs driven by target plan that favors high-rated pure bowlers
        - phase-aware nudge (PP/death) without T20 hardcoded over windows
        """
        self._ensure_lista_bowler_plan()

        overs_remaining = self.fmt.overs - self.current_over
        eligible = self.bowler_manager.get_eligible_bowlers(
            self.current_over,
            overs_remaining,
        )
        if not eligible:
            previous_name = self.current_bowler["name"] if self.current_bowler else None
            eligible = [
                p for p in self.bowling_team
                if p.get("will_bowl", False) and p["name"] != previous_name
            ]
            if not eligible:
                raise Exception("No non-consecutive bowler available for ListA selection")

        pure_bowlers = [b for b in self.bowling_team if self._is_lista_pure_bowler(b)]
        pure_need_overs = any(
            self.lista_bowler_plan.get(p["name"], 0) > self.bowler_history.get(p["name"], 0)
            for p in pure_bowlers
        )

        def _score(bowler):
            name = bowler["name"]
            bowled = self.bowler_history.get(name, 0)
            rating = float(bowler.get("bowling_rating", 0))
            target = self.lista_bowler_plan.get(name, 0)
            deficit = target - bowled
            is_pure = self._is_lista_pure_bowler(bowler)
            btype = bowler.get("bowling_type", "")

            score = rating * 0.25
            score += deficit * 8.0
            score += 8.0 if is_pure else 0.0

            if not is_pure and pure_need_overs:
                score -= 6.0

            if self.fmt.is_powerplay(self.current_over):
                if btype in ("Fast", "Fast-medium", "Medium-fast"):
                    score += 6.0
                if is_pure:
                    score += 3.0
            elif self.fmt.is_death(self.current_over):
                if is_pure:
                    score += 8.0
                if btype in ("Fast", "Fast-medium", "Medium-fast"):
                    score += 5.0
            else:
                if not is_pure and deficit <= 0:
                    score -= 4.0

            # Avoid front-loading one bowler too early unless they are behind plan.
            if self.current_over < 20 and bowled >= 4 and deficit <= 0:
                score -= 4.0

            # Look-ahead guard: avoid creating a next-over dead-end where the
            # same bowler must bowl again because everyone else is at quota.
            if self.current_over < (self.fmt.overs - 1):
                future_available = 0
                for p in self.bowling_team:
                    if not p.get("will_bowl", False):
                        continue
                    p_name = p["name"]
                    future_bowled = self.bowler_history.get(p_name, 0)
                    if p_name == name:
                        future_bowled += 1
                    if future_bowled < self.fmt.max_bowler_overs and p_name != name:
                        future_available += 1
                if future_available == 0:
                    score -= 100.0
                elif future_available == 1:
                    score -= 12.0

            return score

        selected_bowler = max(
            eligible,
            key=lambda b: (
                _score(b),
                b.get("bowling_rating", 0),
                1 if self._is_lista_pure_bowler(b) else 0,
            ),
        )

        self._update_bowler_tracking(selected_bowler)
        return selected_bowler

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
            crucial_overs = ([0] + [pp.end for pp in self.fmt.powerplay_phases]
                             + list(range(self.fmt.death_phase.start, self.fmt.overs)))
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
        
        # ABSOLUTE CHECK 1: bowling quota policy (STRICT)
        max_q = self.fmt.max_bowler_overs
        if overs_bowled >= max_q:
            print(f"  🚨 ABSOLUTE VIOLATION: {selected_bowler['name']} has {overs_bowled} overs (limit: {max_q})")
            return {
                'valid': False,
                'reason': f"ABSOLUTE quota violation: {selected_bowler['name']} has {overs_bowled}/{max_q} overs",
                'critical': True
            }

        # ABSOLUTE CHECK 2: Consecutive policy (STRICT — ListA only; T20 allows it)
        if (not self.fmt.allow_consecutive_overs
                and self.current_bowler and selected_bowler["name"] == self.current_bowler["name"]):
            print(f"  🚨 ABSOLUTE VIOLATION: {selected_bowler['name']} bowled previous over")
            return {
                'valid': False,
                'reason': f"ABSOLUTE consecutive violation: {selected_bowler['name']} bowled previous over",
                'critical': True
            }

        print(f"  ✅ ABSOLUTE VALIDATION PASSED: {selected_bowler['name']} ({overs_bowled}/{max_q} overs)")
        return {'valid': True, 'reason': 'All constraints met', 'critical': False}

    def _force_valid_selection(self, all_bowlers, quota_analysis):
        """Force valid selection with ABSOLUTE constraints - NO compromises"""
        print(f"  🔧 ABSOLUTE FORCE VALID SELECTION:")
        
        # ABSOLUTE RULE: Find ANY bowler within quota who didn't bowl previous over
        max_q = self.fmt.max_bowler_overs
        for bowler in all_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            is_consecutive = self.current_bowler and bowler["name"] == self.current_bowler["name"]
            overs_bowled = bowler_data['overs_bowled']

            if overs_bowled < max_q and not is_consecutive:
                print(f"  ✅ ABSOLUTE VALID: {bowler['name']} ({overs_bowled}/{max_q} overs, not consecutive)")
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
        """Update all tracking systems (D4: quota incremented at over completion, not here)"""
        print(f"  📝 TRACKING UPDATE:")

        # Update over history
        self._log_bowler_for_over(selected_bowler)

        # D4: bowler_history is now incremented at over completion, not at selection
        # This prevents over-counting when an innings ends mid-over
        _quota_display = getattr(self.fmt, "max_bowler_overs", "unlimited")  # FC has no quota
        print(f"    {selected_bowler['name']} current quota: {self.bowler_history.get(selected_bowler['name'], 0)}/{_quota_display}")
        
        # Initialize/update bowler stats
        if selected_bowler["name"] not in self.bowler_stats:
            self.bowler_stats[selected_bowler["name"]] = self._new_bowling_stats(selected_bowler)
            print(f"    Initialized stats for {selected_bowler['name']}")
        
        # Restore any temporary rating modifications
        self._restore_bowler_ratings()

    def _project_future_constraints(self, selected_bowler, all_bowlers):
        """Project future constraint implications"""
        remaining_overs = self.fmt.overs - (self.current_over + 1)

        # Calculate post-selection availability
        available_next_over = 0
        for bowler in all_bowlers:
            future_overs = self.bowler_history.get(bowler["name"], 0)
            if bowler["name"] == selected_bowler["name"]:
                future_overs += 1
            
            if future_overs < self.fmt.max_bowler_overs and bowler["name"] != selected_bowler["name"]:
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
        if self.fmt.is_death(self.current_over):
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
        self.pending_pre_ball_commentary = []
        self.pending_decision = None
        self.current_over_maiden_invalid = False
        self.free_hit_active = False
        self.current_over_runs = 0
        self.current_over_outcomes = []
        # The sides have swapped, so the bowler still held here belongs to the
        # team that is now batting. next_ball() calls
        # _ensure_current_bowler_stats_entry() before picking the new over's
        # bowler, so leaving it set puts a phantom opposition row in the fresh
        # innings' bowler_stats. Cleared alongside bowler_selected_for_over,
        # which is what forces the new pick.
        self.current_bowler = None
        self.bowler_selected_for_over = -1
        self.remaining_batter_indices = set(range(2, len(self.batting_team)))
        self.lista_bowler_plan = {}
        self.lista_plan_innings = None
        self.lista_plan_roster = ()
        self.death_overs_plan = []
        self.death_overs_plan_start = None
        self.death_overs_bowler_objects = {}
        # Restore any modified bowler ratings
        self._restore_bowler_ratings()
        
        # Reset streak tracking for new innings
        self.batter_streaks = {}

        # Reset GSME ball history for new innings
        self.ball_history = []

        # Reset collapse/wicket-cluster tracking for new innings — otherwise
        # first-innings wickets keep boosting wicket probability into the
        # second innings (see recent_wickets_tracker in next_ball()).
        self.recent_wickets_tracker = []
        self.recent_wickets_count = 0

        # Feature 3: reset pitch wear counter for new innings
        self.innings_balls_bowled = 0

        # Feature 8: reset per-over bowler feedback for new innings
        self.bowler_prev_over_runs = {}
        # Reset the bowler manager for the new innings bowling side. Fatigue
        # resets here (a bowler genuinely rests while the other side bats)
        # even for FC, where pitch wear itself does NOT reset — see
        # self.match_balls_bowled, deliberately untouched by this method.
        _carry = getattr(self, "_fc_pending_fatigue_carry", 0.0)
        if _carry:
            self.bowler_manager.reset(self.bowling_team, carry_fraction=_carry)
            self._fc_pending_fatigue_carry = 0.0
        else:
            self.bowler_manager.reset(self.bowling_team)
        # Re-point alias so existing bowler_history reads remain valid
        self.bowler_history = (self.bowler_manager._overs_this_innings if self.is_fc
                                else self.bowler_manager._quota)

        # Reset partnership tracking
        self.current_partnership_balls = 0
        self.current_partnership_runs = 0
        self.current_partnership_start_over = 0.0
        self.current_partnership_contributions = {
            'batsman1': {'name': '', 'runs': 0, 'balls': 0},
            'batsman2': {'name': '', 'runs': 0, 'balls': 0}
        }

    def _restore_bowler_ratings(self):
        """Restore original bowling ratings after matchup bonuses"""
        for player in self.bowling_team:
            if 'original_bowling_rating' in player:
                player['bowling_rating'] = player['original_bowling_rating']
                del player['original_bowling_rating']
            if player.get("_orig_bowling_rating") is not None:
                player["bowling_rating"] = player["_orig_bowling_rating"]
                del player["_orig_bowling_rating"]


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


    # ------------------------------------------------------------------
    # Feature 1 + 2 + 8: effective bowler dict with phase/fatigue/feedback
    # ------------------------------------------------------------------
    def _get_effective_bowler_dict(self, bowler_dict: dict) -> dict:
        """
        Return a shallow copy of bowler_dict with bowling_rating adjusted for:
          • Phase effectiveness (powerplay / middle / death) [Feature 1]
          • Spell fatigue (overs already bowled this innings) [Feature 2]
          • Previous-over performance feedback [Feature 8]
        """
        eff = dict(bowler_dict)   # shallow copy — safe for scalar values
        bowling_type = eff.get("bowling_type", "")
        bowler_name  = eff.get("name", "")

        # Map bowling_type to the phase-mult key
        if bowling_type in ("Fast", "Fast-medium"):
            phase_key = "pace"
        elif bowling_type in ("Medium-fast", "Medium"):
            phase_key = "medium"
        elif bowling_type in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"):
            phase_key = "spin"
        else:
            phase_key = "default"

        # Feature 1: select phase multiplier via format-aware phase detection.
        # FC has no fielding-circle/powerplay/death phases at all — neutral.
        over = self.current_over
        if self.is_fc:
            phase_mult = 1.00
        elif self.fmt.is_powerplay(over):
            phase_mult = _POWERPLAY_BOWLING_MULT.get(phase_key, 1.00)
        elif self.fmt.is_death(over):
            phase_mult = _DEATH_BOWLING_MULT.get(phase_key, 1.00)
        else:
            phase_mult = _MIDDLE_BOWLING_MULT.get(phase_key, 1.00)

        # Feature 2: fatigue. FC uses a continuous stamina-driven decay
        # (engine/fc_bowler_workload.py) instead of BowlerManager's
        # over-count table.
        if self.is_fc:
            fatigue = self.bowler_manager.get_fatigue_mult(
                bowler_name, stamina_rating=eff.get("stamina_rating", 50)
            )
        else:
            fatigue = self.bowler_manager.get_fatigue_mult(bowler_name)

        # Feature 8: previous over performance feedback via BowlerManager
        prev_runs = self.bowler_manager.prev_over_runs(bowler_name)
        if prev_runs == 0:           # Maiden — confidence boost
            feedback_mult = 1.05
        elif prev_runs >= 20:        # Very expensive — confidence hit
            feedback_mult = 0.93
        elif prev_runs >= 15:        # Expensive
            feedback_mult = 0.97
        else:
            feedback_mult = 1.00

        eff["bowling_rating"] = eff["bowling_rating"] * phase_mult * fatigue * feedback_mult
        logger.debug(
            "[BowlerEff] %s | phase_key=%s over=%d phase=%.3f fatigue=%.3f feedback=%.3f → rating=%.1f",
            bowler_name, phase_key, over, phase_mult, fatigue, feedback_mult, eff["bowling_rating"],
        )
        return eff

    # ------------------------------------------------------------------
    # Feature 13: dynamic game mode selection
    # ------------------------------------------------------------------
    def _resolve_game_mode(self) -> str:
        """
        Return the game mode to apply to this delivery.

        The user's ground config may pin a specific mode, in which case it wins
        for every ball of the match; the default "auto" hands the choice to
        _get_dynamic_game_mode(). The pin is read from the per-match snapshot
        (not live config) so a resumed match replays identically.

        Before this existed the dynamic selector ran unconditionally and the
        configured mode was silently ignored — the picker in the Ground
        Conditions UI did nothing at all.
        """
        from engine.ground_config import AUTO_GAME_MODE, get_active_game_mode_name

        configured = get_active_game_mode_name(config=self.ground_config)
        if configured and configured != AUTO_GAME_MODE:
            return configured
        return self._get_dynamic_game_mode()

    def _get_dynamic_game_mode(self) -> str:
        """
        Dynamically select the most appropriate game mode for the current
        delivery based on match state (pitch, innings, overs, wickets, RRR).

        Returns a game mode name present in the T20 ground config:
          natural_game | aggressive | defensive | bowlers_day | flat_track_bully
        """
        pitch   = self.pitch
        over    = self.current_over
        wickets = self.wickets
        innings = self.innings

        # Flat/Dead pitch + strong batting position early on → bully mode
        if pitch in ("Dead", "Flat") and wickets < 3 and over < 10:
            return "flat_track_bully"

        # Heavy wicket loss in 1st innings → bowlers have the upper hand
        if innings == 1 and wickets >= 7:
            return "bowlers_day"

        # 2nd innings: adapt to required run rate
        if innings == 2 and self.target:
            balls_remaining = max(1, (self.overs - over) * 6 - self.current_ball)
            runs_needed     = max(0, self.target - self.score)
            rrr             = (runs_needed * 6) / balls_remaining
            if wickets >= 7:
                return "defensive"
            if rrr > 12:
                return "aggressive"
            if rrr < 6:
                return "defensive"

        return "natural_game"

    def _get_preferred_bowler_type(self, over_number):
        """Get the preferred bowler type for a specific over based on pattern"""
        pattern = self.bowling_pattern
        
        if pattern == "traditional":
            if self.fmt.is_powerplay(over_number):  # Powerplay
                return "fast"
            elif self.fmt.is_death(over_number):    # Death overs
                return "fast"
            else:                                    # Middle overs
                return "spin"

        elif pattern == "fast_heavy":
            if self.fmt.is_powerplay(over_number):  # Powerplay
                return "fast"
            elif self.fmt.is_death(over_number):    # Death overs
                return "fast"
            else:                                    # Middle overs — allow both
                return "mixed"

        elif pattern == "spin_heavy":
            if over_number < 3:                      # Very early overs (new ball)
                return "fast"
            elif self.fmt.is_death(over_number):    # Death overs
                return "fast"
            else:                                    # Long spin phase
                return "spin"
        
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

        # A6: Stumped - always the wicketkeeper
        if wicket_type == "Stumped":
            wicket_keeper = next((p for p in self.bowling_team if p["role"] == "Wicketkeeper"), None)
            if wicket_keeper:
                return wicket_keeper["name"]
            # No keeper in XI: a stumping is physically impossible. Log loudly and
            # pick any non-bowler so we don't generate "bowler stumped batter" commentary.
            logger.warning(
                "Stumping requested but no Wicketkeeper in bowling XI for match %s; "
                "falling back to a non-bowler fielder.",
                getattr(self, "match_id", "<unknown>"),
            )
            non_bowlers = [p for p in self.bowling_team if p["name"] != self.current_bowler["name"]]
            return random.choice(non_bowlers or self.bowling_team)["name"]

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

        elif wicket_type == "Stumped":
            if fielder_name:
                return f"Wicket! {batsman_name} stumped by {fielder_name} off {bowler_name}! Lightning quick work!"
            else:
                return f"Wicket! {batsman_name} stumped! {outcome['description']}"

        # Fallback
        return f"Wicket! {outcome['description']}"

    def _apply_run_out(self, outcome, extra, commentary_line):
        """
        Apply a Run Out dismissal: credit the 1 completed run, count the ball,
        save the partnership, choose the dismissed end (50/50 striker/non-striker),
        and append the wicket commentary.

        Returns (dismissed_end, dismissed_name, fielder_name, commentary_line).
        """
        # 1. Credit the 1 completed run to score
        self.score += 1
        self.current_over_runs += 1
        self.current_over_maiden_invalid = True  # A2: bat-run invalidates maiden

        # For Byes/Leg Byes the run is an extra — not charged to bowler/batter.
        if extra:
            extra_type = outcome.get("extra_type", "")
            if extra_type not in ("Byes", "Leg Bye"):
                self.bowler_stats[self.current_bowler["name"]]["runs"] += 1
                self.batsman_stats[self.current_striker["name"]]["runs"] += 1
                self.batsman_stats[self.current_striker["name"]]["ones"] += 1
                self._credit_partnership_contribution(runs=1)
        else:
            self.bowler_stats[self.current_bowler["name"]]["runs"] += 1
            self.batsman_stats[self.current_striker["name"]]["runs"] += 1
            self.batsman_stats[self.current_striker["name"]]["ones"] += 1
            self._credit_partnership_contribution(runs=1)

        # 2. Count the ball (Byes/Leg Byes are legal deliveries).
        is_legal_delivery = not extra
        if extra:
            extra_type = outcome.get("extra_type", "")
            if extra_type in ("Byes", "Leg Bye"):
                is_legal_delivery = True

        if is_legal_delivery:
            self.current_ball += 1
            self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1
            self.batsman_stats[self.current_striker["name"]]["balls"] += 1
            self.current_partnership_balls += 1
            self._credit_partnership_contribution(balls=1)

        # 3. Add 1 run to partnership, then save before the dismissal.
        self.current_partnership_runs += 1
        self._save_partnership("Run Out")

        # 4. 50/50: either batter can be dismissed at run-out.
        dismissed_end = random.choice(["striker", "non_striker"])
        if dismissed_end == "striker":
            dismissed_name = self.current_striker["name"]
        else:
            dismissed_name = self.current_non_striker["name"]

        # 5. Set dismissal info on the dismissed batter.
        fielder_name = self._select_fielder_for_wicket("Run Out")
        self.batsman_stats[dismissed_name]["wicket_type"] = "Run Out"
        self.batsman_stats[dismissed_name]["fielder_out"] = fielder_name

        # 6. Commentary
        commentary_line += self._generate_wicket_commentary(outcome, fielder_name)
        self.commentary.append(commentary_line)

        return dismissed_end, dismissed_name, fielder_name, commentary_line

    def _apply_normal_wicket(self, outcome, extra, commentary_line):
        """
        Apply a non-run-out dismissal (Bowled / LBW / Caught / Stumped / Hit Wicket):
        striker is always dismissed and no runs are scored from the bat.

        Returns (dismissed_end, dismissed_name, fielder_name, commentary_line).
        """
        wicket_type = outcome["wicket_type"]

        is_legal_delivery = not extra
        if extra:
            extra_type = outcome.get("extra_type", "")
            if extra_type in ("Byes", "Leg Bye"):
                is_legal_delivery = True

        if is_legal_delivery:
            self.current_ball += 1
            self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1
            self.batsman_stats[self.current_striker["name"]]["balls"] += 1
            self._credit_partnership_contribution(balls=1)

        self._save_partnership(wicket_type)

        dismissed_end = "striker"
        dismissed_name = self.current_striker["name"]
        fielder_name = None

        self.batsman_stats[dismissed_name]["wicket_type"] = wicket_type
        self.batsman_stats[dismissed_name]["bowler_out"] = self.current_bowler["name"]

        if wicket_type in ("Caught", "Stumped"):
            # calculate_outcome() already picked the fielder (before rolling
            # the catch-drop odds off that fielder's own rating) and attached
            # it to the outcome. Only re-select here as a defensive fallback.
            fielder_name = outcome.get("fielder_name") or self._select_fielder_for_wicket(wicket_type)
            self.batsman_stats[dismissed_name]["fielder_out"] = fielder_name

        commentary_line += self._generate_wicket_commentary(outcome, fielder_name)
        self.commentary.append(commentary_line)

        return dismissed_end, dismissed_name, fielder_name, commentary_line

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
        
        # Apply half-quota limit to all-rounders
        limited_all_rounders = []
        ar_limit = self.fmt.max_bowler_overs // 2
        for ar in all_rounders:
            overs_bowled = quota_analysis[ar['name']]['overs_bowled']
            if overs_bowled < ar_limit:  # Allow up to half-quota
                limited_all_rounders.append(ar)
                print(f"    ✅ {ar['name']}: {overs_bowled}/{ar_limit} overs - Available")
            else:
                print(f"    🚫 {ar['name']}: {overs_bowled}/{ar_limit} overs - Limit reached")
        
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
        crucial_overs = ([0] + [pp.end for pp in self.fmt.powerplay_phases]
                         + list(range(self.fmt.death_phase.start, self.fmt.overs)))
        is_crucial = self.current_over in crucial_overs
        
        print(f"    📈 Form Filter - Crucial over: {is_crucial}")
        
        if is_crucial:
            # Sort by rating and keep top 50% (ceiling), with a floor of 2 so that
            # small pools (e.g. exactly 2 eligible bowlers) retain both candidates
            # and downstream stages still have variance to work with.
            import math
            sorted_bowlers = sorted(eligible_bowlers, key=lambda b: b["bowling_rating"], reverse=True)
            crucial_count = max(2, math.ceil(len(sorted_bowlers) / 2))
            form_filtered = sorted_bowlers[:crucial_count]
            
            print(f"    Ratings: {[(b['name'], b['bowling_rating']) for b in sorted_bowlers]}")
            print(f"    Top {crucial_count} selected: {[b['name'] for b in form_filtered]}")
            return form_filtered
        else:
            print(f"    Non-crucial over - no form filtering")
            return eligible_bowlers

    def _apply_matchup_strategy_debug(self, eligible_bowlers):
        """Enhanced matchup strategy — considers batter's hand, rating, and bowler type"""
        striker = self.current_striker
        striker_hand = striker.get("batting_hand", "Right")
        striker_rating = striker.get("batting_rating", 50)
        print(f"    🥊 Matchup - Striker: {striker.get('name', '?')} (hand={striker_hand}, rating={striker_rating})")

        scored = []
        for b in eligible_bowlers:
            bonus = 0
            btype = b.get("bowling_type", "")
            bhand = b.get("bowling_hand", "Right")

            # 1. Left-arm pace angle vs right-handers
            if bhand == "Left" and striker_hand == "Right" and btype in ("Fast", "Fast-medium", "Medium-fast"):
                bonus += 8

            # 2. Spin turning away from bat
            if btype in ("Off spin", "Finger spin") and striker_hand == "Left":
                bonus += 10
            if btype in ("Leg spin", "Wrist spin") and striker_hand == "Right":
                bonus += 10

            # 3. Pace vs tail-enders
            if striker_rating < 30 and btype in ("Fast", "Fast-medium", "Medium-fast"):
                bonus += 12

            # 4. Spin vs lower-order on turning tracks
            if striker_rating < 45 and btype in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"):
                if self.pitch in ("Dry",):
                    bonus += 8

            scored.append((b, bonus))

        scored.sort(key=lambda x: -x[1])

        if scored and scored[0][1] >= 8:
            best_bowler, best_bonus = scored[0]
            if 'original_bowling_rating' not in best_bowler:
                best_bowler['original_bowling_rating'] = best_bowler['bowling_rating']
            boost_pct = min(best_bonus / 100, 0.15)  # Max 15% boost
            best_bowler['bowling_rating'] = min(100, int(best_bowler['bowling_rating'] * (1 + boost_pct)))
            print(f"    ✅ Best matchup: {best_bowler['name']} (bonus={best_bonus}, type={best_bowler.get('bowling_type','')})")
            return [b for b, _ in scored]

        print(f"    No strong matchups — using all eligible")
        return eligible_bowlers

    def _is_death_specialist(self, bowler):
        """Check if bowler is a death specialist.

        A bowler is a death specialist when they are categorised as 'fast'
        (_categorize_bowler returns 'fast' iff bowling_type is in
        ['Fast', 'Fast-medium', 'Medium-fast']) AND have a bowling_rating >= 75.
        The redundant bowling_type membership check has been removed.
        """
        return (
            self._categorize_bowler(bowler) == "fast"
            and bowler["bowling_rating"] >= 75
        )

    def _calculate_death_overs_risk(self, death_specialists):
        """Calculate risk level for death overs coverage with detailed logging"""
        print(f"    💡 Death overs risk calculation:")
        
        total_remaining_overs = 0
        for specialist in death_specialists:
            bowled = self.bowler_history.get(specialist["name"], 0)
            remaining = self.fmt.max_bowler_overs - bowled
            total_remaining_overs += remaining
            print(f"    {specialist['name']}: bowled={bowled}, remaining={remaining}")
        
        death_overs_needed = self.fmt.overs - self.fmt.death_phase.start
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
        Count death-specialist overs used in completed middle overs.
        """
        specialist_names = {
            b["name"] for b in self.bowling_team if self._is_death_specialist(b)
        }
        if not specialist_names:
            return 0

        used = 0
        for over_idx, bowler_name in getattr(self, "over_bowler_log", {}).items():
            # Count only completed overs; current_over may already be selected/logged.
            if over_idx >= self.current_over:
                continue
            if self.fmt.is_middle(over_idx) and bowler_name in specialist_names:
                used += 1
        return used


    def _analyze_quota_status(self, all_bowlers):
        """Comprehensive quota analysis with detailed tracking"""
        quota_analysis = {}
        
        print(f"  🔍 Detailed Quota Analysis:")
        
        for bowler in all_bowlers:
            overs_bowled = self.bowler_history.get(bowler["name"], 0)
            max_q = self.fmt.max_bowler_overs
            overs_remaining = max(0, max_q - overs_bowled)  # Never negative
            percentage = (overs_bowled / max_q) * 100

            # STRICT STATUS DETERMINATION
            if overs_bowled >= max_q:
                status = "EXHAUSTED"
                exhausted = True
            elif max_q > 0 and (overs_bowled / max_q) >= 0.75:
                status = "CRITICAL (75%+)"
                exhausted = False
            elif max_q > 0 and (overs_bowled / max_q) >= 0.50:
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
                'exhausted': exhausted  # STRICT: True only if >= format max overs
            }
            
            print(f"    {bowler['name']}: {overs_bowled}/{self.fmt.max_bowler_overs} ({percentage:.1f}%) - {status}")
        
        return quota_analysis

    def _assess_constraint_risk(self, all_bowlers, quota_analysis):
        """Assess risk level for constraint violations"""
        print(f"  🔍 Constraint Risk Assessment:")
        
        # Calculate key metrics
        total_overs_remaining = self.fmt.overs - (self.current_over + 1)
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
        
        if self.fmt.is_death(self.current_over) and available_bowlers <= 3:
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
        
        if self.fmt.is_powerplay(self.current_over):  # Powerplay
            return eligible_bowlers
        elif self.fmt.is_middle(self.current_over):  # Middle overs - prefer regulars
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
        if self.fmt.is_powerplay(self.current_over):
            return "POWERPLAY"
        elif self.fmt.is_death(self.current_over):
            return "DEATH_OVERS"
        else:
            return "MIDDLE_OVERS"

    def _apply_strict_quota_policy(self, all_bowlers, quota_analysis):
        """Strictly enforce bowling quota policy"""
        print(f"  🔒 Quota Policy Enforcement ({self.fmt.max_bowler_overs} overs/bowler):")
        
        quota_eligible = []
        
        for bowler in all_bowlers:
            bowler_data = quota_analysis[bowler["name"]]
            
            if not bowler_data['exhausted']:
                quota_eligible.append(bowler)
                print(f"    ✅ {bowler['name']}: {bowler_data['overs_remaining']} overs remaining")
            else:
                print(f"    ❌ {bowler['name']}: EXHAUSTED ({self.fmt.max_bowler_overs}/{self.fmt.max_bowler_overs} overs)")
        
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
            if bowler_data['overs_bowled'] < self.fmt.max_bowler_overs:
                preferred_bowlers.append(bowler)
            else:
                fallback_bowlers.append(bowler)

        max_q = self.fmt.max_bowler_overs
        print(f" Preferred (< {max_q} overs): {[b['name'] for b in preferred_bowlers]}")
        print(f" Fallback ({max_q}+ overs): {[b['name'] for b in fallback_bowlers]}")
        
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
        # _is_powerplay_eligible already enforces the no-consecutive rule internally,
        # so fast_bowlers here will NEVER contain the previous over's bowler.
        fast_bowlers = [
            b for b in all_bowlers
            if self._is_fast_bowler(b) and self._is_powerplay_eligible(b, quota_analysis)
        ]

        if not fast_bowlers:
            # No eligible fast bowler — either no fast bowlers in squad, or the only one
            # just bowled the previous over (consecutive conflict).
            # Fallback: try any bowling type that is quota-eligible AND non-consecutive,
            # so the early-overs override doesn't silently vanish when the top pacer
            # is unavailable.
            print(f"  ⚠️  No eligible non-consecutive fast bowler for early overs — trying any-type fallback")
            any_type_bowlers = [
                b for b in all_bowlers
                if self._is_powerplay_eligible(b, quota_analysis)
            ]
            if not any_type_bowlers:
                print(f"  ❌ No eligible bowler of any type for early-overs override — handing off to main pipeline")
                return None
            any_type_bowlers.sort(key=lambda b: (
                b['bowling_rating'],
                0 if b['role'] == 'Bowler' else 1
            ), reverse=True)
            selected = any_type_bowlers[0]
            overs_bowled = quota_analysis[selected['name']]['overs_bowled']
            print(f"  ✅ EARLY OVERS FALLBACK ({selected['bowling_type']}): {selected['name']} "
                  f"(Rating: {selected['bowling_rating']}, Overs: {overs_bowled}/{self.fmt.max_bowler_overs})")
            return selected

        # Sort by rating first (highest to lowest), then by role (pure bowlers > all-rounders)
        fast_bowlers.sort(key=lambda b: (
            b['bowling_rating'],
            0 if b['role'] == 'Bowler' else 1
        ), reverse=True)

        selected = fast_bowlers[0]
        overs_bowled = quota_analysis[selected['name']]['overs_bowled']

        print(f"  ✅ EARLY OVERS FAST: {selected['name']} (Rating: {selected['bowling_rating']}, Overs: {overs_bowled}/{self.fmt.max_bowler_overs})")
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

    def _is_consecutive_bowler(self, bowler):
        """
        Centralised consecutive-bowling guard — single source of truth.
        Returns True if `bowler` is the exact same person who bowled the previous over.
        Used as an explicit second-layer check in every early-return path inside
        pick_bowler(), even when the helper functions already call _is_powerplay_eligible
        (which also enforces this rule internally) — defence-in-depth approach.
        """
        return bool(self.current_bowler and bowler["name"] == self.current_bowler["name"])

    def _is_powerplay_eligible(self, bowler, quota_analysis):
        """Check if bowler is eligible for powerplay selection"""
        bowler_data = quota_analysis[bowler['name']]
        
        # Must have overs remaining
        if bowler_data['overs_bowled'] >= self.fmt.max_bowler_overs:
            return False
        
        # Must not have bowled previous over (consecutive check)
        if self.current_bowler and bowler['name'] == self.current_bowler['name']:
            return False
            
        return True

    def _is_constraint_eligible(self, bowler, quota_analysis):
        """
        Check if bowler satisfies ALL hard constraints:
          1. Quota  — has at least 1 over remaining (< 4 overs bowled)
          2. Non-consecutive — is NOT the same bowler who bowled the previous over
        Both conditions must hold; failure in either returns False.
        """
        if not self._is_powerplay_eligible(bowler, quota_analysis):
            return False
        if self._is_consecutive_bowler(bowler):
            return False
        return True

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
            death_overs_needed = self.fmt.overs - self.fmt.death_phase.start
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
        remaining_match_overs = self.fmt.overs - (self.current_over + 1)
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
        if self.fmt.name == "ListA":
            return self._pick_bowler_lista()

        # ================ DEATH OVERS SPECIAL HANDLING ================
        if self.fmt.is_death(self.current_over):
            print(f"\n🎯 === SWITCHING TO DEATH OVERS MODE ===")
            return self._pick_death_overs_bowler()
        
        # ================ CRITICAL 2-BOWLER SCENARIO PRE-CHECK ================
        # Check for 2-bowler scenario even before death overs (overs 16-17)
        if self.current_over >= self.fmt.death_phase.start - 1:  # Start checking 1 over before death
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
            # STRICT CONSECUTIVE GUARD — even if the helper somehow returns the previous
            # bowler (e.g. due to an internal bug), we refuse to use that result.
            if early_overs_result and not self._is_consecutive_bowler(early_overs_result):
                print(f"🚀 EARLY OVERS FAST OVERRIDE: Selected {early_overs_result['name']}")
                self._update_bowler_tracking(early_overs_result)
                return early_overs_result
            elif early_overs_result:
                print(f"🚫 EARLY OVERS FAST OVERRIDE BLOCKED: {early_overs_result['name']} would bowl "
                      f"consecutive overs — falling through to main pipeline")

        # ================ NEW: STAR NEGLECT PREVENTION ================
        if self.current_over >= 10:  # After over 10
            neglect_result = self._prevent_star_neglect(bowler_tiers, quota_analysis)
            # _prevent_star_neglect already calls _is_constraint_eligible (which now checks
            # consecutive), but we add an explicit outer guard as a second layer of defence.
            if neglect_result and not self._is_consecutive_bowler(neglect_result):
                print(f"⚡ STAR NEGLECT PREVENTION: Selected {neglect_result['name']}")
                self._update_bowler_tracking(neglect_result)
                return neglect_result
            elif neglect_result:
                print(f"🚫 STAR NEGLECT PREVENTION BLOCKED: {neglect_result['name']} would bowl "
                      f"consecutive overs — falling through to main pipeline")

        # ================ NEW: LOW-RATED BOWLER STRATEGIC USAGE ================
        if self.current_over >= 5:  # After early overs
            low_rated_result = self._try_low_rated_bowler_usage(bowler_tiers, quota_analysis)
            # _try_low_rated_bowler_usage already calls _is_constraint_eligible (which now checks
            # consecutive), but we add an explicit outer guard as a second layer of defence.
            if low_rated_result and not self._is_consecutive_bowler(low_rated_result):
                print(f"🎯 LOW-RATED STRATEGIC: Selected {low_rated_result['name']}")
                self._update_bowler_tracking(low_rated_result)
                return low_rated_result
            elif low_rated_result:
                print(f"🚫 LOW-RATED STRATEGIC BLOCKED: {low_rated_result['name']} would bowl "
                      f"consecutive overs — falling through to main pipeline")

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
        print(f"After {self.fmt.max_bowler_overs}-overs filter: {[b['name'] for b in quota_eligible]}")

        # Sub-phase 1B: No Consecutive Policy Enforcement
        constraint_eligible = self._apply_strict_consecutive_policy(quota_eligible, risk_assessment)
        print(f"After no-consecutive filter: {[b['name'] for b in constraint_eligible]}")

        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
        # SPECIAL HANDLING: DEAD or FLAT PITCH → force Spinner/Medium-fast/Medium,
        # then boost their bowling_rating by 10%.
        if self.pitch in ("Dead", "Flat"):
            # 1. Keep only Spinner / Medium-fast among constraint_eligible
            filtered = [
                b for b in constraint_eligible
                if b["bowling_type"] in ("Off spin", "Leg spin", "Finger spin", "Wrist spin", "Medium-fast")
            ]
            if filtered:
                # 2. Replace constraint_eligible with that filtered list
                constraint_eligible = filtered

                # 3. Temporarily boost each bowler's bowling_rating by 10% (capped at 100)
                for bowler in constraint_eligible:
                    if bowler.get("_orig_bowling_rating") is None:  # Fix D6: Correct typo
                        bowler["_orig_bowling_rating"] = bowler["bowling_rating"]
                    bowler["bowling_rating"] = int(
                        min(bowler["_orig_bowling_rating"] * 1.1, 100)
                    )
        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
        # NEW: Ensure every 'will_bowl=True' bowler bowls at least 1 over.
        # If the number of remaining overs equals the count of fresh bowlers,
        # force selection from those who haven't yet bowled.
        remaining_overs = self.fmt.overs - (self.current_over + 1)
        # Fresh means: marked will_bowl AND overs_bowled == 0
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

        # Add this right before "return selected_bowler" in pick_bowler()
        # ================ PRODUCTION SAFETY NET ================
        print(f"\n--- PRODUCTION CONSECUTIVE VALIDATION ---")
        self._absolute_consecutive_validation(selected_bowler)
        print(f"✅ CONSECUTIVE VALIDATION PASSED: {selected_bowler['name']} is safe to bowl")

        # Update tracking
        self._update_bowler_tracking(selected_bowler)

        # Project future implications
        future_projection = self._project_future_constraints(selected_bowler, all_bowlers)
        print(f"📈 FUTURE PROJECTION:")
        print(f"  Remaining overs: {self.fmt.overs - (self.current_over + 1)}")
        print(f"  Available bowlers after this over: {future_projection['available_count']}")
        print(f"  Potential risk next over: {future_projection['next_over_risk']}")

        print(f"\n🏁 === BOWLER SELECTION COMPLETE ===\n")

        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
        # If we had boosted any Spinner/Medium-fast/Medium earlier, revert their rating now
        for bowler in self.bowling_team:
            if bowler.get("_orig_bowling_rating") is not None:  # Fix D6: Correct typo
                bowler["bowling_rating"] = bowler["_orig_bowling_rating"]
                del bowler["_orig_bowling_rating"]
        # –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

        return selected_bowler


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
        # 'Dusty' is not a valid pitch type; valid values are Green/Dry/Hard/Flat/Dead.
        # Green = seam-friendly, Dry = spin-friendly — both favour bowlers.
        if self.pitch in ['Green', 'Dry'] and pressure_score >= 60:
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

    # ── RAIN & DLS ─────────────────────────────────────────────────────────────
    # Rain events come from the pre-rolled weather script (see engine/weather.py)
    # and are consumed at over boundaries. Target revision uses the Standard
    # Edition DLS resource table (engine/dls.py).

    def _set_innings_overs(self, revised_overs):
        """Apply a revised allocation to the innings in progress. Mutating the
        per-match fmt copy propagates the new overs + bowler quota to every
        consumer (BowlerManager, GSME, pressure engine, UI payloads)."""
        self.overs = revised_overs
        self.fmt.overs = revised_overs
        self.fmt.max_bowler_overs = weather_engine.revised_max_bowler_overs(revised_overs)

    def _dls_suffix(self):
        return " (DLS method)" if getattr(self, "rain_affected", False) else ""

    def _dls_g50(self):
        """Expected 100%-resource innings total for the R2 > R1 branch of the
        DLS target formula, derived from the sim's own pitch-adjusted par."""
        expected = self.fmt.target_scores.get(self.pitch)
        if expected is None:
            expected = 245.0 if self.fmt.name == "ListA" else 165.0
        return dls.g50_from_expected_total(float(expected), self.original_overs)

    def _global_overs_completed(self):
        """Completed overs across the whole match — the weather script's clock."""
        if self.innings == 1:
            return self.current_over
        base = self._innings1_overs_bowled
        if base is None:
            base = self.original_overs
        return base + self.current_over

    def _compute_innings2_target(self):
        """Target for the chase at the innings break. Plain S+1 in dry
        matches; DLS whenever rain has touched the match."""
        if not self.rain_affected:
            return self.score + 1
        if self.dls_ledger_innings2 is None:
            self.dls_ledger_innings2 = dls.ResourceLedger(self.overs)
        return dls.compute_target(
            self.score,
            self.dls_ledger_innings1.available(),
            self.dls_ledger_innings2.available(),
            self._dls_g50(),
        )

    def _recompute_dls_target(self):
        """Re-derive the chase target after an innings-2 interruption."""
        self.target = dls.compute_target(
            self.first_innings_score,
            self.dls_ledger_innings1.available(),
            self.dls_ledger_innings2.available(),
            self._dls_g50(),
        )
        return self.target

    def _current_dls_par(self):
        """Live DLS par score for the chase (None when not applicable). The
        chasing side wins a washed-out match if it is ahead of this number."""
        if (self.innings != 2 or not self.rain_affected
                or self.first_innings_score is None):
            return None
        if self.dls_ledger_innings2 is None:
            self.dls_ledger_innings2 = dls.ResourceLedger(self.overs)
        return dls.par_score(
            self.first_innings_score,
            self.dls_ledger_innings1.available(),
            self.dls_ledger_innings2.available(),
            dls.overs_from_balls(self._balls_left_in_innings()),
            self.wickets,
        )

    def _rain_commentary(self, kind, **ctx):
        """Dedicated rain commentary pools. Returns a list of HTML lines."""
        if kind == "foreshadow":
            return [random.choice([
                "<em>Dark clouds are rolling in over the ground... the umpires exchange a glance.</em>",
                "<em>The floodlights have taken effect early — there's weather about.</em>",
                "<em>Spectators reaching for their raincoats. Something's brewing up there.</em>",
                "<em>A rumble in the distance. The ground staff edge towards the covers.</em>",
            ])]
        if kind == "covers_on":
            return [
                random.choice([
                    "🌧️ <strong>RAIN STOPS PLAY!</strong> The heavens open and the umpires whip the bails off. Covers coming on at a sprint!",
                    "🌧️ <strong>RAIN STOPS PLAY!</strong> A grey curtain sweeps across the ground. The players dash for the pavilion.",
                    "🌧️ <strong>THE RAIN ARRIVES!</strong> Umpires confer for barely a second — everyone off. The square is covered in moments.",
                ]),
                f"<em>The delay costs the match {ctx['overs_lost']} over(s).</em>",
            ]
        if kind == "resume_innings1":
            return [
                f"☂️ <strong>Play resumes.</strong> The match is reduced to <strong>{ctx['revised_overs']} overs a side</strong>. "
                f"Bowlers are limited to {ctx['max_bowler_overs']} overs each.",
            ]
        if kind == "innings1_cut":
            return [
                f"☂️ <strong>The innings is over.</strong> Rain has ended the first innings at {ctx['score']}/{ctx['wickets']} — "
                f"the chase will be revised by the DLS method.",
            ]
        if kind == "resume_chase":
            return [
                f"☂️ <strong>Play resumes.</strong> The chase is now <strong>{ctx['target']} from {ctx['revised_overs']} overs</strong> (DLS method). "
                f"Bowlers are limited to {ctx['max_bowler_overs']} overs each.",
            ]
        if kind == "chase_reduced_before_start":
            return [
                f"☂️ <strong>Revised chase:</strong> rain at the innings break cuts the chase to "
                f"<strong>{ctx['target']} from {ctx['revised_overs']} overs</strong> (DLS method).",
            ]
        return []

    def _maybe_foreshadow_rain(self):
        """One atmospheric line when a scripted rain event is 1-2 overs away."""
        events = (self.weather_script or {}).get("events") or []
        if self.innings not in (1, 2) or self.weather_next_event >= len(events):
            return None
        gap = events[self.weather_next_event]["at_global_over"] - self._global_overs_completed()
        if 1 <= gap <= 2:
            return self._rain_commentary("foreshadow")[0]
        return None

    def _check_rain_events(self):
        """Consume any due weather-script events at an over boundary.

        Returns None when nothing happens, otherwise:
          {"final": <match-over payload>}          — rain ended the match
          {"lines": [...], "info": {...}}          — play continues, revised
        """
        if self.innings not in (1, 2):
            return None
        events = (self.weather_script or {}).get("events") or []
        lines, info = [], None
        while self.weather_next_event < len(events):
            # Innings 1 is complete but the transition hasn't run yet: defer
            # so the event lands on the chase as a pre-start reduction.
            if self.innings == 1 and self.current_over >= self.overs:
                break
            ev = events[self.weather_next_event]
            if ev["at_global_over"] > self._global_overs_completed():
                break
            self.weather_next_event += 1
            outcome = self._apply_rain_event(ev)
            if outcome.get("final") is not None:
                return {"final": outcome["final"]}
            lines.extend(outcome.get("lines", []))
            info = outcome.get("info") or info
        if not lines and info is None:
            return None
        return {"lines": lines, "info": info}

    def _apply_rain_event(self, ev):
        """Apply one scripted rain event to the current game situation."""
        overs_lost = ev["overs_lost"]
        overs_completed = self.current_over
        resolution = weather_engine.resolve_interruption(
            self.innings, overs_completed, self.overs, overs_lost, self.fmt.name
        )
        rtype = resolution["type"]
        revised = resolution["revised_overs"]

        self.rain_affected = True
        self.data["rain_affected"] = True
        self.rain_events_log.append({
            "innings": self.innings,
            "at_over": overs_completed,
            "overs_lost": overs_lost,
            "outcome": rtype,
            "revised_overs": revised,
        })
        lines = self._rain_commentary("covers_on", overs_lost=overs_lost)

        if rtype == weather_engine.NO_RESULT:
            return {"final": self._finalize_no_result(lines)}

        if self.innings == 1:
            prev_allocation = self.overs
            if rtype == weather_engine.INNINGS_TERMINATED:
                self.dls_ledger_innings1.record_termination(
                    prev_allocation - overs_completed, self.wickets
                )
                self._set_innings_overs(overs_completed)
                lines += self._rain_commentary(
                    "innings1_cut", score=self.score, wickets=self.wickets
                )
            else:  # RESUME
                self.dls_ledger_innings1.record_interruption(
                    prev_allocation - overs_completed, self.wickets,
                    revised - overs_completed,
                )
                self._set_innings_overs(revised)
                lines += self._rain_commentary(
                    "resume_innings1", revised_overs=revised,
                    max_bowler_overs=self.fmt.max_bowler_overs,
                )
            info = {
                "innings": 1,
                "at_over": overs_completed,
                "overs_lost": overs_lost,
                "revised_overs": self.overs,
                "max_bowler_overs": self.fmt.max_bowler_overs,
                "outcome": rtype,
            }
            return {"lines": lines, "info": info}

        # ── Innings 2: the chase ────────────────────────────────────────
        if self.dls_ledger_innings2 is None:
            self.dls_ledger_innings2 = dls.ResourceLedger(self.overs)
        prev_allocation = self.overs

        if rtype == weather_engine.CHASE_TERMINATED:
            self.dls_ledger_innings2.record_termination(
                prev_allocation - overs_completed, self.wickets
            )
            self._recompute_dls_target()
            return {"final": self._finalize_chase_terminated(lines)}

        # RESUME
        self.dls_ledger_innings2.record_interruption(
            prev_allocation - overs_completed, self.wickets,
            revised - overs_completed,
        )
        self._set_innings_overs(revised)
        self._recompute_dls_target()
        kind = "chase_reduced_before_start" if overs_completed == 0 else "resume_chase"
        lines += self._rain_commentary(
            kind, target=self.target, revised_overs=revised,
            max_bowler_overs=self.fmt.max_bowler_overs,
        )
        if self.score >= self.target:
            # The revision leaves the chasers already past the new target.
            return {"final": self._finalize_rain_chase_won(lines)}
        info = {
            "innings": 2,
            "at_over": overs_completed,
            "overs_lost": overs_lost,
            "revised_overs": self.overs,
            "target": self.target,
            "max_bowler_overs": self.fmt.max_bowler_overs,
            "outcome": rtype,
        }
        return {"lines": lines, "info": info}

    def _rain_final_payload(self, scorecard_data, lines):
        """Common shape for a rain-decided match-over response."""
        lines = list(lines)
        lines.append(f"<strong>Match Over!</strong> {self.result}")
        return {
            "match_over": True,
            "scorecard_data": scorecard_data,
            "final_score": self.score,
            "wickets": self.wickets,
            "result": self.result,
            "commentary": "<br>".join(lines),
            "rain_affected": True,
        }

    def _finalize_no_result(self, lines):
        """Rain has washed the match out before a result was possible."""
        washed_out_in_first_innings = (self.innings == 1)
        if self.wickets < 10 and self.current_partnership_balls > 0:
            self._save_partnership("not_out")
        if washed_out_in_first_innings:
            self._save_first_innings_stats()
            # Prevent the innings-3 guard from copying first-innings stats
            # into the (never played) second innings.
            self._second_innings_stats_saved = True
        else:
            self._save_second_innings_stats()
        scorecard_data = self._generate_detailed_scorecard()
        scorecard_data["target_info"] = "Match abandoned due to rain — No Result"
        self._set_outcome(
            result_text="Match abandoned due to rain. No result.",
            winner_is_home=None, match_status='no_result',
            margin_type=None, margin_value=None,
        )
        self.innings = 3
        self._create_match_archive()
        if washed_out_in_first_innings:
            # The completion handlers read the live stats dicts as "second
            # innings" data — clear them so an innings-1 washout does not
            # duplicate the first innings onto the side that never batted.
            self.batsman_stats = {}
            self.bowler_stats = {}
        return self._rain_final_payload(scorecard_data, lines)

    def _finalize_chase_terminated(self, lines):
        """Rain has ended the chase after the minimum overs: DLS par decides."""
        par = self.target - 1
        if self.wickets < 10 and self.current_partnership_balls > 0:
            self._save_partnership("not_out")
        chasing_code = self._get_team_name(self.batting_team)
        defending_code = self._get_team_name(self.bowling_team)
        lines.append(
            f"<em>No further play possible. DLS par score at the stoppage: "
            f"<strong>{par}</strong> — {chasing_code} are {self.score}/{self.wickets}.</em>"
        )
        if self.score > par:
            margin = self.score - par
            self._set_outcome(
                result_text=f"{chasing_code} won by {margin} run(s) (DLS method).",
                winner_is_home=(self.batting_team is self.home_xi),
                match_status='completed', margin_type='runs', margin_value=margin,
            )
        elif self.score == par:
            self._set_outcome(
                result_text="Match Tied (DLS method).",
                winner_is_home=None, match_status='tied',
                margin_type='tie', margin_value=0,
            )
        else:
            margin = par - self.score
            self._set_outcome(
                result_text=f"{defending_code} won by {margin} run(s) (DLS method).",
                winner_is_home=(self.bowling_team is self.home_xi),
                match_status='completed', margin_type='runs', margin_value=margin,
            )
        scorecard_data = self._generate_detailed_scorecard()
        scorecard_data["target_info"] = self.result
        self.innings = 3
        self._save_second_innings_stats()
        self._create_match_archive()
        return self._rain_final_payload(scorecard_data, lines)

    def _finalize_rain_chase_won(self, lines):
        """A target revision leaves the chasing side already home."""
        if self.wickets < 10 and self.current_partnership_balls > 0:
            self._save_partnership("not_out")
        winner_code = self._get_team_name(self.batting_team)
        wkts_left = 10 - self.wickets
        self._set_outcome(
            result_text=f"{winner_code} won by {wkts_left} wicket(s) (DLS method).",
            winner_is_home=(self.batting_team is self.home_xi),
            match_status='completed', margin_type='wickets', margin_value=wkts_left,
        )
        scorecard_data = self._generate_detailed_scorecard()
        scorecard_data["target_info"] = self.result
        self.innings = 3
        self._save_second_innings_stats()
        self._create_match_archive()
        return self._rain_final_payload(scorecard_data, lines)

    def _innings_should_end(self):
        if self.is_fc:
            return self._fc_innings_should_end()
        return self.current_over >= self.overs or self.wickets >= 10

    def _transition_to_next_innings(self):
        if self.is_fc:
            return self._fc_transition_to_next_innings()
        # 🤝 SAVE UNFINISHED PARTNERSHIP (if overs completed and not all out)
        if self.wickets < 10 and self.current_partnership_balls > 0:
             self._save_partnership("not_out")

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
            self.target = self._compute_innings2_target()
            required_rr = self.target / self.overs
            chasing_team_code = self.data["team_away"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_home"].split("_")[0]
            _target_info = f"{chasing_team_code} needs {self.target} runs from {self.overs} overs at {required_rr:.2f} runs per over"
            if self.rain_affected:
                _target_info += " (DLS method)"
            scorecard_data["target_info"] = _target_info

            self._save_first_innings_stats()
            self.first_innings_scorecard = scorecard_data

            self.innings = 2

            # Reload lineups from data to ensure impact player/reordering changes are applied
            if hasattr(self, 'data') and self.data.get('impact_players_swapped'):
                print("🔄 [Innings Change] Applying impact player and reordering changes.")
                self.home_xi = self.data["playing_xi"]["home"]
                self.away_xi = self.data["playing_xi"]["away"]

            # --- START FIX ---
            # Re-determine batting and bowling teams based on original toss decision and the UPDATED XIs
            team_home_code = self.match_data["team_home"].split("_")[0]
            self.batting_team, self.bowling_team = innings_teams(
                self.toss_winner, self.toss_decision, team_home_code,
                self.home_xi, self.away_xi, innings=2,
            )
            # --- END FIX ---

            # Capture first innings wickets before reset
            first_innings_wickets = self.wickets

            innings_complete_summary = self._format_innings_complete_summary("End of innings")

            # Weather clock: actual completed overs of innings 1
            self._innings1_overs_bowled = self.current_over

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

            self.batsman_stats = {p["name"]: self._new_batting_stats(p) for p in self.batting_team}
            # bowler_history reset is handled inside _reset_innings_state() via BowlerManager
            self.bowler_stats = {p["name"]: self._new_bowling_stats(p) for p in self.bowling_team if p.get("will_bowl")}
            self._reset_innings_state()

            # Notify scenario engine of innings transition
            if self.scenario_engine:
                self.scenario_engine.on_innings_transition()

            return {
                "innings_end": True,
                "innings_number": 1,
                "match_over": False,
                "scorecard_data": scorecard_data,
                "score": 0,
                "wickets": 0,
                "over": 0,
                "ball": 0,
                "commentary": (
                    f"{self._format_innings_complete_summary('End of innings')}<br>"
                    f"<strong>End of 1st Innings:</strong> {self.first_innings_score}/{10 if first_innings_wickets >= 10 else first_innings_wickets}. "
                    f"Target: {self.target}"
                ),
                "striker": self.current_striker["name"],
                "non_striker": self.current_non_striker["name"],
                "bowler": ""
            }

        else:
            scorecard_data = self._generate_detailed_scorecard()
            if self.score >= self.target:
                # 🤝 SAVE UNFINISHED PARTNERSHIP (Match Won)
                if self.wickets < 10:
                    self._save_partnership("not_out")

                winner_code = self.data["team_home"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_away"].split("_")[0]
                wkts_left = 10 - self.wickets

                balls_left = self._balls_left_in_innings()
                overs_left = self._balls_to_overs_notation(balls_left)

                print("Check points: {}".format({
                    "current_over": self.current_over,
                    "current_ball": self.current_ball,
                    "balls_left": balls_left,
                    "overs_left": overs_left
                }))

                self._set_outcome(
                    result_text=f"{winner_code} won by {wkts_left} wicket(s) with {overs_left} overs remaining.{self._dls_suffix()}",
                    winner_is_home=(self.batting_team is self.home_xi),
                    match_status='completed', margin_type='wickets', margin_value=wkts_left,
                )
            else:
                # Check for tie
                if self.score == self.target - 1:
                    self.result = "Match Tied"

                    # 🤝 SAVE UNFINISHED PARTNERSHIP (Match Tied)
                    if self.wickets < 10:
                        self._save_partnership("not_out")

                    # ✅ Store original scorecard for later display
                    self.original_scorecard = self._generate_detailed_scorecard()
                    self.original_scorecard["target_info"] = "Match Tied"

                    # ✅ Set up super over (this won't affect main match stats)
                    self.innings = 4
                    return self._setup_super_over()
                else:
                    winner_code = self.data["team_home"].split("_")[0] if self.bowling_team is self.home_xi else self.data["team_away"].split("_")[0]
                    run_diff = self.target - self.score - 1
                    self._set_outcome(
                        result_text=f"{winner_code} won by {run_diff} run(s).{self._dls_suffix()}",
                        winner_is_home=(self.bowling_team is self.home_xi),
                        match_status='completed', margin_type='runs', margin_value=run_diff,
                    )

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

            final_commentary = f"{self._format_innings_complete_summary()}<br><br>"
            final_commentary += f"<strong>Match Over!</strong> {self.result}<br>"
            final_commentary += f"<strong>Final Snapshot:</strong><br>"
            final_commentary += f"{self.current_striker['name']} {striker_stats['runs']}({striker_stats['balls']}) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]<br>"
            final_commentary += f"{self.current_non_striker['name']} {non_striker_stats['runs']}({non_striker_stats['balls']}) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]<br>"
            final_commentary += f"{self.current_bowler['name']} {overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}"

            first_block = self._format_scorecard_block(getattr(self, 'first_innings_scorecard', None), '1st Innings Scorecard')
            second_block = self._format_scorecard_block(scorecard_data, '2nd Innings Scorecard')
            if first_block and second_block:
                final_commentary += f"<br><br><strong>Scorecards:</strong><br>{first_block}<br><br>{second_block}"

            self.innings = 3
            scorecard_data["target_info"] = self.result

            self._save_second_innings_stats()
            self._create_match_archive()

            return {
                "innings_end": True,
                "innings_number": 2,
                "match_over": True,
                "scorecard_data": scorecard_data,
                "final_score": self.score,
                "wickets": self.wickets,
                "result": self.result,
                "commentary": final_commentary
            }

    def _fc_build_match_state(self):
        """Match-state dict for FCPressureEngine.get_pressure_effects() —
        deliberately NOT _calculate_current_match_state()'s shape, which is
        T20/ListA innings==1/2-keyed and has no fc_innings/days_remaining/
        lead concept at all."""
        striker_name = self.current_striker["name"]
        striker_balls_faced = self.batsman_stats.get(striker_name, {}).get("balls", 0)
        days_remaining = self._fc_days_remaining()

        lead = 0
        if self.fc_innings in (2, 3):
            a1 = self.fc_innings_totals.get(1, {}).get("score", 0)
            lead = self.score - a1
        elif self.fc_innings == 4:
            lead = self.score - (self.target or 0)

        # Acceleration: the batting side is running down its own innings
        # time budget and is building toward a declaration. Tied to the
        # budget rather than the whole-match "2 days left" gate, which never
        # fired early enough on a slow surface — the same reason the
        # declaration heuristic had to stop using it.
        _budget = self.fc_innings_time_budget_overs
        acceleration_mode = (
            self.fc_innings in (1, 2, 3) and not self.fc_innings_declared
            and (
                (_budget is not None and self.current_over >= _budget * 0.85)
                or (days_remaining <= 2 and self.current_over >= 60)
            )
        )

        # Survival: batting to save the game. Real first-class cricket is
        # full of this innings — 300 behind with a day and a half left, shut
        # up shop — and it could not happen before, because nothing ever set
        # the flag and FCPressureEngine's own fallback only covers the last
        # day. Without it every match had to end in a result.
        survival_mode = False
        if self.fc_innings == 4 and self.target is not None:
            _needed = self.target - self.score
            _rrr = _needed / max(1.0, self._fc_overs_remaining_in_match())
            # A chase that has drifted out of reach turns into a rearguard —
            # and so does one where the wickets have gone. A side six down
            # chasing a stiff target plays for the draw; it does not keep
            # chasing until it loses.
            survival_mode = _rrr > 4.2 or (self.wickets >= 6 and _rrr > 2.8)
        elif self.fc_innings in (2, 3):
            survival_mode = (
                (self.follow_on_enforced and lead < 0)
                or (lead < -120 and days_remaining <= 2)
            )

        required_run_rate = 0.0
        if self.fc_innings == 4 and self.target is not None:
            required_run_rate = (self.target - self.score) / self._fc_overs_remaining_in_match()

        # The closing overs of a day. Excluded during a live fourth-innings
        # chase — a side going for the win doesn't shut up shop at 6pm.
        _overs_left_today = max(
            0.0, self._fc_effective_overs_today() - self.fc_day_overs_bowled_today)
        _live_chase = (self.fc_innings == 4 and self.target is not None
                       and not survival_mode)
        last_hour = (_overs_left_today <= self.fmt.min_overs_last_hour
                     and not _live_chase)

        return {
            "fc_innings": self.fc_innings,
            "wickets": self.wickets,
            "last_hour": last_hour,
            "striker_balls_faced": striker_balls_faced,
            "days_remaining": days_remaining,
            "recent_wickets": getattr(self, "recent_wickets_count", 0),
            # The current stand. Recorded for the archiver since FC was
            # built, but never fed to anything that could act on it — so a
            # 200-run partnership had no effect on the game at all.
            "partnership_balls": self.current_partnership_balls,
            "partnership_runs": self.current_partnership_runs,
            "lead": lead,
            "acceleration_mode": acceleration_mode,
            "survival_mode": survival_mode,
            "required_run_rate": required_run_rate,
            # Phase 2: technique dampens the settling-in penalty; temperament
            # dampens pressure-driven wicket increases (survival mode,
            # collapse-cluster chance). None (neutral, no dampening) when
            # the player row predates these ratings.
            "striker_technique": self.current_striker.get("technique_rating"),
            "striker_temperament": self.current_striker.get("temperament_rating"),
        }

    def _fc_attack_freshness(self):
        """0-1 read on how much the bowling side has left, averaged over the
        front-line attack. Only meaningful now that fatigue recovers with
        rest (see FCBowlerManager) — before spells existed every attack was
        equally, permanently tired."""
        mgr = self.bowler_manager
        if not hasattr(mgr, "get_fatigue_mult"):
            return None
        bowlers = [p for p in self.bowling_team if p.get("will_bowl")]
        if not bowlers:
            return None
        mults = [mgr.get_fatigue_mult(p["name"], p.get("stamina_rating", 50) or 50)
                 for p in bowlers]
        # Rescale the 0.55-1.0 effectiveness range onto 0-1.
        avg = sum(mults) / len(mults)
        return max(0.0, min(1.0, (avg - 0.55) / 0.45))

    def _fc_declaring_side_freshness(self):
        """Freshness of the batting side's OWN attack — they are the ones who
        must bowl the opposition out after declaring."""
        mgr = self.bowler_manager
        if not hasattr(mgr, "get_fatigue_mult"):
            return None
        bowlers = [p for p in self.batting_team if p.get("will_bowl")]
        if not bowlers:
            return None
        mults = [mgr.get_fatigue_mult(p["name"], p.get("stamina_rating", 50) or 50)
                 for p in bowlers]
        avg = sum(mults) / len(mults)
        return max(0.0, min(1.0, (avg - 0.55) / 0.45))

    def _fc_lead_before_ball(self):
        """Batting side's lead over the opposition's completed innings,
        BEFORE this delivery — so a narrative can spot the moment it goes
        from behind to in front. None when there is no lead concept yet."""
        if not self.is_fc or self.fc_innings == 1:
            return None
        if self.fc_innings == 2:
            return self.score - self.fc_innings_totals.get(1, {}).get("score", 0)
        if self.fc_innings == 3 and not self.follow_on_enforced:
            a1 = self.fc_innings_totals.get(1, {}).get("score", 0)
            b1 = self.fc_innings_totals.get(2, {}).get("score", 0)
            return a1 + self.score - b1
        return None

    def _fc_follow_on_mark(self):
        """Runs the side batting second needs to avoid following on, or
        None when the follow-on isn't in play."""
        if not self.is_fc or self.fc_innings != 2 or self.follow_on_enforced:
            return None
        first = self.fc_innings_totals.get(1, {}).get("score")
        if first is None:
            return None
        return max(0, first - self.fmt.follow_on_margin)

    def _fc_pick_bowler(self):
        """
        FC bowler selection: MCC Law 17.2 eligibility (no consecutive overs)
        + day-stage bowling-style preference
        (engine/fc_bowler_workload.py), then the highest bowling_rating
        within the preferred style bucket. No quota, no death-overs plan,
        no tier system — that's all T20/ListA-specific complexity that
        doesn't apply to FC's uncapped spells.
        """
        eligible = self.bowler_manager.get_eligible_bowlers(self.current_over)
        if not eligible:
            # Only one eligible bowler total — Law 17.2 has no legal
            # alternative; fall back to whoever is marked will_bowl so the
            # match can continue rather than aborting.
            eligible = [p for p in self.bowling_team if p.get("will_bowl", False)]
        pitch_wear = self._compute_pitch_wear()
        ranked = self.bowler_manager.rank_by_style_preference(eligible, pitch_wear, self.fc_day)
        selected = ranked[0]
        self._update_bowler_tracking(selected)
        return selected

    # ========================================================================
    # First-Class (FC) innings state machine
    # ========================================================================
    # fc_innings (1-4) is entirely separate from self.innings, which stays 1
    # for the whole match (see is_fc dispatch in __init__ and next_ball()).
    # Rules implemented here match the design doc's rules reference: up to
    # 2 innings per side, follow-on (MCC Law 14.1), declaration (innings 1
    # and a follow-on-free innings 3 only), draw on day exhaustion, win by
    # innings/runs/wickets, tie (no super over in FC).

    def _fc_innings_should_end(self):
        if self.wickets >= 10 or self.fc_innings_declared:
            return True
        if self.fc_innings == 4 and self.target is not None and self.score >= self.target:
            return True
        return False

    def _fc_days_remaining(self):
        """Full match days left INCLUDING today (today counts as 1)."""
        return max(0, self.fmt.days - self.fc_day + 1)

    # Over rates. A first-class day is 90 overs on the schedule and almost
    # never 90 in practice: a seam-dominated attack with long run-ups gets
    # through fewer, a spin-heavy one gets through more. Modelled as a
    # per-day adjustment fixed at the start of play so the day's length is
    # stable (and so Lunch/Tea don't move around mid-session).
    _FC_OVER_RATE_ALL_PACE = -9      # a four-seamer attack loses overs
    _FC_OVER_RATE_ALL_SPIN = +5      # spinners get through them

    def _fc_compute_day_over_rate_adjust(self):
        """Overs gained or lost today to the over rate, from the bowling
        attack's composition."""
        bowlers = [p for p in (self.bowling_team or []) if p.get("will_bowl")]
        if not bowlers:
            return 0
        spin = sum(1 for p in bowlers
                   if (p.get("bowling_type") or "").strip()
                   in fc_bowler_workload._SPIN_TYPES)
        spin_share = spin / len(bowlers)
        span = self._FC_OVER_RATE_ALL_SPIN - self._FC_OVER_RATE_ALL_PACE
        return int(round(self._FC_OVER_RATE_ALL_PACE + span * spin_share))

    def _fc_effective_overs_today(self):
        """Today's schedulable overs after weather loss and the over rate.
        No longer a flat fmt.overs_per_day: days now come in a bit short or
        a bit long depending on who is bowling, which is what makes running
        out of time a real risk rather than an arithmetic certainty."""
        base = fc_weather.effective_overs_today(
            self.fc_weather_script, self.fc_day, self.fmt.overs_per_day
        )
        if base <= 0:
            return 0
        if getattr(self, "fc_day_over_rate_adjust", None) is None:
            self.fc_day_over_rate_adjust = self._fc_compute_day_over_rate_adjust()
        adjusted = base + self.fc_day_over_rate_adjust
        # The over-rate model must never cut a day below the last-hour
        # minimum the weather model already respects.
        return max(min(base, self.fmt.min_overs_last_hour), adjusted)

    def _fc_overs_remaining_in_match(self):
        """Rough overs-left estimate: today's remaining overs plus a flat
        overs_per_day for every full day still to come. Deliberately doesn't
        call the weather-aware _fc_effective_overs_today() for future days —
        those haven't rolled their forecast outcome yet, so fmt.overs_per_day
        is the only honest estimate available; only today's figure can be
        weather-adjusted. Shared by the required-run-rate estimate
        (_fc_build_match_state, for the live 4th-innings chase) and the
        Monte Carlo declaration model (_fc_check_declaration_and_follow_on)."""
        overs_left_today = max(0, self.fmt.overs_per_day - self.fc_day_overs_bowled_today)
        full_days_left = max(0, self.fmt.days - self.fc_day)
        return max(1, overs_left_today + full_days_left * self.fmt.overs_per_day)

    # ── Sessions ────────────────────────────────────────────────────────
    # A first-class day is played in three sessions. Intervals fall at the
    # thirds of whatever is actually schedulable today, so a rain-shortened
    # day still gets Lunch and Tea in sensible places rather than at a fixed
    # over 30/60 that may no longer exist.

    FC_SESSION_NAMES = ("Lunch", "Tea", "Stumps")

    def _fc_session_boundaries(self):
        """Over-counts within today at which Lunch and Tea fall."""
        total = self._fc_effective_overs_today()
        if total < 6:
            return []
        return [int(round(total / 3.0)), int(round(total * 2.0 / 3.0))]

    def _fc_current_session(self):
        """Session in progress today, 1-3."""
        return min(3, self.fc_sessions_taken_today + 1)

    def _fc_snapshot_session_start(self):
        """Freeze the score at the start of a session, so the interval card
        can report what the session itself produced. Re-taken on an innings
        change too — otherwise the delta would go negative when the score
        resets to 0 mid-session."""
        self.fc_session_start = {
            "score": self.score,
            "wickets": self.wickets,
            "day_overs": self.fc_day_overs_bowled_today,
            "fc_innings": self.fc_innings,
        }

    def _fc_session_summary(self):
        """Runs/wickets/overs produced since the last interval (or since the
        innings started, if that came later)."""
        start = self.fc_session_start or {}
        if start.get("fc_innings") != self.fc_innings:
            runs, wkts = self.score, self.wickets
        else:
            runs = self.score - start.get("score", 0)
            wkts = self.wickets - start.get("wickets", 0)
        overs = self.fc_day_overs_bowled_today - start.get("day_overs", 0)
        return {"runs": max(0, runs), "wickets": max(0, wkts),
                "overs": max(0, overs)}

    def _fc_interval_response(self, interval_name):
        """Lunch/Tea break: the same scorecard pause the end of a day already
        does. Following a first-class match means reading the score at the
        intervals, so this is a real stopping point, not a log line."""
        scorecard_data = self._generate_detailed_scorecard()
        session_no = self._fc_current_session()
        summary = self._fc_session_summary()
        self.fc_sessions_taken_today += 1
        self._fc_snapshot_session_start()
        sess_line = (f"{summary['runs']}/{summary['wickets']} in "
                     f"{summary['overs']} overs this session")
        return {
            "fc_interval": True,
            "interval_name": interval_name,
            "day_number": self.fc_day,
            "session_number": session_no,
            "session_summary": summary,
            "match_over": False,
            "innings_end": False,
            "scorecard_data": scorecard_data,
            "score": self.score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "commentary": (
                f"<strong>{interval_name} &mdash; Day {self.fc_day}</strong><br>"
                f"<em>{sess_line}</em><br>"
                + self._format_innings_complete_summary(
                    f"{interval_name}, Day {self.fc_day}")
            ),
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": self.current_bowler["name"] if self.current_bowler else "",
        }

    def _fc_pre_ball_checks(self):
        """
        Called only at an over boundary (current_ball == 0), before the
        shared bowler-selection/ball-processing code runs. Returns a
        response dict to short-circuit next_ball(), or None to continue.
        Mirrors the existing rain-check's "engine decides automatically at
        an over boundary" precedent — no pending_decision involved (AI
        always decides declaration/follow-on in Phase 1, per the agreed
        scope).
        """
        _day_over = self.fc_day_overs_bowled_today >= self._fc_effective_overs_today()

        _bounds = self._fc_session_boundaries()
        _at_session_break = (
            not _day_over
            and self.fc_sessions_taken_today < len(_bounds)
            and self.fc_day_overs_bowled_today >= _bounds[self.fc_sessions_taken_today]
        )

        # Captains declare at an interval — Lunch, Tea, or overnight — not
        # three overs into a session. The standing exception is the tail
        # being exposed, where the call is about protecting the last pair
        # and can't wait for the next break.
        if _day_over or _at_session_break or self.wickets >= 9:
            _decl = self._fc_check_declaration_and_follow_on()
            if _decl is not None:
                return _decl                 # user-captained: pause and ask
            if self.fc_innings_declared:
                # Declared. Return None so this same next_ball() call falls
                # straight through to _innings_should_end() and closes the
                # innings here, rather than showing an interval card for an
                # innings that is already over.
                return None

        if _day_over:
            if self.fc_day >= self.fmt.days:
                return self._fc_finalize_draw()
            return self._fc_day_break_response()

        if _at_session_break:
            return self._fc_interval_response(
                self.FC_SESSION_NAMES[self.fc_sessions_taken_today])

        return None

    def _fc_check_declaration_and_follow_on(self):
        """
        Declaration check, run at every FC over boundary. AI mode decides
        automatically via engine/fc_declaration.py's should_declare().
        User-captained mode instead pauses with a decision once
        declaration_window_open() says the moment is live, and leaves the
        actual call to the human (see _create_fc_declare_decision).

        Innings 1, 2, and a follow-on-free innings 3 are eligible (a side
        already chasing a set target never declares). Follow-on itself is
        decided inside _fc_transition_to_next_innings() at the moment
        innings 2 ends — that's the one instant the decision is actually
        live, not on every over boundary.
        """
        if self.fc_innings not in (1, 2, 3) or self.follow_on_enforced:
            return None
        if self.wickets >= 10 or self.fc_innings_declared:
            # Innings already over (all out) or already decided (declared,
            # AI or user-captained) — nothing left to ask or auto-decide.
            # Without this, a user-captained "Declare" choice would get
            # re-asked forever: submit_pending_decision() just sets the
            # flag and returns, so the NEXT next_ball() call is what's
            # actually supposed to notice fc_innings_declared and let
            # _fc_innings_should_end() transition the innings — this check
            # running again first would short-circuit that.
            return None

        lead = 0
        if self.fc_innings == 2:
            a1 = self.fc_innings_totals.get(1, {}).get("score", 0)
            lead = self.score - a1
        elif self.fc_innings == 3:
            # Non-follow-on case only (a follow-on-enforced side never
            # reaches this method — see the guard above). The batting
            # side's true lead is their combined 1st+2nd innings total
            # against the opposition's completed innings-2 total, matching
            # the target formula used when this innings actually ends
            # (a1 + a2 - b1 + 1 in _fc_transition_to_next_innings) — NOT
            # just this innings' own score against their own 1st innings,
            # which is what the shared `self.score - a1` line above used to
            # compute here.
            a1 = self.fc_innings_totals.get(1, {}).get("score", 0)
            b1 = self.fc_innings_totals.get(2, {}).get("score", 0)
            lead = a1 + self.score - b1
        days_remaining = self._fc_days_remaining()

        if self._is_manual_mode():
            if self._fc_declined_declare_over == self.current_over:
                return None  # already asked and declined at this exact over
            if fc_declaration.declaration_window_open(
                fc_innings=self.fc_innings, wickets=self.wickets,
                overs_bowled_this_innings=self.current_over,
                days_remaining=days_remaining,
                innings_time_budget_overs=self.fc_innings_time_budget_overs,
            ):
                return self._build_decision_required_response(
                    self._create_fc_declare_decision(lead, days_remaining)
                )
            return None

        pitch_factor = self.fmt.pitch_par_factors.get(self.pitch, 1.0)

        # Monte Carlo inputs (innings 2/3 only — should_declare() falls back
        # to the flat-threshold heuristic for innings 1, which never reads
        # these). self.batting_team is OUR side (deciding whether to
        # declare); self.bowling_team is the opposition.
        mc_kwargs = {}
        if self.fc_innings in (2, 3):
            mc_kwargs = dict(
                overs_remaining_in_match=self._fc_overs_remaining_in_match(),
                own_bowling_strength=self._fc_avg_rating(self.batting_team, "bowling_rating"),
                own_batting_strength=self._fc_avg_rating(self.batting_team, "batting_rating"),
                opp_batting_strength=self._fc_avg_rating(self.bowling_team, "batting_rating"),
                pitch_wear=self._compute_pitch_wear(),
            )

        if fc_declaration.should_declare(
            fc_innings=self.fc_innings,
            wickets=self.wickets,
            overs_bowled_this_innings=self.current_over,
            score=self.score,
            lead=lead,
            days_remaining=days_remaining,
            pitch_par_factor=pitch_factor,
            innings_time_budget_overs=self.fc_innings_time_budget_overs,
            # What the captain can actually see from the balcony. Note the
            # freshness that matters is HIS OWN attack — the side currently
            # batting is the side that has to bowl next.
            rain_risk=self._fc_rain_risk(),
            projected_final_wear=self._fc_projected_final_wear(),
            attack_freshness=self._fc_declaring_side_freshness(),
            **mc_kwargs,
        ):
            self.fc_innings_declared = True
        return None

    def _create_fc_declare_decision(self, lead, days_remaining):
        """Build the user-captained "declare now?" pending_decision. Options
        are fixed (not player picks like next_bowler/next_batter), but still
        expressed as index-keyed options so it reuses the exact same
        submit_pending_decision(selected_index) endpoint/contract."""
        decision = {
            "type": "fc_declare",
            "context": {
                "fc_innings": self.fc_innings,
                "score": self.score,
                "wickets": self.wickets,
                "over": self.current_over,
                "lead": lead if self.fc_innings in (2, 3) else None,
                "days_remaining": days_remaining,
                "batting_team_name": self._get_team_name(self.batting_team),
            },
            "options": [
                {"index": 1, "label": "Declare"},
                {"index": 0, "label": "Continue Batting"},
            ],
        }
        self.pending_decision = decision
        return decision

    @staticmethod
    def _fc_avg_rating(players, rating_key):
        """Mean of one rating field across an XI, for the Monte Carlo
        declaration model — 50.0 (neutral) for an empty list, which should
        never actually happen for a real XI but keeps this a total function."""
        values = [p.get(rating_key, 0) or 0 for p in players]
        return sum(values) / len(values) if values else 50.0

    def _fc_day_break_response(self):
        scorecard_data = self._generate_detailed_scorecard()
        day_ended = self.fc_day
        # Stumps closes the evening session, so it reports one the same way
        # Lunch and Tea do.
        session_summary = self._fc_session_summary()
        session_no = self._fc_current_session()
        weather_line = fc_weather.day_summary_line(self.fc_weather_script, day_ended)
        self.fc_day += 1
        self.fc_day_overs_bowled_today = 0
        self.fc_sessions_taken_today = 0
        self.fc_day_over_rate_adjust = self._fc_compute_day_over_rate_adjust()
        self._fc_snapshot_session_start()
        return {
            "day_break": True,
            "day_number": day_ended,
            "session_number": session_no,
            "session_summary": session_summary,
            "match_over": False,
            "innings_end": False,
            "scorecard_data": scorecard_data,
            "score": self.score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "weather_note": weather_line,
            "commentary": (
                f"<strong>Stumps &mdash; Day {day_ended}</strong><br>"
                + (f"<em>{session_summary['runs']}/{session_summary['wickets']} in "
                   f"{session_summary['overs']} overs this session</em><br>")
                + (f"<em>{weather_line}</em><br>" if weather_line else "")
                + f"{self._format_innings_complete_summary(f'Stumps, Day {day_ended}')}"
            ),
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": self.current_bowler["name"] if self.current_bowler else "",
        }

    def _fc_record_innings_stats(self, fc_innings_number):
        """
        Snapshot this innings' batting/bowling stats for the archiver's
        generalized innings_plan (up to 4 entries, vs. T20/ListA's fixed
        2-tuple) — see match_archiver.py.
        """
        self.fc_innings_stats.append({
            "innings_number": fc_innings_number,
            "batting_side": "home" if self.batting_team is self.home_xi else "away",
            "bowling_side": "home" if self.bowling_team is self.home_xi else "away",
            "batting_stats": copy.deepcopy(self.batsman_stats),
            "bowling_stats": copy.deepcopy(self.bowler_stats),
        })

    def _fc_start_next_innings(self, next_fc_innings, new_batting, new_bowling):
        self.batting_team = new_batting
        self.bowling_team = new_bowling
        self.score = 0
        self.wickets = 0
        self.current_over = 0
        self.current_ball = 0
        self.batter_idx = [0, 1]
        self.current_striker = self.batting_team[0]
        self.current_non_striker = self.batting_team[1]
        self.batsman_stats = {p["name"]: self._new_batting_stats(p) for p in self.batting_team}
        self.bowler_stats = {p["name"]: self._new_bowling_stats(p) for p in self.bowling_team if p.get("will_bowl")}
        self._reset_innings_state()
        self.fc_innings = next_fc_innings
        self.fc_innings_declared = False
        # Recomputed fresh — see __init__'s matching comment. Uses whatever
        # is actually left in the match right now, so an innings that
        # starts late (because an earlier one ran long) gets a
        # correspondingly tighter budget automatically.
        self.fc_innings_time_budget_overs = fc_declaration.compute_innings_time_budget_overs(
            self._fc_overs_remaining_in_match()
        )
        self.fc_ball_overs_bowled = 0  # a fresh new ball is always issued at the start of an innings
        self.fc_nightwatchman_used = False
        self.fc_nightwatchman_name = None
        self.fc_consecutive_maidens = 0
        self._fc_snapshot_session_start()
        if self.scenario_engine:
            self.scenario_engine.on_innings_transition()

    def _fc_create_match_archive(self):
        """
        FC archiving entry point. MatchArchiver._build_innings_plan() reads
        self.fc_innings_stats / self.fc_innings_totals / self.fc_innings_partnerships
        directly (branching on match.is_fc) to persist every real innings
        (2-4, depending on how the match played out) — not just a first+last
        shim. This method only needs to seed the team-name metadata that
        _determine_team_batting_order()'s fallback and a few log lines read.
        """
        if getattr(self, "_archive_created", False):
            return True
        if self.fc_innings_stats:
            first = self.fc_innings_stats[0]
            self.first_batting_team_name = (
                self.match_data["team_home"].split("_")[0] if first["batting_side"] == "home"
                else self.match_data["team_away"].split("_")[0]
            )
            self.first_bowling_team_name = (
                self.match_data["team_home"].split("_")[0] if first["bowling_side"] == "home"
                else self.match_data["team_away"].split("_")[0]
            )
        self._second_innings_stats_saved = True
        return self._create_match_archive()

    def _fc_finalize_match(self, scorecard_data):
        self._fc_create_match_archive()
        return {
            "innings_end": True,
            "innings_number": self.fc_innings,
            "match_over": True,
            "scorecard_data": scorecard_data,
            "final_score": self.score,
            "wickets": self.wickets,
            "result": self.result,
            "days_played": self.fc_day,
            "commentary": f"<strong>Match Over!</strong> {self.result}",
        }

    def _fc_finalize_draw(self):
        if self.wickets < 10 and self.current_partnership_balls > 0:
            self._save_partnership("not_out")
        scorecard_data = self._generate_detailed_scorecard()
        self.fc_innings_totals[self.fc_innings] = {
            "score": self.score, "wickets": self.wickets,
            "overs_str": self._balls_to_overs_notation(self.current_over * 6 + self.current_ball),
            "side": "home" if self.batting_team is self.home_xi else "away",
        }
        self._fc_record_innings_stats(self.fc_innings)
        self._set_outcome(
            result_text="Match drawn.",
            winner_is_home=None,
            match_status='drawn', margin_type=None, margin_value=None,
        )
        return self._fc_finalize_match(scorecard_data)

    def _fc_transition_to_next_innings(self):
        if self.wickets < 10 and self.current_partnership_balls > 0:
            self._save_partnership("not_out")

        ending_innings = self.fc_innings
        scorecard_data = self._generate_detailed_scorecard()
        self.fc_innings_totals[ending_innings] = {
            "score": self.score, "wickets": self.wickets,
            "overs_str": self._balls_to_overs_notation(self.current_over * 6 + self.current_ball),
            "side": "home" if self.batting_team is self.home_xi else "away",
        }
        self._fc_record_innings_stats(ending_innings)

        if ending_innings == 1:
            self.first_innings_score = self.score
            self._fc_first_batting_xi = self.batting_team
            self._fc_first_bowling_xi = self.bowling_team
            self._fc_start_next_innings(2, self.bowling_team, self.batting_team)
            return {
                "innings_end": True, "innings_number": 1, "match_over": False,
                "scorecard_data": scorecard_data,
                "score": 0, "wickets": 0, "over": 0, "ball": 0,
                "commentary": (
                    f"{self._format_innings_complete_summary('End of innings')}<br>"
                    f"<strong>End of 1st Innings:</strong> {self.fc_innings_totals[1]['score']}/"
                    f"{self.fc_innings_totals[1]['wickets']}."
                ),
                "striker": self.current_striker["name"],
                "non_striker": self.current_non_striker["name"],
                "bowler": "",
            }

        if ending_innings == 2:
            a1 = self.fc_innings_totals[1]["score"]
            b1 = self.score
            deficit = a1 - b1

            if deficit > 0 and self._is_manual_mode():
                # A real choice exists (deficit <= 0 means follow-on was
                # never possible — nothing to ask). Pause: the transition
                # completes in _fc_apply_follow_on_decision, called from
                # submit_pending_decision once the captain answers.
                self._fc_pending_innings_end_scorecard = scorecard_data
                return self._build_decision_required_response(
                    self._create_fc_follow_on_decision(deficit),
                    commentary=self._format_innings_complete_summary('End of innings'),
                )

            enforce_fo = deficit > 0 and fc_declaration.should_enforce_follow_on(
                deficit=deficit, follow_on_margin=self.fmt.follow_on_margin,
                days_remaining=self._fc_days_remaining(),
                attack_overs_bowled=self._fc_attack_overs_bowled(),
                projected_final_wear=self._fc_projected_final_wear(),
                rain_risk=self._fc_rain_risk(),
            )
            return self._fc_apply_follow_on_decision(enforce_fo, scorecard_data)

        if ending_innings == 3:
            if self.follow_on_enforced:
                b1 = self.fc_innings_totals[2]["score"]
                b2 = self.score
                a1 = self.fc_innings_totals[1]["score"]
                if (b1 + b2) < a1:
                    margin = a1 - (b1 + b2)
                    self._set_outcome(
                        result_text=f"Match won by an innings and {margin} run(s).",
                        winner_is_home=(self._fc_first_batting_xi is self.home_xi),
                        match_status='completed', margin_type='innings', margin_value=margin,
                    )
                    return self._fc_finalize_match(scorecard_data)
                self.target = (b1 + b2) - a1 + 1
                self._fc_start_next_innings(4, self._fc_first_batting_xi, self._fc_first_bowling_xi)
            else:
                a1 = self.fc_innings_totals[1]["score"]
                a2 = self.score
                b1 = self.fc_innings_totals[2]["score"]
                self.target = a1 + a2 - b1 + 1
                # B chases: B was bowling during innings 3 (A's 2nd innings).
                self._fc_start_next_innings(4, self.bowling_team, self.batting_team)
            return {
                "innings_end": True, "innings_number": 3, "match_over": False,
                "scorecard_data": scorecard_data,
                "score": 0, "wickets": 0, "over": 0, "ball": 0,
                "target": self.target,
                "commentary": (
                    f"{self._format_innings_complete_summary('End of innings')}<br>"
                    f"<strong>Target: {self.target}</strong>"
                ),
                "striker": self.current_striker["name"],
                "non_striker": self.current_non_striker["name"],
                "bowler": "",
            }

        # ending_innings == 4: the chase concludes here (all out, or target
        # reached — the target-reached case is caught by
        # _fc_innings_should_end() so this method is only entered for that
        # or for all-out).
        if self.score >= self.target:
            wkts_left = 10 - self.wickets
            self._set_outcome(
                result_text=f"Match won by {wkts_left} wicket(s).",
                winner_is_home=(self.batting_team is self.home_xi),
                match_status='completed', margin_type='wickets', margin_value=wkts_left,
            )
        elif self.score == self.target - 1:
            self._set_outcome(
                result_text="Match Tied.",
                winner_is_home=None,
                match_status='tied', margin_type='tie', margin_value=0,
            )
        else:
            run_diff = self.target - self.score - 1
            self._set_outcome(
                result_text=f"Match won by {run_diff} run(s).",
                winner_is_home=(self.bowling_team is self.home_xi),
                match_status='completed', margin_type='runs', margin_value=run_diff,
            )
        return self._fc_finalize_match(scorecard_data)

    def _create_fc_follow_on_decision(self, deficit):
        """Build the user-captained "enforce the follow-on?" pending_decision,
        offered only when deficit > 0 (a real choice — see the ending_innings
        == 2 branch above). Same fixed-index-options shape as
        _create_fc_declare_decision, for the same reason: reuse
        submit_pending_decision(selected_index) as-is."""
        decision = {
            "type": "fc_follow_on",
            "context": {
                "deficit": deficit,
                "follow_on_margin": self.fmt.follow_on_margin,
                "days_remaining": self._fc_days_remaining(),
                "trailing_team_name": self._get_team_name(self.batting_team),
            },
            "options": [
                {"index": 1, "label": "Enforce Follow-on"},
                {"index": 0, "label": "Bat Again"},
            ],
        }
        self.pending_decision = decision
        return decision

    # Share of an innings' bowling workload that follows an attack into the
    # next innings when they are sent straight back out on a follow-on.
    # Not 1.0 — there is a break and an adrenaline bump — but not 0 either.
    _FC_FOLLOW_ON_FATIGUE_CARRY = 0.45

    def _fc_attack_overs_bowled(self):
        """Overs the side that has just been bowling has sent down across
        the match so far — what a captain means by "are my bowlers gone?"."""
        total = 0.0
        for entry in self.fc_innings_stats:
            for st in (entry.get("bowling_stats") or {}).values():
                total += (st.get("overs", 0) or 0) + (st.get("balls_bowled", 0) or 0) / 6.0
        return total

    def _fc_projected_final_wear(self):
        """Roughly how worn the pitch will be by the fourth innings — the
        surface the enforcing captain would be batting last on."""
        total_balls = max(1, self.fmt.days * self.fmt.overs_per_day * 6)
        # Assume the rest of the match is played out; that is the situation
        # the decision is being made against.
        return min(1.0, (self.match_balls_bowled + total_balls * 0.35) / total_balls)

    def _fc_rain_risk(self):
        """0-1 read on how much play the forecast threatens to cost over the
        rest of the match. Time lost argues for enforcing the follow-on."""
        return fc_weather.remaining_rain_risk(
            self.fc_weather_script, self.fc_day, self.fmt.days,
            overs_per_day=self.fmt.overs_per_day)

    def _fc_apply_follow_on_decision(self, enforce_fo, scorecard_data):
        """Completes the innings-2 -> innings-3 transition once the
        follow-on call is made, AI-decided or user-captained alike. Must run
        with self.batting_team/self.bowling_team still at their innings-2
        values (true both for the immediate AI-mode call and the delayed
        user-captained one, since nothing else touches match state while a
        decision is pending)."""
        self.follow_on_enforced = enforce_fo
        if enforce_fo:
            # The attack goes straight back out. Carry part of their
            # workload into the new innings rather than resetting to fresh —
            # otherwise enforcing the follow-on is free, which is exactly
            # backwards from the real decision.
            self._fc_pending_fatigue_carry = self._FC_FOLLOW_ON_FATIGUE_CARRY
            # B bats again immediately — batting/bowling roles unchanged
            # from innings 2 (B was batting, A was bowling; B bats on).
            self._fc_start_next_innings(3, self.batting_team, self.bowling_team)
        else:
            # A bats their 2nd innings — swap back to A batting, B bowling.
            self._fc_start_next_innings(3, self._fc_first_batting_xi, self._fc_first_bowling_xi)
        return {
            "innings_end": True, "innings_number": 2, "match_over": False,
            "follow_on_enforced": enforce_fo,
            "scorecard_data": scorecard_data,
            "score": 0, "wickets": 0, "over": 0, "ball": 0,
            "commentary": (
                f"{self._format_innings_complete_summary('End of innings')}<br>"
                + ("<strong>Follow-on enforced.</strong>" if enforce_fo
                   else "<strong>End of 2nd Innings.</strong>")
            ),
            "striker": self.current_striker["name"],
            "non_striker": self.current_non_striker["name"],
            "bowler": "",
        }

    def next_ball(self):
        # Super Over guard: once a tie pushes the match into super-over state
        # (innings 4 = super over pending/in progress, 5 = decided), the normal
        # ball loop must NOT run. Without this guard a stray next_ball() — from a
        # page refresh/resume or a duplicated client loop — falls through to the
        # second-innings-end branch and corrupts the result. Return a safe signal
        # so the frontend can route to the super-over flow instead of looping.
        if self.innings >= 4:
            return {
                "super_over_in_progress": True,
                "match_over": self.innings == 5,
                "result": getattr(self, "result", None),
            }

        if self.innings == 3:

            self._save_second_innings_stats()
            self._create_match_archive()

            return {
                "match_over": True,
                "final_score": self.score,
                "wickets": self.wickets,
                "result": self.result
            }

        if self.pending_decision:
            if self._is_manual_mode():
                return self._build_decision_required_response(
                    self.pending_decision,
                    commentary="<em>Waiting for manual selection...</em>"
                )
            # Auto mode fallback: resolve any pending decision immediately.
            options = self.pending_decision.get("options", [])
            if not options:
                return {"error": "Pending decision has no valid options"}
            auto_index = options[0]["index"]
            apply_result, status_code = self.submit_pending_decision(auto_index)
            if status_code != 200:
                return {"error": apply_result.get("error", "Failed to auto-resolve pending decision")}

        # Rain check at the start of a fresh over — this is where deferred
        # innings-break events land (a chase reduced before it begins).
        # Inert for FC (weather_script has an empty events list — no
        # weather modeling in Phase 1), so no is_fc guard is needed here.
        if self.current_ball == 0:
            _rain_outcome = self._check_rain_events()
            if _rain_outcome:
                if _rain_outcome.get("final") is not None:
                    return _rain_outcome["final"]
                self.pending_pre_ball_commentary.extend(_rain_outcome.get("lines", []))
                self._pending_rain_info = _rain_outcome.get("info")

        # FC day/match-exhaustion and declaration/follow-on checks — engine
        # decides automatically at the over boundary, same style as the
        # rain check above (no pending_decision involved; AI always
        # decides in Phase 1).
        if self.current_ball == 0 and self.is_fc:
            _fc_pre = self._fc_pre_ball_checks()
            if _fc_pre is not None:
                return _fc_pre

        self._ensure_current_bowler_stats_entry()


        if self._innings_should_end():
            return self._transition_to_next_innings()

        if self.current_ball == 0:
            # Initialize partnership contributions if new partnership (moved from inner block)
            if self.current_partnership_balls == 0 and self.current_partnership_runs == 0:
                 if self.current_partnership_contributions['batsman1']['name'] == '':
                     self.current_partnership_contributions['batsman1']['name'] = self.current_striker['name']
                     self.current_partnership_contributions['batsman2']['name'] = self.current_non_striker['name']
            if self.bowler_selected_for_over != self.current_over:
                if self._is_manual_mode():
                    decision = self.pending_decision
                    if not decision or decision.get("type") != "next_bowler":
                        decision = self._create_next_bowler_decision()
                    return self._build_decision_required_response(
                        decision,
                        commentary=f"<em>Select bowler for over {self.current_over + 1}</em>"
                    )
                try:
                    self.current_bowler = self._fc_pick_bowler() if self.is_fc else self.pick_bowler()
                except Exception as e:
                    log_exception(e)
                    logger.exception("Bowler selection failed at over %s.%s: %s", self.current_over, self.current_ball, e)

                    eligible = [p for p in self.bowling_team if p.get("will_bowl", False)]
                    if not eligible:
                        self.match_status = 'aborted'
                        return {
                            "error": "Bowler selection failed and no eligible bowlers are available.",
                            "match_over": True,
                            "result": "Match aborted: No eligible bowler available."
                        }

                    previous_name = self.current_bowler["name"] if self.current_bowler else None
                    # FC has no bowling quota at all — every eligible (non-
                    # consecutive) bowler is quota-safe by definition.
                    max_q = getattr(self.fmt, "max_bowler_overs", None)
                    quota_non_consecutive = (
                        [b for b in eligible if b["name"] != previous_name]
                        if max_q is None else
                        [b for b in eligible
                         if b["name"] != previous_name and self.bowler_history.get(b["name"], 0) < max_q]
                    )
                    non_consecutive = [b for b in eligible if b["name"] != previous_name]
                    fallback_pool = quota_non_consecutive or non_consecutive
                    if not fallback_pool:
                        self.match_status = 'aborted'
                        return {
                            "error": "Bowler selection failed and no non-consecutive bowler is available.",
                            "match_over": True,
                            "result": "Match aborted: No non-consecutive bowler available."
                        }
                    fallback_pool.sort(key=lambda b: (-b.get("bowling_rating", 0), b.get("name", "")))
                    self.current_bowler = fallback_pool[0]
                    logger.warning(
                        "Using fallback bowler '%s' after selection failure",
                        self.current_bowler.get("name", "Unknown")
                    )
                self._ensure_current_bowler_stats_entry()
                self.bowler_selected_for_over = self.current_over
            if self.current_over == 0:
                batting_team_name = self._get_team_name(self.batting_team)
                bowling_team_name = self._get_team_name(self.bowling_team)
                opener_1 = self.current_non_striker['name']
                opener_2 = self.current_striker['name']
                self.pending_pre_ball_commentary.extend([
                    "",
                    f"<strong>INNINGS {self.innings}</strong>",
                    "",
                    f"{opener_1} and {opener_2} will open the attack for {batting_team_name}. {opener_2} is on strike.",
                    f"{self.current_bowler['name']} will bowl the opening over for {bowling_team_name}.",
                    ""
                ])
            else:
                self.pending_pre_ball_commentary.extend([
                    f"{self.current_bowler['name']} is into the attack.",
                    ""
                ])

        # Calculate pressure and effects.
        # FC uses an entirely separate model (session-survival + lead-
        # building, not run-rate-chase pressure) — see FCPressureEngine's
        # module docstring. PressureEngine's calculate_pressure/
        # get_pressure_effects/get_chasing_advantage/calculate_defensive_factor/
        # get_risk_based_effects chain is RRR/death-overs-chase-shaped and
        # keys off self.innings==1/2, which is permanently 1 for FC — none
        # of that logic transfers.
        if self.is_fc:
            match_state = self._fc_build_match_state()
            _win_prob = None  # no chase-win-probability model for FC in Phase 1
            pressure_effects = self.pressure_engine.get_pressure_effects(match_state)
        else:
            match_state = self._calculate_current_match_state()
            pressure_score = self.pressure_engine.calculate_pressure(match_state)

            # Win probability (2nd innings only; None during 1st innings)
            _win_prob = self._calculate_win_probability()


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
                    # Wicket-cluster check for the 2nd-innings chase-collapse case
                    # only. First-innings clusters are evaluated exclusively by
                    # the "First innings collapse psychology" block below —
                    # calling should_trigger_wicket_cluster() here too would
                    # re-roll the same RNG draw for the same ball and could stack
                    # both boosts (up to 1.3 * 1.25 = 1.625x).
                    if self.innings == 2:
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

            # First innings collapse psychology (works even outside death overs)
            if self.innings == 1:
                recent_wickets = getattr(self, 'recent_wickets_count', 0)
                if self.pressure_engine.should_trigger_wicket_cluster(match_state, recent_wickets):
                    pressure_effects['wicket_modifier'] *= 1.25
                    logger.info(f"FIRST INNINGS COLLAPSE: 1.25x wicket boost! ({self.wickets} down, {recent_wickets} recent)")

        # Feature 7: Toss × Conditions modifier.
        # The team that made the correct toss call for the pitch conditions gets
        # a small boundary advantage when it's their turn to bat; the wrong call
        # gives the opposition a slight edge.
        _batting_has_toss_adv = (
            (self.batting_team is self._toss_winner_xi) == self._toss_choice_correct
        )
        _toss_mult = 1.03 if _batting_has_toss_adv else 0.97
        pressure_effects['boundary_modifier'] = pressure_effects.get('boundary_modifier', 1.0) * _toss_mult
        logger.debug("[TossAdv] batting_has_adv=%s toss_mult=%.2f", _batting_has_toss_adv, _toss_mult)

        # Defaults needed by the no-ball re-roll path even when scenario_override
        # bypasses the main calculate_outcome() branch below.
        striker_name = self.current_striker["name"]
        _effective_bowler = self._get_effective_bowler_dict(self.current_bowler)
        _partnership_balls = self.current_partnership_balls
        _partnership_runs = self.current_partnership_runs
        _batting_position = 5
        for _idx, _player in enumerate(self.batting_team):
            if _player.get("name") == striker_name:
                _batting_position = _idx + 1
                break
        _pitch_wear = self._compute_pitch_wear()
        _game_mode_override = self._resolve_game_mode()
        # Historical scenario engines steer both innings; classic scripted
        # scenarios only ever act on the 2nd innings.
        _scenario_steers_now = bool(self.scenario_engine) and (
            self.innings == 2
            or getattr(self.scenario_engine, "steers_first_innings", False)
        )
        _scenario_phase = (
            self.scenario_engine.get_phase() if _scenario_steers_now else "inactive"
        )
        # GSME (Game State Momentum Engine) is tuned entirely around T20/
        # ListA's fixed-innings-length, single-target shape (par-score
        # curves, RRR baselines, death-phase boosts) — none of which exists
        # on MultiDayFormatConfig. FC's own momentum/pressure axis is
        # FCPressureEngine (session-survival + lead-building), already
        # folded into pressure_effects above; calculate_outcome() treats
        # game_state=None as a clean no-op.
        _gsme_state = None if self.is_fc else compute_game_state_vector(
            ball_history=self.ball_history,
            score=self.score,
            current_over=self.current_over,
            current_ball=self.current_ball,
            wickets=self.wickets,
            innings=self.innings,
            target=self.target or 0,
            pitch=self.pitch,
            partnership_balls=_partnership_balls,
            partnership_runs=_partnership_runs,
            scenario_phase=_scenario_phase,
            format_config=self.fmt,
            ground_config=self.ground_config,
        )

        # ===== SCENARIO ENGINE HOOK =====
        scenario_override = None
        if _scenario_steers_now:
            scenario_override = self.scenario_engine.get_override_outcome(
                batter=self.current_striker, bowler=self.current_bowler
            )

        if scenario_override:
            outcome = scenario_override
        else:
            # Merge scenario bias into pressure_effects (convergence/free-play phases)
            if _scenario_steers_now:
                scenario_bias = self.scenario_engine.get_scenario_bias(
                    self._calculate_current_match_state()
                )
                for key, value in scenario_bias.items():
                    if key in pressure_effects:
                        if key == "dot_bonus":
                            pressure_effects[key] += value  # additive for dot_bonus
                        else:
                            pressure_effects[key] *= value  # multiplicative for modifiers
                    else:
                        pressure_effects[key] = value

            striker_name = self.current_striker["name"]
            streak = self.batter_streaks.get(striker_name, {"boundaries": 0})

            # Feature 1+2+8: effective bowler with phase/fatigue/feedback adjustments
            _effective_bowler = self._get_effective_bowler_dict(self.current_bowler)

            # Feature 6: current partnership (balls and runs at the crease together)
            _partnership_balls = self.current_partnership_balls
            _partnership_runs  = self.current_partnership_runs

            # Feature 9: batting position (1-based index in team batting order)
            _batting_position = 5   # safe default (middle-order)
            for _idx, _player in enumerate(self.batting_team):
                if _player.get("name") == striker_name:
                    _batting_position = _idx + 1
                    break

            # Feature 3: pitch wear from balls bowled this innings.
            # Normalised to the total ball capacity of the format
            # (T20=120, ListA=300, FC=continuous across the whole match)
            # so wear progresses at the right pace.
            _pitch_wear = self._compute_pitch_wear()

            # Feature 13: game mode selection (pinned by config, else dynamic)
            _game_mode_override = self._resolve_game_mode()
            logger.debug("[DynMode] over=%d wickets=%d mode=%s", self.current_over, self.wickets, _game_mode_override)

            # Fielding quality fallback: team average, only used by ball_outcome
            # if fielding_team (passed below) is empty. Normally the specific
            # fielder's own rating drives catch-drop/misfield odds instead.
            _bowling_team_fielding = [
                p.get("fielding_rating", 60) for p in self.bowling_team
            ]
            _team_fielding_avg = (
                sum(_bowling_team_fielding) / len(_bowling_team_fielding)
                if _bowling_team_fielding else 60.0
            )

            # Player form: scale batter's rating by in-match form multiplier.
            # The existing balls-faced vulnerability in compute_weighted_prob()
            # handles physical settling-in; form captures hot/cold streaks.
            _form = self.batsman_stats.get(striker_name, {}).get("form", 1.0)
            _batter_with_form = dict(self.current_striker)
            _batter_with_form["batting_rating"] = (
                self.current_striker["batting_rating"] * _form
            )

            # _gsme_state was already built further up from _scenario_steers_now
            # (which correctly honors HistoricalScenarioEngine.steers_first_innings).
            # Do not rebuild it here: a second build used to live in this spot with
            # a narrower `innings == 2` check, silently resetting scenario_phase to
            # "inactive" for every first-innings ball of a historical scenario —
            # since HistoricalScenarioEngine.get_override_outcome() always returns
            # None, that routed every single ball through this branch.

            outcome = calculate_outcome(
                batter=_batter_with_form,
                bowler=_effective_bowler,
                pitch=self.pitch,
                streak=streak,
                over_number=self.current_over,
                batter_runs=self.batsman_stats[striker_name]["runs"],
                innings=self.innings,
                pressure_effects=pressure_effects,
                free_hit=self.free_hit_active,
                balls_faced=self.batsman_stats[striker_name]["balls"],
                game_state=_gsme_state,
                pitch_wear=_pitch_wear,
                batting_position=_batting_position,
                game_mode_override=_game_mode_override,
                fielding_quality=_team_fielding_avg,
                fielding_team=self.bowling_team,
                ground_config_override=self.ground_config,
                format_config=self.fmt,
                is_day_night=self.data.get("is_day_night", False),
                ball_overs_bowled=self.fc_ball_overs_bowled if self.is_fc else 0,
                new_ball_overs=getattr(self.fmt, "new_ball_overs", 80),
            )

        # 🎙️ COMMENTARY REVAMP INTEGRATION
        if hasattr(self, 'commentary_engine'):
            # Enrich outcome with context for the engine
            outcome['batter'] = self.current_striker['name']
            outcome['bowler'] = self.current_bowler['name']
            outcome['batting_team'] = self._get_team_name(self.batting_team)
            outcome['bowling_team'] = self._get_team_name(self.bowling_team)
            outcome['bowling_type'] = self.current_bowler.get('bowling_type') or ''
            if outcome.get('batter_out'):
                outcome['type'] = 'wicket'

            # Build state object
            comm_state = self._calculate_current_match_state()
            comm_state['recent_wickets_match'] = getattr(self, 'recent_wickets_count', 0)
            comm_state['batter_runs'] = self.batsman_stats[self.current_striker["name"]]["runs"]
            comm_state['partnership_runs'] = self.current_partnership_runs
            comm_state['current_over_runs'] = self.current_over_runs
            comm_state['current_ball'] = self.current_ball
            # Format-aware over landmarks for commentary triggers — FC has
            # no fixed innings length or death-overs phase, so these are
            # simply omitted; commentary_engine.py's state.get(..., default)
            # calls fall back safely (not that those T20/ListA-scaled
            # defaults are meaningful at FC over numbers anyway).
            comm_state['is_fc'] = self.is_fc
            if self.is_fc:
                # First-class narrative context. None of this exists in the
                # limited-overs state object, and without it the engine has
                # nothing FC-shaped to talk about — no new ball, no lead, no
                # wearing pitch, no close of play.
                comm_state['fc_day'] = self.fc_day
                comm_state['fc_innings'] = self.fc_innings
                comm_state['fc_ball_overs_bowled'] = self.fc_ball_overs_bowled
                comm_state['fc_new_ball_overs'] = self.fmt.new_ball_overs
                comm_state['pitch_wear'] = self._compute_pitch_wear()
                comm_state['fc_consecutive_maidens'] = self.fc_consecutive_maidens
                comm_state['fc_lead_before'] = self._fc_lead_before_ball()
                comm_state['last_hour'] = (
                    max(0.0, self._fc_effective_overs_today()
                        - self.fc_day_overs_bowled_today) <= self.fmt.min_overs_last_hour
                )
                comm_state['fc_is_nightwatchman'] = (
                    self.fc_nightwatchman_name is not None
                    and self.current_striker["name"] == self.fc_nightwatchman_name
                )
                comm_state['fc_follow_on_mark'] = self._fc_follow_on_mark()
            if not self.is_fc:
                comm_state['_fmt_last_over'] = self.fmt.overs - 1       # 19 for T20, 49 for ListA
                comm_state['_fmt_death_start'] = self.fmt.death_phase.start  # 16 for T20, 40 for ListA

            # Maiden over detection: last legal ball of over, no runs all over, this ball also a dot
            is_legal = not outcome.get('is_extra') or outcome.get('extra_type', '') in ('Byes', 'Leg Bye')
            is_last_ball = is_legal and self.current_ball == 5
            this_ball_dot = outcome.get('runs', 0) == 0 or (outcome.get('is_extra') and outcome.get('extra_type', '') in ('Byes', 'Leg Bye') and outcome.get('runs', 0) == 0)
            comm_state['is_maiden_over'] = is_last_ball and self.current_over_runs == 0 and not self.current_over_maiden_invalid and this_ball_dot

            # Generate new commentary
            new_text = self.commentary_engine.get_commentary(outcome, comm_state)
            if new_text:
                outcome['description'] = new_text

        # No Ball: roll an additional bat outcome (no extras), wicket invalidated
        extra_type = outcome.get("extra_type")
        if outcome.get("is_extra") and extra_type == "No Ball":
            bat_outcome = calculate_outcome(
                batter=self.current_striker,
                bowler=_effective_bowler,
                pitch=self.pitch,
                streak=self.batter_streaks.get(self.current_striker["name"], {"boundaries": 0}),
                over_number=self.current_over,
                batter_runs=self.batsman_stats[self.current_striker["name"]]["runs"],
                innings=self.innings,
                pressure_effects=pressure_effects,
                allow_extras=False,
                free_hit=False,
                balls_faced=self.batsman_stats[self.current_striker["name"]]["balls"],
                game_state=_gsme_state,
                pitch_wear=_pitch_wear,
                batting_position=_batting_position,
                game_mode_override=_game_mode_override,
                ground_config_override=self.ground_config,
                format_config=self.fmt,
                is_day_night=self.data.get("is_day_night", False),
                ball_overs_bowled=self.fc_ball_overs_bowled if self.is_fc else 0,
                new_ball_overs=getattr(self.fmt, "new_ball_overs", 80),
            )

            bat_runs = bat_outcome.get("runs", 0)
            bat_wicket = bat_outcome.get("batter_out", False)

            if bat_wicket:
                bat_runs = 0
                bat_desc = "No Ball! Wicket invalidated."
            else:
                bat_desc = bat_outcome.get("description", "")

            outcome["bat_runs"] = bat_runs
            outcome["bat_description"] = bat_desc
            outcome["runs"] = outcome.get("runs", 0) + bat_runs
            outcome["batter_out"] = False
            outcome["wicket_type"] = None

        # A5: Free hit - convert non-Run-Out wickets to a dot ball BEFORE any
        # downstream state (pressure engine, partnership tracking, batter
        # form, wicket/collapse tracker, GSME ball history) observes the
        # dismissal. This must run first: previously it ran after all of
        # those updates, so a batter who "survived" a free hit was still
        # treated as dismissed everywhere except the scorecard (momentum
        # swung as if a wicket fell and the batter's form multiplier was
        # wiped back to 1.0).
        if self.free_hit_active and outcome.get("batter_out") and outcome.get("wicket_type") != "Run Out":
            outcome["batter_out"] = False
            outcome["runs"] = 0
            outcome["wicket_type"] = None
            outcome["description"] = "Free hit! Batsman survives, no run."

        # Update pressure engine with outcome
        self.pressure_engine.update_recent_events(outcome)
        
        # 🤝 PARTNERSHIP TRACKING UPDATE
        self._update_partnership_tracking(outcome)

        # 📈 PLAYER FORM UPDATE — update batter's in-match form multiplier
        self._update_batter_form(self.current_striker["name"], outcome)

        # Debug wicket outcomes to catch future issues
        if outcome.get("batter_out", False):
            logger.debug(f"Ball {self.current_over}.{self.current_ball + 1} WICKET: type={outcome.get('wicket_type')}, desc='{outcome.get('description')}'")

        ball_number = f"{self.current_over}.{self.current_ball + 1}"
        runs, wicket, extra = outcome["runs"], outcome["batter_out"], outcome["is_extra"]

        # Dashboard: save pre-processing context for ball_data
        _bd_striker = self.current_striker["name"]
        _bd_non_striker = self.current_non_striker["name"]
        _bd_bowler = self.current_bowler["name"] if self.current_bowler else ""
        _bd_over = self.current_over
        _bd_ball = self.current_ball
        _bd_score_before = self.score
        _bd_was_free_hit = self.free_hit_active

        # 🔧 WICKET TRACKING (after wicket is defined)
        # Trim + recompute every ball (not just wicket balls) so the collapse
        # window actually decays once wickets fall outside the last 12 balls,
        # instead of freezing at whatever count the last wicket produced.
        if not hasattr(self, 'recent_wickets_tracker'):
            self.recent_wickets_tracker = []

        current_ball_number = self.current_over * 6 + self.current_ball
        if wicket:
            self.recent_wickets_tracker.append(current_ball_number)

        # Keep only last 12 balls (2 overs)
        self.recent_wickets_tracker = [w for w in self.recent_wickets_tracker
                                    if 0 <= current_ball_number - w <= 12]
        self.recent_wickets_count = len(self.recent_wickets_tracker)
        if wicket:
            logger.info(f"Wicket tracking: {self.recent_wickets_count} wickets in last 12 balls")

        # Update batter streak tracking (boundaries in a row)
        if not extra:
            if runs in (4, 6) and not wicket:
                cur = self.batter_streaks.get(_bd_striker, {"boundaries": 0})
                self.batter_streaks[_bd_striker] = {"boundaries": cur["boundaries"] + 1}
            else:
                self.batter_streaks[_bd_striker] = {"boundaries": 0}
        # On dismissal, clear the dismissed batter's streak
        if wicket:
            self.batter_streaks.pop(_bd_striker, None)

        # ── GSME: append this delivery to the rolling 18-ball history ──
        _ball_event = make_ball_event(outcome)
        self.ball_history.append(_ball_event)
        if len(self.ball_history) > BALL_HISTORY_WINDOW:
            self.ball_history.pop(0)

        # Feature 3: increment pitch wear counter for every delivery
        self.innings_balls_bowled += 1
        # FC only: match-long counter, never reset by _reset_innings_state()
        self.match_balls_bowled += 1

        self.prev_delivery_was_extra = extra

        if not hasattr(self, 'current_over_runs'):
            self.current_over_runs = 0
        if self.current_ball == 0:
            self.current_over_runs = 0

        commentary_line = f"{ball_number} {self.current_bowler['name']} to {self.current_striker['name']} - "
        pending_decision_for_response = None

        # A5: Free hit - prepend indicator (dismissal conversion already
        # happened earlier, before pressure/GSME/form state was updated)
        if self.free_hit_active:
            commentary_line = f"FREE HIT! {commentary_line}"

        if wicket:
            self.wickets += 1
            wicket_type = outcome["wicket_type"]

            if wicket_type != "Run Out":
                self.bowler_stats[self.current_bowler["name"]]["wickets"] += 1

            if wicket_type == "Run Out":
                dismissed_end, dismissed_name, fielder_name, commentary_line = (
                    self._apply_run_out(outcome, extra, commentary_line)
                )
            else:
                dismissed_end, dismissed_name, fielder_name, commentary_line = (
                    self._apply_normal_wicket(outcome, extra, commentary_line)
                )

            # Check if team is all out (works for both striker and non-striker dismissals)
            if not self.remaining_batter_indices:
                if self.is_fc:
                    # Mid-over all-out: the top-of-next_ball() _innings_should_end()
                    # check only catches state from BEFORE this ball; the 10th
                    # wicket falling on THIS ball needs the same transition
                    # immediately. _fc_transition_to_next_innings() already
                    # reads the (already-updated) self.wickets/self.score, so
                    # it's safe to call from here too — this bypasses the
                    # T20-specific inline innings==1/else block entirely
                    # rather than re-implementing it a second time for FC.
                    return self._fc_transition_to_next_innings()
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
                    self.target = self._compute_innings2_target()
                    required_rr = self.target / self.overs
                    chasing_team = self.data["team_away"].split("_")[0] if self.batting_team is self.home_xi else self.data["team_home"].split("_")[0]
                    _target_info = f"{chasing_team} needs {self.target} runs from {self.overs} overs at {required_rr:.2f} runs per over"
                    if self.rain_affected:
                        _target_info += " (DLS method)"
                    scorecard_data["target_info"] = _target_info
                    
                    # Save first innings stats
                    self._save_first_innings_stats()
                    self.first_innings_scorecard = scorecard_data

                    # Reset for 2nd innings
                    self.innings = 2
                    # Update lineups if impact player swaps occurred
                    if hasattr(self, 'data') and self.data.get('impact_players_swapped'):
                        self.home_xi = self.data["playing_xi"]["home"]
                        self.away_xi = self.data["playing_xi"]["away"]

                    # D5: Re-derive teams from toss logic (same as overs-exhausted path)
                    # Simple swap breaks when impact player changes update home_xi/away_xi
                    team_home_code = self.match_data["team_home"].split("_")[0]
                    self.batting_team, self.bowling_team = innings_teams(
                        self.toss_winner, self.toss_decision, team_home_code,
                        self.home_xi, self.away_xi, innings=2,
                    )
                    innings_complete_summary = self._format_innings_complete_summary("End of innings")
                    # Weather clock: completed overs of innings 1 (all out mid-over)
                    self._innings1_overs_bowled = self.current_over
                    self.score = 0
                    self.wickets = 0
                    self.current_over = 0
                    self.current_ball = 0
                    self.batter_idx = [0, 1]
                    self.current_striker = self.batting_team[0]
                    self.current_non_striker = self.batting_team[1]
                    self.batsman_stats = {p["name"]: self._new_batting_stats(p) for p in self.batting_team}
                    # bowler_history reset is handled inside _reset_innings_state() via BowlerManager
                    self.bowler_stats = {p["name"]: self._new_bowling_stats(p) for p in self.bowling_team if p["will_bowl"]}
                    self._reset_innings_state()

                    # Notify scenario engine of innings transition
                    if self.scenario_engine:
                        self.scenario_engine.on_innings_transition()

                    return {
                        "innings_end": True,
                        "innings_number": 1,
                        "match_over": False,  # ✅ Keep match going
                        "scorecard_data": scorecard_data,
                        "score": 0,
                        "wickets": 0,
                        "over": 0,
                        "ball": 0,
                    "commentary": (
                        f"{all_out_commentary}<br>"
                        f"{self._format_innings_complete_summary('End of innings')}<br>"
                        f"<strong>End of 1st Innings:</strong> {self.first_innings_score}/10. Target: {self.target}"
                    ),
                        "striker": self.current_striker["name"],
                        "non_striker": self.current_non_striker["name"],
                        "bowler": ""
                    }
                else:
                    # ✅ SECOND INNINGS ALL OUT - Match over

                    # Scores level (tie) — or target already reached via runs
                    # completed before a run out — when the last wicket fell.
                    # Neither is a win for the bowling side, so don't declare
                    # one here: return a non-final ball response and let the
                    # next next_ball() call run the innings-end branch, which
                    # detects the tie and sets up the super over (or awards
                    # the chase). Declaring the result here produced
                    # "won by 0 run(s)" and skipped the super over entirely.
                    if self.score >= self.target - 1:
                        return {
                            "match_over": False,
                            "score": self.score,
                            "wickets": self.wickets,
                            "over": self.current_over,
                            "ball": self.current_ball,
                            "commentary": all_out_commentary,
                            "striker": self.current_striker["name"] if self.current_striker else "",
                            "non_striker": self.current_non_striker["name"] if self.current_non_striker else "",
                            "bowler": self.current_bowler["name"] if self.current_bowler else ""
                        }

                    # Determine winner (Bowling team won)
                    winner_code = self.data["team_home"].split("_")[0] if self.bowling_team is self.home_xi else self.data["team_away"].split("_")[0]
                    run_diff = self.target - self.score - 1
                    self._set_outcome(
                        result_text=f"{winner_code} won by {run_diff} run(s).{self._dls_suffix()}",
                        winner_is_home=(self.bowling_team is self.home_xi),
                        match_status='completed', margin_type='runs', margin_value=run_diff,
                    )

                    self._save_second_innings_stats()
                    self._create_match_archive()

                    #Include logic for all out result

                    # 3. Add dismissed batsman's line (could be striker or non-striker on run-out)
                    out_name      = dismissed_name
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
                    first_block = self._format_scorecard_block(getattr(self, 'first_innings_scorecard', None), '1st Innings Scorecard')
                    second_block = self._format_scorecard_block(scorecard_data, '2nd Innings Scorecard')
                    scorecards_block = f"<br><br><strong>Scorecards:</strong><br>{first_block}<br><br>{second_block}" if first_block and second_block else ""
                    return {
                        "match_over": True,
                        "scorecard_data": scorecard_data,
                        "final_score": self.score,
                        "wickets": self.wickets,
                        # "result": f"All out for {self.score}",
                        "commentary": f"{all_out_commentary}<br>Match Over! All out for {self.score}{scorecards_block}",
                        "result": self.result
                    }

            # 1) Gather the dismissed batsman's stats:
            out_name      = dismissed_name
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
                fielder_part = f"c {fielder_name}"
                bowler_part  = f"b {bowler_name}"
            elif wkt == "Bowled":
                bowler_part = f"b {bowler_name}"
            elif wkt == "LBW":
                bowler_part = f"lbw b {bowler_name}"
            elif wkt == "Run Out":
                fielder_part = f"f {fielder_name}"
            elif wkt == "Stumped":
                fielder_part = f"st {fielder_name}"
                bowler_part  = f"b {bowler_name}"

            # 3) Build the "[0x4, 1x6]" part:
            extras = []
            if fours_scored > 0:
                extras.append(f"{fours_scored}x4")
            if sixes_scored > 0:
                extras.append(f"{sixes_scored}x6")
            extra_str = f"[{', '.join(extras)}]" if extras else ""

            # 4) Combine into one dismissal-line:
            dismissal_line = f"{out_name} "
            if fielder_part:
                dismissal_line += f"{fielder_part} "
            if bowler_part:
                dismissal_line += f"{bowler_part} "
            dismissal_line += f"{runs_scored}({balls_faced}b) {extra_str}"

            # 5) Append it before "New batsman..."
            commentary_line += "<br>" + dismissal_line + "<br>"

            # 6) Load provisional new batter and optionally request manual override
            candidate_indices = sorted(self.remaining_batter_indices)
            provisional_index = self._auto_pick_next_batter_index()
            if provisional_index is None:
                # Recovery path: lineup/index state may desync after swaps/reorder.
                # Try to find any batter who is not currently at crease and not out yet.
                recovery_candidates = []
                for idx, player in enumerate(self.batting_team):
                    if idx in self.batter_idx:
                        continue
                    pstats = self.batsman_stats.get(player["name"], {})
                    if (pstats.get("wicket_type") or "").strip():
                        continue
                    recovery_candidates.append(idx)

                if recovery_candidates:
                    provisional_index = recovery_candidates[0]
                    self.remaining_batter_indices.discard(provisional_index)
                    print(f"⚠️ Recovered missing next batter index via fallback: {self.batting_team[provisional_index]['name']}")
                else:
                    # No batter left to come in: mark innings closed and let the
                    # normal next-call completion path finalize scorecard/result.
                    self.wickets = 10
                    commentary_line += "<br><em>No batter available. Innings closed.</em>"
                    self.commentary.append(commentary_line)
                    return {
                        "match_over": False,
                        "score": self.score,
                        "wickets": self.wickets,
                        "over": self.current_over,
                        "ball": self.current_ball,
                        "commentary": commentary_line,
                        "striker": self.current_striker["name"] if self.current_striker else "",
                        "non_striker": self.current_non_striker["name"] if self.current_non_striker else "",
                        "bowler": self.current_bowler["name"] if self.current_bowler else ""
                    }
            self.remaining_batter_indices.discard(provisional_index)
            self._bring_new_batter(dismissed_end, provisional_index)
            # Run-Out cross handling: a run-out implies 1 completed run before
            # the dismissal, so the batters had physically crossed. When the
            # original striker was dismissed, the surviving non-striker is now
            # at the striker end and the new batter takes the non-striker end.
            # _bring_new_batter("striker", ...) does not account for the cross
            # (it just replaces current_striker), so swap pointers here.
            # The "non_striker" branch of _bring_new_batter already handles the
            # cross correctly via its own swap, so no fix-up is needed there.
            if wicket_type == "Run Out" and dismissed_end == "striker":
                self.current_striker, self.current_non_striker = (
                    self.current_non_striker, self.current_striker
                )
                self.batter_idx.reverse()
            commentary_line += f"<br>{self.current_striker['name']} walks in next."
            if self._is_manual_mode():
                pending_decision_for_response = {
                    "type": "next_batter",
                    "dismissed_name": dismissed_name,
                    "provisional_index": provisional_index,
                    "candidate_indices": candidate_indices
                }
                commentary_line += "<br><em>Manual batting decision required.</em>"
            self.commentary.append(commentary_line)

        else:
            self.score += runs
            self.current_over_runs += runs
            
            # Byes and Leg Byes are not charged to the bowler
            if extra:
                extra_type = outcome.get("extra_type", "")
                if extra_type not in ("Byes", "Leg Bye"):
                    self.bowler_stats[self.current_bowler["name"]]["runs"] += runs
            else:
                self.bowler_stats[self.current_bowler["name"]]["runs"] += runs
            
            if not extra:
                self.batsman_stats[self.current_striker["name"]]["runs"] += runs
                self.batsman_stats[self.current_striker["name"]]["balls"] += 1
                self._credit_partnership_contribution(runs=runs, balls=1)

                # Track run breakdown for legal deliveries
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

                # A2: Bat-runs > 0 invalidate maiden over
                if runs > 0:
                    self.current_over_maiden_invalid = True
            else:
                extra_type = outcome.get("extra_type", "")
                # Byes, Leg Byes, and No Balls: batsman faced the delivery
                if extra_type in ("Byes", "Leg Bye", "No Ball"):
                    self.batsman_stats[self.current_striker["name"]]["balls"] += 1
                    self._credit_partnership_contribution(balls=1)

                # Byes/Leg Byes: batsman scored 0 off the bat → dot ball
                # (runs go to extras, not credited to batsman)
                if extra_type in ("Byes", "Leg Bye"):
                    self.batsman_stats[self.current_striker["name"]]["dots"] += 1
                # No Ball: bat_runs breakdown handled in the No Ball section below
                # Wide: batsman doesn't face the delivery — no stats at all

            if extra and outcome.get("extra_type") == "No Ball":
                bat_runs = outcome.get("bat_runs", 0)
                if bat_runs > 0:
                    self.batsman_stats[self.current_striker["name"]]["runs"] += bat_runs
                    self._credit_partnership_contribution(runs=bat_runs)
                    if bat_runs == 1:
                        self.batsman_stats[self.current_striker["name"]]["ones"] += 1
                    elif bat_runs == 2:
                        self.batsman_stats[self.current_striker["name"]]["twos"] += 1
                    elif bat_runs == 3:
                        self.batsman_stats[self.current_striker["name"]]["threes"] += 1
                    elif bat_runs == 4:
                        self.batsman_stats[self.current_striker["name"]]["fours"] += 1
                    elif bat_runs == 6:
                        self.batsman_stats[self.current_striker["name"]]["sixes"] += 1
                else:
                    # No Ball with 0 bat runs = dot ball for the batsman
                    self.batsman_stats[self.current_striker["name"]]["dots"] += 1

                commentary_line += f"No Ball + {bat_runs} run(s), {outcome.get('bat_description', '')}"
            else:
                commentary_line += f"{runs} run(s), {outcome['description']}"
            self.commentary.append(commentary_line)

            # A3: Strike rotates on odd runs for all delivery types
            should_rotate = False
            if not extra:
                if runs % 2 == 1:
                    should_rotate = True
            else:
                extra_type = outcome.get("extra_type", "")
                if extra_type in ("Leg Bye", "Byes") and runs % 2 == 1:
                    should_rotate = True
                elif extra_type == "No Ball":
                    bat_runs = outcome.get("bat_runs", 0)
                    if bat_runs % 2 == 1:
                        should_rotate = True
                elif extra_type == "Wide":
                    # Wide: 1 penalty run auto-credited; rotate only if
                    # additional completed runs beyond the penalty are odd
                    additional_runs = runs - 1
                    if additional_runs > 0 and additional_runs % 2 == 1:
                        should_rotate = True
            if should_rotate:
                self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
                self.batter_idx.reverse()

            if self.innings == 2 and self.score >= self.target:
                # ✅ UPDATE BOWLER STATS FOR THE MATCH-WINNING BALL
                # Increment ball count for legal deliveries (non-extras OR Byes/Leg Byes)
                is_legal_delivery = not extra
                if extra:
                    extra_type = outcome.get("extra_type", "")
                    if extra_type in ("Byes", "Leg Bye"):
                        is_legal_delivery = True
                
                if is_legal_delivery:
                    self.current_ball += 1  # Increment ball count for this delivery
                    self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1

                # ✅ ADD THIS: Check if over completed with match-winning ball
                if self.current_ball == 6:
                    if not self.current_over_maiden_invalid:
                        self.bowler_stats[self.current_bowler["name"]]["maidens"] += 1
                    self.bowler_stats[self.current_bowler["name"]]["overs"] += 1
                    # Mirror the regular end-of-over finalisation so BowlerManager,
                    # bowler_history, prev-over-runs and per-over counters stay
                    # consistent for the archiver and any post-match readers.
                    self.bowler_manager.record_over_completion(
                        self.current_bowler["name"], self.current_over_runs
                    )
                    self.bowler_prev_over_runs[self.current_bowler["name"]] = self.current_over_runs
                    self.current_ball = 0
                    self.current_over += 1
                    self.bowler_selected_for_over = -1
                    self.current_over_runs = 0
                    self.current_over_outcomes = []
                    self.current_over_maiden_invalid = False

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
                
                balls_left = self._balls_left_in_innings()
                overs_left = self._balls_to_overs_notation(balls_left)

                
                # NOTE: current_ball/current_over reflect post-finalisation state —
                # if the match-winning ball completed an over, current_ball was
                # rolled to 0 and current_over incremented. balls_left is computed
                # from both, so it remains accurate either way.
                print("Check point1 (post-delivery state): {}".format({
                        "current_over": self.current_over,
                        "current_ball": self.current_ball,
                        "balls_left": balls_left,
                        "overs_left": overs_left
                    }))
                
                self._set_outcome(
                    result_text=f"{winner_code} won by {wkts_left} wicket(s) with {overs_left} overs remaining.{self._dls_suffix()}",
                    winner_is_home=(self.batting_team is self.home_xi),
                    match_status='completed', margin_type='wickets', margin_value=wkts_left,
                )

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

                final_commentary = f"{commentary_line}<br>{self._format_innings_complete_summary()}<br><br>"
                final_commentary += f"<strong>Match Over!</strong> {self.result}<br>"
                final_commentary += f"<strong>Final Snapshot:</strong><br>"
                final_commentary += f"{self.current_striker['name']} {striker_stats['runs']}({striker_stats['balls']}) [{striker_stats['fours']}x4, {striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_non_striker['name']} {non_striker_stats['runs']}({non_striker_stats['balls']}) [{non_striker_stats['fours']}x4, {non_striker_stats['sixes']}x6]<br>"
                final_commentary += f"{self.current_bowler['name']} {overs_bowled:.1f}-{bowler_stats['maidens']}-{bowler_stats['runs']}-{bowler_stats['wickets']}{extras_str}"

                first_block = self._format_scorecard_block(getattr(self, 'first_innings_scorecard', None), '1st Innings Scorecard')
                second_block = self._format_scorecard_block(scorecard_data, '2nd Innings Scorecard')
                if first_block and second_block:
                    final_commentary += f"<br><br><strong>Scorecards:</strong><br>{first_block}<br><br>{second_block}"

                self.innings = 3
                scorecard_data["target_info"] = self.result

                self._save_second_innings_stats()
                self._create_match_archive()

                return {
                    "match_over": True,
                    "scorecard_data": scorecard_data,
                    "final_score": self.score,
                    "wickets": self.wickets,
                    "result": self.result,
                    "commentary": final_commentary,
                    "win_probability": 100.0,
                    "ball_data": {
                        "runs": self.score - _bd_score_before,
                        "batter_out": wicket,
                        "is_extra": extra,
                        "extra_type": outcome.get("extra_type") if extra else None,
                        "wicket_type": outcome.get("wicket_type") if wicket else None,
                        "description": outcome.get("description", ""),
                        "free_hit": _bd_was_free_hit,
                        "over": _bd_over,
                        "ball": _bd_ball,
                        "striker": _bd_striker,
                        "non_striker": _bd_non_striker,
                        "bowler": _bd_bowler,
                        "innings": self.fc_innings if self.is_fc else self.innings,
                        "score": self.score,
                        "wickets": self.wickets,
                        "target": getattr(self, 'target', None),
                        "partnership_runs": self.current_partnership_runs,
                        "partnership_balls": self.current_partnership_balls,
                    }
                }

        # Increment ball count for legal deliveries (non-extras OR Byes/Leg Byes)
        is_legal_delivery = not extra and not wicket
        if extra and not wicket:
            extra_type = outcome.get("extra_type", "")
            # Byes and Leg Byes are legal deliveries, Wide and No Ball are not
            if extra_type in ("Byes", "Leg Bye"):
                is_legal_delivery = True
        
        if is_legal_delivery:
            self.current_ball += 1
            self.bowler_stats[self.current_bowler["name"]]["balls_bowled"] += 1

        if extra:
            extra_type = outcome.get("extra_type", "")
            if not extra_type:
                # Fallback to description-based detection for older code paths
                if "Wide" in outcome['description']:
                    extra_type = "Wide"
                elif "No Ball" in outcome['description']:
                    extra_type = "No Ball"
                elif "Leg Bye" in outcome['description']:
                    extra_type = "Leg Bye"
                elif "Byes" in outcome['description']:
                    extra_type = "Byes"

            if extra_type == "Wide":
                self.bowler_stats[self.current_bowler["name"]]["wides"] += 1
                self.current_over_maiden_invalid = True  # A2: wides invalidate maiden
            elif extra_type == "No Ball":
                self.bowler_stats[self.current_bowler["name"]]["noballs"] += 1
                self.current_over_maiden_invalid = True  # A2: no-balls invalidate maiden
            elif extra_type == "Leg Bye":
                self.bowler_stats[self.current_bowler["name"]]["legbyes"] += 1
                # A2: Leg byes do NOT invalidate maiden
            elif extra_type == "Byes":
                self.bowler_stats[self.current_bowler["name"]]["byes"] += 1
                # A2: Byes do NOT invalidate maiden

            # A5: Free hit state management for extras
            if extra_type == "No Ball":
                self.free_hit_active = True  # No Ball triggers free hit
            elif extra_type == "Wide":
                pass  # Wide: free_hit_active stays unchanged (persists through wides)
            else:
                # Byes/Leg Byes come from legal deliveries: consume the free hit
                self.free_hit_active = False
        else:
            # Legal delivery (not extra): reset free hit
            self.free_hit_active = False

        self.current_over_outcomes.append(self._ball_outcome_token(outcome, wicket, runs, extra))

        all_commentary = []
        if self.pending_pre_ball_commentary:
            all_commentary.extend(self.pending_pre_ball_commentary)
            self.pending_pre_ball_commentary = []
        all_commentary.append(commentary_line)
        over_complete = self.current_ball == 6

        if over_complete:
            if not self.current_over_maiden_invalid:
                self.bowler_stats[self.current_bowler["name"]]["maidens"] += 1
            self.bowler_stats[self.current_bowler["name"]]["overs"] += 1
            # BowlerManager records quota + last_bowler + prev-over-runs atomically
            self.bowler_manager.record_over_completion(
                self.current_bowler["name"], self.current_over_runs
            )
            # Keep legacy alias in sync (bowler_history points to _quota via alias,
            # so it is already updated; this line is intentionally left as comment)
            # self.bowler_prev_over_runs is kept for legacy reads; sync it too:
            self.bowler_prev_over_runs[self.current_bowler["name"]] = self.current_over_runs

            all_commentary.append("")
            all_commentary.append(self._format_over_summary(f"End of over {self.current_over + 1}"))

            if self.innings == 2:
                balls_remaining = (self.overs - self.current_over - 1) * 6
                if balls_remaining > 0:
                    required_rr = ((self.target - self.score) * 6) / balls_remaining
                    all_commentary.append(f"Required: {self.target - self.score} runs from {balls_remaining} balls (RRR: {required_rr:.2f})")
            all_commentary.append("<br>")

            self.current_ball = 0
            self.current_over += 1
            self.bowler_selected_for_over = -1
            self.current_over_runs = 0
            self.current_over_outcomes = []
            self.current_over_maiden_invalid = False  # A2: reset for new over
            self.current_striker, self.current_non_striker = self.current_non_striker, self.current_striker
            self.batter_idx.reverse()

            if self.is_fc:
                self.fc_day_overs_bowled_today += 1
                self.fc_ball_overs_bowled += 1
                # Maidens are commonplace in first-class cricket (~1 over in
                # 8), so a single one is not worth remarking on — a RUN of
                # them is. Track the streak for the commentary engine.
                if self.current_over_runs == 0 and not self.current_over_maiden_invalid:
                    self.fc_consecutive_maidens += 1
                else:
                    self.fc_consecutive_maidens = 0
                # New ball taken automatically as soon as it's due (Phase 2
                # — no user-captained delay option yet, consistent with
                # "AI always decides" for FC in this phase).
                if self.fc_ball_overs_bowled >= self.fmt.new_ball_overs:
                    self.fc_ball_overs_bowled = 0

            # ── Rain check at the over boundary ──────────────────────────
            _rain_outcome = self._check_rain_events()
            if _rain_outcome:
                if _rain_outcome.get("final") is not None:
                    _final = _rain_outcome["final"]
                    # Keep the last delivery + over summary ahead of the rain.
                    _final["commentary"] = "<br>".join(
                        all_commentary + [_final["commentary"]]
                    )
                    return _final
                all_commentary.extend(_rain_outcome.get("lines", []))
                self._pending_rain_info = _rain_outcome.get("info")
            else:
                _foreshadow = self._maybe_foreshadow_rain()
                if _foreshadow:
                    all_commentary.append(_foreshadow)

        ball_data_payload = {
            "runs": self.score - _bd_score_before,
            "batter_out": wicket,
            "is_extra": extra,
            "extra_type": outcome.get("extra_type") if extra else None,
            "wicket_type": outcome.get("wicket_type") if wicket else None,
            "description": outcome.get("description", ""),
            "free_hit": _bd_was_free_hit,
            "over": _bd_over,
            "ball": _bd_ball,
            "striker": _bd_striker,
            "non_striker": _bd_non_striker,
            "bowler": _bd_bowler,
            "innings": self.fc_innings if self.is_fc else self.innings,
            "score": self.score,
            "wickets": self.wickets,
            "target": getattr(self, 'target', None),
            "partnership_runs": self.current_partnership_runs,
            "partnership_balls": self.current_partnership_balls,
        }

        if pending_decision_for_response and pending_decision_for_response.get("type") == "next_batter":
            pending_decision_for_response = self._create_next_batter_decision(
                pending_decision_for_response["dismissed_name"],
                pending_decision_for_response["provisional_index"],
                pending_decision_for_response["candidate_indices"]
            )

        if pending_decision_for_response:
            return self._build_decision_required_response(
                pending_decision_for_response,
                commentary="<br>".join(all_commentary),
                ball_data=ball_data_payload
            )

        # Build player stat summaries for the score banner
        _striker_name = self.current_striker["name"]
        _nonstriker_name = self.current_non_striker["name"]
        _bowler_name = self.current_bowler["name"] if self.current_bowler else ""
        _s_stats = self.batsman_stats.get(_striker_name, {})
        _ns_stats = self.batsman_stats.get(_nonstriker_name, {})
        _bw_stats = self.bowler_stats.get(_bowler_name, {})
        _bw_overs_display = _bw_stats.get("overs", 0) + (_bw_stats.get("balls_bowled", 0) % 6) / 10

        _rain_info = self._pending_rain_info
        self._pending_rain_info = None

        return {
            "match_over": False,
            "score": self.score,
            "wickets": self.wickets,
            "over": self.current_over,
            "ball": self.current_ball,
            "commentary": "<br>".join(all_commentary),
            "striker": _striker_name,
            "non_striker": _nonstriker_name,
            "bowler": _bowler_name,
            "striker_runs": _s_stats.get("runs", 0),
            "striker_balls": _s_stats.get("balls", 0),
            "nonstriker_runs": _ns_stats.get("runs", 0),
            "nonstriker_balls": _ns_stats.get("balls", 0),
            "bowler_wickets": _bw_stats.get("wickets", 0),
            "bowler_runs": _bw_stats.get("runs", 0),
            "bowler_overs": f"{_bw_overs_display:.1f}",
            "innings_number": self.fc_innings if self.is_fc else self.innings,
            "target": getattr(self, "target", None),
            "total_overs": self.overs,
            "match_format": self.fmt.name,
            # FC has no fielding-circle phases at all — no phase_name.
            "phase_name": None if self.is_fc else self.fmt.get_phase(self.current_over).name,
            "bowler_overs_remaining": self.bowler_manager.overs_remaining(_bowler_name),
            # FC has no bowling quota — no cap to report.
            "bowler_max_overs": getattr(self.fmt, "max_bowler_overs", None),
            "fc_day": self.fc_day if self.is_fc else None,
            "partnership_runs": self.current_partnership_runs,
            "partnership_balls": self.current_partnership_balls,
            "win_probability": _win_prob,
            "rain_affected": self.rain_affected,
            "dls_par": self._current_dls_par(),
            "rain_interruption": _rain_info,
            "ball_data": ball_data_payload
        }

    def _generate_detailed_scorecard(self):
        """Generate detailed cricbuzz-style scorecard"""
        
        if self.batting_team is self.home_xi:
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
                            status = f"c. {stats.get('fielder_out', '?')} b. {stats.get('bowler_out', '?')}"
                        elif status_raw == "Bowled":
                            status = f"b. {stats.get('bowler_out', '?')}"
                        elif status_raw == "LBW":
                            status = f"lbw b. {stats.get('bowler_out', '?')}"
                        elif status_raw == "Run Out":
                            status = f"run out ({stats.get('fielder_out', '?')})"
                        elif status_raw == "Stumped":
                            status = f"st. {stats.get('fielder_out', '?')} b. {stats.get('bowler_out', '?')}"
                        elif status_raw == "Hit Wicket":
                             status = f"hit wicket b. {stats.get('bowler_out', '?')}"
                    
                    players.append({
                        "name": player_name,
                        "status": status,
                        "wicket_type": stats.get("wicket_type", ""),
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
                        "wicket_type": "",
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
                    "wicket_type": "",
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
                        # Didn't bowl — leave him off the card entirely.
                        # A real scorecard lists the bowlers who bowled, not
                        # everyone who might have. This used to append a row
                        # of empty strings, which the UI renders verbatim as
                        # a blank line; it shows up most in first-class
                        # cricket, where a captain often never needs his
                        # fifth bowler across a whole innings.
                        continue
                else:
                    # Never even got a stats entry — same story, leave him off.
                    continue

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
        """Setup super over after a tie — returns team rosters for player selection"""
        self.super_over_phase = "awaiting_innings1_selection"

        # Freeze pitch wear from the main match — the pitch has physically
        # been through a full innings (or two); it doesn't reset just
        # because a new contest starts. Also ensure innings-2 batting/
        # bowling figures are saved (idempotent) so the fatigue/form
        # carry-over helpers below can read them regardless of which code
        # path reached this tie.
        self._save_second_innings_stats()
        _so_total_balls = self.fmt.overs * 6
        self.super_over_pitch_wear = (
            min(1.0, self.innings_balls_bowled / _so_total_balls) if _so_total_balls else 0.0
        )

        scorecard_data = self._generate_detailed_scorecard()
        scorecard_data["target_info"] = "Match Tied - Super Over Required!"

        def _player_info(p):
            return {
                "name": p["name"],
                "role": p.get("role", ""),
                "batting_rating": p.get("batting_rating", 0),
                "bowling_rating": p.get("bowling_rating", 0),
                "will_bowl": p.get("will_bowl", False),
            }

        return {
            "match_tied": True,
            "super_over_required": True,
            "scorecard_data": scorecard_data,
            "commentary": "MATCH TIED! Super Over Required to decide the winner!",
            "home_team": self.data["team_home"].split("_")[0],
            "away_team": self.data["team_away"].split("_")[0],
            "home_players": [_player_info(p) for p in self.home_xi],
            "away_players": [_player_info(p) for p in self.away_xi],
        }

    def start_super_over(self, first_batting_team, batsmen_names=None, bowler_name=None):
        """Start the super over with user-chosen or auto-selected players"""
        # Re-entry guard: a duplicate POST would increment super_over_round and
        # reset the round's scores mid-flight. Only valid while awaiting the
        # innings-1 selection (set by _setup_super_over / a tied-again round).
        if getattr(self, "super_over_phase", None) != "awaiting_innings1_selection":
            return {
                "error": "super_over_not_awaiting_selection",
                "phase": getattr(self, "super_over_phase", None),
            }

        # IPL rule: from round 2 onwards, the team that batted second in the
        # previous super over must bat first. Enforce here regardless of input.
        required_first = getattr(self, "_super_over_next_first_batting", None)
        if required_first and first_batting_team != required_first:
            first_batting_team = required_first

        if first_batting_team not in ("home", "away"):
            return {"error": "first_batting_team must be 'home' or 'away'"}

        # Resolve prospective teams into locals — nothing committed to self yet.
        if first_batting_team == "home":
            batting_team, bowling_team = self.home_xi, self.away_xi
        else:
            batting_team, bowling_team = self.away_xi, self.home_xi

        # Validate BEFORE committing any round state: a rejected selection must
        # leave the round counter untouched so the retry starts the SAME round —
        # the 5-round cap and boundary count-back key off super_over_round.
        validation_error = self._validate_super_over_selection(
            batsmen_names, bowler_name, batting_team, bowling_team
        )
        if validation_error:
            return {"error": validation_error}

        # Input accepted — commit the round state.
        self.super_over_round += 1
        self.super_over_innings = 1

        # Reset scores for this round (but keep history)
        self.super_over_scores = {"home": 0, "away": 0}
        self.super_over_wickets = {"home": 0, "away": 0}

        self.super_over_batting_team = batting_team
        self.super_over_bowling_team = bowling_team

        # Track which side is batting for team key
        self.super_over_first_batting = first_batting_team

        if batsmen_names:
            self.super_over_batsmen = self._find_players_by_name(
                self.super_over_batting_team, batsmen_names[:3]
            )
        else:
            self.super_over_batsmen = self._select_super_over_batsmen(self.super_over_batting_team)
        self.super_over_batsmen = self._resolve_super_over_batsmen(
            self.super_over_batting_team, self.super_over_batsmen
        )

        if bowler_name:
            found = self._find_players_by_name(self.super_over_bowling_team, [bowler_name])
            self.super_over_bowler = found[0] if found else self._select_super_over_bowler(self.super_over_bowling_team)
        else:
            self.super_over_bowler = self._select_super_over_bowler(self.super_over_bowling_team)

        self._init_super_over_innings_state()
        self.super_over_phase = "innings_in_progress"

        batting_team_name = self.data["team_home"].split("_")[0] if self.super_over_batting_team is self.home_xi else self.data["team_away"].split("_")[0]

        return {
            "super_over_started": True,
            "innings": self.super_over_innings,
            "round": self.super_over_round,
            "batting_team": first_batting_team,
            "batting_team_name": batting_team_name,
            "batsmen": [p["name"] for p in self.super_over_batsmen],
            "bowler": self.super_over_bowler["name"],
        }

    def _validate_super_over_selection(self, batsmen_names, bowler_name, batting_team, bowling_team):
        """Strict validation: exactly 3 distinct batsmen from batting team, 1 bowler from bowling team."""
        if batsmen_names is not None or bowler_name is not None:
            if not batsmen_names or len(batsmen_names) != 3:
                return "Super over requires exactly 3 batsmen"
            if len(set(batsmen_names)) != 3:
                return "Super over batsmen must be distinct"
            batting_names = {p["name"] for p in batting_team}
            for name in batsmen_names:
                if name not in batting_names:
                    return f"Batsman '{name}' not in batting team"
            if not bowler_name:
                return "Super over requires 1 bowler"
            bowling_names = {p["name"] for p in bowling_team}
            if bowler_name not in bowling_names:
                return f"Bowler '{bowler_name}' not in bowling team"
        return None

    def _find_players_by_name(self, team, names):
        """Find player dicts by name from a team list"""
        result = []
        for name in names:
            for p in team:
                if p["name"] == name:
                    result.append(p)
                    break
        return result

    def _resolve_super_over_batsmen(self, team, selected):
        """Return exactly three distinct batter dicts, falling back safely when names are invalid."""
        resolved = []
        seen = set()

        for player in selected or []:
            name = player.get("name")
            if not name or name in seen:
                continue
            resolved.append(player)
            seen.add(name)
            if len(resolved) == 3:
                return resolved

        for player in self._select_super_over_batsmen(team):
            name = player.get("name")
            if not name or name in seen:
                continue
            resolved.append(player)
            seen.add(name)
            if len(resolved) == 3:
                return resolved

        for player in team:
            name = player.get("name")
            if not name or name in seen:
                continue
            resolved.append(player)
            seen.add(name)
            if len(resolved) == 3:
                return resolved

        # Last-resort padding when the team roster is too small (e.g., synthetic tests).
        while len(resolved) < 3 and resolved:
            resolved.append(resolved[-1])

        if len(resolved) < 3:
            raise ValueError("Unable to resolve super over batsmen from team roster")

        return resolved

    def _init_super_over_innings_state(self):
        """Initialize/reset super over innings state"""
        self.super_over_ball = 0
        # This Super Over innings' own delivery-by-delivery history — feeds
        # the micro-GSME momentum layer. Deliberately NOT the main match's
        # ball_history: momentum should reflect this shootout, not the
        # innings that just ended.
        self.super_over_ball_history = []
        self.super_over_current_striker = self.super_over_batsmen[0]
        self.super_over_current_non_striker = self.super_over_batsmen[1]
        # First two are at the crease; index 2 is the next batter in.
        self.super_over_batter_idx = [0, 1]
        self.super_over_next_batter_idx = 2
        self.super_over_bowler_runs = 0
        self.super_over_bowler_wickets = 0

        self.super_over_batsman_stats = {
            p["name"]: {
                "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                "wicket_type": "", "out": False, "did_bat": False,
            }
            for p in self.super_over_batsmen
        }
        # Mark the two openers as having batted; the 3rd flips to True only if they come in.
        self.super_over_batsman_stats[self.super_over_batsmen[0]["name"]]["did_bat"] = True
        self.super_over_batsman_stats[self.super_over_batsmen[1]["name"]]["did_bat"] = True

    def start_super_over_innings2(self, batsmen_names=None, bowler_name=None):
        """Start innings 2 of the current super over with user-chosen players"""
        # Re-entry guard: a duplicate POST after innings 2 has started would
        # wipe its in-progress state via _init_super_over_innings_state.
        if getattr(self, "super_over_phase", None) != "awaiting_innings2_selection":
            return {
                "error": "super_over_not_awaiting_selection",
                "phase": getattr(self, "super_over_phase", None),
            }

        # Teams were already swapped by _end_super_over_innings
        validation_error = self._validate_super_over_selection(
            batsmen_names, bowler_name,
            self.super_over_batting_team, self.super_over_bowling_team
        )
        if validation_error:
            return {"error": validation_error}

        if batsmen_names:
            self.super_over_batsmen = self._find_players_by_name(
                self.super_over_batting_team, batsmen_names[:3]
            )
        else:
            self.super_over_batsmen = self._select_super_over_batsmen(self.super_over_batting_team)
        self.super_over_batsmen = self._resolve_super_over_batsmen(
            self.super_over_batting_team, self.super_over_batsmen
        )

        if bowler_name:
            found = self._find_players_by_name(self.super_over_bowling_team, [bowler_name])
            self.super_over_bowler = found[0] if found else self._select_super_over_bowler(self.super_over_bowling_team)
        else:
            self.super_over_bowler = self._select_super_over_bowler(self.super_over_bowling_team)

        self._init_super_over_innings_state()
        self.super_over_phase = "innings_in_progress"

        batting_team_name = self.data["team_home"].split("_")[0] if self.super_over_batting_team is self.home_xi else self.data["team_away"].split("_")[0]
        team_key = "home" if self.super_over_batting_team is self.home_xi else "away"
        other_key = "away" if team_key == "home" else "home"
        target = self.super_over_scores[other_key] + 1

        return {
            "super_over_innings2_started": True,
            "innings": 2,
            "round": self.super_over_round,
            "target": target,
            "batting_team_name": batting_team_name,
            "batsmen": [p["name"] for p in self.super_over_batsmen],
            "bowler": self.super_over_bowler["name"],
        }

    def _select_super_over_batsmen(self, team):
        """Auto-select top 3 batsmen by rating for the super over (2 openers + 1 reserve)."""
        sorted_batsmen = sorted(team, key=lambda p: p["batting_rating"], reverse=True)
        return sorted_batsmen[:3]

    def _select_super_over_bowler(self, team):
        """Select best bowler by rating"""
        bowlers = [p for p in team if p.get("will_bowl", False)]
        if bowlers:
            return max(bowlers, key=lambda p: p.get("bowling_rating", 0))
        if team:
            return max(team, key=lambda p: p.get("bowling_rating", 0))
        raise ValueError("Cannot select super over bowler from an empty team")

    def _get_super_over_effective_bowler(self, bowler_dict: dict) -> dict:
        """
        Bowler fatigue carried into the Super Over from whichever innings
        they actually bowled in — any bowler is selectable for the Super
        Over regardless of how much they've already bowled, so a strike
        bowler who sent down a full spell (including the last over) should
        show up more tired than someone rested.

        Deliberately reads the frozen first/second_innings_bowling_stats
        snapshots rather than the live self.bowler_manager: bowler_manager
        only tracks the CURRENT innings (it's rebuilt via .reset() at the
        innings-2 transition, discarding innings-1 data) and isn't part of
        the Super Over resume snapshot, so it can't be trusted for either
        side by Super Over time — the frozen stats dicts are.

        Phase and previous-over-feedback multipliers (used for regular
        deliveries via _get_effective_bowler_dict) are intentionally not
        applied here — self.current_over is stale by Super Over time, and
        there's no "previous over" or "phase" concept within a single over.
        """
        name = bowler_dict.get("name", "")
        eff = dict(bowler_dict)

        fig = (self.first_innings_bowling_stats or {}).get(name) \
            or (self.second_innings_bowling_stats or {}).get(name)
        overs_bowled = fig.get("overs", 0) if fig else 0

        fatigue = BowlerManager._FATIGUE_TABLE.get(
            min(overs_bowled, self.fmt.max_bowler_overs),
            BowlerManager._FATIGUE_TABLE[self.fmt.max_bowler_overs],
        )
        eff["bowling_rating"] = eff["bowling_rating"] * fatigue
        logger.debug(
            "[SuperOver][Fatigue] %s overs_bowled=%d fatigue=%.3f -> rating=%.1f",
            name, overs_bowled, fatigue, eff["bowling_rating"],
        )
        return eff

    def _get_super_over_effective_batter(self, batter_dict: dict) -> dict:
        """Batter form carried into the Super Over from whichever main-innings
        stats snapshot they appear in (first or second innings — either team's
        batter can be selected). Defaults to neutral form (1.0) for a batter
        who never got to the crease in the main match."""
        name = batter_dict.get("name", "")
        stats = (self.first_innings_batting_stats or {}).get(name) \
            or (self.second_innings_batting_stats or {}).get(name)
        form = stats.get("form", 1.0) if stats else 1.0
        eff = dict(batter_dict)
        eff["batting_rating"] = eff["batting_rating"] * form
        return eff

    def next_super_over_ball(self):
        """Process next ball in super over — returns rich data for modal UI"""
        # Re-entry guard: between innings the phase is "awaiting_*_selection"
        # and after the decision it is "complete", but super_over_ball and
        # super_over_batsman_stats stay stale until the next start_super_over*
        # call resets them. A duplicate/retried POST in those windows would
        # re-run _end_super_over_innings on already-accumulated stats —
        # double-counting boundaries/career runs under the swapped team key
        # and declaring a winner before innings 2 is ever played.
        if getattr(self, "super_over_phase", None) != "innings_in_progress":
            return {
                "error": "super_over_not_in_progress",
                "phase": getattr(self, "super_over_phase", None),
            }

        team_key = "home" if self.super_over_batting_team is self.home_xi else "away"
        other_key = "away" if team_key == "home" else "home"

        if self.super_over_ball >= 6 or self.super_over_wickets[team_key] >= 2:
            return self._end_super_over_innings()

        # Innings 2: end immediately if target was already reached on a previous ball
        runs_needed = None
        if self.super_over_innings == 2:
            target = self.super_over_scores[other_key] + 1
            runs_needed = max(0, target - self.super_over_scores[team_key])
            if runs_needed <= 0:
                return self._end_super_over_innings()

        # Calculate outcome on the same rating/matchup/pressure/momentum
        # stack as a regular delivery, recalibrated for a 6-ball/2-wicket
        # contest — see engine/super_over_outcome.py.
        #
        # Thread the striker's running boundary count into `streak` (drives
        # compute_weighted_prob's own ≥2-boundary streak penalty/boost) and
        # this over's own ball-by-ball history (drives the micro-GSME
        # momentum layer — NOT the main innings' ball_history).
        striker_so_stats = self.super_over_batsman_stats[self.super_over_current_striker["name"]]
        effective_batter = self._get_super_over_effective_batter(self.super_over_current_striker)
        effective_bowler = self._get_super_over_effective_bowler(self.super_over_bowler)
        outcome = calculate_super_over_outcome(
            batter=effective_batter,
            bowler=effective_bowler,
            pitch=self.pitch,
            streak={"boundaries": striker_so_stats["fours"] + striker_so_stats["sixes"]},
            batter_runs=striker_so_stats["runs"],
            balls_faced=striker_so_stats["balls"],
            so_innings=self.super_over_innings,
            wickets_down=self.super_over_wickets[team_key],
            balls_remaining=6 - self.super_over_ball,
            runs_needed=runs_needed,
            score_so_far=self.super_over_scores[team_key],
            history=self.super_over_ball_history,
            pitch_wear=getattr(self, "super_over_pitch_wear", 0.0),
            fielding_team=self.super_over_bowling_team,
            pressure_engine=self.pressure_engine,
            ground_config_override=self.ground_config,
        )
        self.super_over_ball_history.append(make_ball_event(outcome))

        runs, wicket, extra = outcome["runs"], outcome["batter_out"], outcome["is_extra"]
        extra_type = outcome.get("extra_type", "")

        # Rich commentary: use commentary_engine (same as regular match) with ball/player prefix
        ball_num = self.super_over_ball + 1  # 1-indexed for display
        commentary_prefix = f"0.{ball_num} {self.super_over_bowler['name']} to {self.super_over_current_striker['name']} - "

        if hasattr(self, 'commentary_engine'):
            # Enrich outcome with context for the commentary engine
            outcome['batter'] = self.super_over_current_striker['name']
            outcome['bowler'] = self.super_over_bowler['name']
            outcome['batting_team'] = self._get_team_name(self.super_over_batting_team)
            outcome['bowling_team'] = self._get_team_name(self.super_over_bowling_team)
            outcome['bowling_type'] = self.super_over_bowler.get('bowling_type') or ''
            if outcome.get('batter_out'):
                outcome['type'] = 'wicket'

            # Build minimal match state for commentary engine
            so_comm_state = {
                'is_super_over': True,  # suppress main-innings narratives (powerplay etc.)
                'innings': self.super_over_innings,
                'score': self.super_over_scores[team_key],
                'wickets': self.super_over_wickets[team_key],
                'overs': 0,
                'current_ball': self.super_over_ball,
                'batter_runs': self.super_over_batsman_stats[self.super_over_current_striker["name"]]["runs"],
                'partnership_runs': 0,
                'current_over_runs': 0,
                'recent_wickets_match': 0,
                'is_maiden_over': False,
            }

            new_text = self.commentary_engine.get_commentary(outcome, so_comm_state)
            if new_text:
                commentary_line = f"{commentary_prefix}{runs} run(s), {new_text}"
            else:
                commentary_line = f"{commentary_prefix}{outcome.get('description', '')}"
        else:
            commentary_line = f"{commentary_prefix}{outcome.get('description', '')}"

        if wicket:
            self.super_over_wickets[team_key] += 1
            wicket_type = outcome["wicket_type"]
            # Run outs are not credited to the bowler (same rule as the main
            # innings) — they still count against the batting side's 2-wicket
            # limit above.
            if wicket_type != "Run Out":
                self.super_over_bowler_wickets += 1
            so_crossed = False  # only meaningful for run-outs

            if wicket_type == "Run Out":
                # Completed runs before dismissal (0, 1, or 2)
                completed = max(0, runs)
                self.super_over_scores[team_key] += completed
                self.super_over_bowler_runs += completed
                self.super_over_batsman_stats[self.super_over_current_striker["name"]]["runs"] += completed
                # Byes/Leg Byes/No Balls are faced deliveries — count the ball
                so_ro_legal = not extra
                if extra and extra_type in ("Byes", "Leg Bye", "No Ball"):
                    so_ro_legal = True
                if so_ro_legal:
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["balls"] += 1

                so_dismissed_end = random.choice(["striker", "non_striker"])
                so_dismissed_name = (self.super_over_current_striker["name"]
                                     if so_dismissed_end == "striker"
                                     else self.super_over_current_non_striker["name"])
                self.super_over_batsman_stats[so_dismissed_name]["wicket_type"] = "Run Out"
                self.super_over_batsman_stats[so_dismissed_name]["out"] = True
                so_crossed = (completed % 2 == 1)
            else:
                so_dismissed_name = self.super_over_current_striker["name"]
                so_dismissed_end = "striker"
                self.super_over_batsman_stats[so_dismissed_name]["wicket_type"] = wicket_type
                self.super_over_batsman_stats[so_dismissed_name]["out"] = True
                # Byes/Leg Byes/No Balls are faced deliveries — count the ball
                so_wk_legal = not extra
                if extra and extra_type in ("Byes", "Leg Bye", "No Ball"):
                    so_wk_legal = True
                if so_wk_legal:
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["balls"] += 1

            # Super over: 3 batsmen selected, max 2 wickets allowed.
            # On the first wicket, bring the next batter in at the dismissed end.
            # For run-outs, account for whether batters had crossed before the dismissal —
            # crossing flips which physical end the surviving batter and the new batter occupy.
            if self.super_over_wickets[team_key] < 2:
                next_batter = None
                if self.super_over_next_batter_idx < len(self.super_over_batsmen):
                    next_batter = self.super_over_batsmen[self.super_over_next_batter_idx]
                    self.super_over_next_batter_idx += 1
                if next_batter is not None:
                    self.super_over_batsman_stats[next_batter["name"]]["did_bat"] = True
                    surviving = (self.super_over_current_non_striker
                                 if so_dismissed_end == "striker"
                                 else self.super_over_current_striker)
                    if so_crossed:
                        # Batters crossed during the runs — surviving batter has swapped ends.
                        if so_dismissed_end == "striker":
                            self.super_over_current_striker = surviving
                            self.super_over_current_non_striker = next_batter
                        else:
                            self.super_over_current_striker = next_batter
                            self.super_over_current_non_striker = surviving
                    else:
                        # No crossing — new batter takes the dismissed end.
                        if so_dismissed_end == "striker":
                            self.super_over_current_striker = next_batter
                        else:
                            self.super_over_current_non_striker = next_batter
        else:
            self.super_over_scores[team_key] += runs
            # Byes and Leg Byes are not charged to the bowler (same rule as
            # the main innings); Wides/No Balls are.
            if not (extra and extra_type in ("Byes", "Leg Bye")):
                self.super_over_bowler_runs += runs

            if not extra:
                # Legal delivery: credit runs, balls, boundaries to batsman
                self.super_over_batsman_stats[self.super_over_current_striker["name"]]["runs"] += runs
                self.super_over_batsman_stats[self.super_over_current_striker["name"]]["balls"] += 1

                if runs == 4:
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["fours"] += 1
                elif runs == 6:
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["sixes"] += 1
            else:
                # Fix #9: Byes, Leg Byes, No Balls — batsman faced the delivery
                if extra_type in ("Byes", "Leg Bye", "No Ball"):
                    self.super_over_batsman_stats[self.super_over_current_striker["name"]]["balls"] += 1
                # Wide: batsman doesn't face — no stats

            # Fix #8: Strike rotation applies to ALL delivery types
            so_should_rotate = False
            if not extra:
                if runs % 2 == 1:
                    so_should_rotate = True
            else:
                if extra_type in ("Leg Bye", "Byes") and runs % 2 == 1:
                    so_should_rotate = True
                elif extra_type == "No Ball":
                    so_bat_runs = outcome.get("bat_runs", 0)
                    if so_bat_runs % 2 == 1:
                        so_should_rotate = True
                elif extra_type == "Wide":
                    so_additional = runs - 1
                    if so_additional > 0 and so_additional % 2 == 1:
                        so_should_rotate = True
            if so_should_rotate:
                self.super_over_current_striker, self.super_over_current_non_striker = \
                    self.super_over_current_non_striker, self.super_over_current_striker

        # Fix #7: Ball counter — Byes/Leg Byes ARE legal deliveries
        is_so_legal = not extra
        if extra and extra_type in ("Byes", "Leg Bye"):
            is_so_legal = True
        if is_so_legal:
            self.super_over_ball += 1

        over_complete = self.super_over_ball >= 6
        # Innings 2: end immediately when target is reached or exceeded
        target_reached = False
        if self.super_over_innings == 2:
            other_key = "away" if team_key == "home" else "home"
            target = self.super_over_scores[other_key] + 1
            if self.super_over_scores[team_key] >= target:
                target_reached = True
        is_innings_complete = over_complete or self.super_over_wickets[team_key] >= 2 or target_reached

        # Build rich response
        striker_name = self.super_over_current_striker["name"]
        non_striker_name = self.super_over_current_non_striker["name"]
        striker_stats = self.super_over_batsman_stats.get(striker_name, {})
        non_striker_stats = self.super_over_batsman_stats.get(non_striker_name, {})

        return {
            "super_over_ball_complete": True,
            "wicket": wicket,
            "runs": runs,
            "commentary": commentary_line,
            "score": self.super_over_scores[team_key],
            "wickets": self.super_over_wickets[team_key],
            "ball": self.super_over_ball,
            "innings_complete": is_innings_complete,
            # Rich data for modal UI
            "striker": striker_name,
            "striker_runs": striker_stats.get("runs", 0),
            "striker_balls": striker_stats.get("balls", 0),
            "non_striker": non_striker_name,
            "nonstriker_runs": non_striker_stats.get("runs", 0),
            "nonstriker_balls": non_striker_stats.get("balls", 0),
            "bowler": self.super_over_bowler["name"],
            "bowler_runs": self.super_over_bowler_runs,
            "bowler_wickets": self.super_over_bowler_wickets,
            "bowler_overs": f"0.{self.super_over_ball}",
            "ball_data": {
                "runs": runs,
                "batter_out": wicket,
                "extra_type": extra_type if extra else None,
                "is_extra": extra,
            },
        }

    def _get_super_over_innings_scorecard(self):
        """Build mini scorecard data for the current super over innings"""
        batting = []
        for name, stats in self.super_over_batsman_stats.items():
            sr = round((stats["runs"] / stats["balls"]) * 100, 1) if stats["balls"] > 0 else 0
            if stats["out"]:
                status = stats["wicket_type"]
            elif stats.get("did_bat"):
                status = "not out"
            else:
                status = "did not bat"
            batting.append({
                "name": name, "runs": stats["runs"], "balls": stats["balls"],
                "fours": stats["fours"], "sixes": stats["sixes"],
                "sr": sr, "status": status, "out": stats["out"],
                "did_bat": stats.get("did_bat", False),
            })
        bowling = {
            "name": self.super_over_bowler["name"],
            "runs": self.super_over_bowler_runs,
            "wickets": self.super_over_bowler_wickets,
            "balls": self.super_over_ball,
            "overs": f"0.{self.super_over_ball}",
        }
        team_key = "home" if self.super_over_batting_team is self.home_xi else "away"
        return {
            "batting": batting,
            "bowling": bowling,
            "total": self.super_over_scores[team_key],
            "wickets": self.super_over_wickets[team_key],
        }

    def _end_super_over_innings(self):
        """Handle end of super over innings"""
        team_key = "home" if self.super_over_batting_team is self.home_xi else "away"

        # Save innings 1 scorecard before swapping
        innings_scorecard = self._get_super_over_innings_scorecard()

        # Accumulate this innings into the cumulative super-over stores (used for
        # the boundary count-back and for career-stat aggregation at archive time).
        # The batting side is team_key; the bowler belongs to the other side.
        self._accumulate_super_over_player_stats(innings_scorecard, team_key)

        if self.super_over_innings == 1:
            # Save innings 1 data
            self.super_over_innings1_scorecard = innings_scorecard
            self.super_over_phase = "awaiting_innings2_selection"

            # Swap teams for innings 2
            self.super_over_innings = 2
            self.super_over_batting_team, self.super_over_bowling_team = \
                self.super_over_bowling_team, self.super_over_batting_team

            other_key = "away" if team_key == "home" else "home"
            target = self.super_over_scores[team_key] + 1

            def _pi(p):
                return {
                    "name": p["name"], "role": p.get("role", ""),
                    "batting_rating": p.get("batting_rating", 0),
                    "bowling_rating": p.get("bowling_rating", 0),
                    "will_bowl": p.get("will_bowl", False),
                }

            batting_team_key = "home" if self.super_over_batting_team is self.home_xi else "away"

            return {
                "super_over_innings_end": True,
                "innings": 1,
                "round": self.super_over_round,
                "target": target,
                "first_innings_score": self.super_over_scores[team_key],
                "innings_scorecard": innings_scorecard,
                "batting_team": batting_team_key,
                "batting_team_name": self.data["team_home"].split("_")[0] if self.super_over_batting_team is self.home_xi else self.data["team_away"].split("_")[0],
                "bowling_team_name": self.data["team_home"].split("_")[0] if self.super_over_bowling_team is self.home_xi else self.data["team_away"].split("_")[0],
                # Team rosters for innings 2 player selection
                "batting_team_players": [_pi(p) for p in self.super_over_batting_team],
                "bowling_team_players": [_pi(p) for p in self.super_over_bowling_team],
            }
        else:
            # End of second innings — determine winner
            home_score = self.super_over_scores["home"]
            away_score = self.super_over_scores["away"]

            self.super_over_history.append({
                "round": self.super_over_round,
                "home_score": home_score,
                "away_score": away_score,
            })

            home_name = self.data["team_home"].split("_")[0]
            away_name = self.data["team_away"].split("_")[0]

            if home_score != away_score:
                if home_score > away_score:
                    winner = home_name
                    margin = home_score - away_score
                else:
                    winner = away_name
                    margin = away_score - home_score

                result = f"{winner} won by Super Over"
                self._set_outcome(
                    result_text=result, winner_is_home=(winner == home_name),
                    match_status='completed', margin_type='runs', margin_value=margin,
                )
                self.innings = 5
                self.super_over_phase = "complete"

                if hasattr(self, 'original_scorecard'):
                    self.original_scorecard["target_info"] = result

                self._save_second_innings_stats()
                self._create_match_archive()

                return {
                    "super_over_complete": True,
                    "match_over": True,
                    "result": result,
                    "scorecard_data": getattr(self, 'original_scorecard', None),
                    "round": self.super_over_round,
                    "home_score": home_score,
                    "away_score": away_score,
                    "home_team": home_name,
                    "away_team": away_name,
                    "innings1_scorecard": getattr(self, 'super_over_innings1_scorecard', None),
                    "innings2_scorecard": innings_scorecard,
                }
            else:
                # Another tie — but cap at 5 super overs max
                if self.super_over_round >= 5:
                    # After 5 super overs still level, decide by total boundaries
                    # (fours + sixes) hit across ALL super overs — the classic
                    # count-back tie-breaker. Only a dead-level boundary count
                    # results in an actual draw.
                    home_b = self.super_over_team_boundaries.get("home", 0)
                    away_b = self.super_over_team_boundaries.get("away", 0)
                    if home_b != away_b:
                        bound_winner = home_name if home_b > away_b else away_name
                        result = (f"{bound_winner} won on boundary count-back after "
                                  f"{self.super_over_round} Super Overs "
                                  f"({max(home_b, away_b)}-{min(home_b, away_b)})")
                        self._set_outcome(
                            result_text=result, winner_is_home=(bound_winner == home_name),
                            match_status='completed', margin_type='boundary_count',
                            margin_value=abs(home_b - away_b),
                        )
                    else:
                        result = (f"Match Drawn after {self.super_over_round} Super Overs "
                                  f"— scores and boundaries level")
                        self._set_outcome(
                            result_text=result, winner_is_home=None,
                            match_status='tied', margin_type='tie', margin_value=None,
                        )
                    self.innings = 5
                    self.super_over_phase = "complete"

                    if hasattr(self, 'original_scorecard'):
                        self.original_scorecard["target_info"] = result

                    self._save_second_innings_stats()
                    self._create_match_archive()

                    return {
                        "super_over_complete": True,
                        "match_over": True,
                        "result": result,
                        "scorecard_data": getattr(self, 'original_scorecard', None),
                        "round": self.super_over_round,
                        "home_score": home_score,
                        "away_score": away_score,
                        "home_team": home_name,
                        "away_team": away_name,
                        "innings1_scorecard": getattr(self, 'super_over_innings1_scorecard', None),
                        "innings2_scorecard": innings_scorecard,
                    }

                # Another tie — allow next super over
                def _pi(p):
                    return {
                        "name": p["name"], "role": p.get("role", ""),
                        "batting_rating": p.get("batting_rating", 0),
                        "bowling_rating": p.get("bowling_rating", 0),
                        "will_bowl": p.get("will_bowl", False),
                    }

                # Per IPL rule: team that batted 2nd in this round bats 1st in next.
                next_first = "away" if self.super_over_first_batting == "home" else "home"
                self._super_over_next_first_batting = next_first
                self.super_over_phase = "awaiting_innings1_selection"

                return {
                    "super_over_tied_again": True,
                    "match_over": False,
                    "round": self.super_over_round,
                    "home_score": home_score,
                    "away_score": away_score,
                    "home_team": home_name,
                    "away_team": away_name,
                    "innings1_scorecard": getattr(self, 'super_over_innings1_scorecard', None),
                    "innings2_scorecard": innings_scorecard,
                    "home_players": [_pi(p) for p in self.home_xi],
                    "away_players": [_pi(p) for p in self.away_xi],
                    "next_first_batting_team": next_first,
                }

    def _accumulate_super_over_player_stats(self, sc, batting_key):
        """Fold one finished super-over innings into the cumulative cross-round
        stores: per-player batting/bowling (for career-stat aggregation at
        archive time) and per-team boundaries (for the count-back tie-breaker).
        ``batting_key`` is the side that just batted; the bowler is the other side."""
        bowling_key = "away" if batting_key == "home" else "home"
        bat_store = self.super_over_career_batting.setdefault(batting_key, {})
        bowl_store = self.super_over_career_bowling.setdefault(bowling_key, {})

        team_boundaries = 0
        for b in sc.get("batting", []):
            if not b.get("did_bat"):
                continue
            agg = bat_store.setdefault(b["name"], {
                "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "wicket_type": "",
            })
            agg["runs"] += b.get("runs", 0)
            agg["balls"] += b.get("balls", 0)
            agg["fours"] += b.get("fours", 0)
            agg["sixes"] += b.get("sixes", 0)
            # status carries the dismissal type for out batters (else "not out").
            if b.get("out") and b.get("status"):
                agg["wicket_type"] = b["status"]
            team_boundaries += b.get("fours", 0) + b.get("sixes", 0)

        self.super_over_team_boundaries[batting_key] = \
            self.super_over_team_boundaries.get(batting_key, 0) + team_boundaries

        bowl = sc.get("bowling", {})
        bname = bowl.get("name")
        if bname:
            bagg = bowl_store.setdefault(bname, {
                "balls_bowled": 0, "runs": 0, "wickets": 0,
            })
            bagg["balls_bowled"] += bowl.get("balls", 0)
            bagg["runs"] += bowl.get("runs", 0)
            bagg["wickets"] += bowl.get("wickets", 0)

    def get_super_over_resume_state(self):
        """Snapshot of super-over state for resuming the UI after a page refresh.
        The frontend maps ``phase`` to the right modal/loop. (Process-restart
        resume is handled separately — see serialize_super_over_snapshot.)"""
        phase = getattr(self, "super_over_phase", None)
        if self.innings >= 5 or phase == "complete":
            return {"phase": "complete"}

        home_name = self.data["team_home"].split("_")[0]
        away_name = self.data["team_away"].split("_")[0]

        def _pi(p):
            return {
                "name": p["name"], "role": p.get("role", ""),
                "batting_rating": p.get("batting_rating", 0),
                "bowling_rating": p.get("bowling_rating", 0),
                "will_bowl": p.get("will_bowl", False),
            }

        scores = getattr(self, "super_over_scores", None) or {"home": 0, "away": 0}
        state = {
            "phase": phase,
            "round": self.super_over_round,
            "innings": getattr(self, "super_over_innings", 1),
            "home_team": home_name,
            "away_team": away_name,
            "home_score": scores.get("home", 0),
            "away_score": scores.get("away", 0),
        }

        if phase == "awaiting_innings1_selection":
            state.update({
                # Round about to be played (start_super_over increments the counter).
                "display_round": self.super_over_round + 1,
                "forced_first_batting": getattr(self, "_super_over_next_first_batting", None),
                "home_players": [_pi(p) for p in self.home_xi],
                "away_players": [_pi(p) for p in self.away_xi],
            })
        elif phase == "awaiting_innings2_selection":
            team_key = "home" if self.super_over_batting_team is self.home_xi else "away"
            other_key = "away" if team_key == "home" else "home"
            state.update({
                "target": scores.get(other_key, 0) + 1,
                "batting_team_name": home_name if team_key == "home" else away_name,
                "batting_team_players": [_pi(p) for p in self.super_over_batting_team],
                "bowling_team_players": [_pi(p) for p in self.super_over_bowling_team],
                "innings1_scorecard": getattr(self, "super_over_innings1_scorecard", None),
            })
        elif phase == "innings_in_progress":
            team_key = "home" if self.super_over_batting_team is self.home_xi else "away"
            other_key = "away" if team_key == "home" else "home"
            in2 = getattr(self, "super_over_innings", 1) == 2
            state.update({
                "target": (scores.get(other_key, 0) + 1) if in2 else None,
                "ball": getattr(self, "super_over_ball", 0),
                "batting_team_name": home_name if team_key == "home" else away_name,
            })

        return state

    def serialize_super_over_snapshot(self):
        """JSON-safe snapshot of the super-over state PLUS the tied main match's
        completion payload (everything MatchArchiver / the tournament finalizer
        read off this object at super-over completion). Persisted into the match
        JSON by the routes layer after every super-over transition so a process
        restart or instance eviction mid-super-over is recoverable instead of
        silently resimulating the whole match. Returns None when there is no
        live super over to snapshot."""
        phase = getattr(self, "super_over_phase", None)
        if self.innings < 4 or phase in (None, "complete"):
            return None

        def _side(team):
            return "home" if team is self.home_xi else "away"

        snap = {
            "v": 1,
            "innings": self.innings,
            "main_match": {
                # Side batting the (tied) second innings — batting_team identity
                # must survive the round-trip for _save_second_innings_stats.
                "second_batting_side": _side(self.batting_team),
                "score": self.score,
                "wickets": self.wickets,
                "target": getattr(self, "target", None),
                "first_innings_score": getattr(self, "first_innings_score", None),
                "batsman_stats": self.batsman_stats,
                "bowler_stats": self.bowler_stats,
                "first_innings_batting_stats": self.first_innings_batting_stats,
                "first_innings_bowling_stats": self.first_innings_bowling_stats,
                # Needed for Super Over batter-form/bowler-fatigue carry-over
                # (_get_super_over_effective_batter/_bowler) to survive a
                # process restart mid-super-over.
                "second_innings_batting_stats": getattr(self, "second_innings_batting_stats", {}),
                "second_innings_bowling_stats": getattr(self, "second_innings_bowling_stats", {}),
                "first_innings_partnerships": self.first_innings_partnerships,
                "second_innings_partnerships": self.second_innings_partnerships,
                "first_batting_team_name": self.first_batting_team_name,
                "first_bowling_team_name": self.first_bowling_team_name,
                "rain_affected": getattr(self, "rain_affected", False),
                "original_scorecard": getattr(self, "original_scorecard", None),
                "first_innings_scorecard": getattr(self, "first_innings_scorecard", None),
                "commentary": self.commentary,
                "commentary_replay_log": getattr(self, "commentary_replay_log", []),
            },
            "super_over": {
                "phase": phase,
                "round": self.super_over_round,
                "so_innings": getattr(self, "super_over_innings", 1),
                "scores": getattr(self, "super_over_scores", None),
                "wickets_by_side": getattr(self, "super_over_wickets", None),
                "first_batting": getattr(self, "super_over_first_batting", None),
                "next_first_batting": getattr(self, "_super_over_next_first_batting", None),
                "history": self.super_over_history,
                "team_boundaries": self.super_over_team_boundaries,
                "career_batting": self.super_over_career_batting,
                "career_bowling": self.super_over_career_bowling,
                "innings1_scorecard": getattr(self, "super_over_innings1_scorecard", None),
                # This round's own ball-by-ball history (micro-GSME momentum
                # input) and the pitch wear frozen from the main match.
                "ball_history": getattr(self, "super_over_ball_history", []),
                "pitch_wear": getattr(self, "super_over_pitch_wear", 0.0),
            },
        }

        so = snap["super_over"]
        if getattr(self, "super_over_batting_team", None) is not None:
            so["batting_side"] = _side(self.super_over_batting_team)
        if getattr(self, "super_over_batsmen", None):
            so["batsmen"] = [p["name"] for p in self.super_over_batsmen]
        if getattr(self, "super_over_bowler", None):
            so["bowler"] = self.super_over_bowler["name"]

        if phase == "innings_in_progress":
            so["in_progress"] = {
                "ball": self.super_over_ball,
                "striker": self.super_over_current_striker["name"],
                "non_striker": self.super_over_current_non_striker["name"],
                "next_batter_idx": self.super_over_next_batter_idx,
                "bowler_runs": self.super_over_bowler_runs,
                "bowler_wickets": self.super_over_bowler_wickets,
                "batsman_stats": self.super_over_batsman_stats,
            }

        return snap

    def restore_super_over_snapshot(self, snap):
        """Rebuild super-over + tied-main-match state onto a freshly constructed
        Match (built from the same match JSON). Inverse of
        serialize_super_over_snapshot. Player references are re-resolved by name
        against this instance's XIs so identity checks (``team is self.home_xi``)
        keep working. Raises ValueError on a snapshot it cannot safely restore —
        callers should surface that rather than silently starting the match over."""
        if not isinstance(snap, dict) or snap.get("v") != 1:
            raise ValueError("Unsupported super-over snapshot format")

        xi = {"home": self.home_xi, "away": self.away_xi}
        other = {"home": "away", "away": "home"}

        main = snap.get("main_match") or {}
        second_side = main.get("second_batting_side")
        if second_side not in xi:
            raise ValueError("Super-over snapshot missing second-innings side")
        self.batting_team = xi[second_side]
        self.bowling_team = xi[other[second_side]]
        self.innings = snap.get("innings", 4)
        self.score = main.get("score", 0)
        self.wickets = main.get("wickets", 0)
        if main.get("target") is not None:
            self.target = main["target"]
        if main.get("first_innings_score") is not None:
            self.first_innings_score = main["first_innings_score"]
        self.batsman_stats = main.get("batsman_stats") or {}
        self.bowler_stats = main.get("bowler_stats") or {}
        self.first_innings_batting_stats = main.get("first_innings_batting_stats") or {}
        self.first_innings_bowling_stats = main.get("first_innings_bowling_stats") or {}
        self.second_innings_batting_stats = main.get("second_innings_batting_stats") or {}
        self.second_innings_bowling_stats = main.get("second_innings_bowling_stats") or {}
        self.first_innings_partnerships = main.get("first_innings_partnerships") or []
        self.second_innings_partnerships = main.get("second_innings_partnerships") or []
        self.first_batting_team_name = main.get("first_batting_team_name", "")
        self.first_bowling_team_name = main.get("first_bowling_team_name", "")
        self.rain_affected = main.get("rain_affected", False)
        if main.get("original_scorecard") is not None:
            self.original_scorecard = main["original_scorecard"]
        if main.get("first_innings_scorecard") is not None:
            self.first_innings_scorecard = main["first_innings_scorecard"]
        self.commentary = main.get("commentary") or []
        self.commentary_replay_log = main.get("commentary_replay_log") or []

        so = snap.get("super_over") or {}
        phase = so.get("phase")
        if phase not in ("awaiting_innings1_selection", "awaiting_innings2_selection",
                         "innings_in_progress"):
            raise ValueError(f"Super-over snapshot has invalid phase: {phase!r}")
        self.super_over_phase = phase
        self.super_over_round = so.get("round", 0)
        self.super_over_innings = so.get("so_innings", 1)
        if so.get("scores"):
            self.super_over_scores = so["scores"]
        if so.get("wickets_by_side"):
            self.super_over_wickets = so["wickets_by_side"]
        self.super_over_first_batting = so.get("first_batting")
        if so.get("next_first_batting"):
            self._super_over_next_first_batting = so["next_first_batting"]
        self.super_over_history = so.get("history") or []
        self.super_over_team_boundaries = so.get("team_boundaries") or {"home": 0, "away": 0}
        self.super_over_career_batting = so.get("career_batting") or {"home": {}, "away": {}}
        self.super_over_career_bowling = so.get("career_bowling") or {"home": {}, "away": {}}
        self.super_over_ball_history = so.get("ball_history") or []
        self.super_over_pitch_wear = so.get("pitch_wear", 0.0)
        if so.get("innings1_scorecard") is not None:
            self.super_over_innings1_scorecard = so["innings1_scorecard"]

        bat_side = so.get("batting_side")
        if bat_side in xi:
            self.super_over_batting_team = xi[bat_side]
            self.super_over_bowling_team = xi[other[bat_side]]

        # Resolve player refs by name, preferring the side they belong to but
        # falling back to both XIs: in the awaiting_innings2_selection window
        # super_over_batsmen still belong to the innings-1 side while
        # batting_side already points at the innings-2 side (those refs are
        # replaced by the next start_super_over_innings2 call anyway).
        if bat_side in xi:
            bat_pool = xi[bat_side] + xi[other[bat_side]]
            bowl_pool = xi[other[bat_side]] + xi[bat_side]
        else:
            bat_pool = bowl_pool = self.home_xi + self.away_xi
        if so.get("batsmen"):
            self.super_over_batsmen = self._find_players_by_name(bat_pool, so["batsmen"])
        if so.get("bowler"):
            found = self._find_players_by_name(bowl_pool, [so["bowler"]])
            if found:
                self.super_over_bowler = found[0]

        if phase == "innings_in_progress":
            prog = so.get("in_progress") or {}
            by_name = {p["name"]: p for p in getattr(self, "super_over_batsmen", [])}
            striker = by_name.get(prog.get("striker"))
            non_striker = by_name.get(prog.get("non_striker"))
            if striker is None or non_striker is None or not getattr(self, "super_over_bowler", None):
                raise ValueError("Super-over snapshot references unknown players")
            self.super_over_ball = prog.get("ball", 0)
            self.super_over_next_batter_idx = prog.get("next_batter_idx", 2)
            self.super_over_bowler_runs = prog.get("bowler_runs", 0)
            self.super_over_bowler_wickets = prog.get("bowler_wickets", 0)
            self.super_over_batsman_stats = prog.get("batsman_stats") or {}
            self.super_over_current_striker = striker
            self.super_over_current_non_striker = non_striker

    # ========================================================================
    # First-Class (FC) match snapshot — resume support
    # ========================================================================
    # Same discipline as serialize_super_over_snapshot()/restore_super_over_
    # snapshot() above: a versioned JSON-safe dict, team identity captured as
    # "home"/"away" tags (never object references), players re-resolved by
    # name against the freshly-built XIs at restore time. Persisted by the
    # routes layer after every over completes (see routes/match_routes.py's
    # _persist_fc_snapshot) so a process restart or instance eviction
    # mid-match resumes instead of silently restarting the match from
    # fc_innings=1 — the same failure mode the super-over snapshot exists to
    # prevent for tied T20/ListA matches, now closed for FC too.

    def serialize_fc_snapshot(self):
        """JSON-safe snapshot of the live FC match state. Returns None when
        this isn't an FC match or no over has completed yet (nothing to
        snapshot before the first natural checkpoint)."""
        if not self.is_fc:
            return None

        def _side(team):
            return "home" if team is self.home_xi else "away"

        snap = {
            "v": 1,
            "fc_innings": self.fc_innings,
            "batting_side": _side(self.batting_team),
            "bowling_side": _side(self.bowling_team),
            "score": self.score,
            "wickets": self.wickets,
            "current_over": self.current_over,
            "current_ball": self.current_ball,
            "batter_idx": list(self.batter_idx),
            "remaining_batter_indices": sorted(self.remaining_batter_indices),
            "bowler_selected_for_over": self.bowler_selected_for_over,
            "striker": self.current_striker["name"] if self.current_striker else None,
            "non_striker": self.current_non_striker["name"] if self.current_non_striker else None,
            "bowler": self.current_bowler["name"] if self.current_bowler else None,
            "batsman_stats": self.batsman_stats,
            "bowler_stats": self.bowler_stats,
            "over_bowler_log": self.over_bowler_log,
            "bowler_manager": {
                "overs_this_innings": dict(self.bowler_manager._overs_this_innings),
                "last_bowler": self.bowler_manager._last_bowler,
                "prev_over_runs": dict(self.bowler_manager._prev_over_runs),
                # FC spell state. Absent for T20/ListA's BowlerManager, and
                # absent from snapshots taken before spells existed — the
                # restore side treats both as "everyone fresh".
                "spell_overs": dict(getattr(self.bowler_manager, "_spell_overs", {}) or {}),
                "rest_overs": dict(getattr(self.bowler_manager, "_rest_overs", {}) or {}),
                "fatigue": dict(getattr(self.bowler_manager, "_fatigue", {}) or {}),
            },
            "fc_day": self.fc_day,
            "fc_day_overs_bowled_today": self.fc_day_overs_bowled_today,
            "fc_sessions_taken_today": self.fc_sessions_taken_today,
            "fc_session_start": self.fc_session_start,
            "fc_day_over_rate_adjust": self.fc_day_over_rate_adjust,
            "fc_nightwatchman_used": self.fc_nightwatchman_used,
            "fc_ball_overs_bowled": self.fc_ball_overs_bowled,
            "fc_innings_declared": self.fc_innings_declared,
            "fc_innings_time_budget_overs": self.fc_innings_time_budget_overs,
            "follow_on_enforced": self.follow_on_enforced,
            "fc_innings_totals": self.fc_innings_totals,
            "fc_innings_stats": self.fc_innings_stats,
            "fc_innings_partnerships": self.fc_innings_partnerships,
            "target": getattr(self, "target", None),
            "first_innings_score": getattr(self, "first_innings_score", None),
            "match_balls_bowled": self.match_balls_bowled,
            "innings_balls_bowled": self.innings_balls_bowled,
            "current_partnership_balls": self.current_partnership_balls,
            "current_partnership_runs": self.current_partnership_runs,
            "current_partnership_start_over": self.current_partnership_start_over,
            "current_partnership_contributions": self.current_partnership_contributions,
            "batter_streaks": self.batter_streaks,
            "recent_wickets_tracker": getattr(self, "recent_wickets_tracker", []),
            "recent_wickets_count": getattr(self, "recent_wickets_count", 0),
            "commentary": self.commentary,
            "commentary_replay_log": getattr(self, "commentary_replay_log", []),
            "match_status": self.match_status,
            "margin_type": self.margin_type,
            "margin_value": self.margin_value,
            "winner_is_home": self.winner_is_home,
            "result": self.result,
        }

        first_batting_xi = getattr(self, "_fc_first_batting_xi", None)
        if first_batting_xi is not None:
            snap["first_batting_side"] = _side(first_batting_xi)

        return snap

    def restore_fc_snapshot(self, snap):
        """Rebuild live FC match state onto a freshly constructed Match
        (built from the same match_data). Inverse of serialize_fc_snapshot.
        Raises ValueError on a snapshot it cannot safely restore — callers
        must surface that rather than silently falling through to a fresh
        (fc_innings=1) instance, which would silently restart the match."""
        if not isinstance(snap, dict) or snap.get("v") != 1:
            raise ValueError("Unsupported FC snapshot format")

        xi = {"home": self.home_xi, "away": self.away_xi}
        other = {"home": "away", "away": "home"}

        batting_side = snap.get("batting_side")
        bowling_side = snap.get("bowling_side")
        if batting_side not in xi or bowling_side not in xi:
            raise ValueError("FC snapshot missing batting/bowling side")
        self.batting_team = xi[batting_side]
        self.bowling_team = xi[bowling_side]

        self.fc_innings = snap.get("fc_innings", 1)
        self.score = snap.get("score", 0)
        self.wickets = snap.get("wickets", 0)
        self.current_over = snap.get("current_over", 0)
        self.current_ball = snap.get("current_ball", 0)
        self.batter_idx = list(snap.get("batter_idx", [0, 1]))
        self.remaining_batter_indices = set(snap.get("remaining_batter_indices", []))
        self.bowler_selected_for_over = snap.get("bowler_selected_for_over", -1)

        self.batsman_stats = snap.get("batsman_stats") or {}
        self.bowler_stats = snap.get("bowler_stats") or {}
        # Same int-key-vs-JSON-string-key concern as fc_innings_totals above.
        self.over_bowler_log = {
            int(k): v for k, v in (snap.get("over_bowler_log") or {}).items()
        }

        pool = self.batting_team + self.bowling_team
        striker = self._find_players_by_name(pool, [snap["striker"]]) if snap.get("striker") else []
        non_striker = self._find_players_by_name(pool, [snap["non_striker"]]) if snap.get("non_striker") else []
        bowler = self._find_players_by_name(pool, [snap["bowler"]]) if snap.get("bowler") else []
        if not striker or not non_striker:
            raise ValueError("FC snapshot references unknown striker/non-striker")
        self.current_striker = striker[0]
        self.current_non_striker = non_striker[0]
        self.current_bowler = bowler[0] if bowler else None

        bm = snap.get("bowler_manager") or {}
        self.bowler_manager._overs_this_innings = dict(bm.get("overs_this_innings") or {})
        self.bowler_manager._last_bowler = bm.get("last_bowler")
        self.bowler_manager._prev_over_runs = dict(bm.get("prev_over_runs") or {})
        if hasattr(self.bowler_manager, "_spell_overs"):
            self.bowler_manager._spell_overs = dict(bm.get("spell_overs") or {})
            self.bowler_manager._rest_overs = dict(bm.get("rest_overs") or {})
            self.bowler_manager._fatigue = {
                k: float(v) for k, v in (bm.get("fatigue") or {}).items()
            }
        self.bowler_history = self.bowler_manager._overs_this_innings

        self.fc_day = snap.get("fc_day", 1)
        self.fc_day_overs_bowled_today = snap.get("fc_day_overs_bowled_today", 0)
        self.fc_sessions_taken_today = snap.get("fc_sessions_taken_today", 0)
        self.fc_day_over_rate_adjust = snap.get("fc_day_over_rate_adjust")
        self.fc_nightwatchman_used = snap.get("fc_nightwatchman_used", False)
        self.fc_session_start = snap.get("fc_session_start") or {
            "score": self.score, "wickets": self.wickets,
            "day_overs": self.fc_day_overs_bowled_today,
            "fc_innings": self.fc_innings,
        }
        self.fc_ball_overs_bowled = snap.get("fc_ball_overs_bowled", 0)
        self.fc_innings_declared = snap.get("fc_innings_declared", False)
        # Must be the exact frozen value captured when the CURRENT innings
        # started, not recomputed from post-restore state — fc_day/
        # fc_day_overs_bowled_today above already reflect "now", which is
        # generally well past this innings' actual start. Falls back to a
        # fresh computation only for snapshots taken before this field
        # existed (pre-migration in-flight matches); the result is an
        # approximation for those specific resumes, not the true frozen value.
        self.fc_innings_time_budget_overs = snap.get(
            "fc_innings_time_budget_overs",
            fc_declaration.compute_innings_time_budget_overs(self._fc_overs_remaining_in_match()),
        )
        self.follow_on_enforced = snap.get("follow_on_enforced")
        # fc_innings_totals is keyed by innings number (int) everywhere it's
        # read (e.g. self.fc_innings_totals.get(1, {})) — a JSON round-trip
        # turns those into string keys, which would silently break every
        # such lookup (falling back to {} instead of raising), corrupting
        # follow-on/declaration decisions rather than crashing visibly.
        self.fc_innings_totals = {
            int(k): v for k, v in (snap.get("fc_innings_totals") or {}).items()
        }
        self.fc_innings_stats = snap.get("fc_innings_stats") or []
        # Same int-key-vs-JSON-string-key concern as fc_innings_totals above.
        self.fc_innings_partnerships = {
            int(k): v for k, v in (snap.get("fc_innings_partnerships") or {}).items()
        }
        if snap.get("target") is not None:
            self.target = snap["target"]
        if snap.get("first_innings_score") is not None:
            self.first_innings_score = snap["first_innings_score"]
        self.match_balls_bowled = snap.get("match_balls_bowled", 0)
        self.innings_balls_bowled = snap.get("innings_balls_bowled", 0)

        self.current_partnership_balls = snap.get("current_partnership_balls", 0)
        self.current_partnership_runs = snap.get("current_partnership_runs", 0)
        self.current_partnership_start_over = snap.get("current_partnership_start_over", 0.0)
        self.current_partnership_contributions = snap.get("current_partnership_contributions") or {
            'batsman1': {'name': '', 'runs': 0, 'balls': 0},
            'batsman2': {'name': '', 'runs': 0, 'balls': 0},
        }
        self.batter_streaks = snap.get("batter_streaks") or {}
        self.recent_wickets_tracker = snap.get("recent_wickets_tracker") or []
        self.recent_wickets_count = snap.get("recent_wickets_count", 0)

        self.commentary = snap.get("commentary") or []
        self.commentary_replay_log = snap.get("commentary_replay_log") or []

        self.match_status = snap.get("match_status")
        self.margin_type = snap.get("margin_type")
        self.margin_value = snap.get("margin_value")
        self.winner_is_home = snap.get("winner_is_home")
        self.result = snap.get("result", "")

        first_batting_side = snap.get("first_batting_side")
        if first_batting_side in xi:
            self._fc_first_batting_xi = xi[first_batting_side]
            self._fc_first_bowling_xi = xi[other[first_batting_side]]
