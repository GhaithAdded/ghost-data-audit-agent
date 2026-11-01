"""Normalize every evidence file into uniform, time-stamped observations.

The evidence comes in many shapes: snapshot bundles, service exports, record
lists, a handover text file, SQLite dumps with primary and replica tables, six
log syntaxes, PDFs and screenshots. Each parser below reduces its format to the
same two facts so later steps never need to know where a fact came from:

Observation  "file F says resource R in system S was in state X at time T,
              still exposes identifiers E, and is linked to people/records L"
Coverage     "file F is a full listing of system S at its capture time", so a
              resource of S missing from F was ABSENT then

Nothing here decides which person a record belongs to or whether a deletion is
complete; that is the job of the next step. Values are only normalized:
identifiers become tagged keys (email:, phone:, hash:, user:, res:, ref:,
name:) so they can be compared with SubjectIdentity.link_keys().

Screenshots need a vision model: vision.py extracts their records and passes them
in; screenshots without an extraction are reported as pending instead of being
silently skipped.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from identity import normalize_email, normalize_hash, normalize_phone
from loaders import Dataset, EvidenceFile, parse_ts

# --- systems and states -------------------------------------------------------

SYSTEM_BY_PREFIX = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "audit_record": "security_service",
    "support": "support_platform",
    "email_delivery": "email_delivery_service",
    "data_flow": "integration_service",
    "cache": "cache_service",
    "export": "export_service",
    "backup": "backup_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
}
RESOURCE_ID = re.compile(r"\b(" + "|".join(sorted(SYSTEM_BY_PREFIX, key=len, reverse=True)) + r")_\d+\b")

PRESENT, ABSENT = "PRESENT", "ABSENT"  # a record exists / does not exist
IDENTIFIABLE, ANONYMIZED, REDACTED = "IDENTIFIABLE", "ANONYMIZED", "REDACTED"  # explicit treatment labels
ACTIVE, DISABLED = "ACTIVE", "DISABLED"  # data flows; ACTIVE also means "restore still possible" for backups
BLOCKED = "BLOCKED"  # backup restore blocked

RESULT_STATE = {
    "FOUND": PRESENT, "NOT_FOUND": ABSENT, "IDENTIFIABLE": IDENTIFIABLE, "ANONYMIZED": ANONYMIZED,
    "REDACTED": REDACTED, "ACTIVE": ACTIVE, "DISABLED": DISABLED, "BLOCKED": BLOCKED,
}
CATEGORY_BY_COLLECTION = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "backup_catalog": "BACKUP_ARCHIVE",
    "connector_configurations": "DATA_FLOW",
}
CATEGORY_BY_EXPORT_SECTION = {
    "profile_lookups": "USER_PROFILE",
    "index_queries": "SEARCH_INDEX_ENTRY",
    "profiles": "ANALYTICS_PROFILE",
    "tickets": "SUPPORT_MESSAGE",
    "exports": "USER_EXPORT",
    "connectors": "DATA_FLOW",
    "restore_entries": "BACKUP_ARCHIVE",
    "audit_records": "SECURITY_AUDIT_LOG",
}
CATEGORY_BY_INVENTORY_TABLE = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_records": "SUPPORT_MESSAGE",
    "customer_exports": "USER_EXPORT",
    "connector_subscriptions": "DATA_FLOW",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_records": "SECURITY_AUDIT_LOG",
}


def system_for(resource_id: str) -> str | None:
    match = RESOURCE_ID.fullmatch(resource_id or "")
    return SYSTEM_BY_PREFIX[match.group(1)] if match else None


# --- identifier normalization -------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d \-().]{6,}\d")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
REF_RE = re.compile(r"\b(?:ORD|CS)-\d+\b")

EMAIL_FIELDS = {"email", "owner_email", "customer_email", "requester_email", "uploaded_by"}
PHONE_FIELDS = {"phone", "owner_phone", "requester_phone"}
HASH_FIELDS = {"email_hash"}
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name"}
TEXT_FIELDS = {"message_text", "message_excerpt"}
REFERENCE_FIELDS = {  # say who/what a record concerns without being stored PII themselves
    "customer_reference", "subject_reference", "user_id", "subject_selector", "source_resource_id",
    "lookup_value", "order_reference", "ticket_id", "query", "matched_value",
    "lookup_key", "match_key", "subject_ref", "scan_key", "owner_key", "subject_key", "correlation_key",
}
NULLS = {"", "null", "none", "no direct identifier"}


def tag_identifier(value: object) -> str | None:
    """Turn a raw value into a tagged key, or None when it is not an identifier."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = str(value).strip()
    if text.lower() in NULLS:
        return None
    if RESOURCE_ID.fullmatch(text):
        return f"res:{text}"
    if USER_RE.fullmatch(text):
        return f"user:{text}"
    if REF_RE.fullmatch(text):
        return f"ref:{text}"
    if EMAIL_RE.fullmatch(text):
        return f"email:{normalize_email(text)}"
    if HASH_RE.fullmatch(text):
        return f"hash:{normalize_hash(text)}"
    if re.fullmatch(r"\+?[\d \-().]{7,}", text) and len(normalize_phone(text)) >= 7:
        return f"phone:{normalize_phone(text)}"
    return None


