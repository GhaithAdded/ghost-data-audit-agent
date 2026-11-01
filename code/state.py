"""Reconstruct, for each case, which resources belong to the subject and their state at audit_at.

Linking a resource to the subject uses, in order of strength:
1. evidence links: an observation known by audit_at carries one of the subject's
   identifiers (email, phone, hash, user_id), directly or through its log trace;
2. associations: identifiers stated together in one fact (e.g. a PDF register row);
3. catalog links: LINK_RESOURCE_IDENTITY activities naming the resource and the subject;
4. references: one hop from a linked record to a resource it names (search doc -> account);
5. deletion-worker activity: deleted accounts and anonymized analytics records carry no
   identifiers, so the only participant-visible link is the worker action the workflow
   ran for that request shortly before reported_at. Accounts are assigned by a global
   one-to-one matching over all requests (see match_accounts).

State at audit_at comes from the latest evidence file that says something about the
resource. A file listing a whole system (Coverage) but not the resource says ABSENT.
Within one file any surviving copy wins over absence (replica drift). Facts true by
audit_at may come from a file exported later (known), but only files captured by
audit_at are cited (visible).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from evidence import (ABSENT, EMAIL_RE, PRESENT, USER_RE, Association, EvidenceStore, Observation,
                      identifiers_in_text, system_for, tag_identifier)
from identity import IdentityIndex, SubjectIdentity
from loaders import Activity, Dataset, DeletionRequest

# Workflow status -> the deletion worker's reported result for the account it deleted.
RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
ACCOUNT_WINDOW = (timedelta(minutes=30), timedelta(minutes=90))  # delete runs this long before reported_at
RELAXED_WINDOW = (timedelta(minutes=-30), timedelta(minutes=120))
ANONYMIZE_WINDOW = (timedelta(minutes=10), timedelta(minutes=45))
ASSIGN_TIES = False  # when several requests are equally plausible, still assign in a fixed order
RETAINED_SYSTEMS = ("security_service", "legal_service")
# Lookups by this service report accounts that never appear in account_service evidence.
NON_AUTHORITATIVE_LOCATIONS = ("log:profile_service/",)


@dataclass(frozen=True)
class ResourceState:
    system: str
    resource_id: str
    category: str | None
    state: str  # PRESENT/ABSENT/IDENTIFIABLE/ANONYMIZED/REDACTED/ACTIVE/DISABLED/BLOCKED
    exposed: frozenset[str]  # identifiers still stored, from the deciding observation
    evidence_id: str  # latest citable file showing this state; "" when none is citable
    observed_at: datetime | None
    linked_by: str  # which rule tied it to the subject


@dataclass
class CaseState:
    request: DeletionRequest
    subject: SubjectIdentity | None
    resources: list[ResourceState] = field(default_factory=list)


def _owner(key: str, index: IdentityIndex) -> str | None:
    kind, _, value = key.partition(":")
    if kind == "user":
        return value
    if kind == "email":
        return index.owner_of_email(value)
    if kind == "phone":
        return index.owner_of_phone(value)
    if kind == "hash":
        return index.owner_of_hash(value)
    return None


def _authoritative(obs: Observation) -> bool:
    return not obs.location.startswith(NON_AUTHORITATIVE_LOCATIONS)


class StateBuilder:
    def __init__(self, data: Dataset, store: EvidenceStore, index: IdentityIndex,
                 all_requests: list[DeletionRequest], eligible: set[str] | None = None) -> None:
        self.data = data
        self.store = store
        self.index = index
        self.catalog = self._catalog_links(data.activities)
        # LINK_RESOURCE_IDENTITY: "linked export_1842 (NST-2026-0718-1842) to <email> and <user>".
        # The bracketed alias is the name documents use for the resource (a statement number,
        # a file name), so a citable document containing it shows the resource exists.
        # Aliases that are identifiers (user_17) or generic file names reused by other customers
        # are not proof, so the document must be a reference and also name the linked subject.
        self.catalog_confirmations: dict[str, list[tuple[datetime, str, str | None, tuple[str, ...]]]] = \
            defaultdict(list)
        for a in data.activities:
            if a.event_type == "LINK_RESOURCE_IDENTITY":
                alias = re.search(re.escape(a.resource_id) + r" \(([^)]+)\)", a.details)
                alias = alias.group(1) if alias else None
                if alias == a.resource_id or tag_identifier(alias or "") is not None:
                    alias = None
                subject_terms = tuple(EMAIL_RE.findall(a.details) + USER_RE.findall(a.details))
                self.catalog_confirmations[a.resource_id].append((a.event_time, a.activity_id, alias, subject_terms))
        self.treatments: dict[str, list[Activity]] = defaultdict(list)  # delete/anonymize/redact actions
        for a in data.activities:
            if a.event_type in ("DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD"):
                self.treatments[a.resource_id].append(a)
        self.owners = self._resource_owners()
        eligible = eligible if eligible is not None else {r.case_id for r in all_requests}
        pool = [r for r in all_requests if r.case_id in eligible]
        self.previous_audit: dict[str, datetime] = {}  # case_id -> audit_at of the subject's earlier request
        by_subject: dict[str, list[DeletionRequest]] = defaultdict(list)
        for r in pool:
            by_subject[self._subject_of(r)].append(r)
        for requests in by_subject.values():
            requests.sort(key=lambda r: r.requested_at)
            for earlier, later in zip(requests, requests[1:]):
                if earlier.audit_at <= later.requested_at:
                    self.previous_audit[later.case_id] = earlier.audit_at
        self.account_for = self.match_accounts(pool)
        self.anonymized_for = self._match_anonymizations(pool)

    # --- ownership ------------------------------------------------------------

    @staticmethod
    def _catalog_links(activities: list[Activity]) -> list[tuple[datetime, str, frozenset[str]]]:
        """LINK_RESOURCE_IDENTITY / CLASSIFY_REQUEST name a resource together with identifiers."""
        links = []
        for a in activities:
            if a.event_type in ("LINK_RESOURCE_IDENTITY", "CLASSIFY_REQUEST"):
                keys = frozenset(k for k in identifiers_in_text(a.details) if not k.startswith("res:"))
                if keys:
                    links.append((a.event_time, a.resource_id, keys))
        return links

    def _resource_owners(self) -> dict[str, set[str]]:
        """Every user_id any evidence (at any time) ties to each resource."""
        owners: dict[str, set[str]] = defaultdict(set)
        for obs in self.store.observations:
            if not _authoritative(obs):
                continue
            for key in obs.links | obs.exposed:
                user = _owner(key, self.index)
                if user:
                    owners[obs.resource_id].add(user)
        for _, resource_id, keys in self.catalog:
            for key in keys:
                user = _owner(key, self.index)
                if user:
                    owners[resource_id].add(user)
        return owners

    def _subject_of(self, request: DeletionRequest) -> str | None:
        subject = self.index.resolve(request.email, request.audit_at)
        return subject.user_id if subject else None

    # --- worker-activity linking -----------------------------------------------

    def match_accounts(self, requests: list[DeletionRequest]) -> dict[str, str]:
        """Assign each request the account its deletion workflow deleted (one-to-one).

        Candidates: deletion-worker DELETE_RECORD on account_service shortly before
        reported_at, not owned by someone else. A strict pass also requires the worker
        result to match the reported status; a relaxed pass handles the rest. Forced
        choices are assigned first; remaining ties prefer an account with citable
        evidence by the request's audit_at, then the closest time.
        """
        deletes = [a for a in self.data.activities if a.event_type == "DELETE_RECORD"
                   and a.target_system == "account_service" and a.source_system == "deletion_worker"]
        subjects = {r.case_id: self._subject_of(r) for r in requests}

        def candidates(r: DeletionRequest, window, strict: bool) -> list[Activity]:
            lo, hi = window
            found = []
            for a in deletes:
                if not lo <= r.reported_at - a.event_time <= hi:
                    continue
                if strict and a.reported_result != RESULT_FOR_STATUS.get(r.reported_status):
                    continue
                owners = self.owners.get(a.resource_id)
                if owners and subjects[r.case_id] not in owners:
                    continue
                found.append(a)
            own = [a for a in found if subjects[r.case_id] in self.owners.get(a.resource_id, ())]
            return own or found

        assigned: dict[str, str] = {}
        for window, strict in ((ACCOUNT_WINDOW, True), (RELAXED_WINDOW, False)):
            options = {r.case_id: candidates(r, window, strict) for r in requests if r.case_id not in assigned}
            by_id = {r.case_id: r for r in requests}
            while True:
                taken = set(assigned.values())
                open_ = {cid: [a for a in acts if a.resource_id not in taken]
                         for cid, acts in options.items() if cid not in assigned}
                forced = {cid: acts[0] for cid, acts in open_.items() if len({a.resource_id for a in acts}) == 1}
                if forced:
                    for cid, act in sorted(forced.items()):
                        if act.resource_id not in set(assigned.values()):
                            assigned[cid] = act.resource_id
                    continue
                pending = sorted((cid for cid, acts in open_.items() if acts),
                                 key=lambda c: (by_id[c].reported_at, c))
                if not pending:
                    break
                cid = pending[0]
                r = by_id[cid]
                best = min(open_[cid], key=lambda a: (
                    not self.store.visible(a.resource_id, r.audit_at),
                    abs((r.reported_at - a.event_time) - (ACCOUNT_WINDOW[0] + ACCOUNT_WINDOW[1]) / 2),
                    a.resource_id))
                assigned[cid] = best.resource_id
        return assigned

    def _match_anonymizations(self, requests: list[DeletionRequest]) -> dict[str, set[str]]:
        """Assign anonymized analytics records that no evidence ties to anyone.

        A claimant's workflow must have run the anonymization shortly before its
        reported_at, its subject must have no analytics record of their own, and the
        record must appear in evidence captured shortly before the claimant's audit_at
        (readbacks are produced for the cases being audited); only the claimants with
        the closest such capture remain. Records are then assigned one-to-one: forced
        choices first, then in a fixed order.
        """
        lo, hi = ANONYMIZE_WINDOW
        has_analytics = {u for rid, users in self.owners.items() if rid.startswith("analytics_") for u in users}
        claims: dict[str, list[str]] = {}
        for a in self.data.activities:
            if (a.event_type != "ANONYMIZE_RECORD" or a.source_system != "deletion_worker"
                    or a.target_system != "analytics_platform" or self.owners.get(a.resource_id)):
                continue
            scored = []
            for r in requests:
                if not lo <= r.reported_at - a.event_time <= hi or self._subject_of(r) in has_analytics:
                    continue
                seen = [o.captured_at for o in self.store.visible(a.resource_id, r.audit_at)]
                if seen:
                    scored.append((r.audit_at - max(seen), r.case_id))
            if scored:
                best = min(gap for gap, _ in scored)
                claims[a.resource_id] = sorted(cid for gap, cid in scored if gap == best)

        result: dict[str, set[str]] = defaultdict(set)
        taken: set[str] = set()
        while claims:
            open_ = {rid: [c for c in cids if c not in taken] for rid, cids in claims.items()}
            open_ = {rid: cids for rid, cids in open_.items() if cids}
            if not open_:
                break
            forced = sorted(rid for rid, cids in open_.items() if len(cids) == 1)
            if not forced and not ASSIGN_TIES:
                break
            rid = forced[0] if forced else sorted(open_)[0]
            case_id = open_[rid][0]
            result[case_id].add(rid)
            taken.add(case_id)
            del claims[rid]
        return result

    # --- per case ----------------------------------------------------------------

    def linked_resources(self, request: DeletionRequest, subject: SubjectIdentity) -> dict[str, str]:
        """resource_id -> rule that linked it, using only facts known by audit_at."""
        at = request.audit_at
        keys = set(subject.link_keys())
        for assoc in self.store.associations:
            if assoc.captured_at <= at and assoc.keys & keys:
                keys |= {k for k in assoc.keys if not k.startswith(("res:", "name:"))}

        linked: dict[str, str] = {}
        last_link: dict[str, datetime] = {}  # newest fact tying each resource to the subject

        def link(resource_id: str, how: str, when: datetime) -> None:
            linked.setdefault(resource_id, how)
            last_link[resource_id] = max(when, last_link.get(resource_id, when))

        for key in keys:
            for resource_id in self.store.by_link.get(key, ()):
                for o in self.store.known(resource_id, at):
                    if _authoritative(o) and key in o.links | o.exposed:
                        link(resource_id, "evidence", o.observed_at)
        for when, resource_id, cat_keys in self.catalog:
            if when <= at and cat_keys & keys:
                link(resource_id, "catalog", when)
        for resource_id in list(linked):  # one hop through references such as source_resource_id
            for obs in self.store.known(resource_id, at):
                for key in obs.links:
                    if key.startswith("res:") and system_for(key[4:]):
                        other = key[4:]
                        owners = self.owners.get(other)
                        if not owners or subject.user_id in owners:
                            link(other, "reference", obs.observed_at)

        # A subject with an earlier request: resources last tied to them before that
        # request's audit, and either gone or not seen since, belong to the earlier episode.
        previous = self.previous_audit.get(request.case_id)
        if previous:
            for resource_id in list(linked):
                if last_link[resource_id] > previous:
                    continue
                current = self.resource_state(resource_id, at, "")
                if (current.state in (ABSENT, "UNKNOWN") or current.observed_at is None
                        or current.observed_at <= previous):
                    del linked[resource_id]

        account = self.account_for.get(request.case_id)
        if account and not any(r.startswith("account_") for r in linked):
            linked[account] = "worker_activity"
        for resource_id in self.anonymized_for.get(request.case_id, ()):
            linked.setdefault(resource_id, "worker_activity")
        return {rid: how for rid, how in linked.items() if system_for(rid)}

    def resource_state(self, resource_id: str, audit_at: datetime, linked_by: str) -> ResourceState:
        system = system_for(resource_id)
        known = [o for o in self.store.known(resource_id, audit_at) if _authoritative(o)]
        # Decide from evidence captured by audit_at; facts from files exported later are
        # used only when nothing citable mentions the resource (later evidence must not
        # change the answer, but can show that a retained record exists).
        # Listings never prove a retained record (security audit log, legal hold) gone:
        # those listings are partial and retained records are kept by policy.
        retained = system in RETAINED_SYSTEMS
        listed = not retained and any(c.system == system and c.captured_at <= audit_at for c in self.store.coverage)
        citable = [o for o in known if o.captured_at <= audit_at]
        if citable or listed:
            known = citable

        # One verdict per evidence file: any surviving copy in the file beats absence.
        per_file: dict[str, list[Observation]] = defaultdict(list)
        for obs in known:
            per_file[obs.evidence_id].append(obs)
        # (time, state, deciding observation, evidence_id, captured_at)
        explicit: list[tuple[datetime, str, Observation | None, str, datetime]] = []
        for evidence_id, group in per_file.items():
            present = [o for o in group if o.state != ABSENT]
            chosen = max(present or group, key=lambda o: o.observed_at)
            explicit.append((max(o.observed_at for o in group), chosen.state, chosen, evidence_id, chosen.captured_at))
        # A citable document carrying the resource's catalog alias shows the resource exists.
        for when, _, alias, subject_terms in self.catalog_confirmations.get(resource_id, ()):
            if alias and when <= audit_at:
                for file_id in self.store.files_mentioning(alias, audit_at):
                    text = self.store.text[file_id].lower()
                    if file_id not in per_file and any(t.lower() in text for t in subject_terms):
                        captured = self.store.captured_at[file_id]
                        explicit.append((captured, PRESENT, None, file_id, captured))
                        per_file[file_id] = []
        # A full listing of the system that omits the resource says ABSENT, but only
        # when nothing names the resource later, and never for a copy last seen in a
        # replica (a primary-side listing cannot prove a replica copy is gone).
        implicit = [(cov.captured_at, ABSENT, None, cov.evidence_id, cov.captured_at)
                    for cov in self.store.coverage
                    if not retained and cov.system == system and cov.captured_at <= audit_at
                    and cov.evidence_id not in per_file]
        if not explicit and not implicit:
            # No file says anything about the resource: a data-catalog confirmation that it
            # belongs to the subject is the only participant-visible proof that it exists.
            explicit = [(when, PRESENT, None, activity_id, when)
                        for when, activity_id, _, _ in self.catalog_confirmations.get(resource_id, ())
                        if when <= audit_at]
        explicit.sort(key=lambda v: (v[0], v[1] != ABSENT, v[3]))
        implicit.sort(key=lambda v: (v[0], v[3]))

        latest = explicit[-1] if explicit else None
        replica_copy = (latest is not None and latest[1] != ABSENT and latest[2] is not None
                        and "replica" in latest[2].location)
        # Omission only overrides a record seen present when something acted on it in between.
        acted_on = latest is not None and implicit and any(
            latest[0] < a.event_time <= implicit[-1][0] for a in self.treatments.get(resource_id, ()))
        if latest and (latest[1] == ABSENT or replica_copy or not implicit
                       or implicit[-1][0] <= latest[0] or not acted_on):
            when, state, obs = latest[0], latest[1], latest[2]
        elif implicit:
            when, state, obs = implicit[-1][0], ABSENT, None
        else:
            return ResourceState(system, resource_id, None, "UNKNOWN", frozenset(), "", None, linked_by)

        # Cite the latest citable file that names the resource in this state, else a listing.
        cite = [v for v in explicit if v[1] == state and v[4] <= audit_at]
        if not cite and state == ABSENT:
            cite = [v for v in implicit if v[4] <= audit_at]
        evidence_id = cite[-1][3] if cite else ""
        category = next((o.category for o in reversed(known) if o.category), None)
        return ResourceState(system, resource_id, category, state,
                             obs.exposed if obs else frozenset(), evidence_id, when, linked_by)

    def build(self, request: DeletionRequest) -> CaseState:
        subject = self.index.resolve(request.email, request.audit_at)
        case = CaseState(request, subject)
        if subject is None:
            return case
        for resource_id, how in sorted(self.linked_resources(request, subject).items()):
            case.resources.append(self.resource_state(resource_id, request.audit_at, how))
        return case
