"""Versioned team captaincy: bounded choices, never player-rating bonuses.

Only observable match state enters this policy. All stochastic selection is
performed at committed engine boundaries, never by status/UI readers.
"""
from copy import deepcopy
from typing import TypedDict, NotRequired

MODEL_VERSION = 1
ATTRIBUTES = (
    'match_awareness', 'batting_management', 'bowling_management',
    'adaptability', 'composure', 'batting_aggression', 'bowling_aggression',
)
LABELS = {key: key.replace('_', ' ').capitalize() for key in ATTRIBUTES}
PRESETS = {
    name: dict(schema_version=1, **{k: aggression if k.endswith('_aggression') else 50
                                for k in ATTRIBUTES})
    for name, aggression in [('Balanced', 50), ('Attacking', 75), ('Conservative', 25)]
}


class CaptaincyConfig(TypedDict):
    schema_version: int
    match_awareness: int
    batting_management: int
    bowling_management: int
    adaptability: int
    composure: int
    batting_aggression: int
    bowling_aggression: int


class CaptainContext(TypedDict):
    team: str
    innings: str
    legal_balls: int
    balls_remaining: int
    score: int
    wickets: int
    runs_needed: int | None
    par_rate: float  # per legal ball, independent of five/six-ball units
    recent: list[dict]
    phase: str
    pitch: str
    pressure: float
    fc_intent: NotRequired[dict]


class CaptainDecision(TypedDict):
    id: str
    team: str
    innings: str
    legal_balls: int
    action: str
    previous_action: str | None
    trigger: str
    reason: str
    evidence: dict


class CaptainState(TypedDict):
    batting: str
    bowling: str
    reservations: dict[str, int]
    recent: list[dict]
    last_evaluation: list | None
    last_change: dict[str, int]
    recommendation: str | None
    selection_boundary: list | None
    review: str | None
    score_memory: dict


def default_config() -> CaptaincyConfig:
    return deepcopy(PRESETS['Balanced'])


def parse_config(value=None) -> CaptaincyConfig:
    if value is None:
        return default_config()
    if not isinstance(value, dict):
        raise ValueError('Captaincy configuration must be an object.')
    version = value.get('schema_version', 1)
    if type(version) is not int or version != 1:
        raise ValueError('Unsupported captaincy schema version.')
    result = default_config()
    for key in ATTRIBUTES:
        if key in value:
            rating = value[key]
            if type(rating) is not int or not 0 <= rating <= 100:
                raise ValueError(f'{LABELS[key]} must be an integer from 0 to 100.')
            result[key] = rating
    return result


def preset_name(config):
    value = parse_config(config)
    return next((name for name, preset in PRESETS.items() if value == preset), 'Custom')


def _clamp(value, low=0., high=1.):
    return max(low, min(high, value))


def _scores(actions, desired):
    # Close alternatives permit observable decision errors; distant choices do
    # not win merely because a weak captain got a noisy observation.
    return {action: _clamp(1 - .24 * abs(i - desired)) for i, action in enumerate(actions)}


def batting_scores(context: CaptainContext, config: CaptaincyConfig):
    needed = context['runs_needed']
    left = max(1, context['balls_remaining'])
    rate = max(.2, context['par_rate'])
    wickets = context['wickets']
    recent = context['recent']
    cluster = sum(e.get('wicket', False) for e in recent)
    if 'fc_intent' in context:
        intent = context['fc_intent']
        desired = 1 + intent.get('attack', 0) * 1.3 - intent.get('survival', 0)
        reason = intent.get('reason', 'accumulate')
    elif needed is not None:
        ratio = needed / left / rate
        desired = 1 if ratio < 1.05 else 2 if ratio < 1.55 else 3
        reason = 'controlled_chase' if ratio < 1.05 else 'required_rate'
        if ratio < .6:
            desired = .7
    else:
        desired = 2 if context['phase'] == 'death' else 1
        reason = 'phase'
    # A collapse does not justify giving up a chase that demands acceleration.
    if 'fc_intent' not in context and (cluster >= 2 or wickets >= 5) and left > 18:
        if needed is None or needed / left < rate * 1.1:
            desired = 0
            reason = 'rebuild_after_wickets'
    if 'fc_intent' not in context and left <= 6 and (needed is None or needed / left > rate):
        desired = 3
        reason = 'final_deliveries'
    desired = _clamp(desired + _clamp((config['batting_aggression'] - 50) / 40, -.75, .75), 0, 3)
    # Mandatory urgency and draw survival override personality.
    if reason == 'rebuild_after_wickets':
        desired = 0
    if needed is not None and needed / left < rate * .75:
        desired = min(1., desired)
    if reason == 'final_deliveries':
        desired = 3
    if 'fc_intent' in context and context['fc_intent'].get('survival', 0) >= .8:
        desired = 0
    return _scores(('rebuild', 'balanced', 'attack', 'all_out'), desired), reason


