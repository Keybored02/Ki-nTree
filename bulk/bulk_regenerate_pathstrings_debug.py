#!/usr/bin/env python3
"""Bulk regenerate pathstrings by patching all locations."""

import requests

from kintree.config import settings
from kintree.database import inventree_api


def main() -> int:
    settings.load_inventree_settings()

    if not settings.SERVER_ADDRESS or not settings.USERNAME or not settings.PASSWORD:
        print("Missing InvenTree credentials")
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
    token = getattr(api_obj, "token", None)
    base_url = getattr(api_obj, "base_url", settings.SERVER_ADDRESS)

    if not token:
        print("Missing API token")
        return 1

    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }

    # Fetch all locations
    print("Fetching all locations...")
    all_locs = []
    url = f"{base_url.rstrip('/')}/api/stock/location/"
    params = {}
    page = 0

    while url:
        page += 1
        print(f"  Fetching page {page}: {url if page == 1 else '(next page)'}")

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            print(f"    Response type: {type(data).__name__}")

            if isinstance(data, dict):
                count = data.get("count", 0)
                results = data.get("results", [])
                print(f"    Count: {count}, results in this page: {len(results)}")

                chunk = results or []
                all_locs.extend(chunk)
                url = data.get("next")
                params = {}

                if not url:
                    print("    No next page")
            else:
                print(f"    ERROR: Expected dict, got {type(data).__name__}")
                break
        except Exception as e:
            print(f"    EXCEPTION: {repr(e)}")
            raise

    print(f"\nFound {len(all_locs)} locations total\n")

    if len(all_locs) == 0:
        print("ERROR: No locations fetched!")
        return 1

    # Patch each location in order (level by level would be optimal but just do them all)
    print("Patching locations to regenerate pathstrings...")
    patched = 0
    failed = 0

    # Sort by level (ascending) so parents are updated before children
    all_locs.sort(key=lambda x: int(x.get("level") or 0))

    for idx, loc in enumerate(all_locs):
        pk = loc.get("pk")
        name = loc.get("name")
        level = loc.get("level", 0)

        # Just PATCH with a no-op (empty dict) or minimal change
        # This triggers InvenTree to recalculate pathstring
        url = f"{base_url.rstrip('/')}/api/stock/location/{pk}/"

        try:
            response = requests.patch(url, headers=headers, json={}, timeout=30)
            if response.status_code == 200:
                patched += 1
                # Print progress
                if (idx + 1) % 100 == 0 or idx == len(all_locs) - 1:
                    print(f"  Progress: {idx + 1}/{len(all_locs)} patched...")
            else:
                failed += 1
                print(f"  WARN: pk={pk} status={response.status_code}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: pk={pk} {repr(e)}")

    print(
        f"\nPathstring regeneration triggered for {patched} locations ({failed} failures)"
    )
    print("\nVerifying a few updated pathstrings...")

    url = f"{base_url.rstrip('/')}/api/stock/location/?limit=10"
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code == 200:
        data = response.json()
        for loc in data.get("results", [])[:10]:
            pk = loc.get("pk")
            name = loc.get("name")
            path = loc.get("pathstring")
            level = loc.get("level")
            print(f"  pk={pk} level={level} name={name:20s} pathstring={path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
