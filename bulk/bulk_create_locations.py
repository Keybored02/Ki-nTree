#!/usr/bin/env python3
"""
Bulk create nested stock locations (racks → shelves → boxes) in InvenTree.

Uses InvenTree stock location API endpoints:
  GET  /api/stock/location/  (list locations)
  POST /api/stock/location/  (create location)

Examples:
  python bulk_create_locations.py --config location_config.yaml --dry-run
  python bulk_create_locations.py --config location_config.yaml --confirm
  python bulk_create_locations.py --config location_config.yaml --parent-path "Root/Electronics" --confirm

Location config YAML format:
  racks: 5
  shelves_per_rack: 4
  boxes_per_shelf: 6
  naming_pattern:
    rack: "RACK-{i:02d}"
    shelf: "RACK-{rack}/SHELF-{i:02d}"
    box: "RACK-{rack}/SHELF-{shelf}/BOX-{i:02d}"
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional
from pathlib import Path

import requests
import yaml

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


def find_location_by_path(
    base_url: str,
    headers: Dict[str, str],
    path: str,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Find a stock location by its full path (e.g. "Electronics / Storage / Drawer 1").
    Fetches all locations and searches by building hierarchy path.
    
    Returns the location dict with pk and metadata, or None if not found.
    """
    all_locations: List[Dict[str, Any]] = []
    url = _api_url(base_url, "api/stock/location/")
    
    # Fetch all locations
    while url:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "results" in data:
            results = data.get("results") or []
            all_locations.extend(results)
            url = data.get("next")
        elif isinstance(data, list):
            all_locations.extend(data)
            break
    
    # Build location map: pk -> location object
    location_map: Dict[int, Dict[str, Any]] = {}
    for loc in all_locations:
        pk = loc.get("pk")
        if pk:
            location_map[int(pk)] = loc
    
    # Build path map: full_path -> pk
    path_map: Dict[str, int] = {}
    memo: Dict[int, str] = {}
    
    def build_path(loc_pk: int) -> str:
        """Recursively build full path for a location."""
        if loc_pk in memo:
            return memo[loc_pk]
        
        loc = location_map.get(loc_pk)
        if not loc:
            return ""
        
        name = loc.get("name", "")
        parent = loc.get("parent")
        
        if parent is None:
            result = name
        else:
            parent_path = build_path(int(parent))
            result = f"{parent_path} / {name}" if parent_path else name
        
        memo[loc_pk] = result
        return result
    
    # Build complete path map
    for loc_pk in location_map.keys():
        full_path = build_path(loc_pk)
        path_map[full_path] = loc_pk
    
    # Search for exact match
    search_path = path.strip()
    if search_path in path_map:
        return location_map[path_map[search_path]]
    
    return None