def identifiers_in_text(text: str) -> set[str]:
    found = {f"email:{normalize_email(m)}" for m in EMAIL_RE.findall(text)}
    found |= {f"phone:{normalize_phone(m)}" for m in PHONE_RE.findall(text)}
    found |= {f"hash:{normalize_hash(m)}" for m in HASH_RE.findall(text)}
    found |= {f"user:{m}" for m in USER_RE.findall(text)}
    found |= {f"ref:{m}" for m in REF_RE.findall(text)}
    found |= {f"res:{m.group(0)}" for m in RESOURCE_ID.finditer(text)}
    return found


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


# --- output records -----------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    evidence_id: str  # file_id to cite
    captured_at: datetime  # manifest capture time: the file is usable only if this <= audit_at
    observed_at: datetime  # when the fact was true (record/line time when given, else captured_at)
    system: str
    resource_id: str
    state: str
    category: str | None  # deletion_rules data_category when the evidence states it
    exposed: frozenset[str]  # direct identifiers still stored in the record (tagged)
    links: frozenset[str]  # everything tying the record to a person or another record (tagged)
    location: str  # where in the file, e.g. "sql:customer_data_read_replica"


@dataclass(frozen=True)
class Coverage:
    evidence_id: str
    captured_at: datetime
    system: str
    location: str


@dataclass(frozen=True)
class Association:
    """Identifiers stated together in one fact, e.g. a register row 'ORD-20264118 | user_08'."""
    evidence_id: str
    captured_at: datetime
    keys: frozenset[str]


@dataclass
class _FileResult:
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    associations: list[Association] = field(default_factory=list)
    text: str = ""  # searchable text of the file (raw text, or text extracted from a PDF)