def bowling_scores(context: CaptainContext, config: CaptaincyConfig):
    recent = context['recent']
    rate = max(.2, context['par_rate'])
    # Shrink small samples: a single six must not look like a sustained trend.
    runs = sum(e.get('runs', 0) for e in recent)
    observed = (runs + rate * 12) / (len(recent) + 12)
    cluster = sum(e.get('wicket', False) for e in recent)
    needed = context['runs_needed']
    left = max(1, context['balls_remaining'])
    desired, reason = 1., 'balanced_attack'
    if context['phase'] == 'death' or (len(recent) >= 6 and observed > rate * 1.2):
        desired, reason = 0., 'restrict_scoring'
    elif cluster or context['wickets'] >= 7:
        desired, reason = 2., 'press_for_wickets'
    if needed is not None and needed / left < rate * .75:
        desired, reason = 2., 'wickets_needed_to_defend'
    elif needed is not None and needed / left > rate * 1.4:
        desired, reason = 0., 'protect_required_rate'
    if 'fc_intent' in context:
        desired, reason = 2., 'take_twenty_wickets'
    desired = _clamp(desired + _clamp((config['bowling_aggression'] - 50) / 40, -.75, .75), 0, 2)
    return _scores(('contain', 'balanced', 'attack'), desired), reason


def choose_action(scores, config, kind, pressure, rng):
    management = config['batting_management' if kind == 'batting' else 'bowling_management']
    quality = (config['match_awareness'] + management) / 2
    amplitude = .20 * (1 - quality / 100) * (1 + _clamp(pressure) * (1 - config['composure'] / 100))
    noisy = {action: score + (rng.uniform(-amplitude, amplitude) if amplitude else 0)
             for action, score in scores.items()}
    return max(noisy, key=noisy.get)


BATTING_EFFECTS = {'rebuild': (.88, .90), 'balanced': (1., 1.),
                   'attack': (1.12, 1.10), 'all_out': (1.22, 1.20)}
BOWLING_EFFECTS = {'attack': (1.08, 1.12), 'balanced': (1., 1.), 'contain': (.90, .92)}


def apply_tactics(weights, tactics=None, *, is_fc=False):
    """Change conditional non-extra weights once, keeping extras mass intact."""
    if not tactics:
        return weights
    result = dict(weights)
    before = sum(v for k, v in result.items() if k != 'Extras')
    batting = (1., 1.) if is_fc else BATTING_EFFECTS[tactics.get('batting', 'balanced')]
    bowling = BOWLING_EFFECTS[tactics.get('bowling', 'balanced')]
    boundary = _clamp(batting[0] * bowling[0], .75, 1.30)
    wicket = _clamp(batting[1] * bowling[1], .75, 1.30)
    for key in ('Four', 'Six'):
        result[key] = result.get(key, 0) * boundary
    result['Wicket'] = result.get('Wicket', 0) * wicket
    after = sum(v for k, v in result.items() if k != 'Extras')
    if after > 0:
        for key in result:
            if key != 'Extras':
                result[key] *= before / after
    return result


