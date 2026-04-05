#!/usr/bin/env python3
"""
Profile Assign-page API pipeline performance against an InvenTree server.

What this measures per lookup:
- part search latency (/api/part/?search=...)
- location resolve latency (get_stock_location_tree)
- barcode fetch latency (/api/barcode/ filtered and optional fallback)
- optional stock list latency (/api/stock/?part=...)

Usage examples:
  python tests/debug_assign_pipeline_profile.py --lookup NE555 --lookup "#101"
  python tests/debug_assign_pipeline_profile.py --lookups-file tests/test_samples.yaml --yaml-key assign_lookups
  python tests/debug_assign_pipeline_profile.py --lookup NE555 --repeat 5 --include-stock

Notes:
- Run from repo root so Ki-nTree config paths resolve correctly.
- This script is read-only (no write/update operations).
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kintree.database import inventree_interface  # noqa: E402


def ms(start: float, end: float) -> float:
    return round((end - start) * 1000.0, 2)


def timed(fn, *args, **kwargs) -> Tuple[Any, float, Optional[str]]:
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        return result, ms(t0, time.perf_counter()), None
    except Exception as exc:  # pragma: no cover - debug path
        return None, ms(t0, time.perf_counter()), str(exc)


def connect_with_retries(attempts: int, delay_s: float) -> Tuple[bool, float]:
    t0 = time.perf_counter()
    for i in range(1, attempts + 1):
        if inventree_interface.connect_to_server():
            return True, ms(t0, time.perf_counter())
        if i < attempts:
            time.sleep(delay_s)
    return False, ms(t0, time.perf_counter())


def get_api_context() -> Tuple[Optional[str], Optional[str]]:
    api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
    if not api_obj:
        return None, None
    token = getattr(api_obj, "token", None)
    base_url = getattr(api_obj, "base_url", None)
    return token, base_url


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    attempts: int,
    delay_s: float,
    **kwargs,
) -> Tuple[Optional[requests.Response], float, Optional[str]]:
    t0 = time.perf_counter()
    err_text = None
    for i in range(1, attempts + 1):
        try:
            resp = session.request(method=method.upper(), url=url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp, ms(t0, time.perf_counter()), None
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            err_text = f"{type(exc).__name__}: {exc}"
            if status and 400 <= int(status) < 500 and int(status) != 429:
                break
            if i < attempts:
                time.sleep(delay_s)
    return None, ms(t0, time.perf_counter()), err_text


def fetch_part(
    session: requests.Session,
    base_url: str,
    token: str,
    lookup: str,
    attempts: int,
    delay_s: float,
) -> Tuple[Optional[Dict], float, Optional[str], int]:
    endpoint = f"{base_url.rstrip('/')}/api/part/"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    resp, elapsed, err = request_with_retries(
        session,
        "GET",
        endpoint,
        attempts,
        delay_s,
        headers=headers,
        params={"search": lookup, "limit": 20},
        timeout=20,
    )
    if resp is None:
        return None, elapsed, err, 0

    payload = resp.json()
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []

    if not rows:
        return None, elapsed, None, 0

    needle = lookup.strip().lower()
    for candidate in rows:
        ipn = str(candidate.get("IPN") or "").strip().lower()
        name = str(candidate.get("name") or "").strip().lower()
        if needle and (needle == ipn or needle == name):
            return candidate, elapsed, None, len(rows)

    return rows[0], elapsed, None, len(rows)


def resolve_location_name(part: Dict) -> Tuple[str, float, Optional[str]]:
    location_name = str(part.get("default_location_name") or "").strip()
    if location_name:
        return location_name, 0.0, None

    location_id = part.get("default_location")
    if location_id in [None, "", "None"]:
        return "-", 0.0, None

    try:
        location_id = int(location_id)
    except Exception:
        return str(location_id), 0.0, None

    def _get_tree() -> str:
        tree = inventree_interface.inventree_api.get_stock_location_tree(location_id)
        names = [str(n) for n in reversed(list(tree.values())) if str(n).strip()]
        return "/".join(names) if names else str(location_id)

    resolved, elapsed, err = timed(_get_tree)
    if resolved is None:
        return str(location_id), elapsed, err
    return resolved, elapsed, None


def detect_barcode_get_support(
    session: requests.Session,
    base_url: str,
    token: str,
    attempts: int,
    delay_s: float,
) -> Tuple[bool, float, Optional[str]]:
    endpoint = f"{base_url.rstrip('/')}/api/barcode/"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    resp, elapsed, err = request_with_retries(
        session,
        "GET",
        endpoint,
        attempts,
        delay_s,
        headers=headers,
        params={"limit": 1},
        timeout=10,
    )
    if resp is None:
        # If server returns 404/405, request_with_retries exits with err text.
        if err and ("404" in err or "405" in err):
            return False, elapsed, None
        return False, elapsed, err
    return True, elapsed, None


def fetch_barcodes_for_part(
    session: requests.Session,
    base_url: str,
    token: str,
    part_pk: int,
    barcode_supported: bool,
    attempts: int,
    delay_s: float,
) -> Tuple[List[str], float, Optional[str]]:
    if not barcode_supported:
        return [], 0.0, None

    endpoint = f"{base_url.rstrip('/')}/api/barcode/"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

    def _extract(payload: Any) -> List[str]:
        rows = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            rows = []
        values = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            value = item.get("data") or item.get("barcode") or item.get("value")
            if value:
                values.append(str(value))
        return values

    t0 = time.perf_counter()

    # Filtered query first.
    resp, _, err = request_with_retries(
        session,
        "GET",
        endpoint,
        attempts,
        delay_s,
        headers=headers,
        params={"part": int(part_pk), "limit": 100},
        timeout=20,
    )
    if resp is None:
        return [], ms(t0, time.perf_counter()), err

    values = _extract(resp.json())
    if values:
        return values, ms(t0, time.perf_counter()), None

    # Fallback full list + client filter.
    resp2, _, err2 = request_with_retries(
        session,
        "GET",
        endpoint,
        attempts,
        delay_s,
        headers=headers,
        params={"limit": 200},
        timeout=20,
    )
    if resp2 is None:
        return [], ms(t0, time.perf_counter()), err2

    payload = resp2.json()
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []

    values = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        part_ref = item.get("part")
        item_part_pk = part_ref.get("pk") if isinstance(part_ref, dict) else part_ref
        try:
            if int(item_part_pk) != int(part_pk):
                continue
        except Exception:
            continue
        value = item.get("data") or item.get("barcode") or item.get("value")
        if value:
            values.append(str(value))

    return values, ms(t0, time.perf_counter()), None


def fetch_stock_items_count(
    session: requests.Session,
    base_url: str,
    token: str,
    part_pk: int,
    attempts: int,
    delay_s: float,
) -> Tuple[int, float, Optional[str]]:
    endpoint = f"{base_url.rstrip('/')}/api/stock/"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    resp, elapsed, err = request_with_retries(
        session,
        "GET",
        endpoint,
        attempts,
        delay_s,
        headers=headers,
        params={"part": int(part_pk), "limit": 100},
        timeout=20,
    )
    if resp is None:
        return 0, elapsed, err

    payload = resp.json()
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []
    return len(rows), elapsed, None


def load_lookups(args: argparse.Namespace) -> List[str]:
    lookups = list(args.lookup or [])
    if args.lookups_file:
        p = Path(args.lookups_file)
        if not p.exists():
            raise FileNotFoundError(f"lookups file not found: {p}")
        if p.suffix.lower() in [".yaml", ".yml"]:
            import yaml

            payload = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            key = args.yaml_key
            values = payload.get(key, []) if isinstance(payload, dict) else []
            if isinstance(values, list):
                lookups.extend([str(v) for v in values if str(v).strip()])
        else:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    lookups.append(line)
    dedup = []
    seen = set()
    for item in lookups:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            dedup.append(item.strip())
    return dedup


def summarize(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p90_idx = min(len(ordered) - 1, max(0, int(round(0.9 * len(ordered) + 0.5)) - 1))
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "p50": round(p50, 2),
        "p90": round(ordered[p90_idx], 2),
        "max": round(max(values), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile Assign pipeline API latency")
    parser.add_argument("--lookup", action="append", help="Lookup value (IPN, name, barcode, etc). Can be repeated.")
    parser.add_argument("--lookups-file", help="Text or YAML file with lookup values")
    parser.add_argument("--yaml-key", default="assign_lookups", help="YAML key for lookups list (default: assign_lookups)")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each lookup N times (default: 1)")
    parser.add_argument("--attempts", type=int, default=3, help="Retry attempts for connect/HTTP (default: 3)")
    parser.add_argument("--delay", type=float, default=0.8, help="Retry delay seconds (default: 0.8)")
    parser.add_argument("--include-stock", action="store_true", help="Also profile stock-list call per found part")
    args = parser.parse_args()

    lookups = load_lookups(args)
    if not lookups:
        print("No lookups provided. Use --lookup or --lookups-file.")
        return 2

    ok, connect_ms = connect_with_retries(attempts=args.attempts, delay_s=args.delay)
    print(f"connect_ok={ok} connect_ms={connect_ms}")
    if not ok:
        print("Failed to connect to InvenTree.")
        return 1

    token, base_url = get_api_context()
    if not token or not base_url:
        print("Missing API context (token/base_url).")
        return 1

    session = requests.Session()
    barcode_supported, probe_ms, probe_err = detect_barcode_get_support(
        session,
        base_url,
        token,
        attempts=args.attempts,
        delay_s=args.delay,
    )
    print(
        f"barcode_get_supported={barcode_supported} probe_ms={probe_ms}"
        + (f" probe_err={probe_err}" if probe_err else "")
    )

    rows: List[Dict[str, Any]] = []

    for lookup in lookups:
        for i in range(max(1, args.repeat)):
            row: Dict[str, Any] = {
                "lookup": lookup,
                "iter": i + 1,
                "found": False,
                "part_pk": None,
                "part_rows": 0,
                "part_ms": 0.0,
                "location_ms": 0.0,
                "barcode_ms": 0.0,
                "stock_ms": 0.0,
                "total_ms": 0.0,
                "error": "",
            }

            t0 = time.perf_counter()

            part, part_ms, part_err, part_rows = fetch_part(
                session,
                base_url,
                token,
                lookup,
                attempts=args.attempts,
                delay_s=args.delay,
            )
            row["part_ms"] = part_ms
            row["part_rows"] = part_rows
            if part_err:
                row["error"] = f"part_lookup: {part_err}"
                row["total_ms"] = ms(t0, time.perf_counter())
                rows.append(row)
                continue

            if not part:
                row["error"] = "part_not_found"
                row["total_ms"] = ms(t0, time.perf_counter())
                rows.append(row)
                continue

            row["found"] = True
            row["part_pk"] = int(part.get("pk") or part.get("id"))

            _, location_ms, loc_err = resolve_location_name(part)
            row["location_ms"] = location_ms
            if loc_err:
                row["error"] = f"location: {loc_err}"

            _, barcode_ms, barcode_err = fetch_barcodes_for_part(
                session,
                base_url,
                token,
                row["part_pk"],
                barcode_supported,
                attempts=args.attempts,
                delay_s=args.delay,
            )
            row["barcode_ms"] = barcode_ms
            if barcode_err and not row["error"]:
                row["error"] = f"barcode: {barcode_err}"

            if args.include_stock:
                _, stock_ms, stock_err = fetch_stock_items_count(
                    session,
                    base_url,
                    token,
                    row["part_pk"],
                    attempts=args.attempts,
                    delay_s=args.delay,
                )
                row["stock_ms"] = stock_ms
                if stock_err and not row["error"]:
                    row["error"] = f"stock: {stock_err}"

            row["total_ms"] = ms(t0, time.perf_counter())
            rows.append(row)

    print("\n--- per-run timings (ms) ---")
    print("lookup | it | found | part | loc | barcode | stock | total | error")
    for r in rows:
        print(
            f"{r['lookup']} | {r['iter']} | {r['found']} | {r['part_ms']} | "
            f"{r['location_ms']} | {r['barcode_ms']} | {r['stock_ms']} | {r['total_ms']} | {r['error']}"
        )

    metric_keys = ["part_ms", "location_ms", "barcode_ms", "stock_ms", "total_ms"]
    print("\n--- aggregate ---")
    for key in metric_keys:
        vals = [float(r[key]) for r in rows if float(r[key]) > 0]
        s = summarize(vals)
        print(f"{key}: count={s['count']} avg={s['avg']} p50={s['p50']} p90={s['p90']} max={s['max']}")

    slowest = sorted(rows, key=lambda r: float(r["total_ms"]), reverse=True)[:10]
    print("\n--- slowest runs ---")
    for r in slowest:
        print(
            f"lookup={r['lookup']} it={r['iter']} total={r['total_ms']} "
            f"(part={r['part_ms']} loc={r['location_ms']} barcode={r['barcode_ms']} stock={r['stock_ms']}) "
            f"error={r['error']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