class _Builder:
    """Collects observations for one evidence file."""

    def __init__(self, ev: EvidenceFile) -> None:
        self.ev = ev
        self.result = _FileResult()

    def cover(self, system: str, location: str) -> None:
        self.result.coverage.append(Coverage(self.ev.file_id, self.ev.captured_at, system, location))

    def associate(self, keys: Iterable[str]) -> None:
        keys = frozenset(keys)
        if len(keys) >= 2:
            self.result.associations.append(Association(self.ev.file_id, self.ev.captured_at, keys))

    def observe(self, resource_id: str, state: str, location: str, *, observed_at: datetime | None = None,
                system: str | None = None, category: str | None = None,
                exposed: Iterable[str] = (), links: Iterable[str] = ()) -> None:
        system = system_for(resource_id) or system
        if not system:
            return
        exposed = frozenset(exposed)
        links = frozenset(links) | {key for key in exposed if not key.startswith("name:")}
        self.result.observations.append(Observation(
            evidence_id=self.ev.file_id,
            captured_at=self.ev.captured_at,
            observed_at=observed_at or self.ev.captured_at,
            system=system,
            resource_id=resource_id,
            state=state,
            category=category,
            exposed=exposed,
            links=links - {f"res:{resource_id}"},
            location=location,
        ))

    def observe_record(self, record: dict, location: str, *, observed_at: datetime | None = None,
                       system: str | None = None, category: str | None = None, state: str | None = None,
                       extra_links: Iterable[str] = ()) -> None:
        """A stored record: it exists, and its identifier fields show what it still exposes."""
        flat = dict(record)
        for nested in ("document", "profile"):
            if isinstance(flat.get(nested), dict):
                flat.update(flat.pop(nested))
        exposed: set[str] = set()
        links: set[str] = set(extra_links)
        for key, value in flat.items():
            if key in EMAIL_FIELDS | PHONE_FIELDS | HASH_FIELDS:
                tagged = tag_identifier(value)
                if tagged:
                    exposed.add(tagged)
            elif key in NAME_FIELDS and value and str(value).lower() not in NULLS:
                exposed.add(f"name:{str(value).casefold()}")
            elif key in TEXT_FIELDS and value:
                exposed |= {k for k in identifiers_in_text(str(value)) if k.split(":")[0] in ("email", "phone", "hash")}
            elif key in REFERENCE_FIELDS:
                tagged = tag_identifier(value)
                if tagged:
                    links.add(tagged)
        resource_id = str(flat.get("resource_id", ""))
        system = system_for(resource_id) or system
        if state is None:
            state = PRESENT
            if system == "backup_service" and "restore_allowed" in flat:
                state = ACTIVE if _truthy(flat["restore_allowed"]) else BLOCKED
                if "archive_present" in flat and not _truthy(flat["archive_present"]):
                    state = ABSENT
            elif system == "integration_service" and "enabled" in flat:
                state = ACTIVE if _truthy(flat["enabled"]) else DISABLED
        self.observe(resource_id, state, location, observed_at=observed_at, system=system,
                     category=category, exposed=exposed, links=links)

    def observe_listing(self, row: dict, location: str, observed_at: datetime | None = None) -> None:
        """A record-list row: system | resource_id | record_type | result | subject reference."""
        state = RESULT_STATE.get(str(row.get("result", "")).upper())
        if state is None:
            return
        ref = row.get("subject_reference", row.get("lookup_value"))
        tagged = tag_identifier(ref)
        self.observe(row["resource_id"], state, location, observed_at=observed_at, system=row.get("system"),
                     category=row.get("record_type"), links={tagged} if tagged else set())


# --- JSON / TXT ---------------------------------------------------------------

def _parse_json(b: _Builder) -> None:
    doc = json.loads(b.ev.path.read_text(encoding="utf-8"))
    if "snapshots" in doc:  # records-assurance snapshot bundle: full listings per data store
        for snap in doc["snapshots"]:
            at = parse_ts(snap["captured_at"])
            location = f"snapshot:{snap['data_store']}/{snap['collection']}"
            if snap["collection"] == "operational_record_history":
                for row in snap["records"]:
                    b.observe_listing(row, location, parse_ts(row["recorded_at"]))
                continue
            b.cover(snap["data_store"], location)
            for record in snap["records"]:
                b.observe_record(record, location, observed_at=at, system=snap["data_store"],
                                 category=CATEGORY_BY_COLLECTION.get(snap["collection"]))
    elif "service_exports" in doc:  # per-service readback export
        for service, sections in doc["service_exports"].items():
            for section, items in sections.items():
                location = f"export:{service}/{section}"
                category = CATEGORY_BY_EXPORT_SECTION.get(section)
                b.cover(service, location)
                for item in items:
                    at = parse_ts(item["observed_at"]) if item.get("observed_at") else None
                    if section == "profile_lookups":
                        if item.get("status_code") == 404 or not item.get("profile"):
                            b.observe(item["resource_id"], ABSENT, location, observed_at=at, system=service,
                                      category=category)
                        else:
                            b.observe_record(item, location, observed_at=at, system=service, category=category)
                    elif section == "index_queries":
                        query_link = {tag_identifier(item.get("query"))} - {None}
                        docs = item.get("hits", {}).get("documents", [])
                        if not docs:
                            b.observe(item["resource_id"], ABSENT, location, observed_at=at, system=service,
                                      category=category, links=query_link)
                        for hit in docs:
                            b.observe_record({**hit, "query": item.get("query")}, location, observed_at=at,
                                             system=service, category=category)
                    else:
                        b.observe_record(item, location, observed_at=at, system=service, category=category)
    elif doc.get("records") and "system" in doc["records"][0]:  # record list (configs, ledger, manifest)
        for row in doc["records"]:
            b.observe_listing(row, f"list:{doc.get('origin', 'records')}", parse_ts(row["recorded_at"]))
    elif "collection" in doc:  # single-collection capture
        location = f"collection:{doc.get('source')}/{doc['collection']}"
        category = CATEGORY_BY_COLLECTION.get(doc["collection"])
        for record in doc.get("records", []):
            b.observe_record(record, location, category=category)
        systems = {system_for(r.get("resource_id", "")) for r in doc.get("records", [])} - {None}
        for system in systems:
            b.cover(system, location)


