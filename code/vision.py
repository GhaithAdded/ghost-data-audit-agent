"""Read screenshot evidence with a vision model into the same facts as other evidence.

Each screenshot is an internal admin page (customer profile, connector list, backup
catalog, search results, audit events...). The model lists every record shown with
its resource ID, a normalized state, and the identifiers displayed for that record.
evidence.py turns the result into Observations exactly like a parsed JSON or log file.
Several screenshots are sent per call to stay within the free-tier request quota.
"""

from __future__ import annotations

import sys

from llm import LLM, LLMUnavailable
from loaders import Dataset

IMAGES_PER_CALL = 4

PROMPT = """You get {count} screenshots of internal admin pages of an online shop, numbered 0 to {last}
in the order given. Treat each screenshot separately. For each one, extract every data record
that has a resource ID shown on the page. Resource IDs look like account_123, search_45,
support_9538, analytics_7438, data_flow_1121, backup_9287, export_88, audit_record_003,
tombstone_17, legal_hold_4743, cache_12, email_delivery_7, marketing_3.

For each record give:
- resource_id: exactly as shown.
- state, one of:
  ACTIVE     for a data flow / connector / sync that is running, enabled, healthy or continuous,
             or for a backup / archive that can still be restored (no restore block shown);
  DISABLED   for a data flow that is disabled, paused or stopped;
  BLOCKED    for a backup whose restore is blocked or denied;
  REDACTED   for a record shown with its personal identifiers removed;
  ANONYMIZED for an analytics record shown without any link to a person;
  ABSENT     for a record shown as deleted or not found;
  PRESENT    for any other record that exists.
- identifiers: every email address, phone number, person name, user ID (user_123), order
  reference (ORD-...) and support ticket reference (CS-...) displayed for THAT record: in its
  own table row, or in the page header / detail panel when that panel describes the record or
  the customer the record belongs to. Do not attach identifiers of other rows.

Also give page_identifiers: the identifiers of the single customer or subject the whole page
is about (from the header, title or filter), or an empty list if the page lists many people.
Copy every value exactly as displayed. Return one entry per screenshot with its image_index."""

RECORD = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string"},
        "state": {"type": "string", "enum": ["ACTIVE", "DISABLED", "BLOCKED", "REDACTED",
                                             "ANONYMIZED", "ABSENT", "PRESENT"]},
        "identifiers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["resource_id", "state", "identifiers"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "screenshots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_index": {"type": "integer"},
                    "records": {"type": "array", "items": RECORD},
                    "page_identifiers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["image_index", "records", "page_identifiers"],
            },
        }
    },
    "required": ["screenshots"],
}


def extract_screenshots(data: Dataset, llm: LLM) -> dict[str, dict]:
    """file_id -> {"records": [...], "page_identifiers": [...]} for every PNG evidence file."""
    images = [ev for ev in sorted(data.evidence.values(), key=lambda e: e.file_id) if ev.kind == "png"]
    groups = [images[i:i + IMAGES_PER_CALL] for i in range(0, len(images), IMAGES_PER_CALL)]

    def read(group) -> dict | None:
        prompt = PROMPT.format(count=len(group), last=len(group) - 1)
        try:
            return llm.generate_json("screenshot", prompt, SCHEMA, images=[ev.path for ev in group])
        except LLMUnavailable as exc:
            print(f"WARNING: screenshots {[ev.file_id for ev in group]} not read ({exc}); they stay pending. "
                  "Rerun to fill the cache.", file=sys.stderr)
            return None

    extractions = {}
    for group, answer in zip(groups, llm.map(read, groups)):
        if answer is None:
            continue
        by_index = {shot["image_index"]: shot for shot in answer["screenshots"]}
        for i, ev in enumerate(group):
            if i in by_index:
                extractions[ev.file_id] = by_index[i]
    return extractions
