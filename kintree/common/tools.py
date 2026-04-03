import builtins
import json
import os
import urllib.parse
from shutil import copyfile


# CUSTOM PRINT METHOD
class pcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    ERROR = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# Overload print function with custom pretty-print


def cprint(*args, **kwargs):
    # Check if silent is set
    try:
        silent = kwargs.pop('silent')
    except:
        silent = False
    if not silent:
        if type(args[0]) is dict:
            return builtins.print(json.dumps(*args, **kwargs, indent=4, sort_keys=True))
        else:
            try:
                args = list(args)
                if 'warning' in args[0].lower():
                    args[0] = f'{pcolors.WARNING}{args[0]}{pcolors.ENDC}'
                elif 'error' in args[0].lower():
                    args[0] = f'{pcolors.ERROR}{args[0]}{pcolors.ENDC}'
                elif 'fail' in args[0].lower():
                    args[0] = f'{pcolors.ERROR}{args[0]}{pcolors.ENDC}'
                elif 'success' in args[0].lower():
                    args[0] = f'{pcolors.OKGREEN}{args[0]}{pcolors.ENDC}'
                elif 'pass' in args[0].lower():
                    args[0] = f'{pcolors.OKGREEN}{args[0]}{pcolors.ENDC}'
                elif 'main' in args[0].lower():
                    args[0] = f'{pcolors.HEADER}{args[0]}{pcolors.ENDC}'
                elif 'skipping' in args[0].lower():
                    args[0] = f'{pcolors.BOLD}{args[0]}{pcolors.ENDC}'
                args = tuple(args)
            except:
                pass
            return builtins.print(*args, **kwargs, flush=True)
###


def create_library(library_path: str, symbol: str, template_lib: str):
    ''' Create library files if they don\'t exist '''
    if not os.path.exists(library_path):
        os.mkdir(library_path)
    new_kicad_sym_file = os.path.join(library_path, f'{symbol}.kicad_sym')
    if not os.path.exists(new_kicad_sym_file):
        copyfile(template_lib, new_kicad_sym_file)


def get_image_with_retries(url, headers, retries=3, wait=5, silent=False):
    """Fetch URL with cloudscraper (bypasses anti-bot measures)."""
    import cloudscraper
    import time
    scraper = cloudscraper.create_scraper()
    for attempt in range(retries):
        try:
            response = scraper.get(url, headers=headers, timeout=wait)
            if response.status_code == 200:
                return response
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(wait)
    return None





def _is_expected_filetype(filetype: str, content_type: str, data: bytes, source_url: str) -> bool:
    expected = str(filetype or '').lower()
    ctype = str(content_type or '').lower()
    source = str(source_url or '').lower()
    sample = data[:2048] if data else b''
    sample_lower = sample.lower()

    # Guard against anti-bot/challenge pages masquerading as downloadable files.
    if b'<html' in sample_lower or b'<!doctype html' in sample_lower or b'just a moment' in sample_lower:
        return False

    if expected == 'pdf':
        # Accept only real PDF payload signatures, not just URL extension or headers.
        if sample.startswith(b'%PDF'):
            return True
        if b'%PDF' in sample[:1024]:
            return True
        return False

    if expected == 'image':
        if ctype.startswith('image/'):
            return True
        if sample.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'RIFF', b'BM')):
            return True
        # SVG can be XML text, so allow it by content and URL suffix.
        if b'<svg' in sample_lower or source.endswith('.svg'):
            return True
        return False

    return False


def _validate_downloaded_file(file_path: str, filetype: str, source_url: str = '', content_type: str = '', silent=False) -> bool:
    if not os.path.isfile(file_path):
        return False

    try:
        with open(file_path, 'rb') as f:
            data = f.read(2048)
    except OSError:
        return False

    if _is_expected_filetype(filetype, content_type, data, source_url):
        return True

    cprint(
        f'[INFO]\tWarning: Downloaded file validation failed for {filetype} (likely HTML/compressed payload)',
        silent=silent,
    )

    try:
        os.remove(file_path)
    except OSError:
        pass

    return False


def validate_downloaded_file(file_path: str, filetype: str, source_url: str = '', content_type: str = '', silent=False) -> bool:
    ''' Public wrapper to validate local downloaded files by real content signature '''
    return _validate_downloaded_file(
        file_path=file_path,
        filetype=filetype,
        source_url=source_url,
        content_type=content_type,
        silent=silent,
    )


