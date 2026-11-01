"""Load the participant-facing CSV files under dataset/ into typed records.

Only files inside dataset/ are read. Every file's header is checked against the
expected columns so a schema change fails loudly instead of silently producing
wrong answers. Timestamps are parsed into timezone-aware UTC datetimes so they
can be compared directly against each case's audit_at cutoff.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"

REQUEST_COLUMNS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
IDENTITY_COLUMNS = ["user_id", "name", "email", "phone", "email_hash", "updated_at"]
AUDIT_LOG_COLUMNS = ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"]
ACTIVITY_COLUMNS = [
    "activity_id", "event_time", "event_type", "source_system",
    "target_system", "resource_id", "reported_result", "details",
]
RULE_COLUMNS = ["rule_id", "data_category", "condition", "required_action"]
EVIDENCE_COLUMNS = ["file_id", "file_path", "origin", "captured_at"]
OUTPUT_COLUMNS = ["case_id", "deletion_status", "steps_json"]


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' timestamp into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DeletionRequest:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str  # a workflow claim, never proof
    reported_at: datetime
    audit_at: datetime  # evidence after this instant must be ignored
    request_text: str


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime


@dataclass(frozen=True)
class IdentityChange:
    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str  # email, phone or email_hash
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str  # a claim: SUCCESS / FAILED / PARTIAL
    details: str


@dataclass(frozen=True)
class DeletionRule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str  # DELETE / REDACT / ANONYMIZE / RETAIN


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    relative_path: str  # as written in the manifest, relative to dataset/
    path: Path  # absolute path on disk
    origin: str
    captured_at: datetime

    @property
    def kind(self) -> str:
        """File extension without the dot, e.g. 'sql', 'json', 'log', 'png', 'pdf'."""
        return self.path.suffix.lstrip(".").lower()


@dataclass(frozen=True)
class Dataset:
    requests: list[DeletionRequest]
    identities: dict[str, UserIdentity]  # keyed by user_id
    identity_changes: list[IdentityChange]  # sorted by event_time
    activities: list[Activity]  # sorted by event_time
    rules: dict[str, DeletionRule]  # keyed by data_category
    evidence: dict[str, EvidenceFile]  # keyed by file_id


def _read_rows(path: Path, expected_columns: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_columns:
            raise ValueError(f"{path.name}: expected columns {expected_columns}, found {reader.fieldnames}")
        return list(reader)


def _unique_by(records: list, key: str, source: str) -> dict:
    keyed = {}
    for record in records:
        value = getattr(record, key)
        if value in keyed:
            raise ValueError(f"{source}: duplicate {key} {value!r}")
        keyed[value] = record
    return keyed


def load_requests(path: Path) -> list[DeletionRequest]:
    requests = [
        DeletionRequest(
            case_id=row["case_id"],
            email=row["email"].strip(),
            requested_at=parse_ts(row["requested_at"]),
            reported_status=row["reported_status"],
            reported_at=parse_ts(row["reported_at"]),
            audit_at=parse_ts(row["audit_at"]),
            request_text=row["request_text"],
        )
        for row in _read_rows(path, REQUEST_COLUMNS)
    ]
    _unique_by(requests, "case_id", path.name)
    return requests


def load_identities(path: Path) -> dict[str, UserIdentity]:
    identities = [
        UserIdentity(
            user_id=row["user_id"],
            name=row["name"],
            email=row["email"].strip(),
            phone=row["phone"],
            email_hash=row["email_hash"],
            updated_at=parse_ts(row["updated_at"]),
        )
        for row in _read_rows(path, IDENTITY_COLUMNS)
    ]
    return _unique_by(identities, "user_id", path.name)


def load_identity_changes(path: Path) -> list[IdentityChange]:
    changes = [
        IdentityChange(
            audit_id=row["audit_id"],
            user_id=row["user_id"],
            event_time=parse_ts(row["event_time"]),
            field_name=row["field_name"],
            old_value=row["old_value"].strip(),
            new_value=row["new_value"].strip(),
        )
        for row in _read_rows(path, AUDIT_LOG_COLUMNS)
    ]
    _unique_by(changes, "audit_id", path.name)
    return sorted(changes, key=lambda c: (c.event_time, c.audit_id))


def load_activities(path: Path) -> list[Activity]:
    activities = [
        Activity(
            activity_id=row["activity_id"],
            event_time=parse_ts(row["event_time"]),
            event_type=row["event_type"],
            source_system=row["source_system"],
            target_system=row["target_system"],
            resource_id=row["resource_id"],
            reported_result=row["reported_result"],
            details=row["details"],
        )
        for row in _read_rows(path, ACTIVITY_COLUMNS)
    ]
    _unique_by(activities, "activity_id", path.name)
    return sorted(activities, key=lambda a: (a.event_time, a.activity_id))


def load_rules(path: Path) -> dict[str, DeletionRule]:
    rules = [DeletionRule(**row) for row in _read_rows(path, RULE_COLUMNS)]
    _unique_by(rules, "rule_id", path.name)
    return _unique_by(rules, "data_category", path.name)


def load_evidence_manifest(path: Path, dataset_dir: Path) -> dict[str, EvidenceFile]:
    files = []
    for row in _read_rows(path, EVIDENCE_COLUMNS):
        absolute = (dataset_dir / row["file_path"]).resolve()
        if not absolute.is_file():
            raise FileNotFoundError(f"{path.name}: {row['file_id']} points to missing file {row['file_path']}")
        files.append(
            EvidenceFile(
                file_id=row["file_id"],
                relative_path=row["file_path"],
                path=absolute,
                origin=row["origin"],
                captured_at=parse_ts(row["captured_at"]),
            )
        )
    return _unique_by(files, "file_id", path.name)


def load_dataset(dataset_dir: Path = DATASET_DIR, requests_file: str = "deletion_requests.csv") -> Dataset:
    """Load every context file plus the chosen request file.

    Pass requests_file="sample_deletion_requests.csv" to run against the public samples.
    """
    return Dataset(
        requests=load_requests(dataset_dir / requests_file),
        identities=load_identities(dataset_dir / "user_identities.csv"),
        identity_changes=load_identity_changes(dataset_dir / "user_audit_log.csv"),
        activities=load_activities(dataset_dir / "system_activity.csv"),
        rules=load_rules(dataset_dir / "deletion_rules.csv"),
        evidence=load_evidence_manifest(dataset_dir / "evidence_files.csv", dataset_dir),
    )
