#!/usr/bin/env python3
"""
Delete stock locations by PK range via InvenTree API.

Uses InvenTree delete endpoint:
  DELETE /api/stock/location/{id}/

Examples:
  python bulk_delete_locations.py --pks 29-61 --dry-run
  python bulk_delete_locations.py --pks 29-61 --confirm
  python bulk_delete_locations.py --pks 29,30,31,38,39,40 --confirm
"""

from __future__ import annotations

import argparse
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


def parse_pk_list(pk_spec: str) -> List[int]:
    """
    Parse PK specification into list of PKs.
    Supports:
      - "29,30,31" (comma-separated)
      - "29-61" (range inclusive)
      - Mix: "29-35,38,40-42"
    """
    pks: List[int] = []
    
    for part in pk_spec.split(","):
        part = part.strip()
        if "-" in part and part[0] != "-":  # Handle negative numbers
            start_str, end_str = part.split("-", 1)
            try:
                start = int(start_str.strip())
                end = int(end_str.strip())
                pks.extend(range(start, end + 1))
            except ValueError:
                raise ValueError(f"Invalid range specification: {part}")
        else:
            try:
                pks.append(int(part))
            except ValueError:
                raise ValueError(f"Invalid PK specification: {part}")
    
    return sorted(set(pks))  # Remove duplicates and sort


def delete_location(
    base_url: str,
    headers: Dict[str, str],
    pk: int,
    timeout: int = 30,
) -> bool:
    """
    Delete a stock location by PK.
    
    Returns True on success, False on failure.
    """
    endpoint = _api_url(base_url, f"api/stock/location/{pk}/")
    try:
        response = requests.delete(endpoint, headers=headers, timeout=timeout)
        if response.status_code in [200, 202, 204]:
            return True
        else:
            raise RuntimeError(
                f"Delete failed: HTTP {response.status_code}: {response.text[:300]}"
            )
    except requests.RequestException as exc:
        raise RuntimeError(f"Request failed: {repr(exc)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete stock locations by PK via InvenTree API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--pks",
        required=True,
        help="PK list: '29-61', '29,30,31', or mix '29-35,38,40-42'"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview PKs to delete; do not delete"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually execute deletion"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds"
    )
    
    args = parser.parse_args()
    
    if not args.dry_run and not args.confirm:
        print("ERROR: refusing to run without --dry-run or --confirm")
        print("Tip: start with --dry-run to preview")
        return 2
    
    # Parse PK list
    try:
        pks = parse_pk_list(args.pks)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    
    if not pks:
        print("ERROR: no PKs to delete")
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
    
    # Print preview
    print(f"\nPreview: {len(pks)} locations to DELETE (in reverse order)")
    print("-" * 60)
    for pk in reversed(pks[:20]):
        print(f"  pk={pk}")
    if len(pks) > 20:
        print(f"  ... and {len(pks) - 20} more")
    print("-" * 60)
    
    if args.dry_run:
        print("Dry-run complete. No deletions performed.")
        return 0
    
    # Execute deletion in reverse order (delete children before parents)
    deleted = 0
    failed = 0
    
    print("\nDeleting locations (reverse order)...")
    for pk in reversed(pks):
        try:
            delete_location(base_url, headers, pk, timeout=args.timeout)
            deleted += 1
            print(f"  ✓ Deleted pk={pk}")
        except Exception as exc:
            failed += 1
            print(f"  ✗ Failed to delete pk={pk}: {repr(exc)}")
    
    print(f"\nDeletion complete: deleted={deleted} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user")
        raise SystemExit(130)
