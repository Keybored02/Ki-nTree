#!/usr/bin/env python3
"""
List stock locations from InvenTree API.

Uses InvenTree list endpoint:
  GET /api/stock/location/

Examples:
  python list_locations.py
  python list_locations.py --top-level
  python list_locations.py --parent 22
  python list_locations.py --search "Rack"
  python list_locations.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

import requests

from kintree.config import settings
from kintree.database import inventree_api


def _build_auth_headers(api_obj) -> Dict[str, str]:
    """Build authorization headers for InvenTree API requests."""
    token = getattr(api_obj, "token", None)
    if not token:
        raise RuntimeError("InvenTree auth token is missing after login")
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api_url(base_url: str, path: str) -> str:
    """Construct full API URL."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


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


def build_path(location: Dict[str, Any]) -> str:
    """Build full path string from location object."""
    path = location.get("path") or []
    path_parts = [p.get("name", "") for p in path if isinstance(p, dict)]
    name = location.get("name", "")
    if path_parts:
        return " / ".join(path_parts) + " / " + name
    return name


def print_locations_table(locations: List[Dict[str, Any]]) -> None:
    """Print locations in table format."""
    if not locations:
        print("No locations found")
        return

    print(f"Total locations: {len(locations)}\n")
    print("-" * 120)
    print(f"{'PK':>6} {'Level':>5} {'Items':>7} {'Sub':>5} {'Name':<50} {'Path':>45}")
    print("-" * 120)

    for loc in sorted(locations, key=lambda x: x.get("pk", 0)):
        pk = loc.get("pk", "")
        level = loc.get("level", "")
        items = loc.get("items", 0)
        sublocations = loc.get("sublocations", 0)
        name = loc.get("name", "")[:50]
        path = build_path(loc)[:45]

        print(f"{pk:>6} {level:>5} {items:>7} {sublocations:>5} {name:<50} {path:>45}")

    print("-" * 120)


def print_locations_tree(locations: List[Dict[str, Any]]) -> None:
    """Print locations in hierarchical tree format."""
    if not locations:
        print("No locations found")
        return

    print(f"Total locations: {len(locations)}\n")

    # Build mapping: pk -> location
    loc_map: Dict[int, Dict[str, Any]] = {}
    for loc in locations:
        pk = loc.get("pk")
        if pk:
            loc_map[int(pk)] = loc

    # Find root locations (parent=None)
    def print_tree(loc_pk: int, indent: int = 0) -> None:
        loc = loc_map.get(loc_pk)
        if not loc:
            return

        name = loc.get("name", "")
        items = loc.get("items", 0)
        sublocations = loc.get("sublocations", 0)
        prefix = "  " * indent + "├─ " if indent > 0 else ""

        print(f"{prefix}{name} (pk={loc_pk}, items={items}, sub={sublocations})")

        # Print children
        children = [l for l in locations if l.get("parent") == loc_pk]
        for child in sorted(children, key=lambda x: x.get("name", "")):
            child_pk = child.get("pk")
            if child_pk:
                print_tree(child_pk, indent + 1)

    # Print root locations
    roots = [l for l in locations if l.get("parent") is None]
    for root in sorted(roots, key=lambda x: x.get("name", "")):
        root_pk = root.get("pk")
        if root_pk:
            print_tree(root_pk)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List stock locations from InvenTree API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--top-level", action="store_true", help="Show only top-level locations"
    )
    parser.add_argument("--parent", type=int, help="Filter by parent location PK")
    parser.add_argument(
        "--search", help="Search term (searches name, description, pathstring)"
    )
    parser.add_argument(
        "--structural", action="store_true", help="Show only structural locations"
    )
    parser.add_argument(
        "--external", action="store_true", help="Show only external locations"
    )
    parser.add_argument(
        "--tree", action="store_true", help="Display as hierarchical tree"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout in seconds"
    )

    args = parser.parse_args()

    # Load InvenTree credentials
    settings.load_inventree_settings()

    if not settings.SERVER_ADDRESS:
        print("ERROR: Missing InvenTree SERVER_ADDRESS in config")
        return 2

    if not settings.USERNAME or not settings.PASSWORD:
        print("ERROR: Missing InvenTree USERNAME/PASSWORD in config")
        return 2

    # Connect
    print(f"Connecting to InvenTree: {settings.SERVER_ADDRESS}", file=sys.stderr)
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

    # Build filters
    filters: Dict[str, Any] = {}
    if args.top_level:
        filters["top_level"] = True
    if args.parent is not None:
        filters["parent"] = args.parent
    if args.search:
        filters["search"] = args.search
    if args.structural:
        filters["structural"] = True
    if args.external:
        filters["external"] = True

    # Fetch locations
    try:
        locations = fetch_all_locations(
            base_url, headers, filters, timeout=args.timeout
        )
    except requests.RequestException as exc:
        print(f"ERROR: failed to fetch locations: {repr(exc)}")
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected error: {repr(exc)}")
        return 1

    # Output
    if args.json:
        print(json.dumps(locations, indent=2))
    elif args.tree:
        print_locations_tree(locations)
    else:
        print_locations_table(locations)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user")
        raise SystemExit(130)