HANDOVER_LINE = re.compile(r"^- (\S+) \| (\S+) \| (\S+) \| (\S+) \| (\S+) \| (.+)$")


def _parse_txt(b: _Builder) -> None:
    for line in b.ev.path.read_text(encoding="utf-8").splitlines():
        m = HANDOVER_LINE.match(line.strip())
        if m:
            at, system, resource_id, record_type, result, ref = m.groups()
            b.observe_listing({"system": system, "resource_id": resource_id, "record_type": record_type,
                               "result": result, "subject_reference": ref}, "handover", parse_ts(at))


# --- SQL ----------------------------------------------------------------------

INVENTORY_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def _parse_sql(b: _Builder) -> None:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    try:
        con.executescript(b.ev.path.read_text(encoding="utf-8"))
        tables = [r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if "snapshot_scope" in tables:  # inventory: scope table lists every system captured, even empty ones
            for row in con.execute("SELECT origin_system FROM snapshot_scope"):
                b.cover(row["origin_system"], "sql:snapshot_scope")
        for table in tables:
            if table == "snapshot_scope":
                continue
            location = f"sql:{table}"
            rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
            columns = [c[1] for c in con.execute(f'PRAGMA table_info("{table}")')]
            if "record_json" in columns:  # primary/replica tables holding raw records per origin system
                for row in rows:
                    b.observe_record(json.loads(row["record_json"]), location, system=row["origin_system"],
                                     category=CATEGORY_BY_COLLECTION.get(row["collection_name"]))
                for system in {r["origin_system"] for r in rows}:
                    b.cover(system, location)
            elif table.startswith("customer_profiles_"):  # account primary/failover pair
                b.cover("account_service", location)
                for row in rows:
                    b.observe_record(row, location, system="account_service", category="USER_PROFILE")
            elif table == "search_documents_primary":
                b.cover("search_service", location)
                for row in rows:
                    b.observe_record(row, location, system="search_service", category="SEARCH_INDEX_ENTRY")
            elif table in INVENTORY_TABLE_SYSTEM:
                for row in rows:
                    b.observe_record(row, location, system=INVENTORY_TABLE_SYSTEM[table],
                                     category=CATEGORY_BY_INVENTORY_TABLE.get(table))
    finally:
        con.close()


# --- logs ---------------------------------------------------------------------

_KV = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
_TS = r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"
_LOG_FORMATS = [  # (name, pattern); groups are mapped in _parse_log_line
    ("json", re.compile(_TS + r" (\{.*\})$")),
    ("syslog", re.compile(_TS + r" <\d+>\d+ (\S+) (\S+) (\S+) - - (.*)$")),
    ("bracket", re.compile(_TS + r" \[(\w+)\] \[([^\]]+)\] \[([^\]]+)\] (\w+): ?(.*)$")),
    ("cloud", re.compile(_TS + r' requestId=(\S+) level=(\w+) function=(\S+) message="(.*)"$')),
    ("pipe", re.compile(_TS + r" \| (\w+) \| (\S+) \| (\S+) \| (\w+) \| ?(.*)$")),
    ("kv", re.compile(_TS + r" (?:(?:DEBUG|INFO|WARN|ERROR) )?(.*)$")),
]


@dataclass(frozen=True)
class LogLine:
    ts: datetime
    service: str | None
    trace_id: str | None
    event: str | None
    fields: dict


def _kv(text: str) -> dict:
    out = {}
    for key, value in _KV.findall(text):
        if value.startswith('"'):
            value = value[1:-1].replace('\\"', '"')
        out[key] = value
    return out


def parse_log_line(line: str) -> LogLine | None:
    """Parse one line in any of the six log syntaxes; None for blank or unrecognized lines."""
    line = line.rstrip("\n")
    for name, pattern in _LOG_FORMATS:
        m = pattern.match(line)
        if not m:
            continue
        ts = parse_ts(m.group(1))
        if name == "json":
            f = json.loads(m.group(2))
            return LogLine(ts, f.pop("service", f.get("logger")), f.pop("trace_id", None), f.pop("event", None), f)
        if name == "syslog":
            f = _kv(m.group(5))
            return LogLine(ts, m.group(3), m.group(4), f.pop("event", None), f)
        if name in ("bracket", "pipe"):
            return LogLine(ts, m.group(3), m.group(4), m.group(5), _kv(m.group(6)))
        if name == "cloud":
            f = _kv(m.group(5).replace('\\"', '"'))
            return LogLine(ts, m.group(4), m.group(2), f.pop("event", None), f)
        f = _kv(m.group(2))
        return LogLine(ts, f.pop("service", None), f.pop("trace_id", None), f.pop("event", None), f)
    return None


def _rows_state(f: dict) -> str:
    return ABSENT if f.get("rows") == "0" or f.get("http_status") == "404" else PRESENT


def _flow_state(f: dict) -> str:
    return ACTIVE if _truthy(f.get("enabled")) else DISABLED


def _restore_state(f: dict) -> str:
    if "archive_present" in f and not _truthy(f["archive_present"]):
        return ABSENT
    return ACTIVE if _truthy(f.get("restore_allowed")) else BLOCKED


# event -> (state from fields, category, matched_value is exposed PII)
LOG_EVENTS: dict[str, tuple[Callable[[dict], str], str | None, bool]] = {
    "profile_lookup_completed": (_rows_state, "USER_PROFILE", False),
    "index_query_completed": (lambda f: ABSENT if f.get("result") == "miss" or f.get("total_hits") == "0" else PRESENT,
                              "SEARCH_INDEX_ENTRY", False),
    "analytics_identity_scan": (lambda f: PRESENT, "ANALYTICS_PROFILE", True),
    "analytics_profile_read": (lambda f: PRESENT, "ANALYTICS_PROFILE", False),
    "analytics_profile_read_completed": (lambda f: PRESENT, "ANALYTICS_PROFILE", False),
    # an opted-in contact record kept beside analytics: a marketing profile, not an analytics profile
    "contact_lookup_completed": (_rows_state, "MARKETING_PROFILE", False),
    "restore_policy_read_completed": (_restore_state, "BACKUP_ARCHIVE", False),
    "restore_candidate_evaluated": (lambda f: ACTIVE if _truthy(f.get("eligible")) and not _truthy(f.get("deletion_block"))
                                    else BLOCKED, "BACKUP_ARCHIVE", False),
    "restore_authorization_completed": (lambda f: BLOCKED if f.get("decision") == "deny" else ACTIVE,
                                        "BACKUP_ARCHIVE", False),
    "export_download_check_completed": (lambda f: PRESENT if _truthy(f.get("available")) else ABSENT, "USER_EXPORT", False),
    "export_metadata_read_completed": (lambda f: PRESENT if _truthy(f.get("available")) else ABSENT, "USER_EXPORT", False),
    "connector_config_loaded": (_flow_state, "DATA_FLOW", False),
    "connector_config_read_completed": (_flow_state, "DATA_FLOW", False),
    "connector_heartbeat": (_flow_state, "DATA_FLOW", False),
    "retained_order_loaded": (lambda f: PRESENT if f.get("access") != "denied" else ABSENT, "LEGAL_HOLD_RECORD", False),
    "audit_record_read_completed": (_rows_state, "SECURITY_AUDIT_LOG", False),
    "attachment_pii_scan": (lambda f: PRESENT, "SUPPORT_ATTACHMENT", True),
    "message_pii_scan": (lambda f: PRESENT, "SUPPORT_MESSAGE", True),
    "message_read": (lambda f: PRESENT, "SUPPORT_MESSAGE", False),
    "support_ticket_read_completed": (lambda f: PRESENT, "SUPPORT_MESSAGE", False),
    # a legacy contact-index entry: a secondary copy of the identifier, not a conversation
    "contact_query_completed": (_rows_state, "SECONDARY_IDENTIFIER", False),
    "deletion_marker_check": (lambda f: PRESENT if not _truthy(f.get("marker_found")) else ABSENT, None, False),
}


def _parse_log(b: _Builder) -> None:
    lines = [parsed for raw in b.ev.path.read_text(encoding="utf-8").splitlines()
             if (parsed := parse_log_line(raw)) is not None]
    # A lookup often names the person on its "started" line and the record on a later
    # line of the same trace, so every line inherits the identifiers of its whole trace.
    trace_links: dict[str, set[str]] = defaultdict(set)
    for line in lines:
        if line.trace_id:
            for key in REFERENCE_FIELDS:
                tagged = tag_identifier(line.fields.get(key))
                if tagged:
                    trace_links[line.trace_id].add(tagged)
    for line in lines:
        f = line.fields
        location = f"log:{line.service}/{line.event}"
        if line.event == "record_lookup":
            b.observe_listing(f, location, line.ts)
            continue
        handler = LOG_EVENTS.get(line.event or "")
        resource_id = f.get("resource_id", "")
        if handler is None or not system_for(resource_id):
            continue
        state_of, category, matched_is_pii = handler
        record = {k: v for k, v in f.items() if k != "matched_value" or not matched_is_pii}
        if matched_is_pii:
            record["email" if "@" in f.get("matched_value", "") else "phone"] = f.get("matched_value")
        b.observe_record(record, location, observed_at=line.ts, category=category, state=state_of(f),
                         extra_links=trace_links.get(line.trace_id or "", set()))


# --- PDF and images -----------------------------------------------------------

def _parse_pdf(b: _Builder) -> None:
    from pypdf import PdfReader  # imported lazily so the rest works without it

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(b.ev.path)).pages)
    b.result.text = text
    found = identifiers_in_text(text)
    exposed = {k for k in found if k.split(":")[0] in ("email", "phone")}
    for resource_id in sorted({k[4:] for k in found if k.startswith("res:")}):
        # a document naming a support record is that record's uploaded attachment
        category = "SUPPORT_ATTACHMENT" if system_for(resource_id) == "support_platform" else None
        b.observe(resource_id, PRESENT, "pdf", category=category, exposed=exposed, links=found)
    # A document about several people (a register) links identifiers row by row;
    # a document about one person links everything it mentions.
    if len({k for k in found if k.startswith("user:")}) > 1:
        for line in text.splitlines():
            b.associate(identifiers_in_text(line))
    else:
        b.associate(found)


