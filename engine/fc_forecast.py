"""First-class forward model: what happens next, given a chosen tempo.

Two pieces, both pure and deterministic:

1. `frontier()` — the fitted rate/risk response. For a state and a chosen
   aggression it returns runs per over AND wickets per over. Aggression is
   the axis the old declaration estimator lacked: without it a captain can
   only buy runs with overs, which is why it batted on to unreachable
   targets. Coefficients are fitted from the ball engine itself by
   scripts/fc_fit_frontier.py, so this cannot drift into being a second,
   independent model of cricket.

2. `innings_forecast()` — exact distribution of an innings outcome by
   dynamic programming over (overs remaining, wickets in hand, runs). No
   sampling, so a decision is a pure function of state and consumes no RNG.

Nothing here samples deliveries. Delivery probabilities remain the sole
responsibility of engine/ball_outcome.py.
"""
import functools
import json
import math
import os

import numpy as np

from engine.fc_decision_budget import check_budget

_MODEL_PATH = os.path.join(os.path.dirname(__file__), "data", "fc_frontier.json")

with open(_MODEL_PATH) as _handle:
    MODEL = json.load(_handle)

PITCHES = MODEL["pitches"]
BALL_AGE_EDGES = MODEL["ball_age_edges"]
N_AGE_BUCKETS = MODEL["n_age_buckets"]

# Runs in one over are over-dispersed relative to Poisson: boundaries put
# most of the variance in. Var/mean ~ 2.5 follows from the per-ball outcome
# spread in the FC scoring matrices (a four contributes 16 to E[x^2]).
# Declarations live in the tails of the runs distribution, so this matters
# more than the mean does.
OVER_DISPERSION = 2.5

# Guard rails. The frontier is fitted over the sweep's range; outside it the
# exponential form extrapolates badly, so clamp rather than trust it.
MIN_RATE, MAX_RATE = 1.0, 8.0
MIN_WICKET_RATE, MAX_WICKET_RATE = 0.005, 1.0


def ball_age_bucket(overs):
    for index, edge in enumerate(BALL_AGE_EDGES):
        if overs < edge:
            return index
    return N_AGE_BUCKETS - 1


def _design(pitch, wear, age_bucket, strength_diff, aggression):
    pitch = pitch if pitch in PITCHES else "Hard"
    row = {f"pitch[{pitch}]": 1.0, f"aggression[{pitch}]": aggression,
           "wear": max(0.0, min(1.0, wear)),
           "strength_diff_per_100": strength_diff / 100.0}
    if age_bucket:
        row[f"ball_age[{age_bucket}]"] = 1.0
    return row


def _predict(coefficients, row):
    return math.exp(sum(coefficients[k] * v for k, v in row.items()))


def frontier(pitch, wear, ball_age_overs, strength_diff, aggression):
    """Runs per over and wickets per over at a chosen aggression.

    aggression runs -1 (full rearguard) to +1 (maximum attack).
    strength_diff is batting strength minus bowling strength, 0-100 scale.
    Both outputs rise with aggression; how steeply the wicket rate rises is
    pitch-specific, which is the whole point of the interaction terms.
    """
    aggression = max(-1.0, min(1.0, float(aggression)))
    row = _design(pitch, wear, ball_age_bucket(ball_age_overs),
                  strength_diff, aggression)
    rate = _predict(MODEL["log_rate"], row)
    wicket_rate = _predict(MODEL["log_wicket_rate"], row)
    return (max(MIN_RATE, min(MAX_RATE, rate)),
            max(MIN_WICKET_RATE, min(MAX_WICKET_RATE, wicket_rate)))


@functools.lru_cache(maxsize=256)
def _frontier_basis(pitch, aggression):
    """frontier() split into the parts innings_forecast can hoist.

    The fitted frontier is log-linear, and inside one dynamic programme the
    only term that moves with wickets in hand is the strength one. So

        exp(intercept + wear*w + age + sd_coef*sd/100)
            == exp(intercept + wear*w + age) * exp(sd_coef*sd/100)

    splits into a factor that changes once per over and a factor that is
    fixed for the whole innings. Hoisting it turns two dict builds and two
    generator sums per (over, wicket) into a single multiply.

    Returns, for runs then wickets: (intercept, wear coefficient, per-age-
    bucket offsets, strength coefficient). `aggression` must already be
    clamped by the caller, exactly as frontier() clamps it.
    """
    pitch = pitch if pitch in PITCHES else "Hard"
    out = []
    for block in (MODEL["log_rate"], MODEL["log_wicket_rate"]):
        intercept = (block[f"pitch[{pitch}]"]
                     + block[f"aggression[{pitch}]"] * aggression)
        # _design only emits a ball_age term for a non-zero bucket, so
        # bucket 0 contributes nothing.
        ages = tuple(0.0 if b == 0 else block.get(f"ball_age[{b}]", 0.0)
                     for b in range(N_AGE_BUCKETS))
        out.append((intercept, block["wear"], ages,
                    block["strength_diff_per_100"]))
    return tuple(out)


