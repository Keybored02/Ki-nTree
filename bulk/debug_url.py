import requests

from kintree.config import settings
from kintree.database import inventree_api

settings.load_inventree_settings()
connected = inventree_api.connect(
    server=settings.SERVER_ADDRESS,
    username=settings.USERNAME,
    password=settings.PASSWORD,
    silent=False,
)

if connected:
    api_obj = inventree_api.inventree_api
    token = getattr(api_obj, "token", None)
    base_url = getattr(api_obj, "base_url", settings.SERVER_ADDRESS)

    print(f"Base URL from api_obj: {base_url}")
    print(f"Settings SERVER_ADDRESS: {settings.SERVER_ADDRESS}")

    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }

    # Try with base_url
    url1 = f"{base_url.rstrip('/')}/api/stock/location/"
    print(f"\nURL 1 (from api_obj.base_url): {url1}")
    r1 = requests.get(url1, headers=headers, timeout=10)
    print(f"  Status: {r1.status_code}")
    print(f"  Type: {type(r1.json()).__name__}")

    # Try with settings
    url2 = f"{settings.SERVER_ADDRESS.rstrip('/')}/api/stock/location/"
    print(f"\nURL 2 (from settings): {url2}")
    r2 = requests.get(url2, headers=headers, timeout=10)
    print(f"  Status: {r2.status_code}")
    print(f"  Type: {type(r2.json()).__name__}")
