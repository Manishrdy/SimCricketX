"""Serialize deliveries and replay a lost response without advancing the engine."""
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from uuid import uuid4


class DeliveryConflict(Exception):
    pass


class MatchDelivery:
    def __init__(self):
        self.lock = RLock()
        self.token = uuid4().hex
        self.results = OrderedDict()

    def recover(self, token=None):
        with self.lock:
            if token in self.results:
                return {"status": "delivery_recovery", "delivery": self.results[token]}
            if (token and token != self.token):
                raise DeliveryConflict("Match state changed. Reload to resume safely.")
            return {"status": "delivery_ready", "delivery_token": self.token}

    def advance(self, token, advance):
        with self.lock:
            if token in self.results:
                return self.results[token], None
            if (token and token != self.token):
                raise DeliveryConflict("Match state changed. Reload to resume safely.")
            try:
                payload, error = advance()
            except Exception:
                # An exception may follow an engine mutation. Never blindly retry.
                self.token = uuid4().hex
                raise
            if error is not None:
                return payload, error
            previous = self.token
            self.token = uuid4().hex
            if token:
                payload = deepcopy(dict(payload, delivery_request=previous, delivery_token=self.token))
                self.results[previous] = payload
                while len(self.results) > 32:
                    self.results.popitem(last=False)
            return payload, None