class CaptaincyMixin:
    """Match integration; state is plain JSON-compatible dictionaries."""

    def _init_captaincy(self):
        version = self.data.get('captaincy_model_version')
        if version not in (None, MODEL_VERSION):
            raise ValueError('Unsupported captaincy model version.')
        self.captaincy_enabled = version == MODEL_VERSION
        self.captaincy_states = {}
        self.captaincy_events = []
        self.captaincy_rng_state = None
        if self.captaincy_enabled:
            from engine import random_source
            self.captaincy_rng_state = random_source.getstate()
            self.captaincy_configs = {side: parse_config(self.data.get('captaincy', {}).get(side, {}).get('config'))
                                      for side in ('home', 'away')}

    def _captain_side(self, team):
        # Team list aliases are retained by all supported checkpoints.
        if team is self.home_xi:
            return 'home'
        if team is self.away_xi:
            return 'away'
        names = {p['name'] for p in team}
        return 'home' if names <= {p['name'] for p in self.home_xi} else 'away'

    def _captain_key(self, side, shootout=False):
        innings = f'super_{getattr(self, "super_over_round", 1)}_{self.super_over_innings}' if shootout else str(self.fc_innings if self.is_fc else self.innings)
        return f'{innings}:{side}'

    def _captain_state(self, side, shootout=False):
        key = self._captain_key(side, shootout)
        if key not in self.captaincy_states:
            self.captaincy_states[key] = dict(batting='balanced', bowling='balanced', reservations={}, recent=[],
                last_evaluation=None, last_change={}, recommendation=None, selection_boundary=None, review=None, score_memory={})
        return self.captaincy_states[key]

    def _captain_context(self, shootout=False):
        if shootout:
            team = self.super_over_batting_team
            balls = self.super_over_ball
            left = self.balls_per_over - balls
            side = self._captain_side(team)
            other = 'away' if side == 'home' else 'home'
            score, wickets = self.super_over_scores[side], self.super_over_wickets[side]
            target = self.super_over_scores[other] + 1 if self.super_over_innings == 2 else None
            phase = 'death'
        else:
            team = self.batting_team
            balls = self.current_over * self.balls_per_over + self.current_ball
            left = max(0, self.overs * self.balls_per_over - balls)
            score, wickets = self.score, self.wickets
            target = self.target if not self.is_fc or self.fc_innings == 4 else None
            phase = 'death' if not self.is_fc and self.fmt.is_death(self.current_over) else 'powerplay' if not self.is_fc and self.fmt.is_powerplay(self.current_over) else 'middle'
        side = self._captain_side(team)
        state = self._captain_state(side, shootout)
        rates = {'Green': 1.05, 'Dry': 1.1, 'Hard': 1.6, 'Flat': 1.8, 'Dead': 2.}
        par = rates.get(self.pitch, 1.4)
        if not self.is_fc and not shootout:
            from engine.game_state_engine import get_par_score
            horizon = max(1, min(self.overs, self.current_over + 1))
            par = max(.2, (get_par_score(horizon, self.fmt, self.pitch)
                           - get_par_score(horizon - 1, self.fmt, self.pitch)) / self.balls_per_over)
        if self.is_fc:
            par = {'Green': .52, 'Dry': .57, 'Hard': .60, 'Flat': .67, 'Dead': .75}.get(self.pitch, .6)
            left = max(1, int(self._fc_overs_remaining_in_match() * 6))
        needed = max(0, target - score) if target is not None else None
        pressure = _clamp((needed / max(1, left) / par - .8) / 1.2) if needed is not None else _clamp(wickets / 8)
        context = CaptainContext(team=side, innings=self._captain_key(side, shootout).split(':')[0],
            legal_balls=balls, balls_remaining=left, score=score, wickets=wickets, runs_needed=needed,
            par_rate=par, recent=state['recent'], phase=phase, pitch=self.pitch, pressure=pressure)
        if self.is_fc:
            # The existing FC intent builder contains the session/tail logic.
            context['fc_intent'] = self._fc_build_match_state(captain_baseline=True)['intent']
        return context

    def _captain_name(self, side, shootout=False):
        team = self.home_xi if side == 'home' else self.away_xi
        nominated = self.data.get('captaincy', {}).get(side, {}).get('captain', {})
        player = next((p for p in team if p.get('is_captain')), None)
        if player is None:
            player = next((p for p in team if p.get('name') == nominated.get('name')), None)
        return (player or (team[0] if team else {})).get('name', 'Captain')

    def _captain_event(self, side, context, kind, action, previous, trigger, reason, shootout=False):
        event = CaptainDecision(id=f'captain:{len(self.captaincy_events)+1}', team=side,
            innings=context['innings'], legal_balls=context['legal_balls'], action=action,
            previous_action=previous, trigger=trigger, reason=reason,
            evidence={k: context[k] for k in ('score', 'wickets', 'balls_remaining', 'runs_needed', 'phase')})
        event['kind'] = kind
        event['captain'] = self._captain_name(side, shootout)
        self.captaincy_events.append(event)
        labels = {'all_out': 'all-out batting', 'rebuild': 'rebuilding', 'attack': 'attack',
                  'contain': 'containment', 'balanced': 'balanced'}
        explanations = {
            'phase': 'balancing scoring risk for this phase',
            'controlled_chase': 'keeping the chase under control',
            'required_rate': 'responding to the required scoring rate',
            'rebuild_after_wickets': 'protecting the remaining wickets after a collapse',
            'final_deliveries': 'prioritizing runs with few deliveries remaining',
            'balanced_attack': 'balancing wickets against runs conceded',
            'restrict_scoring': 'responding to scoring pressure and the remaining overs',
            'press_for_wickets': 'keeping pressure on the remaining batters',
            'wickets_needed_to_defend': 'seeking wickets to defend the target',
            'protect_required_rate': 'keeping the required rate difficult',
            'take_twenty_wickets': 'seeking the wickets needed for a First-Class result',
            'conditions_matchup_and_spell': 'using conditions, batter matchup, and attack freshness',
            'conditions_matchup_and_phase_allocation': 'using conditions and batter matchup while preserving later bowling options',
            'validated_shootout_bowler': 'using the available shootout attack',
        }
        verb = 'recommends' if kind == 'bowler' else 'switches to' if previous else 'sets'
        text = f"{event['captain']} {verb} {labels.get(action, action)} ({kind}), {explanations.get(reason, reason.replace('_', ' '))}."
        from html import escape
        text = escape(text)
        self.commentary.append(text)
        self.pending_pre_ball_commentary.append(text)

    def _captain_evaluate(self, shootout=False):
        if not self.captaincy_enabled:
            return
        from engine import random_source as rng
        context = self._captain_context(shootout)
        batting_side = context['team']
        bowling_side = 'away' if batting_side == 'home' else 'home'
        for side, kind, score_fn in ((batting_side, 'batting', batting_scores), (bowling_side, 'bowling', bowling_scores)):
            state = self._captain_state(side, shootout)
            config = self.captaincy_configs[side]
            review = state['review']
            signature = [context['legal_balls'] // self.balls_per_over, context['wickets'], context['runs_needed'] is not None,
                         getattr(self, 'target', None), getattr(self, 'overs', None), context['phase'],
                         getattr(self, 'fc_day', None), getattr(self, 'fc_ball_overs_bowled', 0) // 20, review]
            if state['last_evaluation'] == signature:
                continue
            initial = state['last_evaluation'] is None
            changed_target = not initial and signature[3:5] != state['last_evaluation'][3:5]
            observed = dict(context)
            if len(context['recent']) < self.balls_per_over and config['composure'] >= 50:
                observed['recent'] = []
            scores, reason = score_fn(observed, config)
            forced_urgency = reason in ('final_deliveries', 'save match')
            previous_scores = state.setdefault('score_memory', {}).get(kind)
            if previous_scores and not changed_target and not forced_urgency:
                alpha = .25 + .65 * config['adaptability'] / 100
                scores = {action: previous_scores.get(action, value) * (1-alpha) + value * alpha
                          for action, value in scores.items()}
            state['score_memory'][kind] = scores
            action = choose_action(scores, config, kind, context['pressure'], rng)
            current = state[kind]
            hold = context['legal_balls'] - state['last_change'].get(kind, -self.balls_per_over) >= self.balls_per_over
            forced = initial or changed_target or reason == 'final_deliveries' or review == 'interruption'
            threshold = .08 - .06 * config['adaptability'] / 100
            if initial or (action != current and (forced or (hold and scores[action] - scores[current] > threshold))):
                state[kind] = action
                state['last_change'][kind] = context['legal_balls']
                self._captain_event(side, context, kind, action, None if initial else current,
                    'innings_start' if initial else 'target_revision' if changed_target else review or 'match_review', reason, shootout)
            state['last_evaluation'] = signature[:-1] + [None]
            state['review'] = None

        if shootout:
            state = self._captain_state(bowling_side, True)
            name = self.super_over_bowler['name']
            if state['recommendation'] != name:
                self._captain_event(bowling_side, context, 'bowler', name, state['recommendation'],
                    'shootout_selection', 'validated_shootout_bowler', True)
                state['recommendation'] = name

    def _captain_tactics(self, shootout=False):
        if not self.captaincy_enabled:
            return None
        team = self.super_over_batting_team if shootout else self.batting_team
        side = self._captain_side(team)
        return {'batting': self._captain_state(side, shootout)['batting'],
                'bowling': self._captain_state('away' if side == 'home' else 'home', shootout)['bowling']}

    def _captain_record(self, outcome, shootout=False):
        if not self.captaincy_enabled:
            return
        team = self.super_over_batting_team if shootout else self.batting_team
        batting = self._captain_side(team)
        legal = not outcome.get('is_extra') or outcome.get('extra_type') in ('Byes', 'Leg Bye')
        for side in ('home', 'away'):
            state = self._captain_state(side, shootout)
            if legal:
                state['recent'].append({'runs': outcome.get('runs', 0), 'wicket': bool(outcome.get('batter_out'))})
                state['recent'] = state['recent'][-(15 if self.is_hundred else 18):]
            if outcome.get('batter_out'):
                state['review'] = 'wicket'

    def _captain_eligible(self):
        if self.is_fc:
            return self.bowler_manager.get_eligible_bowlers(self.current_over)
        left = self.overs - self.current_over
        eligible = self.bowler_manager.get_eligible_bowlers(self.current_over, left)
        if getattr(self.fmt, 'strict_short_bowling', False):
            return eligible
        # Legacy manager can relax quotas; captain policy cannot. Feasibility
        # checks capacity in alternating slots, including the candidate's use.
        all_bowlers = [p for p in self.bowling_team if p.get('will_bowl')]
        cap = self.fmt.max_bowler_overs
        last = self.bowler_manager._last_bowler
        result = []
        for p in eligible:
            if p['name'] == last or self.bowler_manager.overs_bowled(p['name']) >= cap:
                continue
            remaining = left - 1
            capacity = sum(min(max(0, cap - self.bowler_manager.overs_bowled(b['name']) - (b['name'] == p['name'])),
                               remaining // 2 if b['name'] == p['name'] else (remaining + 1) // 2)
                           for b in all_bowlers)
            if capacity >= remaining:
                result.append(p)
        return result

    def _captain_pick_bowler(self, candidates=None, shootout=False):
        from engine import random_source as rng
        self._captain_evaluate(shootout)
        context = self._captain_context(shootout)
        side = 'away' if context['team'] == 'home' else 'home'
        state = self._captain_state(side, shootout)
        boundary = [context['innings'], context['legal_balls'] // self.balls_per_over]
        eligible = candidates if candidates is not None else self._captain_eligible()
        if not eligible:
            raise ValueError('No legal bowling allocation remains')
        cached = next((b for b in eligible if b['name'] == state['recommendation']), None)
        if state['selection_boundary'] == boundary and cached:
            return cached
        config = self.captaincy_configs[side]
        phase = context['phase']
        # Reserve up to half a specialist's quota for the death; reassess at
        # every boundary, and yield automatically when alternatives are unsafe.
        if not self.is_fc and not shootout:
            attack = [b for b in self.bowling_team if b.get('will_bowl')]
            specialists = sorted(attack, key=lambda b: (-b.get('bowling_rating', 0), b['name']))[:2]
            reserve = max(1, min(2, self.fmt.max_bowler_overs // 2))
            state['reservations'] = {b['name']: min(reserve, self.bowler_manager.overs_remaining(b['name'])) for b in specialists}
        scores = {}
        fc_baseline = self.bowler_manager.weighted_selection_scores(eligible, self._compute_pitch_wear(), self.fc_day,
            new_ball_window=self._fc_is_new_ball_window(), under_lights=self._fc_is_under_lights()) if self.is_fc else {}
        for bowler in eligible:
            effective = self._get_effective_bowler_dict(bowler)['bowling_rating'] if not shootout else bowler.get('bowling_rating', 50)
            score = effective / 100
            spin = 'spin' in str(bowler.get('bowling_type', '')).lower()
            affinity = .12 if (self.pitch == 'Dry' and spin) or (self.pitch == 'Green' and not spin) else 0
            if not self.is_fc and not shootout and self._categorize_bowler(bowler) == self._get_preferred_bowler_type(self.current_over):
                affinity += .03
            if self.is_fc:
                score = fc_baseline[bowler['name']]['composite']
                if self._fc_is_new_ball_window() and not spin:
                    affinity += .06
                if self._compute_pitch_wear() > .5 and spin:
                    affinity += .06
            # The same physical matchup applies to weak and strong captains;
            # awareness controls reliable selection, not physical advantages.
            from engine.ball_outcome import compute_matchup_boost
            batter = self.current_striker if not shootout else self.super_over_current_striker
            matchup, _ = compute_matchup_boost(bowler.get('bowling_type', ''), bowler.get('bowling_hand', ''),
                batter.get('batting_hand', ''), batter.get('batting_rating', 50), self.pitch)
            affinity += _clamp(matchup - 1, -.10, .10)
            recent_runs = self.bowler_manager.prev_over_runs(bowler['name']) if not shootout else 0
            control = _clamp((recent_runs - context['par_rate'] * self.balls_per_over) / 100, -.04, .12)
            if state['bowling'] == 'contain':
                score -= control * 1.5
                if bowler.get('bowling_style') == 'Economist':
                    affinity += .05
            elif state['bowling'] == 'attack' and bowler.get('bowling_style') == 'Strike':
                affinity += .05
            reserve_penalty = 0
            if not self.is_fc and not shootout and phase != 'death':
                remaining = self.bowler_manager.overs_remaining(bowler['name'])
                if remaining <= state['reservations'].get(bowler['name'], 0) and len(eligible) > 1:
                    reserve_penalty = .15
            scores[bowler['name']] = _clamp(score + affinity - control - reserve_penalty)
        name = choose_action(scores, config, 'bowling', context['pressure'], rng)
        selected = next(b for b in eligible if b['name'] == name)
        previous = state['recommendation']
        state['recommendation'] = name
        state['selection_boundary'] = boundary
        if name != previous:
            reason = 'conditions_matchup_and_spell' if self.is_fc else 'conditions_matchup_and_phase_allocation'
            self._captain_event(side, context, 'bowler', name, previous, 'bowling_boundary', reason, shootout)
        return selected

    def captaincy_status(self, events_since=None):
        if not self.captaincy_enabled:
            return None
        shootout = self.innings >= 4 and bool(getattr(self, 'super_over_round', 0))
        return {'model_version': MODEL_VERSION, 'teams': {
            side: {'captain': self._captain_name(side), 'config': deepcopy(self.captaincy_configs[side]),
                   **{key: deepcopy(self.captaincy_states.get(self._captain_key(side, shootout),
                       dict(batting='balanced', bowling='balanced', recommendation=None, reservations={}))[key])
                      for key in ('batting', 'bowling', 'recommendation', 'reservations')}}
            for side in ('home', 'away')},
            'events': deepcopy(self.captaincy_events[events_since:] if events_since is not None else [])}

    def captaincy_snapshot(self):
        return {'states': deepcopy(self.captaincy_states), 'events': deepcopy(self.captaincy_events),
                'rng_state': self.captaincy_rng_state,
                'commentary_rng': self.commentary_engine._rng.getstate(),
                'commentary_recent': {k: list(v) for k, v in self.commentary_engine._recent.items()},
                'commentary_announced': list(self.commentary_engine._announced)} if self.captaincy_enabled else None

    def restore_captaincy(self, snapshot):
        if not self.captaincy_enabled or not snapshot:
            return
        self.captaincy_states = deepcopy(snapshot['states'])
        self.captaincy_events = deepcopy(snapshot['events'])
        def tuples(value):
            return tuple(tuples(v) for v in value) if isinstance(value, list) else value
        self.captaincy_rng_state = tuples(snapshot['rng_state'])
        if snapshot.get('commentary_rng'):
            from collections import deque
            self.commentary_engine._rng.setstate(tuples(snapshot['commentary_rng']))
            self.commentary_engine._recent.clear()
            self.commentary_engine._recent.update({k: deque(v) for k, v in snapshot.get('commentary_recent', {}).items()})
            self.commentary_engine._announced = set(snapshot.get('commentary_announced', []))
