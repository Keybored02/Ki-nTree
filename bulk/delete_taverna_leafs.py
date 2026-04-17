#!/usr/bin/env python3
"""
Delete only leaf locations (B0x boxes) under Taverna parent.
Keeps R01-R09 racks and their L0x shelves intact.

Examples:
  python delete_taverna_leafs.py --dry-run
  python delete_taverna_leafs.py --confirm
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Dict, List

import requests

# Add parent directory to path so we can import kintree
sys.path.insert(0, str(Path(__file__).parent.parent))


from bulk_delete_locations import _api_url
from bulk_delete_locations import _build_auth_headers
from bulk_delete_locations import delete_location

from kintree.config import settings
from kintree.database import inventree_api


def fetch_all_locations(
    base_url: str,
    headers: Dict[str, str],
    filters: Dict[str, Any],
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch all locations from InvenTree, handling pagination.

    Returns list of location objects.
    """
    all_locations: List[Dict[str, Any]] = []
    url = _api_url(base_url, "api/stock/location/")
    params = dict(filters)

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        # DRF pagination format: {count, next, previous, results}
        if isinstance(data, dict) and "results" in data:
            results = data.get("results") or []
            if not isinstance(results, list):
                raise RuntimeError(
                    "Unexpected response format: 'results' is not a list"
                )
            all_locations.extend(results)
            url = data.get("next")
            params = {}  # next URL already encodes query params
        elif isinstance(data, list):
            all_locations.extend(data)
            break
        else:
            raise RuntimeError("Unexpected response format from InvenTree")

    return all_locations


def find_taverna_parent(locations: List[Dict[str, Any]]) -> int | None:
    """Find Taverna parent location PK."""
    for loc in locations:
        if loc.get("name") == "Taverna":
            return loc.get("pk")
    return None


