"""Turn a case's resource states into a deletion status and the ordered steps_json.

For each resource the required treatment comes from deletion_rules.csv through
its data category (DELETE, REDACT, ANONYMIZE, RETAIN). Data flows have no rule
row; an active flow must always be disabled. A resource is either:
- a violation: its state breaks the treatment (record still present, identifiers
  still exposed, flow active, backup restorable) -> finding + remediation +
  verification;
- compliant: it already has the required final state -> verification only.

Status: INCOMPLETE when any violation exists, otherwise COMPLETE. COMPLETE steps
cite the evidence proving the state; INCOMPLETE remediation and verification
describe future work and cite nothing.

Ordering (problem statement): phase FINDING -> REMEDIATION -> VERIFICATION, then
the operation order of the allowed-operations table, then system_id, resource_id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from loaders import DeletionRule
from state import CaseState, ResourceState

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OPERATION_STATE = {  # allowed operations in table order -> resulting state
    "REPORT_REMAINING_RECORD": "PRESENT",
    "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED",
    "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED",
    "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
OPERATION_ORDER = {op: i for i, op in enumerate(OPERATION_STATE)}
PHASE_OF = {op: ("FINDING" if op.startswith("REPORT_") else "VERIFICATION" if op.startswith("VERIFY_")
                 else "REMEDIATION") for op in OPERATION_STATE}

DEFAULT_CATEGORY = {  # when no evidence states a category, the system's main record type
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "support_platform": "SUPPORT_MESSAGE",
    "analytics_platform": "ANALYTICS_PROFILE",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": "DATA_FLOW",
    "object_storage": "SUPPORT_ATTACHMENT",  # uploaded files such as delivery confirmations
}
# required action -> (finding, remediation, verification)
TREATMENT = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN_BACKUP": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
}
DIRECT_IDENTIFIERS = ("email:", "phone:", "hash:", "name:")


@dataclass(frozen=True)
class Step:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    def sort_key(self) -> tuple:
        return PHASES.index(self.phase), OPERATION_ORDER[self.operation], self.system_id, self.resource_id


@dataclass(frozen=True)
class Decision:
    case_id: str
    deletion_status: str
    steps: list[Step]

    def steps_json(self) -> str:
        return serialize_steps(self.steps)


def serialize_steps(steps: list[Step]) -> str:
    return json.dumps([
        {"step_no": i, "phase": s.phase, "system_id": s.system_id, "resource_id": s.resource_id,
         "operation": s.operation, "state": s.state, "evidence_id": s.evidence_id}
        for i, s in enumerate(steps, start=1)
    ], ensure_ascii=False, separators=(",", ":"))


def required_action(resource: ResourceState, rules: dict[str, DeletionRule]) -> str:
    category = resource.category or DEFAULT_CATEGORY.get(resource.system)
    if category == "DATA_FLOW" or resource.system == "integration_service":
        return "DISABLE"
    rule = rules.get(category or "")
    action = rule.required_action if rule else "DELETE"
    if action == "RETAIN" and category == "BACKUP_ARCHIVE":
        return "RETAIN_BACKUP"
    return action


def _exposes(resource: ResourceState) -> bool:
    return resource.state == "IDENTIFIABLE" or (
        resource.state == "PRESENT" and any(k.startswith(DIRECT_IDENTIFIERS) for k in resource.exposed))


def assess(resource: ResourceState, action: str) -> str | None:
    """'violation', 'compliant', or None when the resource needs no step."""
    state = resource.state
    if action == "DELETE":
        if state in ("ABSENT",):
            return "compliant"
        if state == "UNKNOWN":  # the account is always verified, even without evidence
            return "compliant" if resource.system == "account_service" else None
        return "violation"
    if action in ("REDACT", "ANONYMIZE"):
        if state == "UNKNOWN":
            return None
        return "violation" if _exposes(resource) else "compliant"
    if action == "RETAIN":
        return "compliant" if state in ("PRESENT", "UNKNOWN") else None
    if action in ("RETAIN_BACKUP", "DISABLE"):
        if state == "ACTIVE":
            return "violation"
        return "compliant" if state in ("BLOCKED", "DISABLED") else None
    return None


def decide(case: CaseState, rules: dict[str, DeletionRule], authorized: bool = True) -> Decision:
    case_id = case.request.case_id
    if not authorized:
        return Decision(case_id, "NOT_APPLICABLE", [])

    assessed = []
    for resource in case.resources:
        action = required_action(resource, rules)
        verdict = assess(resource, action)
        if verdict == "violation" and not resource.evidence_id:
            # A finding must cite evidence available at audit_at; without it the
            # violation is unproven. The account is still verified; others are left out.
            verdict = "compliant" if resource.system == "account_service" else None
            action = "DELETE" if verdict else action
        if verdict:
            assessed.append((resource, action, verdict))
    incomplete = any(verdict == "violation" for _, _, verdict in assessed)

    steps: list[Step] = []
    for resource, action, verdict in assessed:
        finding, remediation, verification = TREATMENT[action]
        if verdict == "violation":
            steps.append(Step("FINDING", resource.system, resource.resource_id, finding,
                              OPERATION_STATE[finding], resource.evidence_id))
            steps.append(Step("REMEDIATION", resource.system, resource.resource_id, remediation,
                              OPERATION_STATE[remediation], ""))
        if action in ("REDACT", "ANONYMIZE") and verdict == "compliant" and resource.state == "ABSENT":
            verification = "VERIFY_RECORD_ABSENT"  # removed entirely, which also satisfies the rule
        steps.append(Step("VERIFICATION", resource.system, resource.resource_id, verification,
                          OPERATION_STATE[verification], "" if incomplete else resource.evidence_id))
    steps.sort(key=Step.sort_key)
    return Decision(case_id, "INCOMPLETE" if incomplete else "COMPLETE", steps)
