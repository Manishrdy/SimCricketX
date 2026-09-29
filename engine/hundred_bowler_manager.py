"""Completion-safe five-ball allocation, independent of bowling ends."""
from functools import lru_cache
from engine.bowler_manager import BowlerManager


class HundredBowlerManager(BowlerManager):
    def __init__(self, bowling_xi, format_config):
        super().__init__(bowling_xi, format_config)
        self.consecutive_sets = 0

    def reset(self, new_bowling_xi):
        super().reset(new_bowling_xi)
        self.consecutive_sets = 0

    def record_over_completion(self, bowler_name, runs):
        self.consecutive_sets = self.consecutive_sets + 1 if bowler_name == self._last_bowler else 1
        super().record_over_completion(bowler_name, runs)

    def overs_remaining(self, bowler_name):
        used = self._quota.get(bowler_name, 0)
        cap = self.fmt.max_bowler_overs
        if self.fmt.overs >= 10:
            base, extra_slots = divmod(self.fmt.overs, 5)
            extra_used = sum(n > base for n in self._quota.values())
            if used <= base and extra_used >= extra_slots:
                cap = base
        return max(0, cap - used)

    def get_eligible_bowlers(self, current_over, overs_remaining_in_innings, *, death_reserve=None, death_start=None):
        players = self._eligible_xi
        used = tuple(self._quota.get(p["name"], 0) for p in players)
        previous = next((i for i, p in enumerate(players) if p["name"] == self._last_bowler), -1)
        base, extra = divmod(self.fmt.overs, 5)
        cap = self.fmt.max_bowler_overs

        reserves = tuple((death_reserve or {}).get(p['name'], 0) for p in players)

        def legal(i, counts, last, streak, at):
            if death_start is not None and at < death_start and counts[i] >= cap - reserves[i]:
                return False
            if counts[i] >= cap or (i == last and streak >= 2):
                return False
            return self.fmt.overs < 10 or counts[i] != base or sum(n > base for n in counts) < extra

        @lru_cache(None)
        def finish(counts, last, streak, left):
            if left == 0:
                return True
            if sum(max(0, cap - n) for n in counts) < left:
                return False
            for i in range(len(players)):
                if legal(i, counts, last, streak, self.fmt.overs - left):
                    nxt = counts[:i] + (counts[i] + 1,) + counts[i+1:]
                    if finish(nxt, i, streak + 1 if i == last else 1, left - 1):
                        return True
            return False

        left = max(0, overs_remaining_in_innings)
        return [p for i, p in enumerate(players) if left and legal(i, used, previous, self.consecutive_sets, current_over)
                and finish(used[:i] + (used[i] + 1,) + used[i+1:], i,
                           self.consecutive_sets + 1 if i == previous else 1, left - 1)]

    def validate_attack(self):
        if len(self._eligible_xi) < 5 or not self.get_eligible_bowlers(0, self.fmt.overs):
            raise ValueError("The Hundred requires at least five designated bowlers and a legal allocation")
