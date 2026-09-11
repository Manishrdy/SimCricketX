"""Match-level FC benchmark observations. No random sampling or engine edits."""

from collections import Counter, defaultdict
import statistics


class MatchMetrics:
    def __init__(self, match):
        self.match = match
        self.deliveries = 0
        self.legal = Counter()
        self.runs = Counter()
        self.extras_events = Counter()
        self.extras_runs = Counter()
        self.intent = Counter()
        self.wicket_balls = defaultdict(list)
        self.collapse_episodes = Counter()
        self.in_collapse = defaultdict(bool)
        self.tail_balls = self.specialist_balls = self.tail_runs = 0
        self.chase = None
        self.original = match._update_partnership_tracking
        match._update_partnership_tracking = self.observe
        self.partnerships = []
        self.original_save = match._save_partnership
        match._save_partnership = self.save_partnership

    def save_partnership(self, wicket_type=None):
        innings = self.match.fc_innings
        count = len(self.match.fc_innings_partnerships.get(innings, []))
        self.original_save(wicket_type)
        saved = self.match.fc_innings_partnerships.get(innings, [])
        if len(saved) > count:
            self.partnerships.append(
                dict(saved[-1], unfinished=wicket_type == "not_out")
            )

    def observe(self, outcome):
        m = self.match
        d = outcome["delivery"]
        n = m.fc_innings
        self.deliveries += 1
        self.legal[n] += int(d["legal"])
        self.runs[n] += d["total_runs"]
        for kind, runs in d["extras"].items():
            self.extras_events[kind] += 1
            self.extras_runs[kind] += runs
        state = m._fc_build_match_state()
        for mode in ("attack", "survival", "neutral"):
            self.intent[mode] += state["intent"][mode]
        if n == 4 and self.chase is None:
            self.chase = dict(
                target=m.target,
                overs_available=m._fc_overs_remaining_today()
                + max(0, m.fmt.days - m.fc_day) * m.fmt.overs_per_day,
                wickets_available=10 - m.wickets,
            )
        if outcome.get("batter_out"):
            self.wicket_balls[n].append(self.legal[n])
        # A collapse episode starts when a window first reaches 3 wickets.
        window = [b for b in self.wicket_balls[n] if self.legal[n] - b < 30]
        active = len(window) >= 3
        if active and not self.in_collapse[n]:
            self.collapse_episodes[n] += 1
        self.in_collapse[n] = active
        if m.wickets >= 8:
            self.tail_balls += 1
            self.specialist_balls += int(
                m.current_striker.get("batting_rating", 0) >= 60
            )
            self.tail_runs += d["total_runs"]
        self.original(outcome)

    def finish(self):
        return dict(
            deliveries=self.deliveries,
            legal_balls=dict(self.legal),
            innings_runs=dict(self.runs),
            extras_events=dict(self.extras_events),
            extras_runs=dict(self.extras_runs),
            intent_exposure=dict(self.intent),
            collapse_episodes=dict(self.collapse_episodes),
            partnerships=list(self.partnerships),
            tail_deliveries=self.tail_balls,
            specialist_tail_deliveries=self.specialist_balls,
            final_two_wicket_runs=self.tail_runs,
            chase=self.chase,
        )


