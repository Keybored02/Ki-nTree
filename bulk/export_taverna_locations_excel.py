#!/usr/bin/env python3
"""Export Rack and Shelf locations under Genesy / Taverna to an Excel file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from openpyxl import Workbook
from openpyxl.styles import Font
import requests

from kintree.config import settings
from kintree.database import inventree_api


def _headers(api_obj) -> Dict[str, str]:
    token = getattr(api_obj, "token", None)
    if not token:
        raise RuntimeError("Missing API token after login")
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _fetch_all(
    base: str, headers: Dict[str, str], path: str, timeout: int = 30
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    url = _url(base, path)
    params: Dict[str, Any] = {}

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and "results" in data:
            chunk = data.get("results") or []
            if not isinstance(chunk, list):
                raise RuntimeError(f"Unexpected paginated response for {path}")
            rows.extend(chunk)
            url = data.get("next")
            params = {}
        elif isinstance(data, list):
            rows.extend(data)
            break
        else:
            raise RuntimeError(f"Unexpected response format for {path}")

    return rows


def _find_location_by_path(
    all_locations: List[Dict[str, Any]], target_path: str
) -> Optional[Dict[str, Any]]:
    by_pk = {int(loc["pk"]): loc for loc in all_locations if loc.get("pk") is not None}
    memo: Dict[int, str] = {}

    def build_path(pk: int) -> str:
        if pk in memo:
            return memo[pk]
        loc = by_pk.get(pk)
        if not loc:
            return ""
        name = str(loc.get("name") or "")
        parent = loc.get("parent")
        if parent is None:
            memo[pk] = name
            return name
        parent_path = build_path(int(parent))
        memo[pk] = f"{parent_path} / {name}" if parent_path else name
        return memo[pk]

    for pk in by_pk:
        if build_path(pk) == target_path:
            return by_pk[pk]
    return None


def _collect_descendants(
    all_locations: List[Dict[str, Any]], root_pk: int
) -> List[Dict[str, Any]]:
    children: Dict[int, List[int]] = {}
    by_pk = {int(loc["pk"]): loc for loc in all_locations if loc.get("pk") is not None}

    for loc in all_locations:
        pk = loc.get("pk")
        parent = loc.get("parent")
        if pk is None or parent is None:
            continue
        children.setdefault(int(parent), []).append(int(pk))

    keep: Set[int] = set()
    stack = [root_pk]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in keep:
                keep.add(child)
                stack.append(child)

    return [by_pk[pk] for pk in sorted(keep) if pk in by_pk]


def _barcode_text(row: Dict[str, Any]) -> str:
    level = int(row.get("level") or -1)
    name = str(row.get("name") or "")
    path = str(row.get("pathstring") or "")

    if level == 2:
        return name

    if level == 3:
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if len(parts) >= 2:
            rack = parts[-2]
            shelf = parts[-1]
            return f"{rack}-{shelf}"
    return name


def main() -> int:
    settings.load_inventree_settings()
    if not settings.SERVER_ADDRESS or not settings.USERNAME or not settings.PASSWORD:
        print("Missing InvenTree credentials in settings")
        return 2

    connected = inventree_api.connect(
        server=settings.SERVER_ADDRESS,
        username=settings.USERNAME,
        password=settings.PASSWORD,
        connect_timeout=10,
        silent=False,
        proxies=settings.PROXIES if getattr(settings, "ENABLE_PROXY", False) else None,
    )
    if not connected:
        print("Could not authenticate to InvenTree")
        return 1

    api_obj = inventree_api.inventree_api
    headers = _headers(api_obj)
    base_url = getattr(api_obj, "base_url", settings.SERVER_ADDRESS)

    all_locations = _fetch_all(base_url, headers, "api/stock/location/")
    location_types = _fetch_all(base_url, headers, "api/stock/location-type/")
    type_map = {
        int(t["pk"]): str(t.get("name") or "")
        for t in location_types
        if t.get("pk") is not None
    }

    taverna = _find_location_by_path(all_locations, "Genesy / Taverna")
    if not taverna:
        print("Could not find location path: Genesy / Taverna")
        return 1

    descendants = _collect_descendants(all_locations, int(taverna["pk"]))

    filtered: List[Dict[str, Any]] = []
    for row in descendants:
        loc_type_id = row.get("location_type")
        loc_type_name = (
            type_map.get(int(loc_type_id), "") if loc_type_id is not None else ""
        )
        if loc_type_name not in {"Rack", "Shelf"}:
            continue
        filtered.append(row)

    filtered.sort(
        key=lambda r: (int(r.get("level") or 0), str(r.get("pathstring") or ""))
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "Taverna Rack-Shelf"

    headers_row = [
        "pk",
        "name",
        "description",
        "pathstring",
        "level",
        "location_type",
        "barcode_text",
        "barcode_hash",
    ]
    ws.append(headers_row)

    for col in ws[1]:
        col.font = Font(bold=True)

    for row in filtered:
        loc_type_id = row.get("location_type")
        loc_type_name = (
            type_map.get(int(loc_type_id), "") if loc_type_id is not None else ""
        )
        ws.append(
            [
                row.get("pk"),
                row.get("name"),
                row.get("description"),
                row.get("pathstring"),
                row.get("level"),
                loc_type_name,
                _barcode_text(row),
                row.get("barcode_hash"),
            ]
        )

    for column_cells in ws.columns:
        length = max(
            len(str(cell.value)) if cell.value is not None else 0
            for cell in column_cells
        )
        ws.column_dimensions[column_cells[0].column_letter].width = min(
            max(length + 2, 12), 60
        )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path.cwd() / f"taverna_racks_shelves_{ts}.xlsx"
    wb.save(out_path)

    print(f"Exported {len(filtered)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
