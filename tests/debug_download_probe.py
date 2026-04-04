#!/usr/bin/env python3
"""
Debug probe for supplier file downloads.

Usage:
  python tests/debug_download_probe.py <url> --filetype PDF
  python tests/debug_download_probe.py <url> --filetype Image

This script runs multiple download methods independently and reports:
- final URL / redirect info
- status code (when available)
- content headers
- file signature (first bytes)
- whether payload looks like a valid PDF/Image
- whether payload looks like HTML/challenge page
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}


def _read_head(path: Path, size: int = 4096) -> bytes:
    with path.open("rb") as f:
        return f.read(size)


def _ascii_preview(data: bytes, length: int = 120) -> str:
    preview = []
    for b in data[:length]:
        if 32 <= b <= 126:
            preview.append(chr(b))
        else:
            preview.append(".")
    return "".join(preview)


def _hex_preview(data: bytes, length: int = 32) -> str:
    return data[:length].hex(" ")


def _looks_like_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF")


def _looks_like_image(data: bytes) -> bool:
    return data.startswith(
        (
            b"\x89PNG\r\n\x1a\n",  # png
            b"\xff\xd8\xff",  # jpeg
            b"GIF87a",
            b"GIF89a",
            b"RIFF",  # webp container
            b"BM",  # bmp
        )
    )


def _looks_like_html(data: bytes) -> bool:
    lower = data.lower()
    return (
        b"<!doctype html" in lower
        or b"<html" in lower
        or b"<head" in lower
        or b"<script" in lower
        or b"cf-chl" in lower
        or b"captcha" in lower
    )


def _report_file(path: Path, filetype: str) -> dict:
    data = _read_head(path)
    size = path.stat().st_size

    is_pdf = _looks_like_pdf(data)
    is_image = _looks_like_image(data)
    is_html = _looks_like_html(data)

    valid = is_pdf if filetype == "PDF" else is_image if filetype == "Image" else False

    return {
        "size": size,
        "is_valid": valid,
        "is_pdf": is_pdf,
        "is_image": is_image,
        "is_html": is_html,
        "head_hex": _hex_preview(data),
        "head_ascii": _ascii_preview(data),
    }


def _print_result(name: str, result: dict) -> None:
    print(f"\n=== {name} ===")
    for key in [
        "ok",
        "status",
        "final_url",
        "content_type",
        "content_encoding",
        "content_length",
        "file_size",
        "is_valid",
        "is_pdf",
        "is_image",
        "is_html",
    ]:
        if key in result:
            print(f"{key}: {result[key]}")

    if "head_hex" in result:
        print(f"head_hex:   {result['head_hex']}")
    if "head_ascii" in result:
        print(f"head_ascii: {result['head_ascii']}")

    if "error" in result and result["error"]:
        print(f"error: {result['error']}")


def _method_urllib(url: str, out_path: Path, with_headers: bool) -> dict:
    result = {"ok": False, "method": "urllib", "error": ""}
    opener = None

    try:
        if with_headers:
            opener = urllib.request.build_opener()
            opener.addheaders = list(REQUEST_HEADERS.items())
            urllib.request.install_opener(opener)

        local_file, headers = urllib.request.urlretrieve(url, filename=str(out_path))

        file_report = _report_file(Path(local_file), filetype=ARGS.filetype)
        result.update(
            {
                "ok": file_report["is_valid"],
                "status": "n/a",
                "final_url": url,
                "content_type": headers.get("Content-Type", ""),
                "content_encoding": headers.get("Content-Encoding", ""),
                "content_length": headers.get("Content-Length", ""),
                "file_size": file_report["size"],
                "is_valid": file_report["is_valid"],
                "is_pdf": file_report["is_pdf"],
                "is_image": file_report["is_image"],
                "is_html": file_report["is_html"],
                "head_hex": file_report["head_hex"],
                "head_ascii": file_report["head_ascii"],
            }
        )
    except Exception as e:
        result["error"] = repr(e)

    return result


def _method_requests(url: str, out_path: Path) -> dict:
    result = {"ok": False, "method": "requests", "error": ""}

    try:
        import requests

        response = requests.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=20,
            allow_redirects=True,
        )

        out_path.write_bytes(response.content)

        file_report = _report_file(out_path, filetype=ARGS.filetype)
        result.update(
            {
                "ok": file_report["is_valid"],
                "status": response.status_code,
                "final_url": response.url,
                "content_type": response.headers.get("Content-Type", ""),
                "content_encoding": response.headers.get("Content-Encoding", ""),
                "content_length": response.headers.get("Content-Length", ""),
                "file_size": file_report["size"],
                "is_valid": file_report["is_valid"],
                "is_pdf": file_report["is_pdf"],
                "is_image": file_report["is_image"],
                "is_html": file_report["is_html"],
                "head_hex": file_report["head_hex"],
                "head_ascii": file_report["head_ascii"],
            }
        )
    except Exception as e:
        result["error"] = repr(e)

    return result


def _method_cloudscraper(url: str, out_path: Path) -> dict:
    result = {"ok": False, "method": "cloudscraper", "error": ""}

    try:
        import cloudscraper

        scraper = cloudscraper.create_scraper()
        response = scraper.get(url, headers=REQUEST_HEADERS, timeout=20)

        out_path.write_bytes(response.content)

        file_report = _report_file(out_path, filetype=ARGS.filetype)
        result.update(
            {
                "ok": file_report["is_valid"],
                "status": response.status_code,
                "final_url": response.url,
                "content_type": response.headers.get("Content-Type", ""),
                "content_encoding": response.headers.get("Content-Encoding", ""),
                "content_length": response.headers.get("Content-Length", ""),
                "file_size": file_report["size"],
                "is_valid": file_report["is_valid"],
                "is_pdf": file_report["is_pdf"],
                "is_image": file_report["is_image"],
                "is_html": file_report["is_html"],
                "head_hex": file_report["head_hex"],
                "head_ascii": file_report["head_ascii"],
            }
        )
    except Exception as e:
        result["error"] = repr(e)

    return result


def main() -> int:
    print(f"URL: {ARGS.url}")
    print(f"Filetype: {ARGS.filetype}")

    temp_dir = Path(tempfile.mkdtemp(prefix="kintree-download-probe-"))
    print(f"Workdir: {temp_dir}")

    try:
        results = []

        results.append(
            _method_urllib(
                ARGS.url,
                temp_dir / f"urllib_no_headers.{ARGS.ext}",
                with_headers=False,
            )
        )

        results.append(
            _method_urllib(
                ARGS.url,
                temp_dir / f"urllib_headers.{ARGS.ext}",
                with_headers=True,
            )
        )

        results.append(_method_requests(ARGS.url, temp_dir / f"requests.{ARGS.ext}"))

        if ARGS.include_cloudscraper:
            results.append(_method_cloudscraper(ARGS.url, temp_dir / f"cloudscraper.{ARGS.ext}"))

        for res in results:
            label = res.get("method", "unknown")
            if label == "urllib" and "headers" in str(res.get("final_url", "")):
                pass
            _print_result(label, res)

        print("\nSaved raw payloads for inspection in:")
        for file in sorted(temp_dir.iterdir()):
            print(f"- {file}")

        if ARGS.keep:
            print("\n--keep is set, files are preserved.")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            print("\nTemporary files removed (use --keep to preserve).")

        return 0
    except Exception as e:
        print(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe downloader payload validity")
    parser.add_argument("url", help="Target URL to download")
    parser.add_argument("--filetype", choices=["PDF", "Image"], default="PDF")
    parser.add_argument("--keep", action="store_true", help="Keep downloaded payload files")
    parser.add_argument("--include-cloudscraper", action="store_true", help="Also test cloudscraper")

    ARGS = parser.parse_args()
    ARGS.ext = "pdf" if ARGS.filetype == "PDF" else "bin"

    sys.exit(main())
