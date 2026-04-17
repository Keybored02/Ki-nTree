"""BOM / build-order resolution logic for the Pickup view.

Flow
----
1. ``resolve_query`` detects BO-… reference vs free-text part name.
2. Build order → get root part → collect BOM + recurse into child BOs.
   Part name   → search, pick best match → collect BOM.
3. BOM recursion walks sub-assemblies up to MAX_DEPTH = 5 levels.
4. Each leaf is resolved to a location:
     - actual StockItem location (first result)  → fallback → default_location
5. Returns ``(ok, message, items_list)``.

Auth is handled by ``inventree_interface.connect_to_server()`` — the same
call used by every other view in the app.  No separate session or token
management here.
"""

from __future__ import annotations

import re
import threading
from typing import Dict, List, Tuple

from inventree.build import Build
from inventree.part import BomItem
from inventree.part import Part
from inventree.stock import StockItem

from ..common.tools import cprint
from ..database import inventree_api as _inv_module
from ..database import inventree_interface
from ..database.inventree_api import get_inventree_api

MAX_DEPTH = 5

# ---------------------------------------------------------------------------
# Auth — delegate entirely to the existing connect flow
# ---------------------------------------------------------------------------


def _get_api():
    """Ensure connection and return the live InvenTreeAPI object, or None."""
    # connect_to_server is a no-op when already connected (fast-path check inside)
    if not inventree_interface.connect_to_server():
        cprint("[PICKUP]\tCould not connect to InvenTree server", silent=False)
        return None
    api = get_inventree_api()
    cprint(
        f"[PICKUP]\tAPI object: {type(api).__name__ if api else 'None'}, token set: {bool(api)}",
        silent=False,
    )
    return api


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

_location_cache: Dict[int, List[str]] = {}
_location_cache_lock = threading.Lock()


def _location_pk_to_path(loc_pk: int) -> str:
    """Build a full path string for a location pk using the existing tree walker."""
    try:
        tree = _inv_module.get_stock_location_tree(loc_pk)
        names = [str(n) for n in reversed(list(tree.values())) if str(n).strip()]
        return "/".join(names) if names else str(loc_pk)
    except Exception as exc:
        cprint(f"[PICKUP]\t  location tree failed for pk {loc_pk}: {exc}", silent=False)
        return str(loc_pk)


def _resolve_locations(api, part_pk: int) -> List[str]:
    """Return all distinct location path strings for *part_pk*.

    Collects every stock item's location, deduplicates, then falls back to
    the part's default_location if no stock items have a location set.
    Returns ``['(no location)']`` as a last resort.
    """
    with _location_cache_lock:
        if part_pk in _location_cache:
            return _location_cache[part_pk]

    seen_pks: set = set()
    locations: List[str] = []

    # All stock items for this part
    try:
        stock_items = StockItem.list(api, part=part_pk)
        cprint(
            f"[PICKUP]\t  stock items for part {part_pk}: {len(stock_items)}",
            silent=False,
        )
        for si in stock_items:
            loc_pk = getattr(si, "location", None)
            if isinstance(loc_pk, dict):
                loc_pk = loc_pk.get("pk") or loc_pk.get("id")
            if loc_pk and int(loc_pk) not in seen_pks:
                seen_pks.add(int(loc_pk))
                path = _location_pk_to_path(int(loc_pk))
                if path:
                    locations.append(path)
    except Exception as exc:
        cprint(f"[PICKUP]\t  StockItem.list failed for part {part_pk}: {exc}", silent=False)

    # Fallback: part default_location
    if not locations:
        try:
            part_obj = Part(api, part_pk)
            def_loc = getattr(part_obj, "default_location", None)
            if isinstance(def_loc, dict):
                def_loc = def_loc.get("pk") or def_loc.get("id")
            if def_loc and int(def_loc) not in seen_pks:
                path = _location_pk_to_path(int(def_loc))
                if path:
                    locations.append(path)
        except Exception as exc:
            cprint(
                f"[PICKUP]\t  default_location fallback failed for part {part_pk}: {exc}",
                silent=False,
            )

    if not locations:
        locations = ["(no location)"]

    cprint(f"[PICKUP]\t  resolved locations for part {part_pk}: {locations}", silent=False)

    with _location_cache_lock:
        _location_cache[part_pk] = locations

    return locations


