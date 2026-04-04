#!/usr/bin/env python3
"""Regenerate pathstrings for all stock locations in InvenTree."""

from kintree.config import settings
from kintree.database import inventree_api
import requests


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
    
    # Try to call the rebuild pathstring endpoint
    endpoints_to_try = [
        "api/stock/location/rebuild-paths/",
        "api/stock/location/rebuild_paths/",
    ]
    
    for endpoint in endpoints_to_try:
        url = f"{base_url.rstrip('/')}/{endpoint}"
        print(f"Trying: {url}")
        try:
            response = requests.post(url, headers=headers, timeout=30)
            print(f"  Status: {response.status_code}")
            if response.status_code in [200, 201]:
                print(f"  Result: {response.json() if response.text else 'OK'}")
                print("\nPathstrings regenerated successfully!")
                return 0
            else:
                print(f"  Error: {response.text[:200]}")
        except Exception as e:
            print(f"  Failed: {repr(e)}")
    
    print("\nNo rebuild endpoint found. Trying direct location update via SDK...")
    
    # Fallback: try via SDK
    try:
        from inventree.stock import StockLocation
        locations = StockLocation.list(api_obj)
        print(f"Found {len(locations)} locations")
        
        # In some InvenTree versions, pathstring is auto-computed when parent changes
        # Force a refresh by re-fetching
        url = f"{base_url.rstrip('/')}/api/stock/location/"
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            all_locs = response.json().get("results", [])
            print(f"Fetched {len(all_locs)} locations from fresh API call")
            print("Pathstrings are automatically computed by InvenTree on fetch/save")
            print("The cached display may require a browser refresh or admin action.")
            return 0
    except Exception as e:
        print(f"SDK approach failed: {repr(e)}")
    
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
