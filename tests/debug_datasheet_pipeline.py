#!/usr/bin/env python3
"""
Probe the exact Ki-nTree datasheet download pipeline for a single URL.

This mirrors the production path in kintree.common.tools:
- Playwright (if enabled for supplier)
- urllib
- urllib + headers
- requests
- cloudscraper (for image and TME PDF fallback)

Usage:
  python tests/debug_datasheet_pipeline.py --url "https://www.analog.com/media/en/technical-documentation/data-sheets/AD829.pdf" --supplier Digi-Key --timeout 10 --keep
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import tempfile

from kintree.common import tools


def print_result(label: str, ok: bool, path: Path | None = None, note: str = "") -> None:
    print(f"\n=== {label} ===")
    print(f"ok: {ok}")
    if path:
        exists = path.exists()
        print(f"file: {path}")
        print(f"exists: {exists}")
        if exists:
            print(f"size: {path.stat().st_size}")
            with path.open("rb") as f:
                head = f.read(64)
            print(f"head_hex: {head.hex(' ')}")
            ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in head)
            print(f"head_ascii: {ascii_preview}")
            valid = tools.validate_downloaded_file(
                file_path=str(path),
                filetype="PDF",
                source_url=ARGS.url,
                silent=False,
            )
            print(f"validate_downloaded_file: {valid}")
    if note:
        print(f"note: {note}")


def main() -> int:
    supplier = ARGS.supplier
    timeout = ARGS.timeout

    workdir = Path(tempfile.mkdtemp(prefix="kintree-datasheet-pipeline-"))
    print(f"URL: {ARGS.url}")
    print(f"Supplier: {supplier}")
    print(f"Timeout: {timeout}")
    print(f"Workdir: {workdir}")

    try:
        # 0) Playwright is now universal (no supplier gate)
        print("\nplaywright_enabled: True (now universal for all PDF/Image downloads)")

        # 1) Playwright path
        pw_file = workdir / "playwright.pdf"
        pw_ok = tools._download_with_playwright(
            url=ARGS.url,
            fileoutput=str(pw_file),
            filetype="PDF",
            timeout=timeout,
            silent=False,
        )
        print_result("playwright", pw_ok, pw_file)

        # 2) urllib (no headers)
        urllib_file = workdir / "urllib.pdf"
        urllib_ok = bool(
            tools.download(
                ARGS.url,
                filetype="PDF",
                fileoutput=str(urllib_file),
                timeout=timeout,
                enable_headers=False,
                requests_lib=False,
                try_cloudscraper=False,
                silent=False,
            )
        )
        print_result("urllib", urllib_ok, urllib_file)

        # 3) urllib + headers
        urllib_h_file = workdir / "urllib_headers.pdf"
        urllib_h_ok = bool(
            tools.download(
                ARGS.url,
                filetype="PDF",
                fileoutput=str(urllib_h_file),
                timeout=timeout,
                enable_headers=True,
                requests_lib=False,
                try_cloudscraper=False,
                silent=False,
            )
        )
        print_result("urllib+headers", urllib_h_ok, urllib_h_file)

        # 4) requests
        req_file = workdir / "requests.pdf"
        req_ok = bool(
            tools.download(
                ARGS.url,
                filetype="PDF",
                fileoutput=str(req_file),
                timeout=timeout,
                enable_headers=True,
                requests_lib=True,
                try_cloudscraper=False,
                silent=False,
            )
        )
        print_result("requests", req_ok, req_file)

        # 5) cloudscraper
        cs_file = workdir / "cloudscraper.pdf"
        cs_ok = bool(
            tools.download(
                ARGS.url,
                filetype="PDF",
                fileoutput=str(cs_file),
                timeout=timeout,
                enable_headers=True,
                requests_lib=False,
                try_cloudscraper=True,
                silent=False,
            )
        )
        print_result("cloudscraper", cs_ok, cs_file)

        # 6) Full wrapper path
        wrapper_file = workdir / "download_with_retry.pdf"
        wrapper_ok = tools.download_with_retry(
            ARGS.url,
            str(wrapper_file),
            filetype="PDF",
            timeout=timeout,
            supplier=supplier,
            silent=False,
        )
        print_result("download_with_retry", bool(wrapper_ok), wrapper_file)

        print("\nArtifacts:")
        for f in sorted(workdir.iterdir()):
            print(f"- {f}")

        if ARGS.keep:
            print("\n--keep enabled; files preserved.")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
            print("\nTemporary files removed (use --keep to preserve).")

        return 0
    except Exception as e:
        print(f"Fatal error: {e!r}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Debug Ki-nTree datasheet pipeline")
    parser.add_argument("--url", required=True, help="Datasheet URL")
    parser.add_argument("--supplier", default="Digi-Key", help="Supplier name (e.g. Digi-Key, TME)")
    parser.add_argument("--timeout", type=int, default=10, help="Per-attempt timeout seconds")
    parser.add_argument("--keep", action="store_true", help="Keep artifacts")
    ARGS = parser.parse_args()
    raise SystemExit(main())
