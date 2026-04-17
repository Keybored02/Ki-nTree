#!/usr/bin/env python3
"""
Deep trace for Mouser datasheet PDF download issues.

This focuses on redirect-heavy manufacturer links (TE, etc.) surfaced by Mouser.

Usage:
  python tests/debug_mouser_pdf_trace.py --url "https://www.te.com/commerce/DocumentDelivery/DDEController?Action=srchrtrv&DocNm=4-1773442-7&DocType=Data%20Sheet&DocLang=English&PartCntxt=5-1734857-5&DocFormat=pdf" --timeout 30 --keep
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import tempfile
import urllib.parse


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
        import logging

        logging.exception("requests_import_error:")
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
    for hop in range(1, 16):
        try:
            resp = requests.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        except Exception as e:
            import logging

            logging.exception(f"hop_{hop}_error:")
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

    print("redirect_limit_reached: 15")


def trace_playwright(url: str, timeout: int, out_dir: Path) -> None:
    print("\n=== playwright trace ===")
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except Exception as e:
        import logging

        logging.exception("playwright_import_error:")
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
            if "pdf" in ct.lower() or "mouser" in rurl.lower() or "te.com" in rurl.lower():
                seen_responses.append((status, rurl, ct))

        page.on("response", on_response)

        # Direct request-context fetch first (often succeeds even when page navigation does not).
        try:
            direct = context.request.get(url, timeout=timeout_ms, max_redirects=20)
            body = direct.body()
            print(f"direct_request_status: {direct.status}")
            print(f"direct_request_ct: {direct.headers.get('content-type', '')}")
            print(f"direct_request_is_pdf: {looks_like_pdf(body[:2048])}")
            if looks_like_pdf(body[:2048]):
                out = out_dir / "playwright_direct.pdf"
                out.write_bytes(body)
                print_file_probe(out)
        except Exception as e:
            import logging

            logging.exception("direct_request_error:")
            print(f"direct_request_error: {e}")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            import logging

            logging.exception("goto_error:")
            print(f"goto_error: {e}")

        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            import logging

            logging.exception("Exception in wait_for_load_state:")

        print(f"page_url_after_goto: {page.url}")

        # Download event capture.
        try:
            out_file = out_dir / "playwright_download_event.pdf"
            with page.expect_download(timeout=timeout_ms) as dl:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
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

        # Candidate extraction and request.
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
                import logging

                logging.exception("Exception extracting candidate URLs:")

        if page.url.startswith("http") and page.url not in candidates:
            candidates.insert(0, page.url)
        if url not in candidates:
            candidates.insert(0, url)

        print(f"candidate_count: {len(candidates)}")
        for idx, candidate in enumerate(candidates[:12], start=1):
            print(f"candidate_{idx}: {candidate}")

        for idx, candidate in enumerate(candidates[:12], start=1):
            try:
                resp = context.request.get(candidate, timeout=timeout_ms, max_redirects=20)
                ct = resp.headers.get("content-type", "")
                body = resp.body()
                print(f"candidate_{idx}_status: {resp.status}")
                print(f"candidate_{idx}_ct: {ct}")
                print(f"candidate_{idx}_is_pdf: {looks_like_pdf(body[:2048])}")
                if looks_like_pdf(body[:2048]):
                    out = out_dir / f"playwright_candidate_{idx}.pdf"
                    out.write_bytes(body)
                    print_file_probe(out)
                    break
            except Exception as e:
                import logging

                logging.exception(f"candidate_{idx}_error:")
                print(f"candidate_{idx}_error: {e}")

        print("responses_seen:")
        for status, rurl, ct in seen_responses[:40]:
            print(f"- status={status} ct={ct} url={rurl}")

        browser.close()


def main() -> int:
    out_dir = Path(tempfile.mkdtemp(prefix="kintree-mouser-pdf-trace-"))
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
    parser = argparse.ArgumentParser(description="Trace Mouser datasheet download behavior")
    parser.add_argument(
        "--url",
        default="https://www.te.com/commerce/DocumentDelivery/DDEController?Action=srchrtrv&DocNm=4-1773442-7&DocType=Data%20Sheet&DocLang=English&PartCntxt=5-1734857-5&DocFormat=pdf",
        help="Datasheet URL",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Timeout seconds")
    parser.add_argument("--keep", action="store_true", help="Keep downloaded artifacts")
    ARGS = parser.parse_args()
    raise SystemExit(main())
