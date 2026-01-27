import random
import logging

logger = logging.getLogger(__name__)

class PressureEngine:
    def __init__(self):
        # Expected run rates by phase
        self.expected_rr_first_innings = {
            'powerplay': 7.5,      # Overs 1-6
            'middle': 8.0,         # Overs 7-15  
            'death': 10.5          # Overs 16-20
        }
        
        # Recent events for momentum (last 3 balls)
        self.recent_events = []
    

    def calculate_unified_risk_factor(self, match_state):
        """Calculate unified risk factor based on death overs and required rate"""
        if match_state['innings'] != 2:
            return 1.0  # No risk in first innings
        
        current_over = match_state.get('current_over', 0)
        required_rr = match_state.get('required_run_rate', 0)
        overs_remaining = match_state.get('overs_remaining', 0)
        
        risk_factor = 1.0
        risk_components = []
        
        # 1. Death overs base risk (overs 17-20)
        if current_over >= 16:  # Overs 17, 18, 19, 20
            death_risk = 0.3 + (current_over - 16) * 0.1  # Increases each over
            risk_factor += death_risk
            risk_components.append(f"Death overs: +{death_risk:.1f}")
        
        # 2. High required rate risk (throughout 2nd innings)
        if required_rr > 12:
            rr_risk = min((required_rr - 12) * 0.15, 0.8)  # Max +0.8 for very high RR
            risk_factor += rr_risk
            risk_components.append(f"High RRR ({required_rr:.1f}): +{rr_risk:.1f}")
        
        # 3. Final overs desperation (overs 19-20 only)
        if current_over >= 18 and overs_remaining <= 2:
            final_desperation = 0.4
            risk_factor += final_desperation
            risk_components.append(f"Final desperation: +{final_desperation:.1f}")
        
        if risk_factor > 1.1:  # Only log if significant risk
            logger.info(f"UNIFIED RISK FACTOR: {risk_factor:.2f}")
            for component in risk_components:
                logger.info(f"   {component}")
        
        return risk_factor


    def calculate_defensive_factor(self, match_state):
        """Calculate defensive factor when team is protecting wickets in death overs"""
        if match_state['innings'] != 2:
            return None
        
        current_over = match_state.get('current_over', 0)
        wickets_fallen = match_state.get('wickets', 0)
        overs_remaining = match_state.get('overs_remaining', 0)
        
        # Defensive mode only in death overs (17-20) with many wickets down
        if current_over >= 16 and wickets_fallen >= 6:
            
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
                'boundary_reduction': 0.4 + (defensive_level * 0.4),  # 40-80% fewer boundaries
                'wicket_reduction': 0.3 + (defensive_level * 0.5),    # 30-80% fewer wickets
                'dot_increase': defensive_level * 0.3,                 # More defensive dots
                'single_boost': 1.0 + (defensive_level * 0.8),        # 80% more singles
                'mode': mode
            }

            logger.info(f"{mode}: Defensive level {defensive_level:.1f} - Protecting wickets!")
            return effects
        
        return None
    
    def should_trigger_wicket_cluster(self, match_state, recent_wickets=0):
        """Check if conditions are right for rapid wicket fall"""
        if match_state['innings'] != 2:
            return False
        
        current_over = match_state.get('current_over', 0)
        required_rr = match_state.get('required_run_rate', 0)
        wickets_fallen = match_state.get('wickets', 0)
        
        # Only in death overs with extreme required rate
        if current_over >= 16 and required_rr >= 14:
            
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
        """Get FAIR risk-based effects - boom-or-bust without unfair restrictions"""
        risk_factor = self.calculate_unified_risk_factor(match_state)
        
        if risk_factor <= 1.1:
            return None
        
        required_rr = match_state.get('required_run_rate', 0)
        current_over = match_state.get('current_over', 0)
        
        # Risk multiplier
        risk_multiplier = risk_factor - 1.0
        
        # 🔧 FAIR BOOM-OR-BUST: Equal boundary opportunities, proportional wickets
        wicket_multiplier = 1.0 + (risk_multiplier * 1.5)
        
        # Extreme scaling only for impossible chases (RRR 16+)
        if current_over >= 16 and required_rr > 16:
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
    
    def _calculate_first_innings_pressure(self, state):
        """Calculate first innings pressure"""
        pressure = 0
        current_over = state['current_over']
        current_rr = state['current_run_rate']
        wickets = state['wickets']
        
        # Phase-specific pressure
        if current_over < 6:  # Powerplay - slow start pressure
            expected_rr = self.expected_rr_first_innings['powerplay']
            if current_rr < expected_rr - 1.5:  # Significantly behind
                pressure += 25
            elif current_rr < expected_rr - 0.5:  # Slightly behind
                pressure += 15
        
        elif current_over >= 15:  # Death overs - acceleration pressure
            expected_rr = self.expected_rr_first_innings['death']
            if current_rr < expected_rr - 2.0:  # Well behind acceleration
                pressure += 30
            elif current_rr < expected_rr - 1.0:  # Behind acceleration
                pressure += 20
        
        # Wickets pressure (early collapse)
        if current_over < 10 and wickets >= 4:
            pressure += 25
        elif current_over < 15 and wickets >= 6:
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
        
        if pitch in ['Green', 'Dusty']:  # Bowler-friendly
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
        """Apply realistic chasing advantage in T20 cricket"""
        if match_state['innings'] != 2:
            return None
        
        current_over = match_state.get('current_over', 0)
        wickets_remaining = 10 - match_state.get('wickets', 0)
        
        # Chasing teams have advantage knowing the target
        base_advantage = {
            'boundary_boost': 1.08,  # 8% more boundaries (better shot selection)
            'wicket_reduction': 0.95,  # 5% fewer wickets (better game awareness)
            'strike_rotation_boost': 1.1  # 10% better strike rotation
        }
        
        # Additional advantage in death overs with wickets in hand
        if current_over >= 15 and wickets_remaining >= 6:
            base_advantage.update({
                'boundary_boost': 1.12,  # 12% more boundaries
                'wicket_reduction': 0.92,  # 8% fewer wickets
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
        else:  # High pressure - Focus on boom-or-bust, not restriction
            return {
                'dot_bonus': 0.03,  # Minimal - reduced from 0.12
                'boundary_modifier': 1.05,  # 🔧 SLIGHT BONUS for aggressive cricket
                'wicket_modifier': 1.3,     # Higher wickets for boom-or-bust
                'strike_rotation_penalty': 0.1  # Reduced from 0.2
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
