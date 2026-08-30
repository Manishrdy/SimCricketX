
import json
import random
import logging
import os

from utils.exception_tracker import log_exception

logger = logging.getLogger(__name__)

_SPIN_BOWLING_TYPES = {"Off spin", "Leg spin", "Finger spin", "Wrist spin"}


class CommentaryEngine:
    def __init__(self, data_path=None):
        if data_path is None:
            # Default to data/commentary_pack.json relative to project root
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_path = os.path.join(base_dir, "data", "commentary_pack.json")

        self.data_path = data_path
        self.data = self._load_data()
        self.events = self.data.get("events", {})
        self.narratives = self.data.get("narratives", {})
        # One CommentaryEngine is built per Match, so this is per-match state:
        # which "announce once" narratives have already been used, and in
        # what context. Without it a standing condition like the last hour or
        # a turning pitch would be remarked on at the start of every over.
        self._announced = set()

    def _load_data(self):
        try:
            with open(self.data_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            log_exception(e)
            logger.error(f"Failed to load commentary pack from {self.data_path}: {e}")
            return {"events": {}, "narratives": {}}

    def get_commentary(self, ball_context, match_state):
        """Generate commentary string."""
        # 1. Determine Event Key
        event_key = self._map_context_to_key(ball_context)

        # 2. Get Micro Commentary (Shot description) — tag-filtered
        micro_text = self._select_template(event_key, ball_context)

        # 3. Get Macro Commentary (Narrative)
        macro_text = self._check_narratives(ball_context, match_state)

        # 4. Combine
        if macro_text:
            return f"{micro_text} {macro_text}"
        return micro_text

    def _map_context_to_key(self, context):
        """Map ball context to a JSON key."""
        outcome_type = context.get("type", "").lower()
        runs = context.get("runs", 0)
        is_extra = context.get("is_extra", False)

        if outcome_type == "wicket":
            wkt_type = context.get("wicket_type", "caught").lower()
            return f"wicket_{wkt_type}"

        if is_extra:
            extra_type = context.get("extra_type", "").lower()
            if "wide" in extra_type:
                return "wide"
            if "no" in extra_type:
                return "noball"
            return "dot"

        if runs == 4:
            return "boundary_four"
        if runs == 6:
            return "boundary_six"
        if runs == 0:
            return "dot"
        if runs == 1:
            return "single"
        if runs == 2:
            return "double"
        if runs == 3:
            return "three"

        return "dot"

    # ------------------------------------------------------------------ #
    #  Tag-based template selection
    # ------------------------------------------------------------------ #

    def _get_bowling_tags(self, context):
        """Derive filter tags from the ball context (bowler type etc.)."""
        tags = set()
        # We don't have the full bowler dict here, but match.py enriches
        # outcome with 'bowling_type' before calling us. Players imported
        # without a bowling style carry None (nullable DB column), and
        # .get()'s default doesn't apply when the key exists with None —
        # guard with `or ""` so commentary never crashes the ball loop.
        bowling_type = (context.get("bowling_type") or "").lower()
        if bowling_type in ("fast", "medium", "fast-medium", "medium-fast"):
            tags.add("pace")
        elif bowling_type in ("spin", "off-spin", "leg-spin", "left-arm spin",
                              "off spin", "leg spin", "left arm spin"):
            tags.add("spin")
        return tags

    def _select_template(self, key, context):
        """Select a template for the given key, preferring tag-matched templates."""
        templates = self.events.get(key, [])
        if not templates:
            # Fallback chain
            if "wicket" in key:
                templates = self.events.get("wicket_caught", [])
            elif "boundary" in key:
                templates = self.events.get("boundary_four", [])
            else:
                return context.get("description", "Play continues.")

        if not templates:
            return context.get("description", "Play continues.")

        # --- Tag filtering ---
        bowling_tags = self._get_bowling_tags(context)

        if bowling_tags:
            # Prefer templates whose tags overlap with the bowling context
            matched = [t for t in templates if bowling_tags & set(t.get("tags", []))]
            if matched:
                templates = matched
            # else: no matches, fall through to all templates (better than nothing)

        template_obj = random.choice(templates)
        text = template_obj.get("text", "")

        return text.format(
            batter=context.get("batter", "The batter"),
            bowler=context.get("bowler", "The bowler"),
            runs=context.get("runs", 0),
            team=context.get("batting_team", "The batting side"),
            fielding_team=context.get("bowling_team", "The fielding side"),
        )

    # ------------------------------------------------------------------ #
    #  Narrative triggers (macro commentary)
    # ------------------------------------------------------------------ #

    def _check_narratives(self, context, state):
        """Check for narrative triggers — all 10 categories."""
        # Super over: every narrative category here is a main-innings concept
        # (powerplay, death overs, milestones, maidens). A super over's first
        # ball sits at over 0 / ball 0 and would wrongly announce "Powerplay",
        # so skip macro narratives entirely.
        if state.get("is_super_over"):
            return None
        triggers = []
        batter = context.get("batter", "The batter")
        bowler = context.get("bowler", "The bowler")
        batting_team = context.get("batting_team", "The batting side")
        bowling_team = context.get("bowling_team", "The fielding side")

        current_over = state.get("current_over", 0)
        innings = state.get("innings", 1)
        runs = context.get("runs", 0)
        batter_runs_before = state.get("batter_runs", 0)
        batter_runs_after = batter_runs_before + (runs if not context.get("batter_out") else 0)
        partnership_before = state.get("partnership_runs", 0)
        partnership_after = partnership_before + runs

        # --- 1. Collapse: 3+ wickets recently ---
        recent_wickets = state.get("recent_wickets_match", 0)
        if context.get("type") == "wicket" and recent_wickets >= 3:
            triggers.extend(self._format_narratives("collapse_wicket",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 2. Milestone 50: threshold crossing (not exact equality) ---
        if batter_runs_before < 50 <= batter_runs_after:
            triggers.extend(self._format_narratives("milestone_50",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 3. Milestone 100: threshold crossing ---
        if batter_runs_before < 100 <= batter_runs_after:
            triggers.extend(self._format_narratives("milestone_100",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 4. Partnership 50: threshold crossing ---
        if partnership_before < 50 <= partnership_after:
            triggers.extend(self._format_narratives("partnership_50",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 5. Maiden over (detected at ball 6 of an over with 0 runs) ---
        over_runs = state.get("current_over_runs", -1)
        current_ball = state.get("current_ball", 0)
        is_maiden = state.get("is_maiden_over", False)
        if is_maiden and not state.get("is_fc"):
            triggers.extend(self._format_narratives("maiden_over",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 6. Expensive over (15+ runs in the over, at over end) ---
        # Not for FC: 15 in an over is a once-a-match freak there, and the
        # threshold is a limited-overs one anyway.
        if not state.get("is_fc") and over_runs >= 15 and current_ball >= 5:
            triggers.extend(self._format_narratives("expensive_over",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 7. Big over (12-14 runs in the over, at over end) ---
        if not state.get("is_fc") and 12 <= over_runs < 15 and current_ball >= 5:
            triggers.extend(self._format_narratives("big_over",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 8. Last over drama (format-aware last over, 2nd innings, close match) ---
        # Uses _fmt_last_over from match state (49 for ListA, 19 for T20).
        _last_over = state.get("_fmt_last_over", 19)
        if innings == 2 and current_over == _last_over and current_ball == 0:
            runs_needed = state.get("runs_needed", 999)
            if 1 <= runs_needed <= 20:
                triggers.extend(self._format_narratives("last_over_drama",
                                                         batter=batter, bowler=bowler,
                                                         team=batting_team,
                                                         fielding_team=bowling_team))

        # --- 9. Death overs entry (format-aware death start, first ball) ---
        # Uses _fmt_death_start from match state (40 for ListA, 16 for T20).
        # FC has no death-overs concept and no _fmt_death_start is set for
        # it — without this guard the state.get(...) default (16) would
        # wrongly fire this every innings at FC's over 16.
        _death_start = state.get("_fmt_death_start", 16)
        if not state.get("is_fc") and current_over == _death_start and current_ball == 0:
            triggers.extend(self._format_narratives("death_overs",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 10. Powerplay (over 0, first ball only — announce once) ---
        # FC has no fielding-restriction powerplay at all.
        if not state.get("is_fc") and current_over == 0 and current_ball == 0:
            triggers.extend(self._format_narratives("powerplay",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 11. High pressure dot (2nd innings, RRR >= 10, dot ball) ---
        if (innings == 2 and runs == 0 and not context.get("is_extra")
                and state.get("required_run_rate", 0) >= 10
                and current_over >= state.get("_fmt_death_start", 16) - 1):
            triggers.extend(self._format_narratives("high_pressure_dot",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- First-class narratives -------------------------------------
        # Everything above is shaped by limited-overs cricket: powerplays,
        # death overs, run chases, a maiden being a rarity. A first-class
        # match is told through different things entirely — the new ball,
        # the lead, the pitch breaking up, the close of play.
        if state.get("is_fc"):
            triggers.extend(self._fc_narratives(context, state, batter=batter,
                                                bowler=bowler,
                                                team=batting_team,
                                                fielding_team=bowling_team))

        if triggers:
            return random.choice(triggers)
        return None

    # First-class-specific narrative triggers.
    _FC_MAIDEN_SEQUENCE = 3        # maidens in a row worth remarking on
    _FC_WEARING_PITCH = 0.55       # pitch_wear past which the surface talks
    _FC_BIG_HUNDRED = 150

    def _announce_once(self, key):
        """True the first time this key is seen, False afterwards."""
        if key in self._announced:
            return False
        self._announced.add(key)
        return True

    def _fc_narratives(self, context, state, **who):
        out = []
        day = state.get("fc_day", 0)
        inns = state.get("fc_innings", 0)
        runs = context.get("runs", 0)
        first_ball_of_over = state.get("current_ball", 0) == 0

        # The second new ball: ball age back to zero, but not the start of
        # an innings (that is simply the innings beginning).
        if (first_ball_of_over
                and state.get("fc_ball_overs_bowled", 0) == 0
                and state.get("current_over", 0) > 0):
            out.extend(self._format_narratives("fc_new_ball", **who))

        # Going past the opposition's total.
        lead_before = state.get("fc_lead_before")
        if lead_before is not None and lead_before < 0 <= lead_before + runs:
            out.extend(self._format_narratives("fc_lead_taken", **who))

        # Saving the follow-on.
        mark = state.get("fc_follow_on_mark")
        if mark is not None:
            score_before = state.get("score", 0) - runs
            if score_before < mark <= score_before + runs:
                out.extend(self._format_narratives("fc_follow_on_saved", **who))

        # A run of maidens — one on its own is unremarkable in this format.
        streak = state.get("fc_consecutive_maidens", 0)
        if (state.get("is_maiden_over") and streak >= self._FC_MAIDEN_SEQUENCE
                and streak % self._FC_MAIDEN_SEQUENCE == 0):
            out.extend(self._format_narratives("fc_maiden_sequence", **who))

        # The pitch starting to talk, with a spinner operating.
        if (first_ball_of_over
                and state.get("pitch_wear", 0.0) >= self._FC_WEARING_PITCH
                and (context.get("bowling_type") or "") in _SPIN_BOWLING_TYPES
                and self._announce_once(("wearing", day))):
            out.extend(self._format_narratives("fc_pitch_wearing", **who))

        # The closing overs of the day — said once as the light goes, not
        # at the top of every remaining over.
        if (first_ball_of_over and state.get("last_hour")
                and self._announce_once(("last_hour", day, inns))):
            out.extend(self._format_narratives("fc_last_hour", **who))

        # The nightwatchman walking out.
        if (state.get("fc_is_nightwatchman") and state.get("batter_runs", 0) == 0
                and self._announce_once(("nightwatchman", day, inns))):
            out.extend(self._format_narratives("fc_nightwatchman", **who))

        # A big hundred, and a century stand.
        before = state.get("batter_runs", 0)
        after = before + (0 if context.get("batter_out") else runs)
        if before < self._FC_BIG_HUNDRED <= after:
            out.extend(self._format_narratives("fc_milestone_150", **who))
        p_before = state.get("partnership_runs", 0)
        if p_before < 100 <= p_before + runs:
            out.extend(self._format_narratives("fc_partnership_100", **who))

        return out

    def _format_narratives(self, key, **kwargs):
        """Get narrative templates and format them with context."""
        raw = self.narratives.get(key, [])
        if not raw:
            return []
        formatted = []
        for text in raw:
            try:
                formatted.append(text.format(**kwargs))
            except (KeyError, IndexError):
                log_exception(source="backend")
                formatted.append(text)
        return formatted
