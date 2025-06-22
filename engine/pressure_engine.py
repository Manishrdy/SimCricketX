import random

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
    

    def calculate_unified_risk_factor(self, match_state, win_probability=None):
        """Calculate unified risk factor based on death overs, win probability, and required rate"""
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
        
        # 2. Low win probability risk (only in death overs)
        if current_over >= 16 and win_probability is not None and win_probability < 5.0:
            win_prob_risk = (5.0 - win_probability) * 0.25  # Max +1.25 for 0% win prob
            risk_factor += win_prob_risk
            risk_components.append(f"Low win prob ({win_probability:.1f}%): +{win_prob_risk:.1f}")
        
        # 3. High required rate risk (throughout 2nd innings)
        if required_rr > 12:
            rr_risk = min((required_rr - 12) * 0.15, 0.8)  # Max +0.8 for very high RR
            risk_factor += rr_risk
            risk_components.append(f"High RRR ({required_rr:.1f}): +{rr_risk:.1f}")
        
        # 4. Final overs desperation (overs 19-20 only)
        if current_over >= 18 and overs_remaining <= 2:
            final_desperation = 0.4
            risk_factor += final_desperation
            risk_components.append(f"Final desperation: +{final_desperation:.1f}")
        
        if risk_factor > 1.1:  # Only log if significant risk
            print(f"🎲 UNIFIED RISK FACTOR: {risk_factor:.2f}")
            for component in risk_components:
                print(f"   {component}")
        
        return risk_factor

    def get_risk_based_effects(self, match_state, win_probability=None):
        """Get risk-based effects for boom-or-bust cricket"""
        risk_factor = self.calculate_unified_risk_factor(match_state, win_probability)
        
        if risk_factor <= 1.1:  # Minimal risk
            return None
        
        # Convert risk factor to outcome modifiers
        risk_multiplier = risk_factor - 1.0  # How much above baseline
        
        effects = {
            'risk_active': True,
            'risk_factor': risk_factor,
            'boundary_boost': 1.0 + (risk_multiplier * 1.8),    # Boundaries increase most
            'wicket_boost': 1.0 + (risk_multiplier * 1.5),      # Wickets increase significantly  
            'dot_increase': risk_multiplier * 0.6,               # More mistimed shots
            'strike_rotation_penalty': risk_multiplier * 0.8,    # Much less safe singles
            'mode': 'AGGRESSIVE_CRICKET'
        }
        
        # Special modes for extreme risk
        if risk_factor >= 2.5:
            effects['mode'] = 'DEATH_OR_GLORY'
        elif risk_factor >= 2.0:
            effects['mode'] = 'ALL_OUT_ATTACK'
        elif risk_factor >= 1.8:
            effects['mode'] = 'HIGH_RISK_CRICKET'
        
        print(f"🔥 {effects['mode']}: Boundaries={effects['boundary_boost']:.1f}x, Wickets={effects['wicket_boost']:.1f}x")
        
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
    
    def get_pressure_effects(self, pressure_score, batter_rating, bowler_rating, pitch):
        """Get pressure effects on ball outcome probabilities"""
        # Player pressure handling ability
        batter_pressure_handling = self._get_pressure_handling(batter_rating)
        bowler_pressure_advantage = self._get_pressure_advantage(bowler_rating)
        
        # Adjust effective pressure based on player abilities
        effective_pressure = pressure_score * (1 - batter_pressure_handling) * (1 + bowler_pressure_advantage)
        effective_pressure = min(100, max(0, effective_pressure))
        
        # Convert to pressure effects
        if effective_pressure < 30:  # Low pressure
            return {
                'dot_bonus': 0.0,
                'boundary_modifier': 1.0,
                'wicket_modifier': 1.0,
                'strike_rotation_penalty': 0.0
            }
        elif effective_pressure < 70:  # Medium pressure
            return {
                'dot_bonus': 0.05,
                'boundary_modifier': 0.95,
                'wicket_modifier': 1.1,
                'strike_rotation_penalty': 0.1
            }
        else:  # High pressure
            return {
                'dot_bonus': 0.12,
                'boundary_modifier': 0.85,
                'wicket_modifier': 1.25,
                'strike_rotation_penalty': 0.2
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