def _download_with_playwright(url: str, fileoutput: str, filetype='PDF', timeout=20, silent=False) -> bool:
    """Download file using Playwright browser context.
    
    Implements the working logic from test scripts:
    1. Direct request-context fetch (no headers)
    2. page.goto with download event capture
    3. Candidate extraction from page
    4. For each candidate: direct request-context fetch (no headers)
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return False

    timeout_ms = max(5000, int(timeout * 1000))

    def is_http_url(candidate: str) -> bool:
        parsed = urllib.parse.urlparse(candidate or '')
        return parsed.scheme in ['http', 'https']

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()

            # Fast path: try direct request-context fetch first (no custom headers).
            try:
                direct_response = context.request.get(url, timeout=timeout_ms, max_redirects=20)
                direct_data = direct_response.body()
                if _is_expected_filetype(filetype, direct_response.headers.get('content-type', ''), direct_data, url):
                    with open(fileoutput, 'wb') as f:
                        f.write(direct_data)
                    browser.close()
                    return True
            except Exception:
                pass

            # Navigate to page and wait for content to load
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            except Exception:
                pass

            # Download event capture (handles PDFs that trigger browser downloads)
            try:
                with page.expect_download(timeout=timeout_ms) as dl:
                    page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                temp_path = dl.value.path()
                if temp_path and os.path.isfile(temp_path):
                    copyfile(temp_path, fileoutput)
                    if _validate_downloaded_file(fileoutput, filetype, url, silent=silent):
                        browser.close()
                        return True
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass

            # Extract candidate URLs from page (iframes, embeds, links, etc.)
            candidates = [url]
            if is_http_url(page.url):
                candidates.append(page.url)

            selectors = [
                ('iframe[src]', 'src'),
                ('embed[src]', 'src'),
                ('object[data]', 'data'),
                ('a[href*=".pdf"]', 'href'),
            ]

            for selector, attr in selectors:
                try:
                    for loc in page.locator(selector).all():
                        candidate = loc.get_attribute(attr)
                        if candidate:
                            full_url = urllib.parse.urljoin(page.url, candidate)
                            if is_http_url(full_url) and full_url not in candidates:
                                candidates.append(full_url)
                except Exception:
                    pass

            # Try each candidate with direct request-context fetch
            for candidate in candidates:
                if not is_http_url(candidate):
                    continue

                try:
                    response = context.request.get(candidate, timeout=timeout_ms, max_redirects=20)
                    data = response.body()
                    if _is_expected_filetype(filetype, response.headers.get('content-type', ''), data, candidate):
                        with open(fileoutput, 'wb') as f:
                            f.write(data)
                        browser.close()
                        return True
                except Exception:
                    continue

            browser.close()
    except Exception:
        return False

    return False


def download(url, filetype='API data', fileoutput='', timeout=3, enable_headers=False, requests_lib=False, try_cloudscraper=False, silent=False):
    ''' Standard method to download URL content, with option to save to local file (eg. images) '''

    import socket
    import urllib.request
    import requests

    # A more detailed headers was needed for request to Jameco
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.106 Safari/537.36',
        'Accept': 'application/pdf,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache',
    }

    # Set default timeout for download socket
    socket.setdefaulttimeout(timeout)
    if enable_headers and not requests_lib:
        opener = urllib.request.build_opener()
        opener.addheaders = list(headers.items())
        urllib.request.install_opener(opener)
    try:
        if filetype == 'PDF':
            # some distributors/manufacturers implement
            # redirects which don't allow direct downloads
            if 'gotoUrl' in url and 'www.ti.com' in url:
                mpn = url.split('%2F')[-1]
                url = f'https://www.ti.com/lit/ds/symlink/{mpn}.pdf'
        if filetype == 'Image' or filetype == 'PDF':
            # Enable use of requests library for downloading files (some URLs do NOT work with urllib)
            if requests_lib:
                response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
                content_type = response.headers.get('Content-Type', '')
                if not _is_expected_filetype(filetype, content_type, response.content, url):
                    cprint(f'[INFO]\tWarning: {filetype} download returned the wrong file type', silent=silent)
                    return None
                with open(fileoutput, 'wb') as file:
                    file.write(response.content)
            elif try_cloudscraper:
                response = get_image_with_retries(url, headers=headers, wait=max(5, timeout), silent=silent)
                if not response:
                    return None
                content_type = response.headers.get('Content-Type', '')
                if not _is_expected_filetype(filetype, content_type, response.content, url):
                    cprint(f'[INFO]\tWarning: {filetype} download returned the wrong file type', silent=silent)
                    return None
                with open(fileoutput, 'wb') as file:
                    file.write(response.content)
            else:
                (file, headers) = urllib.request.urlretrieve(url, filename=fileoutput)
                if not _validate_downloaded_file(
                    file_path=file,
                    filetype=filetype,
                    source_url=url,
                    content_type=headers.get('Content-Type', ''),
                    silent=silent,
                ):
                    cprint(f'[INFO]\tWarning: {filetype} download returned the wrong file type', silent=silent)
                    return None

            # Ensure the saved file is valid even when headers claim the right type.
            if filetype in ['PDF', 'Image']:
                if not _validate_downloaded_file(
                    file_path=fileoutput,
                    filetype=filetype,
                    source_url=url,
                    silent=silent,
                ):
                    return None
            return file
        else:
            # some suppliers work with requests.get(), others need urllib.request.urlopen()
            try:
                response = requests.get(url)
                data_json = response.json()
                return data_json
            except requests.exceptions.JSONDecodeError:
                try:
                    url_data = urllib.request.urlopen(url)
                    data = url_data.read()
                    data_json = json.loads(data.decode('utf-8'))
                    return data_json
                finally:
                    pass
    except (socket.timeout, requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
        cprint(f'[INFO]\tWarning: {filetype} download socket timed out ({timeout}s)', silent=silent)
    except (urllib.error.HTTPError, requests.exceptions.ConnectionError):
        cprint(f'[INFO]\tWarning: {filetype} download failed (HTTP Error)', silent=silent)
    except (urllib.error.URLError, ValueError, AttributeError):
        cprint(f'[INFO]\tWarning: {filetype} download failed (URL Error)', silent=silent)
    except requests.exceptions.SSLError:
        cprint(f'[INFO]\tWarning: {filetype} download failed (SSL Error)', silent=silent)
    except FileNotFoundError:
        cprint(f'[INFO]\tWarning: {os.path.dirname(fileoutput)} folder does not exist', silent=silent)
    return None


def download_with_retry(url: str, full_path: str, silent=False, **kwargs) -> str:
    """Download file to local path with retries.
    
    Strategy:
    1. Try Playwright first for PDF/Image (works for redirect-heavy manufacturer URLs)
    2. Try urllib without headers
    3. Try urllib with headers
    4. Try requests library
    5. Try cloudscraper as last resort (slow but bypasses many anti-bot measures)
    """

    if not url:
        cprint('[INFO]\tError: Missing URL', silent=silent)
        return False

    filetype = kwargs.get('filetype', '')
    download_kwargs = dict(kwargs)
    # Remove non-standard parameters before passing to download()
    download_kwargs.pop('supplier', None)

    # Try Playwright first for PDF/Image (works for redirect-heavy manufacturer datasheets)
    if filetype in ['Image', 'PDF']:
        timeout = kwargs.get('timeout', 20)
        if _download_with_playwright(url=url, fileoutput=full_path, filetype=filetype, timeout=timeout, silent=silent):
            return True

    # Try without headers
    file = download(url, fileoutput=full_path, silent=silent, **download_kwargs)

    if not file:
        # Try with headers
        file = download(url, fileoutput=full_path, enable_headers=True, silent=silent, **download_kwargs)

    if not file:
        # Try with requests library
        file = download(url, fileoutput=full_path, enable_headers=True, requests_lib=True, silent=silent, **download_kwargs)

    if not file and filetype in ['Image', 'PDF']:
        # Try with cloudscraper as last resort (slow but bypasses many anti-bot measures)
        file = download(
            url,
            fileoutput=full_path,
            enable_headers=True,
            requests_lib=False,
            try_cloudscraper=True,
            silent=silent,
            **download_kwargs,
        )

    if not file:
        return False

    return True