@functools.lru_cache(maxsize=4096)
def _over_runs_pmf(rate_key, bucket):
    """Discretised negative-binomial runs for one over, on the runs grid."""
    mean = rate_key / 100.0
    phi = OVER_DISPERSION
    size = mean / (phi - 1.0)
    prob = 1.0 / phi
    limit = int(max(12.0, mean * 6.0))
    counts = np.arange(limit + 1)
    log_pmf = (
        np.array([math.lgamma(size + c) - math.lgamma(size) - math.lgamma(c + 1.0)
                  for c in counts])
        + size * math.log(prob) + counts * math.log1p(-prob))
    pmf = np.exp(log_pmf)
    pmf /= pmf.sum()
    # Fold onto the bucketed runs axis by splitting each count between its
    # two neighbouring buckets. Flooring instead would shave an average of
    # (bucket-1)/2 runs off EVERY over, which compounds to hundreds of runs
    # across a first-class innings.
    folded = np.zeros(limit // bucket + 2)
    position = counts / bucket
    lower = np.floor(position).astype(int)
    fraction = position - lower
    np.add.at(folded, lower, pmf * (1.0 - fraction))
    np.add.at(folded, lower + 1, pmf * fraction)
    return folded / folded.sum()


@functools.lru_cache(maxsize=8192)
def _as_pmf_array(pmf):
    """ndarray view of a _wicket_pmf tuple. Cached on the tuple itself, which
    _wicket_pmf already memoises, so the conversion happens once per distinct
    (rate, wickets) pair rather than once per over."""
    array = np.asarray(pmf)
    # Shared out of a cache, so nobody gets to write through it.
    array.flags.writeable = False
    return array


@functools.lru_cache(maxsize=8192)
def _wicket_pmf(wicket_rate_key, wickets_in_hand):
    """Wickets lost in one over: Poisson, truncated at wickets in hand."""
    mean = wicket_rate_key / 1000.0
    probabilities = []
    for k in range(wickets_in_hand):
        probabilities.append(math.exp(-mean) * mean**k / math.factorial(k))
    probabilities.append(max(0.0, 1.0 - sum(probabilities)))
    return tuple(probabilities)


def innings_forecast(*, pitch, overs_available, wickets_in_hand,
                     strength_by_wickets, aggression, wear_start=0.3,
                     wear_end=None, ball_age_start=0.0, new_ball_overs=80,
                     target=None, max_runs=900, bucket=5,
                     track_absorption=False):
    """Exact distribution of an innings played at a fixed aggression.

    strength_by_wickets maps wickets in hand (1-10) to batting-minus-bowling
    strength, so the tail is modelled as the tail rather than as the XI's
    average. Returns probabilities and the runs distribution; when `target`
    is given, reaching it is absorbing and `p_target` is the chase-win
    probability.
    """
    overs_available = int(max(0, math.floor(overs_available)))
    wickets_in_hand = int(max(0, min(10, wickets_in_hand)))
    wear_end = wear_start if wear_end is None else wear_end
    size = max_runs // bucket + 1
    target_bucket = None if target is None else int(math.ceil(target / bucket))

    # state[w] is the runs distribution while w wickets remain in hand.
    state = np.zeros((wickets_in_hand + 1, size))
    all_out = np.zeros(size)
    # Mass absorbed into "all out" at each over, with its runs distribution.
    # This is what lets a declaration price the overs the opposition's
    # innings consumes as well as the runs it makes.
    absorption = np.zeros((overs_available, size)) if track_absorption else None
    p_target = 0.0
    if wickets_in_hand == 0 or overs_available == 0:
        state[wickets_in_hand, 0] = 1.0
        return _summarise(state.sum(axis=0), all_out, p_target, bucket, 0.0,
                          absorption)
    state[wickets_in_hand, 0] = 1.0

    # Hoisted frontier (see _frontier_basis). The strength factor is fixed
    # for the whole innings; only the (wear, ball age) factor moves, once
    # per over rather than once per over per wicket.
    clamped_aggression = max(-1.0, min(1.0, float(aggression)))
    (r_intercept, r_wear_coef, r_ages, r_sd_coef), \
        (k_intercept, k_wear_coef, k_ages, k_sd_coef) = _frontier_basis(
            pitch, clamped_aggression)
    rate_strength = [0.0] * (wickets_in_hand + 1)
    wicket_strength = [0.0] * (wickets_in_hand + 1)
    for w in range(1, wickets_in_hand + 1):
        sd = strength_by_wickets.get(w, 0.0) / 100.0
        rate_strength[w] = math.exp(r_sd_coef * sd)
        wicket_strength[w] = math.exp(k_sd_coef * sd)

    expected_overs = 0.0
    for over in range(overs_available):
        check_budget()
        progress = over / max(1, overs_available - 1) if overs_available > 1 else 1.0
        wear = wear_start + (wear_end - wear_start) * progress
        age = (ball_age_start + over) % max(1, new_ball_overs)
        # One reduction for every wicket state, instead of one per state
        # plus two more inside the loop below.
        row_sums = state.sum(axis=1)
        live = row_sums.sum()
        if live < 1e-9:
            break
        expected_overs += live

        clamped_wear = max(0.0, min(1.0, wear))
        age_bucket = ball_age_bucket(age)
        base_rate = math.exp(
            r_intercept + r_wear_coef * clamped_wear + r_ages[age_bucket])
        base_wicket_rate = math.exp(
            k_intercept + k_wear_coef * clamped_wear + k_ages[age_bucket])

        nxt = np.zeros_like(state)
        for w in range(1, wickets_in_hand + 1):
            row_mass = row_sums[w]
            if row_mass < 1e-12:
                continue
            row = state[w]
            rate = max(MIN_RATE, min(MAX_RATE, base_rate * rate_strength[w]))
            wicket_rate = max(MIN_WICKET_RATE,
                              min(MAX_WICKET_RATE,
                                  base_wicket_rate * wicket_strength[w]))
            runs_pmf = _over_runs_pmf(int(round(rate * 100)), bucket)
            wickets_pmf = _wicket_pmf(int(round(wicket_rate * 1000)), w)

            spread = np.convolve(row, runs_pmf)[:size]
            # Runs beyond the grid pile up in the last bucket rather than
            # vanishing, so total probability is conserved.
            spread[-1] += row_mass - spread.sum()
            if target_bucket is not None and target_bucket < size:
                p_target += spread[target_bucket:].sum()
                spread = spread.copy()
                spread[target_bucket:] = 0.0

            # Scatter the over's outcome across the surviving wicket
            # states. wickets_pmf has w+1 entries, so `lost` runs 0..w and
            # `remaining = w - lost` lands on rows w..1 for lost < w, and on
            # "all out" only for lost == w. Each of those rows is written
            # exactly once per w, so the whole fan-out is one outer product
            # — the same additions in the same order, with the per-`lost`
            # Python round trip removed. Rows whose probability is zero add
            # a zero row, which is what the old `continue` did.
            pmf = _as_pmf_array(wickets_pmf)
            # Broadcast rather than np.outer: both operands are already 1-D
            # and contiguous, so outer's ravel/reshape wrapper is pure
            # overhead at this call count.
            nxt[1:w + 1] += pmf[w - 1::-1, None] * spread
            tail = pmf[w]
            if tail > 0.0:
                absorbed = spread * tail
                all_out += absorbed
                if absorption is not None:
                    absorption[over] += absorbed
        state = nxt

    return _summarise(state.sum(axis=0), all_out, p_target, bucket,
                      expected_overs, absorption)


def _summarise(survived, all_out, p_target, bucket, expected_overs, absorption):
    distribution = survived + all_out
    mass = distribution.sum() + p_target
    runs_axis = np.arange(len(distribution)) * bucket
    mean = float((distribution * runs_axis).sum())
    return dict(
        runs_pmf=distribution, survived_pmf=survived, all_out_pmf=all_out,
        bucket=bucket, runs_axis=runs_axis, absorption=absorption,
        p_all_out=float(all_out.sum()), p_target=float(p_target),
        expected_runs=mean, expected_overs=float(expected_overs),
        total_mass=float(mass))


def chase_outcome(forecast, target):
    """Read (win, loss, draw) for a target off a no-target innings forecast.

    Runs only ever increase, so passing the target at any point is the same
    event as finishing on or above it — one forecast therefore prices every
    target, instead of needing a fresh evaluation per candidate.
    """
    axis = forecast["runs_axis"]
    reached = axis >= target
    win = float(forecast["runs_pmf"][reached].sum())
    loss = float(forecast["all_out_pmf"][~reached].sum())
    draw = float(forecast["survived_pmf"][~reached].sum())
    return win, loss, draw


def chase_curves(forecast):
    """(win, loss, draw) for EVERY target at once, indexed by runs bucket.

    One dynamic programme prices every candidate target, so a declaration
    can evaluate its whole option set without a fresh evaluation per target.
    """
    reverse = np.cumsum(forecast["runs_pmf"][::-1])[::-1]
    win = np.concatenate([reverse, [0.0]])
    loss = np.concatenate([[0.0], np.cumsum(forecast["all_out_pmf"])])
    draw = np.concatenate([[0.0], np.cumsum(forecast["survived_pmf"])])
    return win, loss, draw


def probability_runs_below(forecast, threshold):
    """P(the innings finishes short of `threshold` runs)."""
    axis = forecast["runs_axis"]
    return float(forecast["runs_pmf"][axis < threshold].sum())