# ---------------------------------------------------------------------------
# BOM collection
# ---------------------------------------------------------------------------


def _collect_bom(
    api,
    part_pk: int,
    quantity_multiplier: float,
    depth: int,
    visited_parts: set,
    results: Dict[int, dict],
):
    """Recursively collect BOM items for *part_pk* into *results*."""
    if depth > MAX_DEPTH or part_pk in visited_parts:
        return
    visited_parts.add(part_pk)

    try:
        bom_items = BomItem.list(api, part=part_pk)
    except Exception as exc:
        cprint(f"[PICKUP]\t  BomItem.list failed for part {part_pk}: {exc}", silent=False)
        return

    cprint(
        f"[PICKUP]\t  BOM depth={depth} part={part_pk}: {len(bom_items)} item(s)",
        silent=False,
    )

    if not bom_items:
        visited_parts.discard(part_pk)
        return

    for item in bom_items:
        sub_pk = getattr(item, "sub_part", None)
        if isinstance(sub_pk, dict):
            sub_pk = sub_pk.get("pk") or sub_pk.get("id")
        if not sub_pk:
            continue
        sub_pk = int(sub_pk)
        qty = float(getattr(item, "quantity", 1) or 1) * quantity_multiplier

        try:
            sub_part = Part(api, sub_pk)
            is_assembly = bool(getattr(sub_part, "assembly", False))
            sub_name = str(
                getattr(sub_part, "name", "") or getattr(sub_part, "IPN", "") or str(sub_pk)
            )
        except Exception as exc:
            cprint(f"[PICKUP]\t  Part({sub_pk}) lookup failed: {exc}", silent=False)
            is_assembly = False
            sub_name = str(sub_pk)

        if is_assembly and depth < MAX_DEPTH and sub_pk not in visited_parts:
            cprint(
                f"[PICKUP]\t  sub-assembly {sub_pk} ({sub_name}), recursing…",
                silent=False,
            )
            _collect_bom(api, sub_pk, qty, depth + 1, visited_parts, results)
        else:
            if sub_pk in results:
                results[sub_pk]["quantity"] += qty
            else:
                results[sub_pk] = {
                    "part_pk": sub_pk,
                    "part_name": sub_name,
                    "quantity": qty,
                    "location": "",
                    "reference": "",
                }


def _collect_from_build(
    api,
    bo_pk: int,
    quantity_multiplier: float,
    depth: int,
    visited_builds: set,
    results: Dict[int, dict],
):
    """Walk a build order and its child build orders, collecting BOM items."""
    if depth > MAX_DEPTH or bo_pk in visited_builds:
        return
    visited_builds.add(bo_pk)

    try:
        bo = Build(api, bo_pk)
    except Exception as exc:
        cprint(f"[PICKUP]\t  Build({bo_pk}) fetch failed: {exc}", silent=False)
        return

    bo_qty = float(getattr(bo, "quantity", 1) or 1) * quantity_multiplier
    root_part_pk = getattr(bo, "part", None)
    if isinstance(root_part_pk, dict):
        root_part_pk = root_part_pk.get("pk") or root_part_pk.get("id")

    cprint(f"[PICKUP]\tBuild {bo_pk}: root_part={root_part_pk} qty={bo_qty}", silent=False)

    if root_part_pk:
        _collect_bom(api, int(root_part_pk), bo_qty, depth, set(), results)

    try:
        child_builds = Build.list(api, parent=bo_pk)
        cprint(f"[PICKUP]\tBuild {bo_pk}: {len(child_builds)} child build(s)", silent=False)
        for child in child_builds:
            child_pk = getattr(child, "pk", None) or getattr(child, "id", None)
            if child_pk:
                _collect_from_build(api, int(child_pk), 1.0, depth + 1, visited_builds, results)
    except Exception as exc:
        cprint(f"[PICKUP]\t  child Build.list failed for bo {bo_pk}: {exc}", silent=False)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_BO_PATTERN = re.compile(r"^BO-\d+$", re.IGNORECASE)


