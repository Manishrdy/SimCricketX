"""Ball-based Hundred presentation and the authoritative delivery ledger.

Legacy over/set fields remain compatibility fields. This ledger records scoring
facts before any innings reset and is shared by live responses and archives.
"""
from copy import deepcopy

PRESENTATION_VERSION = 2
BOWLING_POLICY_VERSION = 2
CONTINUATION_BONUS = 2
TYPE_PREFERENCE_BONUS = 3


def rate(runs, balls):
    return f"{runs / balls:.2f}" if balls else "—"


class HundredPresentation:
    def _hundred_begin_delivery(self, outcome):
        if not self.is_hundred:
            return
        self.hundred_delivery_sequence += 1
        striker = self.current_striker['name']
        bowler = self.current_bowler['name']
        self._hundred_delivery_context = dict(
            outcome=deepcopy(outcome), innings=self.innings,
            over=self.current_over, ball=self.current_ball,
            striker=striker, non_striker=self.current_non_striker['name'], bowler=bowler,
            score=self.score, wickets=self.wickets, free_hit=self.free_hit_active,
            pre_commentary=list(self.pending_pre_ball_commentary), pre_flushed=False,
            batter_before=dict(self.batsman_stats[striker]),
            bowler_before=dict(self.bowler_stats[bowler]),
        )

    def _hundred_finalize_delivery(self):
        """Idempotently commit the scored ball before an innings can reset."""
        ctx = getattr(self, '_hundred_delivery_context', None)
        if not self.is_hundred or not ctx or getattr(self, '_hundred_delivery_event', None):
            return
        outcome = ctx['outcome']
        batter = self.batsman_stats[ctx['striker']]
        bowler = self.bowler_stats[ctx['bowler']]
        before = ctx['over'] * 5 + ctx['ball']
        extra_type = outcome.get('extra_type') if outcome.get('is_extra') else None
        legal = extra_type not in ('Wide', 'No Ball')
        batting_runs = batter['runs'] - ctx['batter_before']['runs']
        runs = self.score - ctx['score']
        if extra_type:
            counter = {'Wide': 'wides', 'No Ball': 'noballs', 'Byes': 'byes', 'Leg Bye': 'legbyes'}.get(extra_type)
            if counter:
                bowler[counter] += runs - batting_runs
        if self.wickets >= 10 and self.current_ball == 5:
            if not self.current_over_maiden_invalid:
                bowler['maidens'] += 1
            bowler['overs'] += 1
            self.bowler_manager.record_over_completion(ctx['bowler'], self.current_over_runs)
        event = dict(
            delivery_id=f"{ctx['innings']}:{self.hundred_delivery_sequence}",
            innings=ctx['innings'], over=ctx['over'], ball=ctx['ball'],
            legal_balls_before=before, legal_balls_after=before + int(legal),
            is_legal=legal, ball_number=before + 1,
            display_label=f"{before + 1}" + ({'Wide': ' WD', 'No Ball': ' NB'}.get(extra_type, '')),
            bowling_end=(before // 10) % 2,
            next_bowling_end=((before + int(legal)) // 10) % 2,
            innings_ball_limit=self.overs * 5,
            striker=ctx['striker'], non_striker=ctx['non_striker'], bowler=ctx['bowler'],
            runs=runs, batting_runs=batting_runs,
            batter_balls=batter['balls'] - ctx['batter_before']['balls'],
            batter_fours=batter['fours'] - ctx['batter_before']['fours'],
            batter_sixes=batter['sixes'] - ctx['batter_before']['sixes'],
            extras={extra_type: runs - batting_runs} if extra_type else {},
            bowler_runs=bowler['runs'] - ctx['bowler_before']['runs'],
            bowler_wicket=bowler['wickets'] > ctx['bowler_before']['wickets'],
            batter_out=self.wickets > ctx['wickets'], is_extra=bool(extra_type),
            extra_type=extra_type, wicket_type=outcome.get('wicket_type'),
            description=outcome.get('description', ''), free_hit=ctx['free_hit'],
            score=self.score, wickets=self.wickets, target=self.target,
            partnership_runs=self.current_partnership_runs,
            partnership_balls=self.current_partnership_balls,
        )
        # Snapshots are deliberately numeric facts, never reconstructed from text.
        event['batter_totals'] = {n: {k: s[k] for k in ('runs', 'balls', 'fours', 'sixes')}
                                  for n, s in self.batsman_stats.items()}
        event['bowler_totals'] = {n: {k: s[k] for k in ('runs', 'wickets', 'balls_bowled')}
                                  for n, s in self.bowler_stats.items()}
        self._hundred_delivery_event = event
        self.hundred_deliveries.append(event)

    def _hundred_bowler_announcement(self):
        key = (self.innings, self.current_over)
        if getattr(self, '_hundred_announced_set', None) == key:
            return
        self._hundred_announced_set = key
        name = self.current_bowler['name']
        previous = self.bowler_manager._last_bowler
        start = self.current_over * 5 + 1
        end = min(start + 4, self.overs * 5)
        prefix = 'Change of ends. ' if start > 1 and (start - 1) % 10 == 0 else ''
        if start == 1:
            self.pending_pre_ball_commentary.append(
                f"<strong>{self._innings_banner()}</strong><br>"
                f"{self.current_striker['name']} and {self.current_non_striker['name']} open the batting. "
                f"{name} opens the bowling for balls {start}–{end}.")
        else:
            verb = 'continues' if name == previous else ('returns' if self.bowler_stats[name]['balls_bowled'] else 'comes into the attack')
            self.pending_pre_ball_commentary.append(f"{prefix}{name} {verb} for balls {start}–{end}.")

    def _hundred_set_summary(self):
        end = self.current_over * 5 + self.current_ball
        events = [e for e in self.hundred_deliveries if e['innings'] == self.innings and e['over'] == self.current_over]
        wickets = sum(e['batter_out'] for e in events)
        tokens = ' '.join(self.current_over_outcomes) or '—'
        return (f"<strong>End of set: balls {self.current_over * 5 + 1}–{end} — "
                f"{self.current_over_runs} runs, {wickets} wicket{'s' if wickets != 1 else ''} ({tokens})</strong><br>"
                f"{self._format_team_score_rr_line()}<br>"
                f"{self._format_bowler_figures_line(self.current_bowler['name'])}")

    def _hundred_target_info(self, team):
        balls = self.overs * 5
        return f"{team} needs {self.target} runs from {balls} balls at {rate(self.target, balls)} runs per ball{self._dls_suffix()}"

    def _hundred_terminal_set_summary(self):
        event = getattr(self, '_hundred_delivery_event', None)
        if not self.is_hundred or not event or not event['is_legal'] or event['legal_balls_after'] % 5:
            return ''
        events = [e for e in self.hundred_deliveries
                  if e['innings'] == event['innings'] and e['over'] == event['over']]
        runs = sum(e['runs'] for e in events)
        wickets = sum(e['batter_out'] for e in events)
        return (f"<strong>End of set: balls {event['over'] * 5 + 1}–{event['legal_balls_after']} — "
                f"{runs} runs, {wickets} wicket{'s' if wickets != 1 else ''}</strong><br>")

    def _pick_hundred_bowler(self, eligible):
        manager = self.bowler_manager
        preferred = self._get_preferred_bowler_type(self.current_over)
        death_start = self.fmt.death_phase.start
        pool = eligible
        if self.current_over < death_start:
            death_type = self._get_preferred_bowler_type(death_start)
            ranked = sorted((p for p in manager._eligible_xi if manager.overs_remaining(p['name'])), key=lambda p: (
                -(self._get_effective_bowler_dict(p, phase_over=death_start)['bowling_rating']
                  + (TYPE_PREFERENCE_BONUS if self._categorize_bowler(p) == death_type else 0)),
                str(p.get('id') or p['name'])))
            reserved, remaining = {}, self.overs - death_start
            for player in ranked[:2]:
                count = min(2, manager.overs_remaining(player['name']), remaining)
                if count:
                    reserved[player['name']] = count
                    remaining -= count
            protected = manager.get_eligible_bowlers(
                self.current_over, self.overs - self.current_over,
                death_reserve=reserved, death_start=death_start)
            if protected:
                pool = protected
        previous_events = [e for e in self.hundred_deliveries
                           if e['innings'] == self.innings and e['over'] == self.current_over - 1]
        productive = bool(previous_events) and (
            any(e['bowler_wicket'] for e in previous_events)
            or sum(e['bowler_runs'] for e in previous_events) <= self.score / max(1, self.current_over * 5) * 5)
        def rank(p):
            score = self._get_effective_bowler_dict(p)['bowling_rating']
            score += TYPE_PREFERENCE_BONUS if self._categorize_bowler(p) == preferred else 0
            if productive and p['name'] == manager._last_bowler:
                score += CONTINUATION_BONUS
            return (-score, self.bowler_stats.get(p['name'], {}).get('balls_bowled', 0), str(p.get('id') or p['name']))
        selected = min(pool, key=rank)
        self._update_bowler_tracking(selected)
        return selected
