"""Persistence for Pickup/Put-Down operation history.

Each saved operation is one JSON file in the pickup ops cache directory.
File name: ``{uuid}.json``

Record schema
-------------
{
    "id":          "<uuid>",
    "label":       "BO-42 / Part name",   # display name (BO ref or part name)
    "query":       "BO-42",               # original search query
    "created_at":  "2026-04-16T10:00:00", # ISO-8601
    "updated_at":  "2026-04-16T10:05:00",
    "out": {
        "status":  "complete" | "incomplete" | "not_started",
        "items": [
            {
                "part_pk":   123,
                "part_name": "...",
                "location":  "...",
                "quantity":  1.0,
                "reference": "",
                "scanned":   false,
                "checked":   false
            },
            ...
        ]
    },
    "in": {
        "status":  "complete" | "incomplete" | "not_started",
        "items": [ ... ]    # same schema — independent from "out"
    }
}
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from ..config import settings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ops_dir() -> str:
    return settings.pickup_ops_dir


def _record_path(record_id: str) -> str:
    return os.path.join(_ops_dir(), f'{record_id}.json')


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _items_to_dicts(items) -> List[Dict]:
    """Convert a list of PickupItem objects to plain dicts."""
    result = []
    for it in items:
        result.append({
            'part_pk':   int(it.part_pk),
            'part_name': str(it.part_name),
            'location':  str(it.location),
            'quantity':  float(it.quantity),
            'reference': str(it.reference),
            'scanned':   bool(it.scanned),
            'checked':   bool(it.checked),
        })
    return result


def _status_of(items) -> str:
    if not items:
        return 'not_started'
    done = sum(1 for it in items if it.scanned or it.checked)
    return 'complete' if done == len(items) else 'incomplete'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_op(record_id: Optional[str], query: str, label: str,
            mode: str, items) -> str:
    """Persist a single mode's items into the operation record.

    Parameters
    ----------
    record_id   Existing record UUID to update, or ``None`` to create new.
    query       Original search string (e.g. ``'BO-42'``).
    label       Human-readable display name.
    mode        ``'out'`` or ``'in'``.
    items       List of PickupItem objects.

    Returns the record UUID (new or existing).
    """
    os.makedirs(_ops_dir(), exist_ok=True)

    if record_id:
        record = load_record(record_id) or {}
    else:
        record = {}

    now = _now_iso()
    if not record:
        record_id = record_id or str(uuid.uuid4())
        record = {
            'id':         record_id,
            'label':      label,
            'query':      query,
            'created_at': now,
            'updated_at': now,
            'out': {'status': 'not_started', 'items': []},
            'in':  {'status': 'not_started', 'items': []},
        }
    else:
        record['updated_at'] = now

    key = 'out' if mode == 'out' else 'in'
    record[key] = {
        'status': _status_of(items),
        'items':  _items_to_dicts(items),
    }

    with open(_record_path(record_id), 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2)

    return record_id


def load_record(record_id: str) -> Optional[Dict]:
    """Load a single record by id, or ``None`` if not found."""
    path = _record_path(record_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def list_records() -> List[Dict]:
    """Return all records sorted by ``updated_at`` descending (newest first)."""
    records = []
    try:
        for fname in os.listdir(_ops_dir()):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(_ops_dir(), fname)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    records.append(json.load(fh))
            except Exception:
                pass
    except Exception:
        pass
    records.sort(key=lambda r: r.get('updated_at', ''), reverse=True)
    return records


def delete_record(record_id: str) -> bool:
    """Delete a record file. Returns True on success."""
    path = _record_path(record_id)
    try:
        os.remove(path)
        return True
    except Exception:
        return False