def resolve_query(query: str) -> Tuple[bool, str, List[dict]]:
    """Resolve a search query to a flat BOM item list.

    Returns ``(ok, message, items)`` where each item is a dict with keys:
    ``part_pk, part_name, location, quantity, reference``.
    """
    query = (query or "").strip()
    if not query:
        return False, "Empty query", []

    api = _get_api()
    if not api:
        return False, "Not connected to InvenTree — check server settings", []

    # Always clear location cache on a new query so stale data never persists
    clear_location_cache()

    results: Dict[int, dict] = {}

    if _BO_PATTERN.match(query):
        cprint(f"[PICKUP]\tQuery is a build order reference: {query}", silent=False)
        try:
            builds = Build.list(api, reference=query)
        except Exception as exc:
            cprint(f"[PICKUP]\tBuild.list failed: {exc}", silent=False)
            return False, f"Error querying build orders: {exc}", []

        cprint(f"[PICKUP]\tBuild.list returned {len(builds)} result(s)", silent=False)
        if not builds:
            return False, f'Build order "{query}" not found', []

        bo = builds[0]
        bo_pk = int(getattr(bo, "pk", 0) or getattr(bo, "id", 0))
        bo_ref = str(getattr(bo, "reference", query) or query)
        cprint(f"[PICKUP]\tUsing build order pk={bo_pk} ref={bo_ref}", silent=False)

        _collect_from_build(api, bo_pk, 1.0, 0, set(), results)
        label = f"Build order {bo_ref}"
    else:
        cprint(f"[PICKUP]\tQuery is a part name search: {query}", silent=False)
        try:
            parts = Part.list(api, search=query, assembly=True, limit=10)
        except Exception as exc:
            cprint(f"[PICKUP]\tPart.list (assembly) failed: {exc}", silent=False)
            parts = []

        if not parts:
            try:
                parts = Part.list(api, search=query, limit=10)
            except Exception as exc:
                cprint(f"[PICKUP]\tPart.list (open) failed: {exc}", silent=False)
                return False, f"Error searching parts: {exc}", []

        cprint(f"[PICKUP]\tPart.list returned {len(parts)} result(s)", silent=False)
        if not parts:
            return False, f'No parts found for "{query}"', []

        query_lower = query.lower()
        best = next(
            (p for p in parts if str(getattr(p, "name", "") or "").lower() == query_lower),
            parts[0],
        )
        part_pk = int(getattr(best, "pk", 0) or getattr(best, "id", 0))
        part_name = str(getattr(best, "name", "") or getattr(best, "IPN", "") or query)
        cprint(f"[PICKUP]\tUsing part pk={part_pk} name={part_name}", silent=False)

        _collect_bom(api, part_pk, 1.0, 0, set(), results)
        label = f"Part: {part_name}"

    if not results:
        return False, f"{label} — BOM is empty or has no resolvable items", []

    cprint(f"[PICKUP]\tResolving locations for {len(results)} part(s)…", silent=False)
    items = []
    for part_pk, entry in results.items():
        entry["locations"] = _resolve_locations(api, part_pk)
        entry["quantity"] = round(entry["quantity"], 4)
        items.append(entry)

    items.sort(key=lambda x: (x["locations"] == ["(no location)"], x["part_name"].lower()))
    cprint(f"[PICKUP]\tDone — {len(items)} item(s) for {label}", silent=False)
    return True, f"{label} — {len(items)} item(s)", items


def clear_location_cache():
    """Invalidate the location cache (call after stock moves)."""
    with _location_cache_lock:
        _location_cache.clear()
