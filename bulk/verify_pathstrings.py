#!/usr/bin/env python3
"""Verify that pathstrings no longer contain 'Personal' or 'Genesy'."""

from kintree.config import settings
from kintree.database import inventree_api
import requests


def main() -> int:
    settings.load_inventree_settings()
    
    connected = inventree_api.connect(
        server=settings.SERVER_ADDRESS,
        username=settings.USERNAME,
        password=settings.PASSWORD,
        silent=False,
    )
    
    if not connected:
        print("Could not authenticate to InvenTree")
        return 1
    
    api_obj = inventree_api.inventree_api
    token = getattr(api_obj, "token", None)
    base_url = getattr(api_obj, "base_url", settings.SERVER_ADDRESS)
    
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }
    
    # Fetch all locations
    print("Fetching all locations...")
    url = f"{base_url.rstrip('/')}/api/stock/location/?limit=100"
    
    all_locs = []
    while url:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if isinstance(data, dict) and "results" in data:
            all_locs.extend(data.get("results", []))
            url = data.get("next")
        elif isinstance(data, list):
            all_locs = data
            break
    
    print(f"Found {len(all_locs)} locations\n")
    
    # Search for old parent names
    bad_paths = []
    for loc in all_locs:
        pathstring = loc.get("pathstring", "").lower()
        if "personal" in pathstring or "genesy" in pathstring:
            bad_paths.append(loc)
    
    if bad_paths:
        print(f"❌ FOUND {len(bad_paths)} locations with old parent names:\n")
        for loc in bad_paths:
            pk = loc.get("pk")
            name = loc.get("name")
            path = loc.get("pathstring")
            level = loc.get("level")
            print(f"  pk={pk} level={level} name={name:20s} pathstring={path}")
        return 1
    else:
        print("✅ All pathstrings are correct! No 'Personal' or 'Genesy' found.")
        print(f"\nSample paths verified:")
        # Show a few samples
        for loc in all_locs[:5]:
            pk = loc.get("pk")
            name = loc.get("name")
            path = loc.get("pathstring")
            level = loc.get("level")
            print(f"  pk={pk} level={level} name={name:20s} pathstring={path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
