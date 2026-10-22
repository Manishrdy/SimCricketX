"""Versioned email identity rules, independent of Flask and enforcement.

Canonical identity is for duplicate evidence, never a login/recovery alias.
Structural normalization accepts legacy plus tags; new-address validation does
not. Neither function verifies mailbox ownership or checks DNS.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

import idna

NORMALIZATION_VERSION = 1
_LOCAL = re.compile(r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~.-]+", re.ASCII)
_DATA = Path(__file__).parent / "data" / "disposable_email"


class InvalidEmail(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EmailIdentity:
    address: str
    canonical: str
    domain: str
    version: int = NORMALIZATION_VERSION


def normalize_email(value: str) -> EmailIdentity:
    """Normalize a dot-atom address and IDNA domain without signup policy.

Only consumer Gmail/googlemail addresses have provider alias rules. Workspace
and other custom domains retain dots and plus suffixes. Local parts are ASCII
and case-insensitive per the application's existing policy.
"""
    if not isinstance(value, str):
        raise InvalidEmail("invalid_syntax")
    value = value.strip()
    if value.count("@") != 1:
        raise InvalidEmail("invalid_syntax")
    local, domain = value.split("@")
    if not local.isascii():
        raise InvalidEmail("non_ascii_local")
    if (not _LOCAL.fullmatch(local) or len(local) > 64
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise InvalidEmail("invalid_syntax")
    try:
        domain = idna.encode(domain, uts46=True, std3_rules=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError):
        raise InvalidEmail("invalid_domain") from None
    if "." not in domain or domain.endswith("."):
        raise InvalidEmail("invalid_domain")
    local = local.lower()
    address = f"{local}@{domain}"
    if len(address) > 254:
        raise InvalidEmail("address_too_long")
    canonical_local, canonical_domain = local, domain
    if domain in {"gmail.com", "googlemail.com"}:
        canonical_local = local.split("+", 1)[0].replace(".", "")
        canonical_domain = "gmail.com"
        if not canonical_local:
            raise InvalidEmail("invalid_syntax")
    return EmailIdentity(address, f"{canonical_local}@{canonical_domain}", domain)


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    """Load the reviewed local snapshot; missing/corrupt data is an error."""
    entries = set()
    for line in (_DATA / "disposable_email_blocklist.conf").read_text().splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            try:
                domain = idna.encode(value, uts46=True, std3_rules=True).decode("ascii").lower()
            except idna.IDNAError:
                # Upstream includes a few domains (e.g. emoji labels) that our
                # address parser rejects outright; they cannot pass validation.
                continue
            if "." in domain and not domain.endswith("."):
                entries.add(domain)
    if not entries:
        raise RuntimeError("Disposable-domain snapshot is empty")
    return frozenset(entries)


def validate_new_email(value: str) -> EmailIdentity:
    """Apply D12–D17; caller must separately verify mailbox ownership."""
    identity = normalize_email(value)
    if "+" in identity.address.split("@", 1)[0]:
        raise InvalidEmail("plus_tag_not_allowed")
    labels = identity.domain.split(".")
    blocked = disposable_domains()
    if any(".".join(labels[i:]) in blocked for i in range(len(labels) - 1)):
        raise InvalidEmail("disposable_domain")
    return identity


def stored_address_key(value: str) -> str:
    """Case-insensitive stored-address lookup, without provider alias folding.

Legacy callers must continue looking up their existing stored keys; this helper
is for normalized addresses once the corresponding write paths are integrated.
"""
    return normalize_email(value).address