IMAGE_STATES = {PRESENT, ABSENT, ACTIVE, DISABLED, BLOCKED, REDACTED, ANONYMIZED}


def _tag_displayed(value: str) -> str | None:
    """Tag a value read off a screenshot; plain words with a space are taken as a person's name."""
    tagged = tag_identifier(value)
    if tagged:
        return tagged
    text = value.strip()
    if " " in text and re.fullmatch(r"[^\W\d_][^\W\d_ .'-]*(?:[ .'-]+[^\W\d_]+)+", text):
        return f"name:{text.casefold()}"
    return None


def _parse_image(b: _Builder, extraction: dict | None) -> bool:
    """Turn a vision extraction (vision.py) into observations; False when not extracted."""
    if extraction is None:
        return False
    page_keys = {k for v in extraction.get("page_identifiers", []) if (k := _tag_displayed(v))}
    for rec in extraction.get("records", []):
        resource_id = rec.get("resource_id", "").strip()
        if not system_for(resource_id) or rec.get("state") not in IMAGE_STATES:
            continue  # ignore anything that is not a well-formed record
        keys = {k for v in rec.get("identifiers", []) if (k := _tag_displayed(v))} - {f"res:{resource_id}"}
        exposed = {k for k in keys if k.split(":")[0] in ("email", "phone", "hash", "name")}
        if rec["state"] in (REDACTED, ANONYMIZED, ABSENT):
            exposed = set()
        b.observe(resource_id, rec["state"], "image", exposed=exposed, links=keys | page_keys)
    b.associate(k for k in page_keys if not k.startswith("name:"))
    return True


