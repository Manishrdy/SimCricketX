"""Display-name rules shared by registration, account settings and the
community board.

Display names are public on the community board, so a non-admin must not be
able to pose as staff.
"""

from __future__ import annotations

import re

# Kept narrow on purpose: "support"/"staff" would catch "CSKSupporter".
_RESERVED_FRAGMENTS = ("admin", "moderator", "simcricketx", "official")


def _squash(name: str) -> str:
    # "S.i.m-Cricket X", "@dm1n" -> compare on letters only, with common
    # digit-for-letter swaps folded back.
    folded = (name or "").lower().translate(str.maketrans("013457@$", "oieastas"))
    return re.sub(r"[^a-z]", "", folded)


def is_reserved_display_name(name: str) -> bool:
    squashed = _squash(name)
    return any(frag in squashed for frag in _RESERVED_FRAGMENTS)


RESERVED_NAME_MESSAGE = "That display name is reserved. Please choose another."
