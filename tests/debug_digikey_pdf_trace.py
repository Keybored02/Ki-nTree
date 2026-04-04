#!/usr/bin/env python3
"""
Deep trace for DigiKey datasheet PDF download issues.

Goal:
- Trace redirects and response headers
- Trace Playwright network activity
- Test Playwright download events vs response body
- Inspect candidate PDF links extracted from page

Usage:
  python tests/debug_digikey_pdf_trace.py --url "https://www.analog.com/media/en/technical-documentation/data-sheets/AD829.pdf" --timeout 20 --keep
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import urllib.parse
from pathlib import Path


def looks_like_pdf(data: bytes) -> bool:
    if not data:
        return False
    head = data[:2048]
    return head.startswith(b"%PDF") or (b"%PDF" in head[:1024])


def ascii_preview(data: bytes, limit: int = 120) -> str:
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in data[:limit])


def print_file_probe(path: Path) -> None:
    if not path.exists():
        print(f"file_exists: False ({path})")
        return

    blob = path.read_bytes()[:256]
    print(f"file_exists: True ({path})")
    print(f"file_size: {path.stat().st_size}")
    print(f"file_is_pdf: {looks_like_pdf(blob)}")
    print(f"head_hex: {blob[:64].hex(' ')}")
    print(f"head_ascii: {ascii_preview(blob)}")


def trace_requests(url: str, timeout: int) -> None:
    print("\n=== requests trace ===")
    try:
        import requests
    except Exception as e:
        print(f"requests_import_error: {e}")
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/pdf,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

    current = url
    for hop in range(1, 11):
        try:
            resp = requests.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        except Exception as e:
            print(f"hop_{hop}_error: {e}")
            return

        print(f"hop_{hop}_status: {resp.status_code}")
        print(f"hop_{hop}_url: {current}")
        print(f"hop_{hop}_content_type: {resp.headers.get('Content-Type', '')}")
        print(f"hop_{hop}_location: {resp.headers.get('Location', '')}")

        if resp.status_code in [301, 302, 303, 307, 308] and resp.headers.get("Location"):
            current = urllib.parse.urljoin(current, resp.headers["Location"])
            continue

        head = resp.content[:256]
        print(f"final_is_pdf: {looks_like_pdf(head)}")
        print(f"final_head_ascii: {ascii_preview(head)}")
        return

    print("redirect_limit_reached: 10")


def trace_playwright(url: str, timeout: int, out_dir: Path) -> None:
    print("\n=== playwright trace ===")
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as e:
        print(f"playwright_import_error: {e}")
        return

    timeout_ms = max(5000, timeout * 1000)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        seen_responses = []

        def on_response(response):
            ct = response.headers.get("content-type", "")
            status = response.status
            rurl = response.url
            if "pdf" in ct.lower() or "analog.com" in rurl or "digikey" in rurl:
                seen_responses.append((status, rurl, ct))

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            print(f"goto_error: {e}")

        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

        print(f"page_url_after_goto: {page.url}")

        # Attempt download-event capture from current URL.
        try:
            out_file = out_dir / "playwright_download_event.pdf"
            with page.expect_download(timeout=timeout_ms) as dl:
                page.goto(page.url, wait_until="domcontentloaded", timeout=timeout_ms)
            download = dl.value
            tmp = download.path()
            if tmp:
                shutil.copyfile(tmp, out_file)
            print("download_event_captured: True")
            print_file_probe(out_file)
        except PlaywrightTimeoutError:
            print("download_event_captured: False (timeout)")
        except Exception as e:
            print(f"download_event_error: {e}")

        # Candidate extraction.
        candidates = []
        selectors = [
            ("iframe[src]", "src"),
            ("embed[src]", "src"),
            ("object[data]", "data"),
            ("a[href*='.pdf']", "href"),
        ]

        for selector, attr in selectors:
            try:
                for loc in page.locator(selector).all():
                    value = loc.get_attribute(attr)
                    if value:
                        full = urllib.parse.urljoin(page.url, value)
                        if full.startswith("http") and full not in candidates:
                            candidates.append(full)
            except Exception:
                pass

        if page.url.startswith("http") and page.url not in candidates:
            candidates.insert(0, page.url)
        if url not in candidates:
            candidates.insert(0, url)

        print(f"candidate_count: {len(candidates)}")
        for idx, candidate in enumerate(candidates[:10], start=1):
            print(f"candidate_{idx}: {candidate}")

        # Try request context on each candidate.
        for idx, candidate in enumerate(candidates[:10], start=1):
            try:
                resp = context.request.get(candidate, timeout=timeout_ms, max_redirects=10)
                ct = resp.headers.get("content-type", "")
                body = resp.body()
                print(f"candidate_{idx}_status: {resp.status}")
                print(f"candidate_{idx}_ct: {ct}")
                print(f"candidate_{idx}_is_pdf: {looks_like_pdf(body[:2048])}")
                if looks_like_pdf(body[:2048]):
                    out_file = out_dir / f"playwright_candidate_{idx}.pdf"
                    out_file.write_bytes(body)
                    print_file_probe(out_file)
                    break
            except Exception as e:
                print(f"candidate_{idx}_error: {e}")

        print("responses_seen:")
        for status, rurl, ct in seen_responses[:30]:
            print(f"- status={status} ct={ct} url={rurl}")

        browser.close()


def main() -> int:
    out_dir = Path(tempfile.mkdtemp(prefix="kintree-digikey-pdf-trace-"))
    print(f"url: {ARGS.url}")
    print(f"timeout: {ARGS.timeout}")
    print(f"workdir: {out_dir}")

    trace_requests(ARGS.url, ARGS.timeout)
    trace_playwright(ARGS.url, ARGS.timeout, out_dir)

    print("\nartifacts:")
    for f in sorted(out_dir.iterdir()):
        print(f"- {f}")

    if ARGS.keep:
        print("\n--keep enabled; artifacts preserved")
    else:
        shutil.rmtree(out_dir, ignore_errors=True)
        print("\nartifacts removed (use --keep)")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trace DigiKey datasheet download behavior")
    parser.add_argument("--url", required=True, help="Datasheet URL")
    parser.add_argument("--timeout", type=int, default=20, help="Timeout seconds")
    parser.add_argument("--keep", action="store_true", help="Keep downloaded artifacts")
    ARGS = parser.parse_args()
    raise SystemExit(main())