def find_leaf_locations(
    locations: List[Dict[str, Any]],
    taverna_pk: int,
) -> List[int]:
    """
    Find all leaf locations (B0x boxes) under Taverna.
    Pattern: R0x-L0x-B0x naming pattern.

    Returns list of PKs for locations that match the pattern and have no sublocations.
    """
    leaf_pks = []

    # Build parent map for hierarchy
    loc_map = {loc.get("pk"): loc for loc in locations if loc.get("pk")}

    for loc in locations:
        pk = loc.get("pk")
        name = loc.get("name", "")
        parent_pk = loc.get("parent")
        sublocations = loc.get("sublocations", 0)

        # Skip if has children (not a leaf)
        if sublocations > 0:
            continue

        # Check if this is a B0x under the Taverna hierarchy
        # by checking the path and name pattern
        if "-B" not in name:
            continue

        # Verify it's under Taverna by walking up the hierarchy
        current_pk = parent_pk
        is_under_taverna = False

        for _ in range(10):  # Prevent infinite loops, max depth 10
            if current_pk is None:
                break

            parent_loc = loc_map.get(current_pk)
            if not parent_loc:
                break

            if parent_loc.get("pk") == taverna_pk:
                is_under_taverna = True
                break

            current_pk = parent_loc.get("parent")

        if is_under_taverna:
            leaf_pks.append(pk)

    return sorted(leaf_pks)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete leaf locations (B0x) under Taverna parent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview locations to delete; do not delete",
    )
    parser.add_argument(
        "--confirm", action="store_true", help="Actually execute deletion"
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout in seconds"
    )

    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        print("ERROR: refusing to run without --dry-run or --confirm")
        print("Tip: start with --dry-run to preview")
        return 2

    # Load InvenTree credentials
    settings.load_inventree_settings()

    if not settings.SERVER_ADDRESS:
        print("ERROR: Missing InvenTree SERVER_ADDRESS in config")
        return 2

    if not settings.USERNAME or not settings.PASSWORD:
        print("ERROR: Missing InvenTree USERNAME/PASSWORD in config")
        return 2

    # Connect
    print(f"Connecting to InvenTree: {settings.SERVER_ADDRESS}")
    connected = inventree_api.connect(
        server=settings.SERVER_ADDRESS,
        username=settings.USERNAME,
        password=settings.PASSWORD,
        connect_timeout=max(5, args.timeout),
        silent=False,
        proxies=settings.PROXIES if getattr(settings, "ENABLE_PROXY", False) else None,
    )

    if not connected:
        print("ERROR: Could not authenticate to InvenTree")
        return 1

    api_obj = inventree_api.inventree_api
    headers = _build_auth_headers(api_obj)
    base_url = getattr(api_obj, "base_url", settings.SERVER_ADDRESS)

    # Fetch all locations
    print("\nFetching all locations...")
    try:
        locations = fetch_all_locations(base_url, headers, {}, timeout=args.timeout)
    except Exception as exc:
        print(f"ERROR: failed to fetch locations: {repr(exc)}")
        return 1

    print(f"Found {len(locations)} total locations")

    # Find Taverna parent
    taverna_pk = find_taverna_parent(locations)
    if not taverna_pk:
        print("ERROR: Could not find 'Taverna' location")
        return 1

    print(f"Found Taverna at pk={taverna_pk}")

    # Find leaf locations (B0x boxes)
    leaf_pks = find_leaf_locations(locations, taverna_pk)

    if not leaf_pks:
        print("No leaf locations found to delete")
        return 0

    # Build location map for display
    loc_map = {loc.get("pk"): loc for loc in locations if loc.get("pk")}

    # Print preview
    print(f"\nPreview: {len(leaf_pks)} leaf locations to DELETE")
    print("-" * 70)
    for pk in sorted(leaf_pks)[:30]:
        name = loc_map.get(pk, {}).get("name", "?")
        print(f"  pk={pk:4d} name={name}")
    if len(leaf_pks) > 30:
        print(f"  ... and {len(leaf_pks) - 30} more")
    print("-" * 70)

    if args.dry_run:
        print("Dry-run complete. No deletions performed.")
        return 0

    # Try bulk-delete endpoint first (faster)
    print("\nAttempting bulk-delete endpoint...")
    bulk_endpoint = _api_url(base_url, "api/stock/location/bulk-delete/")
    payload = {"items": leaf_pks}

    print(f"Endpoint: {bulk_endpoint}")
    print(f"Payload: items={len(leaf_pks)} locations")

    try:
        response = requests.delete(
            bulk_endpoint,
            headers=headers,
            json=payload,
            timeout=args.timeout * 10,  # Increase timeout for bulk operation
        )

        print(f"Response status: {response.status_code}")
        if response.text:
            print(f"Response body: {response.text[:500]}")

        if response.status_code in [200, 202, 204]:
            print(f"\nBulk-delete successful! Deleted {len(leaf_pks)} locations")
            return 0
        elif response.status_code == 404:
            print(
                "Bulk-delete endpoint not available, falling back to sequential deletion..."
            )
        else:
            print(f"Bulk-delete failed (HTTP {response.status_code})")
            print("Falling back to sequential deletion...")
    except Exception as exc:
        print(f"Bulk-delete request failed: {repr(exc)}")
        print("Falling back to sequential deletion...")

    # Fallback: sequential deletion in reverse order
    deleted = 0
    failed = 0

    print("\nDeleting locations sequentially (reverse order)...")
    for pk in reversed(leaf_pks):
        try:
            delete_location(base_url, headers, pk, timeout=args.timeout)
            deleted += 1
            name = loc_map.get(pk, {}).get("name", "?")
            print(f"  [OK] Deleted pk={pk} {name}")
        except Exception as exc:
            failed += 1
            name = loc_map.get(pk, {}).get("name", "?")
            print(f"  [FAIL] Failed to delete pk={pk} {name}: {repr(exc)}")

    print(f"\nDeletion complete: deleted={deleted} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user")
        raise SystemExit(130)
