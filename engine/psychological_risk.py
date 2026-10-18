"""Shared T20/List A wicket-risk composition; tuning is intentionally explicit.

Independent skill/conditions remain outside this ledger. Only repeated positive
psychological wicket modifiers are pooled; protective modifiers retain full force.
"""
import logging
import math

logger = logging.getLogger(__name__)
OVERLAP_EXPONENT = 0.30
OVERLAP_ALLOWANCE = 1.25  # retain ordinary overlap before discounting excess
COLLAPSE_RECOVERY_GRACE = 3
COLLAPSE_RECOVERY_HALF_LIFE = 12.0  # wicket-free legal deliveries
REBUILD_RECOVERY_HALF_LIFE = 6.0
PSYCHOLOGICAL_ODDS_KNEE = 4.0
PSYCHOLOGICAL_ODDS_LIMIT = 7.0
LIMITED_OVERS_WICKET_CAP = 0.40  # emergency backstop, not routine balancing


def combine_risk_factors(factors):
    """Strongest positive effect in full; overlap in log space. Order independent."""
    positive = [factor for factor in factors if factor > 1.0]
    if not positive:
        return 1.0
    strongest = max(positive)
    overlap = math.prod(positive) / strongest
    return strongest * min(overlap, OVERLAP_ALLOWANCE) * (
        max(1.0, overlap / OVERLAP_ALLOWANCE) ** OVERLAP_EXPONENT)


class WicketRisk:
    """Per-delivery ledger, never stored in Match or serialized game state."""
    def __init__(self):
        self.groups = {}

    def add(self, group, factor):
        if factor > 1.0:
            self.groups.setdefault(group, []).append(factor)

    def multiplier(self):
        multiplier = math.prod(combine_risk_factors(v) for v in self.groups.values())
        logger.debug('[PsychologicalRisk] groups=%s combined=%.3f', self.groups, multiplier)
        return multiplier


def collapse_recovery(history, rebuilding=False):
    """Recover after the last wicket; illegal balls do not advance the clock."""
    legal = 0
    for event in reversed(history):
        if event.get('is_wicket') or event.get('label') == 'Wicket':
            half_life = REBUILD_RECOVERY_HALF_LIFE if rebuilding else COLLAPSE_RECOVERY_HALF_LIFE
            return 2.0 ** (-max(0, legal - (0 if rebuilding else COLLAPSE_RECOVERY_GRACE)) / half_life)
        if event.get('is_legal', event.get('label') not in ('Wide', 'NoBall')):
            legal += 1
    return 1.0  # no recent collapse; fresh opening partnerships still have nerves


def limit_psychological_odds(weights, baseline):
    """Soft-limit situational wicket *odds*, including scoring suppression.

    The baseline includes skill, pitch, phase and individual settling-in. Below
    the knee nothing changes. This preserves the non-wicket outcome ratios.
    """
    wicket = weights.get('Wicket', 0.0)
    others = sum(v for k, v in weights.items() if k != 'Wicket')
    base_wicket = baseline.get('Wicket', 0.0)
    base_others = sum(v for k, v in baseline.items() if k != 'Wicket')
    if min(wicket, others, base_wicket, base_others) <= 0:
        return weights
    base_odds = base_wicket / base_others
    uplift = (wicket / others) / base_odds
    if uplift > PSYCHOLOGICAL_ODDS_KNEE:
        span = PSYCHOLOGICAL_ODDS_LIMIT - PSYCHOLOGICAL_ODDS_KNEE
        limited = PSYCHOLOGICAL_ODDS_KNEE + span * (-math.expm1(
            -(uplift - PSYCHOLOGICAL_ODDS_KNEE) / span))
        weights = dict(weights, Wicket=base_odds * limited * others)
        logger.debug('[PsychologicalRisk] odds uplift %.3f -> %.3f', uplift, limited)
    return weights
