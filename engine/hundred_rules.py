"""Shared league-position decisions for Hundred knockout matches."""


def league_position_decision(context, home_name, away_name, reason):
    """Return a winner and display text, or None without valid saved positions."""
    if not isinstance(context, dict):
        return None
    home = context.get('home_position')
    away = context.get('away_position')
    if (type(home) is not int or type(away) is not int
            or min(home, away) < 1 or home == away):
        return None
    winner_is_home = home < away
    winner = home_name if winner_is_home else away_name
    stage = context.get('stage')
    action = ('win the Eliminator' if stage == 'eliminator' else
              'are awarded the trophy' if stage == 'final' else 'advance')
    return winner_is_home, f'{winner} {action} on league position {reason}'
