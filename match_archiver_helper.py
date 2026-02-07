"""
Helper method for match_archiver.py to add as a method.
This is the _reverse_player_aggregates method implementation for Bug Fix B4.

Add this method to the MatchArchiver class in match_archiver.py after the _save_partnerships_to_db method.
"""

def _reverse_player_aggregates(self, scorecards: List) -> None:
    """
    Reverse player aggregate stats from old scorecards before re-simulation.
    
    Bug Fix B4: When re-simulating, we must reverse the old stats before they're deleted,
    otherwise the new stats will be added on top of the old stats, causing inflation.
    
    Args:
        scorecards: List of MatchScorecard objects to reverse
    """
    if not scorecards:
        return
    
    from models import DBPlayer  # Import here to avoid circular imports
    
    updated_players = set()
    
    for card in scorecards:
        player = DBPlayer.query.get(card.player_id)
        if not player:
            continue
        
        # Decrement matches_played only once per player
        if card.player_id not in updated_players:
            player.matches_played = max(0, player.matches_played - 1)
            updated_players.add(card.player_id)
        
        # Reverse batting stats
        if card.record_type == "batting":
            player.total_runs = max(0, player.total_runs - (card.runs or 0))
            player.total_balls_faced = max(0, player.total_balls_faced - (card.balls or 0))
            player.total_fours = max(0, player.total_fours - (card.fours or 0))
            player.total_sixes = max(0, player.total_sixes - (card.sixes or 0))
            
            # Reverse fifties/centuries (simplified - exact reversal is complex)
            if card.runs >= 50 and card.runs < 100:
                player.total_fifties = max(0, player.total_fifties - 1)
            elif card.runs >= 100:
                player.total_centuries = max(0, player.total_centuries - 1)
            
            # Reverse not outs
            if not card.is_out and card.balls and card.balls > 0:
                player.not_outs = max(0, player.not_outs - 1)
            
            # Note: highest_score cannot be simply reversed - would need to recalculate
            # from all remaining scorecards. For production, consider a full recalculation.
        
        # Reverse bowling stats
        if card.record_type == "bowling":
            player.total_wickets = max(0, player.total_wickets - (card.wickets or 0))
            player.total_balls_bowled = max(0, player.total_balls_bowled - (card.balls_bowled or 0))
            player.total_runs_conceded = max(0, player.total_runs_conceded - (card.runs_conceded or 0))
            player.total_maidens = max(0, player.total_maidens - (card.maidens or 0))
            
            # Reverse five wicket hauls
            if card.wickets and card.wickets >= 5:
                player.five_wicket_hauls = max(0, player.five_wicket_hauls - 1)
            
            # Note: best_bowling_wickets/runs cannot be simply reversed
            # Would need recalculation from remaining scorecards
    
    self.logger.info(f"Reversed aggregate stats for {len(updated_players)} players during re-simulation")
