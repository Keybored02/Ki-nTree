import json

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
    api = inventree_api.inventree_api
    token = api.token
    base = settings.SERVER_ADDRESS.rstrip("/")

    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }

    url = base + "/api/stock/location/?limit=1"
    r = requests.get(url, headers=headers, timeout=10)
    print(f"Status: {r.status_code}")
    data = r.json()
    print(f"Type: {type(data).__name__}")
    print(json.dumps(data, indent=2))
