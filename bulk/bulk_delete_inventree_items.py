#!/usr/bin/env python3
"""
Bulk delete InvenTree objects by category (or explicit PK list).

Uses InvenTree bulk-delete endpoint:
  DELETE /api/<model>/bulk-delete/

Examples:
  python bulk_delete_inventree_items.py --model part --category-id 7 --dry-run
  python bulk_delete_inventree_items.py --model part --category-id 7 --confirm
  python bulk_delete_inventree_items.py --model part --items 1,10,50,99 --confirm
  python bulk_delete_inventree_items.py --model part --category-id 7 --extra-filters active=false --confirm
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

from inventree.part import Part
from inventree.stock import StockItem
import requests

from kintree.config import settings
from kintree.database import inventree_api

MODEL_CONFIG = {
    # model_arg: (api_path, sdk_class)
    "part": ("part", Part),
    "stock": ("stock", StockItem),
    "stockitem": ("stock", StockItem),
}


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_extra_filters(raw_pairs: List[str]) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    for pair in raw_pairs:
        if "=" not in pair:
            raise ValueError(
                f"Invalid --extra-filters value '{pair}', expected key=value"
            )
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid --extra-filters value '{pair}', empty key")

        # Attempt light type coercion for convenience.
        lowered = value.lower()
        if lowered in {"true", "false", "yes", "no", "on", "off", "1", "0"}:
            coerced: Any = _parse_bool(value)
        else:
            try:
                coerced = int(value)
            except ValueError:
                try:
                    coerced = float(value)
                except ValueError:
                    coerced = value

        filters[key] = coerced
    return filters


def _build_auth_headers(api_obj) -> Dict[str, str]:
    token = getattr(api_obj, "token", None)
    if not token:
        raise RuntimeError("InvenTree auth token is missing after login")
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _fetch_all_matching(
    base_url: str,
    headers: Dict[str, str],
    model: str,
    filters: Dict[str, Any],
    timeout: int,
) -> List[Dict[str, Any]]:
    """Fetch all matching records using paginated list endpoint for preview/logging."""
    all_items: List[Dict[str, Any]] = []
    url = _api_url(base_url, f"api/{model}/")
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
                    "Unexpected list response format: 'results' is not a list"
                )
            all_items.extend(results)
            url = data.get("next")
            params = {}  # next URL already encodes query params
        elif isinstance(data, list):
            all_items.extend(data)
            break
        else:
            raise RuntimeError("Unexpected list response format from InvenTree")

    return all_items


def _bulk_delete(
    base_url: str,
    headers: Dict[str, str],
    model: str,
    payload: Dict[str, Any],
    timeout: int,
) -> requests.Response:
    endpoint = _api_url(base_url, f"api/{model}/bulk-delete/")
    return requests.delete(endpoint, headers=headers, json=payload, timeout=timeout)


def _resolve_model(model_arg: str):
    key = str(model_arg or "").strip().lower()
    if key in MODEL_CONFIG:
        return MODEL_CONFIG[key]
    return key, None


def _sequential_delete(
    base_url: str,
    headers: Dict[str, str],
    model_path: str,
    items: List[Dict[str, Any]],
    timeout: int,
    deactivate_first: bool = False,
) -> Dict[str, Any]:
    deleted = 0
    failed = 0
    failures = []

    for row in items:
        pk = row.get("pk") or row.get("id")
        if pk is None:
            failed += 1
            failures.append({"pk": None, "error": "Missing pk/id in row"})
            continue

        endpoint = _api_url(base_url, f"api/{model_path}/{pk}/")
        try:
            if deactivate_first and model_path == "part":
                patch_response = requests.patch(
                    endpoint, headers=headers, json={"active": False}, timeout=timeout
                )
                if patch_response.status_code >= 400:
                    failed += 1
                    failures.append(
                        {
                            "pk": pk,
                            "status": patch_response.status_code,
                            "body": f"deactivate failed: {patch_response.text[:500]}",
                        }
                    )
                    continue

            response = requests.delete(endpoint, headers=headers, timeout=timeout)
            if response.status_code in [200, 202, 204]:
                deleted += 1
            else:
                failed += 1
                failures.append(
                    {
                        "pk": pk,
                        "status": response.status_code,
                        "body": response.text[:500],
                    }
                )
        except requests.RequestException as exc:
            failed += 1
            failures.append({"pk": pk, "error": repr(exc)})

    return {
        "deleted": deleted,
        "failed": failed,
        "failures": failures,
    }


def _fetch_all_categories(
    base_url: str, headers: Dict[str, str], timeout: int
) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    url = _api_url(base_url, "api/part/category/")

    while url:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "results" in data:
            rows = data.get("results") or []
            if not isinstance(rows, list):
                raise RuntimeError(
                    "Unexpected category response format: 'results' is not a list"
                )
            all_rows.extend(rows)
            url = data.get("next")
        elif isinstance(data, list):
            all_rows.extend(data)
            break
        else:
            raise RuntimeError("Unexpected category response format from InvenTree")

    return all_rows


def _category_path(
    category_id: int, category_map: Dict[int, Dict[str, Any]], memo: Dict[int, str]
) -> str:
    if category_id in memo:
        return memo[category_id]

    row = category_map.get(category_id) or {}
    name = str(row.get("name") or "")
    parent = row.get("parent")

    if parent is None:
        result = name
    else:
        parent_path = _category_path(int(parent), category_map, memo)
        result = f"{parent_path} / {name}" if parent_path else name

    memo[category_id] = result
    return result


def _print_categories(categories: List[Dict[str, Any]]) -> None:
    category_map: Dict[int, Dict[str, Any]] = {}
    for row in categories:
        pk = row.get("pk") or row.get("id")
        if pk is None:
            continue
        category_map[int(pk)] = row

    memo: Dict[int, str] = {}
    lines = []
    for cid in sorted(category_map.keys()):
        path = _category_path(cid, category_map, memo)
        lines.append((path.lower(), f"- id={cid} path={path}"))

    print(f"Categories found: {len(lines)}")
    for _, line in sorted(lines, key=lambda x: x[0]):
        print(line)


def _resolve_category_id_by_name(
    categories: List[Dict[str, Any]], category_name: str
) -> int:
    category_map: Dict[int, Dict[str, Any]] = {}
    for row in categories:
        pk = row.get("pk") or row.get("id")
        if pk is None:
            continue
        category_map[int(pk)] = row

    memo: Dict[int, str] = {}
    needle = category_name.strip().lower()

    exact_path_matches: List[int] = []
    leaf_matches: List[int] = []
    contains_path_matches: List[int] = []

    for cid, row in category_map.items():
        path = _category_path(cid, category_map, memo)
        leaf = str(row.get("name") or "")

        path_l = path.lower()
        leaf_l = leaf.lower()

        if path_l == needle:
            exact_path_matches.append(cid)
        if leaf_l == needle:
            leaf_matches.append(cid)
        if needle in path_l:
            contains_path_matches.append(cid)

    if len(exact_path_matches) == 1:
        return exact_path_matches[0]
    if len(leaf_matches) == 1:
        return leaf_matches[0]

    if len(exact_path_matches) > 1 or len(leaf_matches) > 1:
        ambiguous = exact_path_matches if len(exact_path_matches) > 1 else leaf_matches
        print(f"ERROR: category name '{category_name}' is ambiguous")
        for cid in ambiguous:
            print(f"- id={cid} path={_category_path(cid, category_map, memo)}")
        raise RuntimeError("Ambiguous category name")

    if len(contains_path_matches) == 1:
        return contains_path_matches[0]

    if len(contains_path_matches) > 1:
        print(f"ERROR: category name '{category_name}' matched multiple categories")
        for cid in contains_path_matches[:20]:
            print(f"- id={cid} path={_category_path(cid, category_map, memo)}")
        if len(contains_path_matches) > 20:
            print(f"... and {len(contains_path_matches) - 20} more")
        raise RuntimeError("Ambiguous category name")

    raise RuntimeError(f"Category '{category_name}' not found")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk delete InvenTree objects by category or item list"
    )
    parser.add_argument(
        "--model", default="part", help="InvenTree model endpoint (default: part)"
    )
    parser.add_argument(
        "--category-id", type=int, help="Category ID to filter by (e.g. 7)"
    )
    parser.add_argument(
        "--category-name",
        help="Category name or full path (e.g. Electronics / Connectors)",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List all InvenTree part categories and exit",
    )
    parser.add_argument(
        "--items", help="Comma-separated PK list to delete (e.g. 1,10,50)"
    )
    parser.add_argument(
        "--extra-filters",
        nargs="*",
        default=[],
        help="Additional filters key=value (e.g. active=false)",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument(
        "--deactivate-first",
        action="store_true",
        help="For part model, set active=false before deleting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matching items only; do not delete",
    )
    parser.add_argument(
        "--confirm", action="store_true", help="Actually execute bulk delete"
    )

    args = parser.parse_args()

    if (
        not args.list_categories
        and not args.category_id
        and not args.category_name
        and not args.items
        and not args.extra_filters
    ):
        print(
            "ERROR: provide at least one of --category-id, --category-name, --items, or --extra-filters"
        )
        return 2

    if not args.list_categories and not args.dry_run and not args.confirm:
        print("ERROR: refusing to run destructive operation without --confirm")
        print("Tip: start with --dry-run")
        return 2

    # Load InvenTree credentials from Ki-nTree config.
    settings.load_inventree_settings()

    if not settings.SERVER_ADDRESS:
        print("ERROR: Missing InvenTree SERVER_ADDRESS in config")
        return 2

    if not settings.USERNAME or not settings.PASSWORD:
        print("ERROR: Missing InvenTree USERNAME/PASSWORD in config")
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

    if args.list_categories or args.category_name:
        try:
            categories = _fetch_all_categories(base_url, headers, timeout=args.timeout)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            body = exc.response.text[:1200] if exc.response is not None else ""
            print(f"ERROR: failed to fetch categories (HTTP {status})")
            print(body)
            return 1
        except requests.RequestException as exc:
            print(f"ERROR: network error during category fetch: {repr(exc)}")
            return 1
        except Exception as exc:
            print(f"ERROR: unexpected error during category fetch: {repr(exc)}")
            return 1

        if args.list_categories:
            _print_categories(categories)
            return 0

        try:
            args.category_id = _resolve_category_id_by_name(
                categories, args.category_name
            )
            print(f"Resolved category '{args.category_name}' -> id={args.category_id}")
        except Exception as exc:
            print(f"ERROR: {str(exc)}")
            return 2

    payload: Dict[str, Any] = {}
    model_path, model_class = _resolve_model(args.model)

    if args.items:
        try:
            item_ids = [int(x.strip()) for x in args.items.split(",") if x.strip()]
        except ValueError:
            print("ERROR: --items must be a comma-separated list of integers")
            return 2
        if not item_ids:
            print("ERROR: --items provided but no valid integer IDs found")
            return 2
        payload["items"] = item_ids
    else:
        filters = {}
        if args.category_id is not None:
            filters["category"] = args.category_id
        filters.update(_parse_extra_filters(args.extra_filters))
        if not filters:
            print(
                "ERROR: no filters could be determined; provide --category-id, --category-name, or --extra-filters"
            )
            return 2
        payload["filters"] = filters

    print("Bulk-delete payload:")
    print(json.dumps(payload, indent=2, sort_keys=True))

    # Preview list for visibility and safety.
    try:
        if "filters" in payload:
            matching = _fetch_all_matching(
                base_url, headers, model_path, payload["filters"], timeout=args.timeout
            )
            print(f"Matching items: {len(matching)}")
            for row in matching[:20]:
                pk = row.get("pk") or row.get("id")
                name = row.get("name") or row.get("description") or row.get("IPN") or ""
                print(f"- pk={pk} {name}")
            if len(matching) > 20:
                print(f"... and {len(matching) - 20} more")
        else:
            print(f"Requested explicit item count: {len(payload['items'])}")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:1200] if exc.response is not None else ""
        print(f"ERROR: failed preview fetch (HTTP {status})")
        print(body)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: network error during preview fetch: {repr(exc)}")
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected error during preview fetch: {repr(exc)}")
        return 1

    if args.dry_run:
        print("Dry-run complete. No deletion performed.")
        return 0

    # Execute delete (bulk when supported, sequential fallback otherwise).
    # SDK bulkDelete only supports explicit item lists, not arbitrary filters.
    use_sdk_bulk = (
        model_class is not None
        and hasattr(model_class, "bulkDelete")
        and "items" in payload
    )
    try:
        if use_sdk_bulk:
            print(f"Using SDK bulkDelete for model '{args.model}'")
            try:
                result = model_class.bulkDelete(
                    api_obj,
                    items=payload.get("items"),
                    filters=payload.get("filters"),
                )
            except Exception as exc:
                print(f"ERROR: SDK bulkDelete failed: {repr(exc)}")
                return 1

            print("Bulk delete completed")
            if result is not None:
                try:
                    print(json.dumps(result, indent=2, sort_keys=True))
                except Exception:
                    print(result)
            return 0

        # Fallback path: filters-only payloads or models without SDK bulkDelete.
        print(f"Using sequential delete for model '{args.model}'")

        if "items" in payload:
            rows = [{"pk": item_pk} for item_pk in payload["items"]]
        else:
            rows = _fetch_all_matching(
                base_url, headers, model_path, payload["filters"], timeout=args.timeout
            )

        summary = _sequential_delete(
            base_url,
            headers,
            model_path,
            rows,
            timeout=args.timeout,
            deactivate_first=args.deactivate_first,
        )
        print(
            f"Sequential delete completed: deleted={summary['deleted']} failed={summary['failed']}"
        )
        if summary["failed"]:
            print("Failures (up to first 20):")
            for failure in summary["failures"][:20]:
                print(json.dumps(failure, sort_keys=True))
            return 1
        return 0

    except requests.RequestException as exc:
        print(f"ERROR: network error during delete: {repr(exc)}")
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected error during delete: {repr(exc)}")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user")
        raise SystemExit(130)
