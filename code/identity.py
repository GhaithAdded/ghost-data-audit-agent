"""Resolve a request email to its subject and every identifier the subject has held.

A request may quote the subject's current email or an older one, in any letter
case. Older systems and evidence can still hold past emails, phones and email
hashes, so the subject's identifier set is the union of the current snapshot in
user_identities.csv and the history in user_audit_log.csv, limited to values the
subject actually held at or before the case's audit_at.

Matching rules (checked against the dataset):
- emails compare case-insensitively; evidence sometimes upper-cases them;
- email_hash is sha256 of the lower-cased email and is compared in lower case;
- phones compare on their digits only, so formatting differences cannot hide a match;
- names are NOT unique (several users share a name), so a name is kept for
  reference but never used on its own to link a record to the subject.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from loaders import Dataset, IdentityChange, UserIdentity

_NON_DIGITS = re.compile(r"\D")


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return _NON_DIGITS.sub("", value)


def normalize_hash(value: str) -> str:
    return value.strip().lower()


def email_hash(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SubjectIdentity:
    user_id: str
    name: str
    request_email: str
    matched_via: str  # "current_email" or "historical_email"
    emails: frozenset[str]  # normalized, current and historical
    phones: frozenset[str]  # digits only, current and historical
    email_hashes: frozenset[str]  # lower-case hex, current and historical
    history_audit_ids: tuple[str, ...]  # user_audit_log rows that contributed past values

    def has_email(self, value: str) -> bool:
        return normalize_email(value) in self.emails

    def has_phone(self, value: str) -> bool:
        return normalize_phone(value) in self.phones

    def has_email_hash(self, value: str) -> bool:
        return normalize_hash(value) in self.email_hashes

    def link_keys(self) -> frozenset[str]:
        """Tagged keys in the same form evidence.py emits for observation links."""
        return frozenset(
            {f"user:{self.user_id}"}
            | {f"email:{e}" for e in self.emails}
            | {f"phone:{p}" for p in self.phones}
            | {f"hash:{h}" for h in self.email_hashes}
        )

    def has_identifier(self, value: str) -> bool:
        """True when value is one of the subject's emails, phones or email hashes."""
        if "@" in value:
            return self.has_email(value)
        if self.has_email_hash(value):
            return True
        digits = normalize_phone(value)
        return len(digits) >= 7 and digits in self.phones


_NORMALIZERS = {"email": normalize_email, "phone": normalize_phone, "email_hash": normalize_hash}


def _values_held(current: str, changes: list[IdentityChange], audit_at: datetime) -> tuple[set[str], list[str]]:
    """Values of one field the subject held at any time up to audit_at, plus the audit_ids used.

    A change after audit_at means its new value was not yet held at audit_at, and
    the current snapshot value is only valid at audit_at if no later change exists.
    """
    held: set[str] = set()
    used: list[str] = []
    changed_after = False
    for change in changes:
        held.add(change.old_value)
        if change.event_time <= audit_at:
            held.add(change.new_value)
            used.append(change.audit_id)
        else:
            changed_after = True
    if not changed_after:
        held.add(current)
    return held, used


class IdentityIndex:
    """Lookup tables from every known identifier back to its owner's user_id."""

    def __init__(self, data: Dataset) -> None:
        self._identities: dict[str, UserIdentity] = data.identities
        self._changes: dict[str, list[IdentityChange]] = defaultdict(list)
        for change in data.identity_changes:  # already sorted by event_time
            self._changes[change.user_id].append(change)

        self._email_owner: dict[str, str] = {}
        self._phone_owner: dict[str, str] = {}
        self._hash_owner: dict[str, str] = {}
        self._current_emails: set[str] = set()
        for user in self._identities.values():
            self._claim(self._email_owner, normalize_email(user.email), user.user_id)
            self._claim(self._phone_owner, normalize_phone(user.phone), user.user_id)
            self._claim(self._hash_owner, normalize_hash(user.email_hash), user.user_id)
            self._current_emails.add(normalize_email(user.email))
        owners = {"email": self._email_owner, "phone": self._phone_owner, "email_hash": self._hash_owner}
        for change in data.identity_changes:
            normalize = _NORMALIZERS[change.field_name]
            for value in (change.old_value, change.new_value):
                self._claim(owners[change.field_name], normalize(value), change.user_id)

    @staticmethod
    def _claim(owners: dict[str, str], key: str, user_id: str) -> None:
        if not key:
            return
        previous = owners.setdefault(key, user_id)
        if previous != user_id:
            raise ValueError(f"identifier {key!r} is claimed by both {previous} and {user_id}")

    def owner_of_email(self, value: str) -> str | None:
        return self._email_owner.get(normalize_email(value))

    def owner_of_phone(self, value: str) -> str | None:
        return self._phone_owner.get(normalize_phone(value))

    def owner_of_hash(self, value: str) -> str | None:
        return self._hash_owner.get(normalize_hash(value))

    def resolve(self, email: str, audit_at: datetime) -> SubjectIdentity | None:
        """Identify the subject of a request, or None when the email is unknown."""
        user_id = self.owner_of_email(email)
        if user_id is None:
            return None
        user = self._identities[user_id]
        changes = self._changes.get(user_id, [])

        by_field: dict[str, list[IdentityChange]] = defaultdict(list)
        for change in changes:
            by_field[change.field_name].append(change)

        emails, used_e = _values_held(user.email, by_field["email"], audit_at)
        phones, used_p = _values_held(user.phone, by_field["phone"], audit_at)
        hashes, used_h = _values_held(user.email_hash, by_field["email_hash"], audit_at)
        # Every email the subject held implies its hash, even if the hash history is incomplete.
        hashes |= {email_hash(e) for e in emails}

        return SubjectIdentity(
            user_id=user_id,
            name=user.name,
            request_email=email,
            matched_via="current_email" if normalize_email(email) in self._current_emails else "historical_email",
            emails=frozenset(normalize_email(e) for e in emails),
            phones=frozenset(normalize_phone(p) for p in phones if normalize_phone(p)),
            email_hashes=frozenset(normalize_hash(h) for h in hashes),
            history_audit_ids=tuple(sorted(set(used_e + used_p + used_h))),
        )
