
import json
import random
import logging
import os

logger = logging.getLogger(__name__)

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

    def _load_data(self):
        try:
            with open(self.data_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load commentary pack from {self.data_path}: {e}")
            return {"events": {}, "narratives": {}}

    def get_commentary(self, ball_context, match_state):
        """
        Generate commentary string.
        """
        # 1. Determine Event Key
        event_key = self._map_context_to_key(ball_context)
        
        # 2. Get Micro Commentary (Shot description)
        micro_text = self._select_template(event_key, ball_context)
        
        # 3. Get Macro Commentary (Narrative)
        macro_text = self._check_narratives(ball_context, match_state)
        
        # 4. Combine
        if macro_text:
            return f"{micro_text} {macro_text}"
        return micro_text

    def _map_context_to_key(self, context):
        """
        Map ball context to a JSON key (e.g. 'boundary_four', 'wicket_bowled').
        Context comes from ball_outcome.py: type (run/wicket/extra), runs, wicket_type, etc.
        """
        outcome_type = context.get("type", "").lower() # run, wicket, extra
        runs = context.get("runs", 0)
        is_extra = context.get("is_extra", False)
        
        if outcome_type == "wicket":
            wkt_type = context.get("wicket_type", "caught").lower()
            return f"wicket_{wkt_type}"
            
        if is_extra:
            extra_type = context.get("extra_type", "").lower()
            if "wide" in extra_type: return "wide"
            if "no" in extra_type: return "noball"
            return "dot" # Fallback for byes/legbyes if no template
            
        if runs == 4: return "boundary_four"
        if runs == 6: return "boundary_six"
        if runs == 0: return "dot"
        if runs == 1: return "single"
        if runs == 2: return "double" # Need to add to JSON
        if runs == 3: return "three"  # Need to add to JSON
        
        return "dot" # Default fallback

    def _select_template(self, key, context):
        """Select a template for the given key and format it."""
        templates = self.events.get(key, [])
        if not templates:
            # Fallback if specific key missing (e.g. wicket_stumped might not exist yet)
            if "wicket" in key:
                templates = self.events.get("wicket_caught", [])
            elif "boundary" in key:
                templates = self.events.get("boundary_four", [])
            else:
                return context.get("description", "Play continues.") # Use original if all fails
        
        if not templates:
            return context.get("description", "Play continues.")

        # Simple random choice for now. 
        # Future: Filter by context['tags'] vs template['tags']
        template_obj = random.choice(templates)
        text = template_obj.get("text", "")
        
        # Format
        return text.format(
            batter=context.get("batter", "The batter"),
            bowler=context.get("bowler", "The bowler"),
            runs=context.get("runs", 0),
            team=context.get("batting_team", "The batting side"),
            fielding_team=context.get("bowling_team", "The fielding side")
        )

    def _check_narratives(self, context, state):
        """Check for narrative triggers."""
        triggers = []
        
        # Collapse: 3 wickets in last 12 balls (example logic)
        recent_wickets = state.get("recent_wickets_match", 0) # Need to ensure this is passed
        if context.get("type") == "wicket" and recent_wickets >= 3:
             triggers.extend(self.narratives.get("collapse_wicket", []))

        # Milestone: Batter reached 50/100 exactly
        b_runs = state.get("batter_runs", 0)
        if b_runs == 50:
             triggers.extend(self.narratives.get("milestone_50", []))
        elif b_runs == 100:
             triggers.extend(self.narratives.get("milestone_100", []))
             
        # Partnership: e.g. 50 runs
        p_runs = state.get("partnership_runs", 0)
        if p_runs == 50:
             triggers.extend(self.narratives.get("partnership_50", []))

        if triggers:
            return random.choice(triggers)
        return None