def create_location(
    base_url: str,
    headers: Dict[str, str],
    name: str,
    description: str = "",
    parent_pk: Optional[int] = None,
    location_type_pk: Optional[int] = None,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Create a new stock location via API.
    
    Returns the created location object with pk, or None on error.
    """
    payload: Dict[str, Any] = {
        "name": name,
    }
    if description:
        payload["description"] = description
    if parent_pk is not None:
        payload["parent"] = parent_pk
    if location_type_pk is not None:
        payload["location_type"] = location_type_pk
    
    endpoint = _api_url(base_url, "api/stock/location/")
    response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    
    if response.status_code in [200, 201]:
        return response.json()
    else:
        raise RuntimeError(
            f"Failed to create location '{name}': "
            f"HTTP {response.status_code}: {response.text[:500]}"
        )


def load_config(config_path: str) -> Dict[str, Any]:
    """Load location configuration from YAML file."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config or {}


def validate_config(config: Dict[str, Any]) -> None:
    """Validate required fields in config."""
    required = ["racks", "shelves_per_rack", "boxes_per_shelf"]
    for field in required:
        if field not in config:
            raise ValueError(f"Missing required config field: {field}")


def build_location_tree(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build a list of locations to create based on config.
    
    Returns list of dicts with keys: name, description, level (rack/shelf/box), parent_idx
    """
    locations: List[Dict[str, Any]] = []
    
    racks = config.get("racks", 0)
    shelves_per_rack = config.get("shelves_per_rack", 0)
    boxes_per_shelf = config.get("boxes_per_shelf", 0)
    
    naming_pattern = config.get("naming_pattern", {})
    rack_pattern = naming_pattern.get("rack", "RACK-{i:02d}")
    shelf_pattern = naming_pattern.get("shelf", "RACK-{rack}/SHELF-{i:02d}")
    box_pattern = naming_pattern.get("box", "RACK-{rack}/SHELF-{shelf}/BOX-{i:02d}")
    
    # Build rack locations
    rack_indices: Dict[int, int] = {}  # rack_id -> index in locations
    for rack_idx in range(1, racks + 1):
        rack_name = rack_pattern.format(i=rack_idx)
        rack_indices[rack_idx] = len(locations)
        locations.append({
            "name": rack_name,
            "description": f"Rack {rack_idx}",
            "level": "rack",
            "parent_idx": None,
            "rack_id": rack_idx,
            "shelf_id": None,
            "box_id": None,
        })
    
    # Build shelf locations
    shelf_indices: Dict[tuple, int] = {}  # (rack_id, shelf_id) -> index in locations
    for rack_idx in range(1, racks + 1):
        for shelf_idx in range(1, shelves_per_rack + 1):
            shelf_name = shelf_pattern.format(rack=rack_idx, i=shelf_idx)
            parent_rack_idx = rack_indices[rack_idx]
            shelf_indices[(rack_idx, shelf_idx)] = len(locations)
            locations.append({
                "name": shelf_name,
                "description": f"Rack {rack_idx}, Shelf {shelf_idx}",
                "level": "shelf",
                "parent_idx": parent_rack_idx,
                "rack_id": rack_idx,
                "shelf_id": shelf_idx,
                "box_id": None,
            })
    
    # Build box locations
    for rack_idx in range(1, racks + 1):
        for shelf_idx in range(1, shelves_per_rack + 1):
            for box_idx in range(1, boxes_per_shelf + 1):
                box_name = box_pattern.format(rack=rack_idx, shelf=shelf_idx, i=box_idx)
                parent_shelf_idx = shelf_indices[(rack_idx, shelf_idx)]
                locations.append({
                    "name": box_name,
                    "description": f"Rack {rack_idx}, Shelf {shelf_idx}, Box {box_idx}",
                    "level": "box",
                    "parent_idx": parent_shelf_idx,
                    "rack_id": rack_idx,
                    "shelf_id": shelf_idx,
                    "box_id": box_idx,
                })
    
    return locations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk create nested stock locations in InvenTree",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        help="YAML config file defining location hierarchy"
    )
    parser.add_argument(
        "--parent-path",
        help="Parent location path (e.g. 'Electronics / Storage'). If not specified, creates at root."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview locations to be created; do not create"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually execute location creation"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds"
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
    
    if not args.config:
        print("ERROR: provide --config <yaml file>")
        return 2
    
    if not args.dry_run and not args.confirm:
        print("ERROR: refusing to run without --dry-run or --confirm")
        print("Tip: start with --dry-run to preview")
        return 2
    
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
    
    # Load config
    try:
        config = load_config(args.config)
        validate_config(config)
    except FileNotFoundError:
        print(f"ERROR: config file not found: {args.config}")
        return 2
    except ValueError as exc:
        print(f"ERROR: invalid config: {exc}")
        return 2
    except yaml.YAMLError as exc:
        print(f"ERROR: failed to parse YAML: {exc}")
        return 2
    except Exception as exc:
        print(f"ERROR: unexpected error loading config: {repr(exc)}")
        return 1
    
    # Build location tree
    try:
        locations_to_create = build_location_tree(config)
    except Exception as exc:
        print(f"ERROR: failed to build location tree: {repr(exc)}")
        return 1
    
    # Resolve parent location
    parent_pk: Optional[int] = None
    
    if args.parent_path:
        try:
            parent_loc = find_location_by_path(base_url, headers, args.parent_path, timeout=args.timeout)
            if not parent_loc:
                print(f"ERROR: parent location not found: {args.parent_path}")
                return 2
            parent_pk = parent_loc.get("pk")
            print(f"Resolved parent location: {args.parent_path} (pk={parent_pk})")
        except Exception as exc:
            print(f"ERROR: failed to resolve parent location: {repr(exc)}")
            return 1
    
    # Print preview
    print(f"\nPreview: {len(locations_to_create)} locations to create")
    print("-" * 80)
    
    for idx, loc in enumerate(locations_to_create[:20]):
        parent_idx = loc.get("parent_idx")
        parent_name = locations_to_create[parent_idx]["name"] if parent_idx is not None else "(root)"
        level = loc.get("level")
        print(f"  [{level:5s}] {loc['name']:40s} parent={parent_name}")
    
    if len(locations_to_create) > 20:
        print(f"  ... and {len(locations_to_create) - 20} more")
    
    print("-" * 80)
    
    if args.dry_run:
        print("Dry-run complete. No locations created.")
        return 0
    
    # Execute creation
    print("\nCreating locations...")
    created_pks: Dict[int, int] = {}  # Map location list index -> API pk
    
    for idx, loc in enumerate(locations_to_create):
        name = loc["name"]
        description = loc.get("description", "")
        
        # Resolve parent pk
        parent_idx = loc.get("parent_idx")
        loc_parent_pk: Optional[int] = None
        if parent_idx is not None:
            loc_parent_pk = created_pks.get(parent_idx)
        elif parent_pk is not None:
            loc_parent_pk = parent_pk
        
        try:
            created_loc = create_location(
                base_url,
                headers,
                name,
                description=description,
                parent_pk=loc_parent_pk,
                location_type_pk=None,
                timeout=args.timeout,
            )
            created_pk = created_loc.get("pk")
            created_pks[idx] = created_pk
            print(f"  ✓ Created: {name} (pk={created_pk})")
        except Exception as exc:
            print(f"  ✗ Failed: {name}")
            print(f"    {repr(exc)}")
            return 1
    
    print(f"\nSuccessfully created {len(created_pks)} locations!")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user")
        raise SystemExit(130)
