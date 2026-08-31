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
    

    def calculate_unified_risk_factor(self, match_state):
        """Calculate unified risk factor based on death overs and required rate"""
        if match_state['innings'] == 1:
            return self._calculate_first_innings_risk(match_state)

        current_over = match_state.get('current_over', 0)
        required_rr = match_state.get('required_run_rate', 0)
        overs_remaining = match_state.get('overs_remaining', 0)
        
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
        if self.fmt.name == "ListA" and 35 <= current_over < _death_start:
            slog_boost = 0.05 + (current_over - 35) * 0.02  # 0.05 → 0.13
            return min(1.0 + slog_boost, 1.25)

        # Only accelerate from the over before death onwards
        if current_over < _pre_death:
            return 1.0

        risk_factor     = 1.0
        wickets_in_hand = 10 - wickets

        # Death/pre-death base acceleration
        death_boost  = 0.1 + (current_over - _pre_death) * 0.05
        risk_factor += death_boost

        # Wickets-in-hand multiplier
        if wickets_in_hand >= 7:
            risk_factor += 0.15   # Plenty of batting — go big
        elif wickets_in_hand >= 5:
            risk_factor += 0.08   # Comfortable — can afford risks
        elif wickets_in_hand <= 2:
            risk_factor -= 0.15   # Protect wickets, reduce aggression

        # Score-based urgency: use format par scores instead of T20 lookup
        par = self.fmt.par_scores.get(
            current_over,
            self.fmt.par_scores.get(self.fmt.overs, 0)
        )
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

        # Second innings: death overs with extreme required rate
        if self.fmt.is_death(current_over) and required_rr >= 14:
            
            # Higher chance if already under pressure
            if wickets_fallen >= 5:
                cluster_chance = 0.15  # 15% chance per ball
            elif wickets_fallen >= 3:
                cluster_chance = 0.12  # 12% chance per ball
            else:
                cluster_chance = 0.08  # 8% chance per ball
            
            # Increase chance based on how impossible the chase is
            impossibility_factor = min((required_rr - 14) / 6, 1.0)  # 0-1 scale
            cluster_chance += impossibility_factor * 0.1
            
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
        
        # Phase-specific pressure
        if self.fmt.is_powerplay(current_over):
            expected_rr = self.expected_rr_first_innings['powerplay']
            if current_rr < expected_rr - 1.5:   # Significantly behind
                pressure += 25
            elif current_rr < expected_rr - 0.5:  # Slightly behind
                pressure += 15

        elif self.fmt.is_death(current_over):     # Death overs - acceleration pressure
            expected_rr = self.expected_rr_first_innings['death']
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
        
        # High pressure in last 5 overs
        if overs_left <= 5:
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
            if overs_left <= 2 and runs_needed > 15:
                pressure += 20
        
        # General chase pressure (throughout innings)
        if required_rr > 12:
            pressure += 15
        elif required_rr > 10:
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
        is_survival = match_state.get("survival_mode", False) or (
            days_remaining <= 1 and fc_innings in (2, 4)
        )
        if is_survival:
            effects["dot_bonus"] += 0.18
            effects["boundary_modifier"] *= 0.65
            wicket_reduction = 0.20  # base: wicket_modifier *= (1 - 0.20) = 0.80
            if striker_temperament is not None:
                # 50 -> 20%; 100 -> 30%; 0 -> 10%.
                wicket_reduction += (striker_temperament - 50) / 500.0
                wicket_reduction = max(0.10, min(0.30, wicket_reduction))
            effects["wicket_modifier"] *= 1.0 - wicket_reduction
        elif fc_innings == 4 and striker_temperament is not None:
            # In a live fourth-innings chase, calm decision-making protects
            # against the target and deteriorating surface without forcing a
            # high-temperament player into full survival mode.
            resilience = max(-1.0, min(1.0, (striker_temperament - 50) / 50.0))
            effects["wicket_modifier"] *= 1.0 - 0.08 * resilience

        # --- Last hour before stumps: nobody wants to be the man who gets
        # out with ten minutes left. Milder than full survival mode — this
        # is seeing out a day, not saving a match — and it is what makes the
        # close of play a passage of play rather than just more overs. ---
        if match_state.get("last_hour", False):
            effects["dot_bonus"] += 0.08
            effects["boundary_modifier"] *= 0.80
            effects["wicket_modifier"] *= 0.92

        # --- Declaration-push / chase-acceleration: building quickly toward
        # a declaration, or chasing a gettable target with time to spare. ---
        is_accelerating = match_state.get("acceleration_mode", False)
        if is_accelerating:
            effects["boundary_modifier"] *= 1.35
            effects["dot_bonus"] -= 0.08
            effects["wicket_modifier"] *= 1.10

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

        # --- 4th-innings chase: the one FC innings with a genuine target,
        # so it's the one place a rate-pressure signal applies — recalibrated
        # to FC's much lower baseline run rates (RRR ~3.5 is real pressure
        # here; it would be trivial in T20/ListA). ---
        if fc_innings == 4:
            required_rr = match_state.get("required_run_rate", 0.0)
            if required_rr > 4.5:
                rr_risk = min((required_rr - 4.5) * 0.25, 1.2)
                effects["boundary_modifier"] *= 1.0 + rr_risk * 0.6
                effects["wicket_modifier"] *= 1.0 + rr_risk * 0.5
                effects["dot_bonus"] -= min(rr_risk * 0.05, 0.10)

        # dot_bonus should never go negative enough to imply MORE dots from
        # a "less cautious" signal than the base matrix already encodes.
        effects["dot_bonus"] = max(-0.15, effects["dot_bonus"])

        return effects
