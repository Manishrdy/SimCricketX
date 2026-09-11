"""FC extras and resolved-delivery accounting (MCC 2017 Code, 2022 edition).

The public outcome dictionary remains compatible with existing consumers.
`delivery` adds exact accounting and running components before any observer
sees the event. Penalty runs never change ends.
"""

from engine.fc_batting_intent import bounded


def extra_profile(bowler, fielding_team=None):
    """Conditional extras mix; keeping changes only bye mass, not penalties.

    Fielding rating of the designated keeper is the keeping proxy. Neutral is
    68 (the calibration squad), also used when no keeper is supplied.
    """
    pace = bowler.get("bowling_type", "") not in {
        "Off spin",
        "Leg spin",
        "Finger spin",
        "Wrist spin",
    }
    tired = 1 - bounded(bowler.get("_fc_fatigue", 1))
    weights = {
        "Wide": 0.18 * (1 + tired * 0.4),
        "No Ball": 0.18 * (1.08 if pace else 0.85) * (1 + tired * 0.6),
        "Leg Bye": 0.38,
        "Byes": 0.26,
    }
    total = sum(weights.values())
    weights = {k: v / total for k, v in weights.items()}
    keeper = next((p for p in fielding_team or [] if p.get("is_wicketkeeper")), None)
    if keeper is None:
        keeper = next(
            (p for p in fielding_team or [] if p.get("role") == "Wicketkeeper"), {}
        )
    quality = keeper.get("fielding_rating")
    quality = 68 if quality is None else bounded(quality, 0, 100)
    weights["Byes"] *= 1 - (quality - 68) / 200
    return weights


def apply_keeper_mass(weights, profile):
    """Transfer prevented byes to dots; unrelated outcomes keep their mass."""
    result = dict(weights)
    original = result.get("Extras", 0)
    change = original * (sum(profile.values()) - 1)
    change = min(change, result.get("Dot", 0))
    result["Extras"] = original + change
    result["Dot"] = result.get("Dot", 0) - change
    return result


def resolve_delivery(outcome, secondary=None, runs_needed=None):
    """Finalize a single delivery, including no-ball secondary contact.

    Secondary is already sampled with extras disabled. An optional non_bat_type
    marks a ball beating the bat. Run-out is the supported lawful dismissal on
    a no-ball; all other sampled dismissal modes are invalidated.
    """
    result = dict(outcome)
    kind = result.get("extra_type") if result.get("is_extra") else None
    total = int(result.get("runs", 0))
    bat = total if not kind else int(result.get("bat_runs", 0))
    components = {}
    completed = total if total not in (4, 6) else 0
    if kind == "No Ball":
        contact = dict(secondary or {})
        if secondary is not None:
            invalid = (
                contact.get("batter_out") and contact.get("wicket_type") != "Run Out"
            )
            bat = (
                0
                if invalid or contact.get("non_bat_type")
                else int(contact.get("runs", 0))
            )
            result["batter_out"] = bool(contact.get("batter_out") and not invalid)
            result["wicket_type"] = "Run Out" if result["batter_out"] else None
            for key in ("dismissed_end", "fielder_name"):
                if key in contact:
                    result[key] = contact[key]
            nonbat = int(contact.get("runs", 0)) if contact.get("non_bat_type") else 0
            components = {"No Ball": 1}
            if nonbat:
                components[contact["non_bat_type"]] = nonbat
            total = 1 + bat + nonbat
            completed = contact.get(
                "completed_runs", (bat + nonbat) if bat + nonbat not in (4, 6) else 0
            )
            result["bat_description"] = (
                contact.get("description", "")
                if not invalid
                else "No ball: dismissal invalidated."
            )
        else:
            components = dict(
                result.get("extra_components") or {"No Ball": total - bat}
            )
            completed = result.get("completed_runs", bat if bat not in (4, 6) else 0)
    elif kind:
        components = {kind: total}
        completed = (
            max(0, total - 1) if kind == "Wide" else (0 if total == 4 else total)
        )
        if kind == "Wide" and total == 5:
            completed = 0  # boundary wide
    # The penalty can itself win the match; no later contact or dismissal.
    if kind in ("Wide", "No Ball") and runs_needed == 1:
        total, bat, completed, components = 1, 0, 0, {kind: 1}
        result.update(batter_out=False, wicket_type=None)
    elif runs_needed is not None and 0 < runs_needed <= total and completed:
        # Runs completed before a putative run-out already ended the match.
        penalty = 1 if kind in ("Wide", "No Ball") else 0
        completed = max(0, runs_needed - penalty)
        total = runs_needed
        if bat:
            bat = completed
            components = {kind: penalty} if penalty else {}
        elif kind:
            components = (
                {kind: total}
                if kind != "No Ball"
                else {
                    "No Ball": 1,
                    next((k for k in components if k != "No Ball"), "Byes"): completed,
                }
            )
        result.update(batter_out=False, wicket_type=None)
    components = {k: v for k, v in components.items() if v}
    bowler_runs = bat + components.get("Wide", 0) + components.get("No Ball", 0)
    result.update(
        runs=total,
        bat_runs=bat,
        extra_components=components,
        completed_runs=int(completed),
    )
    if not result.get("batter_out"):
        result["type"] = "extra" if kind else "run"
    result["delivery"] = dict(
        total_runs=total,
        bat_runs=bat,
        extras=components,
        bowler_runs=bowler_runs,
        completed_runs=int(completed),
        legal=kind not in ("Wide", "No Ball"),
        batter_faced=kind != "Wide",
        wicket_type=result.get("wicket_type"),
        dismissed_end=result.get("dismissed_end"),
    )
    if total != bat + sum(components.values()):
        raise ValueError("FC delivery runs do not reconcile")
    return result
