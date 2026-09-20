"""Strict, completion-safe bowling policy for short limited-overs presets."""
from functools import lru_cache

from engine.bowler_manager import BowlerManager


class ShortBowlerManager(BowlerManager):
    def get_eligible_bowlers(self, current_over, overs_remaining_in_innings):
        players = self._eligible_xi
        counts = tuple(self._quota.get(p["name"], 0) for p in players)
        last = next((i for i, p in enumerate(players) if p["name"] == self._last_bowler), -1)
        base, extra_slots = divmod(self.fmt.overs, 5)
        cap = self.fmt.max_bowler_overs

        def legal(i, used, previous):
            if i == previous or used[i] >= cap:
                return False
            # Existing overs survive a rain reduction, but cannot create new
            # additional-over slots. Only the necessary bowlers exceed floor(N/5).
            if used[i] == base and sum(n > base for n in used) >= extra_slots:
                return False
            return True

        @lru_cache(maxsize=None)
        def can_finish(used, previous, left):
            if left == 0:
                return True
            if sum(max(0, cap - n) for n in used) < left:
                return False
            for i in range(len(players)):
                if legal(i, used, previous):
                    updated = used[:i] + (used[i] + 1,) + used[i + 1:]
                    if can_finish(updated, i, left - 1):
                        return True
            return False

        left = max(0, overs_remaining_in_innings)
        if not left:
            return []
        return [p for i, p in enumerate(players)
                if legal(i, counts, last) and can_finish(
                    counts[:i] + (counts[i] + 1,) + counts[i + 1:], i, left - 1)]

    def overs_remaining(self, bowler_name):
        used = self._quota.get(bowler_name, 0)
        base, extra_slots = divmod(self.fmt.overs, 5)
        extra_used = sum(n > base for n in self._quota.values())
        cap = self.fmt.max_bowler_overs if used > base or extra_used < extra_slots else base
        return max(0, cap - used)

    def validate_attack(self):
        if len(self._eligible_xi) < 5 or not self.get_eligible_bowlers(0, self.fmt.overs):
            raise ValueError("This format requires at least five designated bowlers and a legal bowling allocation")