def distribution(values):
    values = sorted(values)
    if not values:
        return dict(n=0, mean=None, median=None, p10=None, p90=None)

    def quantile(p):
        at = (len(values) - 1) * p
        lo = int(at)
        hi = min(lo + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (at - lo)

    return dict(
        n=len(values),
        mean=statistics.mean(values),
        median=statistics.median(values),
        p10=quantile(0.1),
        p90=quantile(0.9),
    )


def summarize(matches):
    """Uncertainty uses independent matches, not individual balls/innings."""
    rpos = [m["total_runs"] / m["total_overs"] for m in matches if m["total_overs"]]
    draws = sum(m["match_status"] == "drawn" for m in matches)
    n = len(matches)
    p = draws / n if n else 0
    # Wilson 95% interval, informative even when no draws are observed.
    z = 1.96
    denom = 1 + z * z / max(n, 1)
    center = (p + z * z / (2 * max(n, 1))) / denom
    half = z * ((p * (1 - p) / max(n, 1) + z * z / (4 * max(n, 1) ** 2)) ** 0.5) / denom
    innings = [i for m in matches for i in m["innings"].values()]
    knocks = [k for m in matches for k in m["knocks"] if k["balls"] or k["wicket_type"]]
    reached50 = [k for k in knocks if k["runs"] >= 50]
    dismissed50 = [k for k in reached50 if k["wicket_type"]]
    chases = [m for m in matches if m["metrics"]["chase"]]
    deliveries = max(1, sum(m["metrics"]["deliveries"] for m in matches))
    extra_events, extra_runs = Counter(), Counter()
    for match in matches:
        extra_events.update(match["metrics"]["extras_events"])
        extra_runs.update(match["metrics"]["extras_runs"])
    stands = [s for m in matches for s in m["metrics"]["partnerships"]]
    return dict(
        matches=n,
        draws=draws,
        draw_fraction=p,
        draw_95_interval=[center - half, center + half],
        match_rpo=distribution(rpos),
        extras_per_100_deliveries={
            kind: dict(
                events=100 * extra_events[kind] / deliveries,
                runs=100 * extra_runs[kind] / deliveries,
            )
            for kind in ("No Ball", "Wide", "Byes", "Leg Bye")
        },
        partnerships_by_wicket={
            str(w): dict(
                runs=distribution(
                    [s["runs"] for s in stands if s["wicket_number"] == w]
                ),
                balls=distribution(
                    [s["balls"] for s in stands if s["wicket_number"] == w]
                ),
                unfinished_observed=sum(
                    s.get("unfinished", False)
                    for s in stands
                    if s["wicket_number"] == w
                ),
                unclassified=sum("unfinished" not in s for s in stands if s["wicket_number"] == w),
            )
            for w in range(11)
        },
        batting_by_position={
            str(pos): dict(
                scores=distribution([k["runs"] for k in knocks if k["pos"] == pos]),
                not_outs=sum(not k["wicket_type"] for k in knocks if k["pos"] == pos),
            )
            for pos in range(1, 12)
        },
        specialist_tail_strike_fraction=(
            sum(m["metrics"]["specialist_tail_deliveries"] for m in matches)
            / max(1, sum(m["metrics"]["tail_deliveries"] for m in matches))
        ),
        innings_runs=distribution([i["runs"] for i in innings]),
        innings_by_number={
            str(num): distribution(
                [m["innings"][num]["runs"] for m in matches if num in m["innings"]]
            )
            for num in (1, 2, 3, 4)
        },
        results=dict(Counter(m["result_type"] for m in matches)),
        innings_played=len(knocks),
        ducks=sum(k["runs"] == 0 and bool(k["wicket_type"]) for k in knocks),
        hundreds=sum(k["runs"] >= 100 for k in knocks),
        doubles=sum(k["runs"] >= 200 for k in knocks),
        fifty_conversion=dict(
            reached50=len(reached50),
            hundreds=sum(k["runs"] >= 100 for k in reached50),
            dismissed_reached50=len(dismissed50),
            dismissed_hundreds=sum(k["runs"] >= 100 for k in dismissed50),
            censored_reached50=len(reached50) - len(dismissed50),
        ),
        chases=dict(
            n=len(chases),
            wins=sum(m["result_type"] == "wickets" for m in chases),
            draws=sum(m["match_status"] == "drawn" for m in chases),
        ),
        final_two_wicket_runs=distribution(
            [m["metrics"]["final_two_wicket_runs"] for m in matches]
        ),
        collapse_episodes=distribution(
            [sum(m["metrics"]["collapse_episodes"].values()) for m in matches]
        ),
    )


def validate_batch(matches, minimum=40):
    """Broad product regression gates, not estimates of real-world cricket.

    Keep cohorts separated by duration, pitch, squad and weather policy. First
    innings all-out targets remain in test_fc_calibration; declarations censor
    these full-match scores and require a separate envelope.
    """
    if len(matches) < minimum:
        raise AssertionError(f"Need {minimum} matches, received {len(matches)}")
    for match in matches:
        assert match["completed"], "incomplete match"
        metrics = match["metrics"]
        assert match["total_runs"] == sum(metrics["innings_runs"].values())
        assert match["total_runs"] == sum(k["runs"] for k in match["knocks"]) + sum(metrics["extras_runs"].values())
        assert round(match["total_overs"] * 6) == sum(metrics["legal_balls"].values())
        assert (
            abs(sum(metrics["intent_exposure"].values()) - metrics["deliveries"]) < 1e-6
        )
        for innings in match["innings"].values():
            assert 0 <= innings["wickets"] <= 10
            assert innings["ending"] in ("all_out", "declared", "target", "time")
    summary = summarize(matches)
    assert 2.0 <= summary["match_rpo"]["mean"] <= 5.0, summary
    assert 80 <= summary["innings_runs"]["median"] <= 650, summary
    assert summary["innings_runs"]["p90"] < 1000, summary
    assert summary["final_two_wicket_runs"]["mean"] < 220, summary
    assert 0.1 <= summary["collapse_episodes"]["mean"] <= 12, summary
    n = max(1, summary["innings_played"])
    assert 0.03 <= summary["ducks"] / n <= 0.25, summary
    assert 0.005 <= summary["hundreds"] / n <= 0.18, summary
    if matches[0]["inputs"]["pitch"] == "Green":
        assert summary["draw_fraction"] < 0.8, summary
    return summary
