import random
import logging
from typing import Optional

from engine.format_config import get_format
from engine.game_state_engine import SUPER_OVER_NEUTRAL_RPO

logger = logging.getLogger(__name__)

# Drama floor for a Super Over pressure score (0-100 scale) — a Super Over
# ball starts meaningfully more tense than an average mid-innings ball even
# before wickets/rate are factored in; see calculate_super_over_pressure().
SUPER_OVER_PRESSURE_FLOOR = 45.0

class PressureEngine:
    def __init__(self, format_config=None):
        # Resolve format — defaults to T20 for backward compatibility
        self.fmt = format_config if format_config is not None else get_format("T20")

        # Build expected run rates from FormatConfig so both T20 and ListA
        # phase keys map to the three canonical pressure slots.
        _pp_key = (self.fmt.powerplay_phases[0].name
                   if self.fmt.powerplay_phases else "Powerplay")
        self.expected_rr_first_innings = {
            'powerplay': self.fmt.expected_rr.get(_pp_key, 7.5),
            'middle':    self.fmt.expected_rr.get("Middle", 8.0),
            'death':     self.fmt.expected_rr.get("Death", 10.5),
        }

        # Recent events for momentum (last 3 balls)
        self.recent_events = []

    # ── Short-format (T10) helpers ──────────────────────────────────────────
    # Every raw run-rate threshold in this class (12 / 14 / 16 / 18 / 20 RPO)
    # was written against a 20-over game where par is ~8.5 RPO. A T10 chase
    # asks 9.5-15.5 from the first ball depending on the surface, so those
    # constants fire permanently rather than in a crisis. Short formats read
    # them relative to the pitch's own neutral rate instead.
    @property
    def _is_short(self):
        return bool(getattr(self.fmt, "strict_short_bowling", False))

    def _rate_baseline(self, pitch):
        """Neutral RPO for this pitch in this format (T10 Hard ~12.5)."""
        return (getattr(self.fmt, "rrr_baseline", None) or {}).get(pitch, 12.5)

    def calculate_unified_risk_factor(self, match_state):
        """Calculate unified risk factor based on death overs and required rate"""
        if match_state['innings'] == 1:
            return self._calculate_first_innings_risk(match_state)

        current_over = match_state.get('current_over', 0)
        required_rr = match_state.get('required_run_rate', 0)
        overs_remaining = match_state.get('overs_remaining', 0)
        
        if self.fmt.strict_short_bowling:
            baseline = self._rate_baseline(match_state.get('pitch', 'Hard'))
            urgency = max(0.0, required_rr / baseline - 1.0)
            # A normal T10 asking rate must not trigger T20's 12-RPO crisis.
            phase_risk = .20 if self.fmt.is_death(current_over) else 0.0
            return 1.0 + phase_risk + min(.8, urgency * .6)

        risk_factor = 1.0
        risk_components = []
        
        # 1. Death overs base risk
        if self.fmt.is_death(current_over):
            death_risk = 0.3 + (current_over - self.fmt.death_phase.start) * 0.1
            risk_factor += death_risk
            risk_components.append(f"Death overs: +{death_risk:.1f}")

        # 2. High required rate risk (throughout 2nd innings)
        if required_rr > 12:
            rr_risk = min((required_rr - 12) * 0.15, 0.8)  # Max +0.8 for very high RR
            risk_factor += rr_risk
            risk_components.append(f"High RRR ({required_rr:.1f}): +{rr_risk:.1f}")

        # 3. Final overs desperation (last 2 overs of any format)
        if current_over >= self.fmt.overs - 2 and overs_remaining <= 2:
            final_desperation = 0.4
            risk_factor += final_desperation
            risk_components.append(f"Final desperation: +{final_desperation:.1f}")
        
        if risk_factor > 1.1:  # Only log if significant risk
            logger.info(f"UNIFIED RISK FACTOR: {risk_factor:.2f}")
            for component in risk_components:
                logger.info(f"   {component}")
        
        return risk_factor


    def _calculate_first_innings_risk(self, match_state):
        """First innings acceleration — teams push harder in death/pre-death overs."""
        current_over = match_state.get('current_over', 0)
        wickets      = match_state.get('wickets', 0)
        score        = match_state.get('score', 0)

        _death_start = self.fmt.death_phase.start   # T20: 16, ListA: 40
        _pre_death   = _death_start - 1             # T20: 15, ListA: 39

        # ListA: gradual slog-overs window before death (overs 35–39)
        if self.fmt.name == "ListA" and int(0.7 * (self.fmt.scheduled_overs or 50)) <= current_over < _death_start:
            slog_boost = 0.05 + (current_over - int(0.7 * (self.fmt.scheduled_overs or 50))) * 0.02  # 0.05 → 0.13
            return min(1.0 + slog_boost, 1.25)

        # Only accelerate from the over before death onwards.
        #
        # Short formats start at the middle overs instead. Over 7 of 10 is far
        # too late for a side batting first to answer being behind its own par,
        # and the chase has had an equivalent urgency term from the first ball
        # (calculate_unified_risk_factor). That one-sided timing is why the
        # chase could match the first innings' run rate through overs 4-7 while
        # paying more in wickets for it: only one of the two innings had a way
        # to trade risk for rate there.
        _accel_from = self.fmt.middle_phase.start if self._is_short else _pre_death
        if current_over < _accel_from:
            return 1.0

        risk_factor     = 1.0
        wickets_in_hand = 10 - wickets

        # Death/pre-death base acceleration
        death_boost  = max(0.0, 0.1 + (current_over - _pre_death) * 0.05)
        risk_factor += death_boost

        # Wickets-in-hand multiplier
        if wickets_in_hand >= 7:
            risk_factor += 0.15   # Plenty of batting — go big
        elif wickets_in_hand >= 5:
            risk_factor += 0.08   # Comfortable — can afford risks
        elif wickets_in_hand <= 2:
            risk_factor -= 0.15   # Protect wickets, reduce aggression

        # Score-based urgency: use format par scores instead of T20 lookup.
        # par_scores is the neutral (Hard) curve, so short formats scale it by
        # the surface — otherwise a Green T10 side on 45 after 5 is judged
        # against a Hard par of 58 and told to panic on a pitch where 45 is fine.
        par = self.fmt.par_scores.get(
            current_over,
            self.fmt.par_scores.get(self.fmt.overs, 0)
        )
        if self._is_short:
            par *= (getattr(self.fmt, "pitch_par_factors", None) or {}).get(
                match_state.get('pitch', 'Hard'), 1.0) or 1.0
        if par > 0:
            if score < par - 15:
                risk_factor += 0.15   # Well behind par — desperate
            elif score < par:
                risk_factor += 0.08   # Slightly behind — need to push

        # ListA: dot-ball cluster pressure (3 consecutive dots → forced aggression)
        if self.fmt.name == "ListA":
            recent_dots = sum(
                1 for e in self.recent_events[-3:]
                if e.get('runs') == 0 and not e.get('extra')
            )
            if recent_dots >= 3:
                risk_factor += 0.10   # Break-free pressure after dot cluster

        return max(1.0, min(risk_factor, 1.8))  # Cap at 1.8

    def calculate_defensive_factor(self, match_state):
        """Calculate defensive factor when team is protecting wickets in death overs"""
        if match_state['innings'] != 2:
            return None

        # Short formats have no defensive mode at all. This block is an
        # if/else against get_risk_based_effects() in match.py, so whenever it
        # fires it REPLACES aggression with blocking (boundaries x0.70, dots
        # +0.30, singles x1.8) — and its gate, death overs with 6 down, is a
        # routine T10 position rather than a lost cause. There is no draw to
        # bat out in a 10-over game: a side 6 down in over 8 still has to
        # score. The cases this legitimately covered are already handled
        # elsewhere and stay — an easy chase by the required-aggression bands
        # in game_state_engine (< 0.67 / < 0.89), and a genuinely exposed tail
        # by that module's resource-conservatism section.
        if self._is_short:
            return None

        current_over = match_state.get('current_over', 0)
        wickets_fallen = match_state.get('wickets', 0)
        overs_remaining = match_state.get('overs_remaining', 0)
        
        # Defensive mode only in death overs with many wickets down
        if self.fmt.is_death(current_over) and wickets_fallen >= 6:
            
            # More wickets fallen = more defensive
            if wickets_fallen >= 8:
                defensive_level = 1.0  # Maximum defense
                mode = 'SURVIVAL_MODE'
            elif wickets_fallen >= 7:
                defensive_level = 0.8
                mode = 'DAMAGE_CONTROL'
            else:  # 6 wickets
                defensive_level = 0.6
                mode = 'CAUTIOUS_CRICKET'
            
            # Less time remaining = slightly more defensive
            if overs_remaining <= 2:
                defensive_level += 0.2
            
            defensive_level = min(defensive_level, 1.0)  # Cap at 1.0
            
            effects = {
                'defensive_active': True,
                'defensive_level': defensive_level,
                'boundary_reduction': 0.1 + (defensive_level * 0.2),  # 🔧 RELAXED: Max 30% reduction (was 80%)
                'wicket_reduction': 0.1 + (defensive_level * 0.3),    # 🔧 RELAXED: Max 40% reduction (was 80%)
                'dot_increase': defensive_level * 0.3,                 # More defensive dots
                'single_boost': 1.0 + (defensive_level * 0.8),        # 80% more singles
                'mode': mode
            }

            logger.info(f"{mode}: Defensive level {defensive_level:.1f} - Protecting wickets!")
            return effects
        
        return None
    
    def should_trigger_wicket_cluster(self, match_state, recent_wickets=0):
        """Check if conditions are right for rapid wicket fall"""
        current_over = match_state.get('current_over', 0)
        wickets_fallen = match_state.get('wickets', 0)

        # First innings collapse psychology
        if match_state['innings'] == 1:
            # Collapse triggers when already losing wickets in bunches
            if wickets_fallen >= 4 and recent_wickets >= 2:
                cluster_chance = 0.08
                if wickets_fallen >= 6:
                    cluster_chance = 0.12
                if wickets_fallen >= 8:
                    cluster_chance = 0.15
                # Dampen to prevent unrealistic cascades
                if recent_wickets >= 3:
                    cluster_chance *= 0.4
                return random.random() < cluster_chance
            return False

        required_rr = match_state.get('required_run_rate', 0)

        # Second innings: death overs with extreme required rate.
        # 14 RPO is a crisis in a T20 chase and a par asking rate in a T10 one,
        # so short formats scale the gate to the surface (T10 Hard: 20.0) and
        # halve the per-ball chance — 60 balls cannot absorb a T20 cluster rate.
        _short = self._is_short
        if _short:
            _gate = self._rate_baseline(match_state.get('pitch', 'Hard')) * 1.6
            _span = _gate * 0.43          # same gate-relative width as 14 -> 20
        else:
            _gate, _span = 14, 6

        if self.fmt.is_death(current_over) and required_rr >= _gate:
            
            # Higher chance if already under pressure
            if wickets_fallen >= 5:
                cluster_chance = 0.15  # 15% chance per ball
            elif wickets_fallen >= 3:
                cluster_chance = 0.12  # 12% chance per ball
            else:
                cluster_chance = 0.08  # 8% chance per ball
            
            # Increase chance based on how impossible the chase is
            impossibility_factor = min((required_rr - _gate) / _span, 1.0)  # 0-1 scale
            cluster_chance += impossibility_factor * 0.1

            if _short:
                cluster_chance *= 0.5
            
            # Reduce chance if wickets already fell recently (avoid unrealistic collapses)
            if recent_wickets >= 2:
                cluster_chance *= 0.3  # Much lower chance if 2+ wickets just fell
            elif recent_wickets >= 1:
                cluster_chance *= 0.6  # Lower chance if 1 wicket just fell
            
            return random.random() < cluster_chance
        
        return False

    def get_risk_based_effects(self, match_state):
        """Get risk-based effects — boom-or-bust for both innings"""
        risk_factor = self.calculate_unified_risk_factor(match_state)

        if risk_factor <= 1.1:
            return None

        risk_multiplier = risk_factor - 1.0
        innings = match_state.get('innings', 1)

        # Short formats (T10): ONE shape for both innings, and a boundary
        # coefficient above the wicket one so risk actually buys runs.
        #
        # The T20 model below hands the chase the same boundary payoff as the
        # first innings (1.2x, or 2.0x for T20 itself) but 1.5x the wicket
        # cost, plus a dot bonus and double the strike-rotation penalty. Over
        # 60 balls that made second-innings aggression strictly worse than
        # first-innings aggression: measured T10 chases lost 27-88% more
        # wickets per ball than the side batting first while scoring no
        # faster. Equal coefficients in both innings are what keep the two
        # halves of a T10 match reading alike.
        if self._is_short:
            effects = {
                'risk_active': True,
                'risk_factor': risk_factor,
                'boundary_boost': 1.0 + (risk_multiplier * 1.6),
                'wicket_boost': 1.0 + (risk_multiplier * 1.0),
                'dot_increase': 0,
                'strike_rotation_penalty': min(risk_multiplier * 0.2, 0.3),
                'single_floor': 0.08,
                'mode': 'FIRST_INNINGS_PUSH' if innings == 1 else 'SHORT_CHASE_PUSH',
            }
            if innings == 2:
                # No extra desperation ladder here. The T20 path below multiplies
                # wickets by a further 1.5-2.5x once RRR passes 16/18/20, and
                # over 60 balls that landed almost entirely in the final over:
                # measured, a T10 chase was losing 88% more wickets than the
                # first innings in over 10 alone, which is most of what was
                # left of the parity gap. game_state_engine's required-
                # aggression bands already reach Six x1.90 / Wicket x1.55 at
                # the same required rate on the same delivery, so this layer
                # was only ever counting the same desperation a second time.
                # It is named here rather than deleted because the mode label
                # still reads correctly in commentary and logs.
                _rrr = match_state.get('required_run_rate', 0)
                _base = self._rate_baseline(match_state.get('pitch', 'Hard'))
                if match_state.get('current_over', 0) >= self.fmt.death_phase.start:
                    if _rrr >= _base * 2.00:
                        effects['mode'] = 'ABSOLUTE_CHAOS'
                    elif _rrr >= _base * 1.75:
                        effects['mode'] = 'RECKLESS_HITTING'
                    elif _rrr >= _base * 1.50:
                        effects['mode'] = 'DESPERATE_SWINGING'
            logger.info(
                f"{effects['mode']}: risk={risk_factor:.2f}, "
                f"boundaries={effects['boundary_boost']:.2f}x, "
                f"wickets={effects['wicket_boost']:.2f}x"
            )
            return effects

        # First innings: moderate acceleration (setting a total, not chasing)
        if innings == 1:
            effects = {
                'risk_active': True,
                'risk_factor': risk_factor,
                'boundary_boost': 1.0 + (risk_multiplier * 1.2),
                'wicket_boost': 1.0 + (risk_multiplier * 1.0),
                'dot_increase': 0,
                'strike_rotation_penalty': min(risk_multiplier * 0.2, 0.3),
                'single_floor': 0.08,
                'mode': 'FIRST_INNINGS_PUSH'
            }
            logger.info(f"FIRST INNINGS PUSH: risk={risk_factor:.2f}, boundaries={effects['boundary_boost']:.2f}x, wickets={effects['wicket_boost']:.2f}x")
            return effects

        required_rr = match_state.get('required_run_rate', 0)
        current_over = match_state.get('current_over', 0)

        # Second innings: full boom-or-bust
        wicket_multiplier = 1.0 + (risk_multiplier * 1.5)
        
        # Extreme scaling only for impossible chases (RRR 16+) AND only once
        # the format's death phase has begun.  Using self.fmt.death_phase.start
        # instead of a hardcoded 16 ensures ListA death (over 40) is used rather
        # than T20 death (over 16) — previously this fired 24 overs too early.
        if current_over >= self.fmt.death_phase.start and required_rr > 16:
            if required_rr >= 20:
                extreme_boost = 2.5  # Only for truly impossible
                chaos_level = "ABSOLUTE_CHAOS"
            elif required_rr >= 18:
                extreme_boost = 2.0
                chaos_level = "RECKLESS_HITTING"
            else:  # 16-18 RRR
                extreme_boost = 1.5
                chaos_level = "DESPERATE_SWINGING"

            wicket_multiplier *= extreme_boost
            logger.info(f"{chaos_level}: RRR {required_rr:.1f} = {extreme_boost:.1f}x wicket boost!")
        
        effects = {
            'risk_active': True,
            'risk_factor': risk_factor,
            'boundary_boost': 1.0 + (risk_multiplier * 2.0),  # 🔧 INCREASED from 1.8
            'wicket_boost': wicket_multiplier,
            'dot_increase': max(0, (risk_multiplier - 0.5) * 0.3),  # 🔧 ONLY for extreme risk
            'strike_rotation_penalty': min(risk_multiplier * 0.4, 0.5),  # Capped at 50%
            'single_floor': max(0.06, 0.12 - (required_rr - 12) * 0.01),
            'mode': 'AGGRESSIVE_CRICKET'
        }
        
        # Mode classification
        if required_rr >= 20:
            effects['mode'] = 'ABSOLUTE_CHAOS'
        elif required_rr >= 18:
            effects['mode'] = 'RECKLESS_HITTING'
        elif required_rr >= 16:
            effects['mode'] = 'DESPERATE_SWINGING'
        elif wicket_multiplier >= 2.0:
            effects['mode'] = 'HIGH_RISK_CRICKET'

        logger.info(f"{effects['mode']}: Boundaries={effects['boundary_boost']:.1f}x, Wickets={effects['wicket_boost']:.1f}x")
        
        return effects

    def calculate_pressure(self, match_state):
        """Calculate overall pressure (0-100)"""
        if match_state['innings'] == 1:
            return self._calculate_first_innings_pressure(match_state)
        else:
            return self._calculate_second_innings_pressure(match_state)

    def calculate_super_over_pressure(self, so_state: dict) -> float:
        """
        Pressure score (0-100) for a single Super Over delivery.

        Deliberately NOT calculate_pressure() with fudged inputs: that method
        keys off format phases (powerplay/death overs), par-score tables, and
        an RRR baseline all calibrated for a full 120/300-ball innings — at
        over_number=0 the death-overs boost would simply never fire, and the
        required-run-rate comparison would misjudge a naturally-hot 6-ball
        rate as an "impossible chase". A Super Over needs its own formula:
        a high floor (this is the highest-drama moment in the game by
        design) plus wickets-down (out of only 2) and, for the chasing
        side, required rate against the SAME neutral baseline the setting
        side is judged against (SUPER_OVER_NEUTRAL_RPO — product decision:
        no pitch variation, no side gets an easier baseline than the other).

        The resulting score feeds the existing, rating-aware
        get_pressure_effects(pressure_score, batter_rating, bowler_rating,
        pitch) unchanged — a 95-rated batter still shrugs off pressure that
        would rattle a 60-rated one.

        so_state keys: wickets_down, so_innings (1|2), balls_remaining,
        runs_needed (innings 2 only), consecutive_dots.
        """
        pressure = SUPER_OVER_PRESSURE_FLOOR

        wickets_down = so_state.get('wickets_down', 0)
        pressure += wickets_down * 20  # 0 or 1 → +0 or +20

        if so_state.get('so_innings', 1) == 2 and so_state.get('runs_needed') is not None:
            runs_needed = max(0, so_state['runs_needed'])
            balls_remaining = max(1, so_state.get('balls_remaining', 6))
            required_rr = runs_needed / (balls_remaining / 6.0)
            rrr_ratio = required_rr / SUPER_OVER_NEUTRAL_RPO

            if rrr_ratio > 1.0:
                pressure += min(35, (rrr_ratio - 1.0) * 40)
            elif rrr_ratio < 0.6:
                # Chase is all but sealed — pressure eases off.
                pressure -= 15

        consecutive_dots = so_state.get('consecutive_dots', 0)
        if consecutive_dots >= 2:
            pressure += min(15, consecutive_dots * 5)

        return max(0.0, min(100.0, pressure))


    def _calculate_first_innings_pressure(self, state):
        """Calculate first innings pressure"""
        pressure = 0
        current_over = state['current_over']
        current_rr = state['current_run_rate']
        wickets = state['wickets']

        # expected_rr_first_innings is one format-wide number per phase. For
        # short formats scale it to the surface, exactly as the chase side of
        # this class now does: T10's death expectation is 15.0 RPO, which no
        # Green first innings (par 9.5) can ever reach, so every death
        # delivery there scored the full "well behind acceleration" +30 and
        # pushed the side batting first into the >70 band — boundary x0.95,
        # wicket x1.35. That is first-innings suppression of exactly the kind
        # the chase was just freed from, and it is why Green under-scored.
        _scale = 1.0
        if self._is_short:
            _scale = (getattr(self.fmt, "pitch_par_factors", None) or {}).get(
                state.get('pitch', 'Hard'), 1.0) or 1.0

        # Phase-specific pressure
        if self.fmt.is_powerplay(current_over):
            expected_rr = self.expected_rr_first_innings['powerplay'] * _scale
            if current_rr < expected_rr - 1.5:   # Significantly behind
                pressure += 25
            elif current_rr < expected_rr - 0.5:  # Slightly behind
                pressure += 15

        elif self.fmt.is_death(current_over):     # Death overs - acceleration pressure
            expected_rr = self.expected_rr_first_innings['death'] * _scale
            if current_rr < expected_rr - 2.0:   # Well behind acceleration
                pressure += 30
            elif current_rr < expected_rr - 1.0:  # Behind acceleration
                pressure += 20

        # Wickets pressure (early collapse — first half and first 3/4 of match)
        _early_cutoff = self.fmt.overs // 2        # T20: 10, ListA: 25
        _mid_cutoff   = self.fmt.overs * 3 // 4   # T20: 15, ListA: 37
        if current_over < _early_cutoff and wickets >= 4:
            pressure += 25
        elif current_over < _mid_cutoff and wickets >= 6:
            pressure += 20
        
        # Add momentum pressure
        momentum_pressure = self._calculate_momentum_pressure(state)
        pressure += momentum_pressure
        
        return min(100, max(0, pressure))
    
    def _calculate_second_innings_pressure(self, state):
        """Calculate second innings pressure - last 5 overs focus"""
        pressure = 0
        overs_left = state['overs_remaining']
        runs_needed = state['runs_needed']
        wickets_left = 10 - state['wickets']
        required_rr = state['required_run_rate']
        current_rr = state['current_run_rate']

        # Thresholds below are T20 constants: "last 5 overs" is a quarter of a
        # T20 innings but HALF a T10 one, and "RRR above 12" is a crisis at a
        # T20 par of 8.5 RPO and merely par at a T10 one of 12.5. Left as-is a
        # T10 chase sat permanently in the high-pressure band, which returns a
        # boundary PENALTY (0.95x) and 1.35x wickets from get_pressure_effects
        # — pressure that suppressed hitting for the entire second innings.
        # Short formats read the same shape relative to the format and pitch;
        # T20 and List A keep the exact numbers they were calibrated on.
        if self._is_short:
            _base = self._rate_baseline(state.get('pitch', 'Hard'))
            _late_overs = max(2, self.fmt.overs // 4)
            # A required rate drifts above the pitch baseline as soon as a
            # chase is even slightly behind, so thresholds close to 1.0 put
            # the second innings under standing pressure (and its wicket
            # multiplier) for most of the match with nothing equivalent on
            # the other side. These sit far enough out to mean real trouble.
            _rrr_high, _rrr_mid = _base * 1.50, _base * 1.25
            _endgame_runs = max(1.0, min(2.0, overs_left)) * _base * 1.3
        else:
            _late_overs, _rrr_high, _rrr_mid, _endgame_runs = 5, 12, 10, 15

        # High pressure in the closing overs
        if overs_left <= _late_overs:
            # Required run rate pressure
            rr_gap = required_rr - current_rr
            if rr_gap > 3.0:
                pressure += 40
            elif rr_gap > 2.0:
                pressure += 30
            elif rr_gap > 1.0:
                pressure += 20
            
            # Wickets pressure
            if wickets_left <= 3:
                pressure += 25
            elif wickets_left <= 5:
                pressure += 15
            
            # Overs pressure (very few overs left)
            if overs_left <= 2 and runs_needed > _endgame_runs:
                pressure += 20
        
        # General chase pressure (throughout innings)
        if required_rr > _rrr_high:
            pressure += 15
        elif required_rr > _rrr_mid:
            pressure += 10
        
        # Add momentum pressure
        momentum_pressure = self._calculate_momentum_pressure(state)
        pressure += momentum_pressure
        
        return min(100, max(0, pressure))
    
    def _calculate_momentum_pressure(self, state):
        """Calculate pressure from recent events"""
        if len(self.recent_events) < 2:
            return 0
        
        momentum_pressure = 0
        recent_wickets = sum(1 for event in self.recent_events[-3:] if event.get('wicket'))
        recent_dots = sum(1 for event in self.recent_events[-3:] if event.get('runs') == 0 and not event.get('extra'))
        recent_boundaries = sum(1 for event in self.recent_events[-3:] if event.get('runs') >= 4)
        
        # Pitch-specific momentum
        pitch = state['pitch']
        
        if pitch in ['Green', 'Dry']:  # Bowler-friendly
            # Dots create more pressure
            if recent_dots >= 2:
                momentum_pressure += 15
            if recent_wickets >= 1:
                momentum_pressure += 20
        else:  # Flat/Hard/Dead - batting friendly
            # Lack of acceleration creates pressure
            if recent_dots >= 2 and recent_boundaries == 0:
                momentum_pressure += 20
            if recent_wickets >= 1:
                momentum_pressure += 25
        
        # Partnership break pressure
        if recent_wickets >= 1 and state['current_partnership_balls'] > 30:
            momentum_pressure += 10
        
        return momentum_pressure
    
    def update_recent_events(self, ball_outcome):
        """Update recent events for momentum calculation"""
        event = {
            'runs': ball_outcome.get('runs', 0),
            'wicket': ball_outcome.get('batter_out', False),
            'extra': ball_outcome.get('is_extra', False)
        }
        
        self.recent_events.append(event)
        
        # Keep only last 6 balls for momentum
        if len(self.recent_events) > 6:
            self.recent_events.pop(0)
    

    def get_chasing_advantage(self, match_state):
        """Apply realistic chasing advantage (format-aware: T20 or ListA)."""
        if match_state['innings'] != 2:
            return None
        
        current_over = match_state.get('current_over', 0)
        wickets_remaining = 10 - match_state.get('wickets', 0)

        # Short formats: exactly neutral. The 1.02 wicket bias List A carries
        # below is a scoreboard-pressure tax that makes sense over 300 balls
        # and not over 60 — it was a standing 2% wicket penalty applied to
        # every chase delivery, on top of every other second-innings
        # multiplier. Parity between the innings here is structural, not tuned.
        if self.fmt.strict_short_bowling:
            return {
                'boundary_boost': 1.00,
                'wicket_reduction': 1.00,
                'strike_rotation_boost': 1.00
            }

        # ListA: remove blanket chase buff. Long chases carry scoreboard pressure,
        # so keep boundaries neutral and add a slight wicket-pressure bias.
        if self.fmt.name == "ListA":
            return {
                'boundary_boost': 1.00,
                'wicket_reduction': 1.02,  # >1.0 means slightly higher wicket risk
                'strike_rotation_boost': 1.00
            }
        
        # Chasing teams have slight advantage knowing the target
        # But pressure of the chase should balance this out
        base_advantage = {
            'boundary_boost': 1.04,  # 4% more boundaries (better shot selection)
            'wicket_reduction': 0.97,  # 3% fewer wickets (game awareness)
            'strike_rotation_boost': 1.05  # 5% better strike rotation
        }

        # Additional advantage in pre-death/death overs with wickets in hand
        if current_over >= self.fmt.death_phase.start - 1 and wickets_remaining >= 6:
            base_advantage.update({
                'boundary_boost': 1.07,  # 7% more boundaries
                'wicket_reduction': 0.95,  # 5% fewer wickets
            })
            logger.info(f"CHASING ADVANTAGE: Death overs with {wickets_remaining} wickets - Enhanced scoring!")
        
        return base_advantage

    def get_pressure_effects(self, pressure_score, batter_rating, bowler_rating, pitch):
        """Get pressure effects on ball outcome probabilities"""
        # Player pressure handling ability
        batter_pressure_handling = self._get_pressure_handling(batter_rating)
        bowler_pressure_advantage = self._get_pressure_advantage(bowler_rating)
        
        # Adjust effective pressure based on player abilities
        effective_pressure = pressure_score * (1 - batter_pressure_handling) * (1 + bowler_pressure_advantage)
        effective_pressure = min(100, max(0, effective_pressure))
        
        # 🔧 FAIR PRESSURE EFFECTS - No artificial dot increases in death overs
        if effective_pressure < 30:  # Low pressure
            return {
                'dot_bonus': 0.0,
                'boundary_modifier': 1.0,
                'wicket_modifier': 1.0,
                'strike_rotation_penalty': 0.0
            }
        elif effective_pressure < 70:  # Medium pressure
            return {
                'dot_bonus': 0.02,  # Reduced from 0.05
                'boundary_modifier': 1.0,  # No penalty - keep equal to first innings
                'wicket_modifier': 1.15,   # Slight increase
                'strike_rotation_penalty': 0.05  # Minimal penalty
            }
        else:  # High pressure - boom-or-bust, more wickets
            if self._is_short:
                # No boundary penalty in a short format. Both halves of the
                # T10 match are meant to be hitting; "pressure makes clean
                # hitting harder" is the one term here that says otherwise,
                # and because the chase reaches this band far more often than
                # the side batting first (its score stacks required-rate,
                # wickets-left and overs-left bonuses the first innings has no
                # counterpart for), a symmetric-looking rule was in practice a
                # standing tax on the second innings.
                return {
                    'dot_bonus': 0.0,
                    'boundary_modifier': 1.0,
                    'wicket_modifier': 1.20,
                    'strike_rotation_penalty': 0.1
                }
            return {
                'dot_bonus': 0.03,
                'boundary_modifier': 0.95,  # Pressure makes clean hitting harder
                'wicket_modifier': 1.35,    # Higher wickets under pressure
                'strike_rotation_penalty': 0.1
            }
    
    def _get_pressure_handling(self, player_rating):
        """Calculate pressure handling ability (0.0 to 0.4)"""
        # Higher rated players handle pressure better
        if player_rating >= 85:
            return 0.4  # Excellent pressure handling
        elif player_rating >= 75:
            return 0.3  # Good pressure handling
        elif player_rating >= 65:
            return 0.2  # Average pressure handling
        else:
            return 0.1  # Poor pressure handling
    
    def _get_pressure_advantage(self, bowler_rating):
        """Calculate bowler's ability to exploit pressure (0.0 to 0.3)"""
        if bowler_rating >= 85:
            return 0.3  # Excellent at exploiting pressure
        elif bowler_rating >= 75:
            return 0.2  # Good at exploiting pressure
        elif bowler_rating >= 65:
            return 0.15  # Average
        else:
            return 0.1  # Limited ability


# ---------------------------------------------------------------------------
# FCPressureEngine — First-Class (FC): session-survival + lead-building
# pressure, not run-rate-chase pressure
# ---------------------------------------------------------------------------
#
# PressureEngine above is entirely RRR/death-overs-chase-shaped, referencing
# self.fmt.is_death()/death_phase.start/self.fmt.overs — none of which exist
# on MultiDayFormatConfig, and none of which are meaningful in 3 of FC's 4
# innings (only innings 4 has a real target to chase). This is a genuinely
# separate pressure axis, not a branch inside PressureEngine.
#
# Output contract: get_pressure_effects() returns a dict using the SAME keys
# apply_pressure_effects_to_weights() (engine/ball_outcome.py) already reads
# generically — dot_bonus (additive), boundary_modifier / wicket_modifier
# (multiplicative), optional strike_rotation_penalty. No new application code
# is needed in ball_outcome.py; FCPressureEngine only populates this dict.

class FCPressureEngine:
    def __init__(self, format_config=None):
        self.fmt = format_config
        self.recent_events = []

    def update_recent_events(self, ball_outcome):
        self.recent_events.append(ball_outcome)
        if len(self.recent_events) > 18:
            self.recent_events = self.recent_events[-18:]

    # --- Partnership grind -------------------------------------------------
    # Ramped by BALLS, not runs: a watchful 60 off 200 balls demoralises an
    # attack far more than a breezy 60 off 90. Nothing else in the FC model
    # captures this — the confidence curve is per-batter and the spell model
    # is per-bowler, so a long STAND had no effect on anything at all.
    _PARTNERSHIP_SETTLED_BALLS = 120      # ~20 overs before it starts to tell
    _PARTNERSHIP_FULL_GRIND_BALLS = 480   # ~80 overs for the full effect
    _PARTNERSHIP_MAX_WICKET_SUPPRESSION = 0.10
    _PARTNERSHIP_MAX_BOUNDARY_GAIN = 0.10

    def partnership_grind(self, partnership_balls: int) -> float:
        """0.0-1.0 — how far an established stand has worn the attack down."""
        if partnership_balls <= self._PARTNERSHIP_SETTLED_BALLS:
            return 0.0
        span = self._PARTNERSHIP_FULL_GRIND_BALLS - self._PARTNERSHIP_SETTLED_BALLS
        return min(1.0, (partnership_balls - self._PARTNERSHIP_SETTLED_BALLS) / span)

    # --- Collapse cascade --------------------------------------------------
    # A real collapse accelerates. The old model applied a flat 1.25x however
    # many had just gone, so 3 for 12 looked the same as one loose shot.
    _COLLAPSE_SEVERITY = {2: 1.18, 3: 1.30, 4: 1.42}
    _COLLAPSE_NEW_BATTER_BONUS = 0.12     # walking in mid-collapse is the worst moment
    _COLLAPSE_NEW_BATTER_BALLS = 8

    def collapse_severity(self, recent_wickets: int, striker_balls_faced: int = 99,
                          temperament_rating: Optional[int] = None) -> float:
        """Wicket multiplier once a collapse has been triggered."""
        severity = self._COLLAPSE_SEVERITY.get(
            min(recent_wickets, 4), 1.18 if recent_wickets >= 2 else 1.0)
        if striker_balls_faced < self._COLLAPSE_NEW_BATTER_BALLS:
            severity += self._COLLAPSE_NEW_BATTER_BONUS
        if temperament_rating is not None:
            # A batter who resists pressure well takes less of the extra.
            severity = 1.0 + (severity - 1.0) * (
                1.0 - (temperament_rating - 50) / 150.0)
        return max(1.0, severity)

    def should_trigger_collapse(self, wickets: int, recent_wickets: int, temperament_rating: Optional[int] = None) -> bool:
        """Probabilistic wicket-cluster signal, session-survival flavored —
        FC's counterpart to PressureEngine.should_trigger_wicket_cluster().

        temperament_rating (the batter currently at the crease, 0-100)
        dampens the cluster chance — a batter who resists session-long
        pressure well is less likely to be swept up in a collapse around
        them. Neutral (no dampening) when omitted."""
        if wickets >= 4 and recent_wickets >= 2:
            cluster_chance = 0.10
            if wickets >= 6:
                cluster_chance = 0.14
            if wickets >= 8:
                cluster_chance = 0.18
            if recent_wickets >= 3:
                cluster_chance *= 0.4  # dampen unrealistic cascades
            if temperament_rating is not None:
                # 50 (neutral) -> 1.0x; 100 -> 0.6x; 0 -> 1.4x
                cluster_chance *= 1.0 - (temperament_rating - 50) / 125.0
            return random.random() < max(0.0, cluster_chance)
        return False

    def get_pressure_effects(self, match_state: dict) -> dict:
        """
        Returns a pressure_effects dict (dot_bonus/boundary_modifier/
        wicket_modifier) for the current ball, composed from up to three
        named situational modes plus (innings 4 only) a lightweight
        required-rate-like signal. Multiple modes can combine (e.g. a
        settling-in batter during a survival day both apply).

        Parameters expected on match_state
        -----------------------------------
        fc_innings          : 1-4
        wickets              : wickets down in the current innings
        striker_balls_faced : balls faced by the batter on strike
        days_remaining       : full match days left including today
        lead                 : batting side's lead/deficit (positive = ahead)
        deficit_to_follow_on : follow_on_margin - deficit, only meaningful
                                pre-follow-on-decision; None otherwise
        target               : innings-4 target, or None
        score                : current innings score
        recent_wickets       : wickets fallen in the last few overs
        partnership_balls    : balls faced by the current stand — an
                                established partnership grinds an attack down
        striker_technique    : batter's technique_rating (0-100), Phase 2 —
                                dampens the settling-in penalty's severity
        last_hour            : True in the closing overs of a day's play
        striker_temperament  : batter's temperament_rating (0-100), Phase 2 —
                                dampens pressure-driven wicket increases
                                (survival mode, collapse-cluster chance)
        """
        fc_innings = match_state.get("fc_innings", 1)
        wickets = match_state.get("wickets", 0)
        striker_balls_faced = match_state.get("striker_balls_faced", 0)
        days_remaining = match_state.get("days_remaining", 99)
        recent_wickets = match_state.get("recent_wickets", 0)
        striker_technique = match_state.get("striker_technique")
        striker_temperament = match_state.get("striker_temperament")

        effects = {"dot_bonus": 0.0, "boundary_modifier": 1.0, "wicket_modifier": 1.0}

        # --- Settling in: a batter early in their innings is cautious,
        # regardless of overall match situation. Technique shortens/softens
        # this — a technically correct batter gets through the tricky first
        # deliveries with less visible discomfort. ---
        if 0 < striker_balls_faced < 15:
            settle_factor = 1.0 - (striker_balls_faced / 15.0)  # 1.0 -> 0.0
            if striker_technique is not None:
                # 50 (neutral) -> 1.0x; 100 -> 0.5x; 0 -> 1.5x
                settle_factor *= 1.0 - (striker_technique - 50) / 100.0
                settle_factor = max(0.0, settle_factor)
            effects["dot_bonus"] += 0.10 * settle_factor
            effects["boundary_modifier"] *= 1.0 - (0.30 * settle_factor)

        # --- Survival: batting out time with little/no scoring incentive
        # left (following on with a hopeless chase, or run out of match time
        # to force a positive result). Temperament dampens how much extra
        # SAFETY this buys. Temperament now has the intuitive cricketing
        # identity: the calmer batter is better at executing the rearguard
        # and receives more of the survival wicket reduction. ---
        intent = match_state.get("intent")
        if intent is None:
            # Older callers may supply explicit modes. Never infer survival
            # merely from the calendar day, or stack opposing full modes.
            survival = float(bool(match_state.get("survival_mode", False)))
            attack = float(bool(match_state.get("acceleration_mode", False))) * (1-survival)
            stumps = float(bool(match_state.get("last_hour", False))) * (1-survival)
        else:
            survival, attack, stumps = (intent[k] for k in ("survival", "attack", "stumps"))
        effects["dot_bonus"] += 0.18 * survival - 0.08 * attack + 0.08 * stumps
        effects["boundary_modifier"] *= (1 - 0.35 * survival) * (1 + 0.35 * attack) * (1 - 0.20 * stumps)
        reduction = 0.20
        if striker_temperament is not None:
            reduction = max(0.10, min(0.30, reduction + (striker_temperament - 50) / 500))
        effects["wicket_modifier"] *= (1 - reduction * survival) * (1 + 0.10 * attack) * (1 - 0.08 * stumps)
        if fc_innings == 4 and striker_temperament is not None:
            resilience = max(-1.0, min(1.0, (striker_temperament - 50) / 50))
            effects["wicket_modifier"] *= 1 - 0.08 * resilience * (1-survival)
        if match_state.get("tail_protection"):
            effects["fc_tail_protection"] = match_state["tail_protection"]

        # --- Partnership grind: an established stand wears an attack down.
        # The bowlers have been at it a while, the ball is old, the captain
        # is out of ideas and the field has spread. ---
        grind = self.partnership_grind(match_state.get("partnership_balls", 0))
        if grind > 0.0:
            effects["wicket_modifier"] *= 1.0 - self._PARTNERSHIP_MAX_WICKET_SUPPRESSION * grind
            effects["boundary_modifier"] *= 1.0 + self._PARTNERSHIP_MAX_BOUNDARY_GAIN * grind
            effects["dot_bonus"] -= 0.03 * grind

        # --- Wicket-cluster collapse, as a cascade rather than a flat bump.
        # The more that have just gone the harder it gets, and a batter who
        # has only just walked in is at his most vulnerable. ---
        if self.should_trigger_collapse(wickets, recent_wickets, temperament_rating=striker_temperament):
            severity = self.collapse_severity(
                recent_wickets,
                striker_balls_faced=striker_balls_faced,
                temperament_rating=striker_temperament,
            )
            effects["wicket_modifier"] *= severity
            logger.info("FC COLLAPSE: %.2fx wicket boost (%d down, %d recent)",
                        severity, wickets, recent_wickets)

        # dot_bonus should never go negative enough to imply MORE dots from
        # a "less cautious" signal than the base matrix already encodes.
        effects["dot_bonus"] = max(-0.15, effects["dot_bonus"])

        return effects