# --- store --------------------------------------------------------------------

class EvidenceStore:
    """All observations and coverage from every evidence file, with lookup indexes."""

    def __init__(self, data: Dataset, image_extractions: dict[str, dict] | None = None) -> None:
        self.observations: list[Observation] = []
        self.coverage: list[Coverage] = []
        self.associations: list[Association] = []
        self.pending: list[str] = []  # evidence that still needs extraction (screenshots)
        self.per_file: dict[str, int] = {}
        self.text: dict[str, str] = {}  # file_id -> searchable text (not for screenshots)
        image_extractions = image_extractions or {}
        parsers = {"json": _parse_json, "txt": _parse_txt, "sql": _parse_sql, "log": _parse_log, "pdf": _parse_pdf}

        for ev in sorted(data.evidence.values(), key=lambda e: e.file_id):
            b = _Builder(ev)
            if ev.kind == "png":
                if not _parse_image(b, image_extractions.get(ev.file_id)):
                    self.pending.append(ev.file_id)
            else:
                parsers[ev.kind](b)
            self.observations.extend(b.result.observations)
            self.coverage.extend(b.result.coverage)
            self.associations.extend(b.result.associations)
            self.per_file[ev.file_id] = len(b.result.observations) + len(b.result.associations)
            if ev.kind != "png":
                self.text[ev.file_id] = b.result.text or ev.path.read_text(encoding="utf-8")
        self.captured_at = {ev.file_id: ev.captured_at for ev in data.evidence.values()}

        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.by_link: dict[str, set[str]] = defaultdict(set)  # tagged key -> resource_ids
        for obs in sorted(self.observations, key=lambda o: (o.observed_at, o.evidence_id)):
            self.by_resource[obs.resource_id].append(obs)
            for key in obs.links | obs.exposed:
                self.by_link[key].add(obs.resource_id)

    def files_mentioning(self, text: str, audit_at: datetime) -> list[str]:
        """Citable files (captured by audit_at) whose text contains the given reference."""
        return sorted(f for f, body in self.text.items() if text in body and self.captured_at[f] <= audit_at)

    def visible(self, resource_id: str, audit_at: datetime) -> list[Observation]:
        """Citable observations: from files captured at or before audit_at, oldest first."""
        return [o for o in self.by_resource.get(resource_id, []) if o.captured_at <= audit_at]

    def known(self, resource_id: str, audit_at: datetime) -> list[Observation]:
        """Facts that were true at or before audit_at, even if the file holding them was
        exported later. Usable to discover resources, never as a citation."""
        return [o for o in self.by_resource.get(resource_id, []) if o.observed_at <= audit_at]

    def resources_linked_to(self, keys: Iterable[str], audit_at: datetime) -> set[str]:
        """Resources whose visible observations carry any of the given tagged keys."""
        found = set()
        for key in keys:
            for resource_id in self.by_link.get(key, ()):
                if any(key in o.links | o.exposed for o in self.visible(resource_id, audit_at)):
                    found.add(resource_id)
        return found
