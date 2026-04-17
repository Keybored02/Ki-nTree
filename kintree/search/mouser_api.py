import os
import re
import urllib.parse

from mouser.api import MouserPartSearchRequest

from ..config import config_interface
from ..config import settings

SEARCH_HEADERS = [
    "Description",
    "productCode",
    "MouserPartNumber",
    "Manufacturer",
    "ManufacturerPartNumber",
    "DataSheetUrl",
    "ProductDetailUrl",
    "ImagePath",
]
PARAMETERS_MAP = [
    "ProductAttributes",
    "AttributeName",
    "AttributeValue",
]

PRICING_MAP = [
    "PriceBreaks",
    "Quantity",
    "Price",
    "Currency",
]


def get_default_search_keys():
    return [
        "MouserPartNumber",
        "Manufacturer",
        "ManufacturerPartNumber",
        "ProductDetailUrl",
        "DataSheetUrl",
        "ImagePath",
    ]


def setup_environment(force=False):
    """Setup environmental variables"""

    api_key = os.environ.get("MOUSER_PART_API_KEY", None)
    if not api_key or force:
        mouser_api_settings = config_interface.load_file(settings.CONFIG_MOUSER_API)
        try:
            os.environ["MOUSER_PART_API_KEY"] = mouser_api_settings["MOUSER_PART_API_KEY"]
        except TypeError:
            pass


def find_categories(part_details: str):
    """Find categories"""

    try:
        return part_details["Category"], None
    except Exception:
        return None, None


def _extract_mouser_datasheet_url(product_url: str, timeout: int = 20, silent: bool = True) -> str:
    """Extract datasheet URL from Mouser product detail page when API omits DataSheetUrl."""

    from ..common.tools import cprint

    if not product_url:
        return ""

    def normalize_url(url: str, base: str) -> str:
        candidate = (url or "").strip()
        if not candidate:
            return ""
        return urllib.parse.urljoin(base, candidate)

    # Playwright (dynamic content)
    try:
        from playwright.sync_api import sync_playwright

        timeout_ms = max(5000, int(timeout * 1000))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                browser.close()
                page = None

            if page:
                selectors = [
                    "a#lnkDatasheet",
                    'a[data-testid*="datasheet"]',
                    'a[href*="DocumentDelivery"]',
                    'a[href*="/datasheet/"]',
                    'a[href*=".pdf"]',
                ]
                for selector in selectors:
                    try:
                        for link in page.locator(selector).all():
                            href = link.get_attribute("href")
                            full_url = normalize_url(href, page.url or product_url)
                            if not full_url:
                                continue
                            if any(
                                token in full_url.lower()
                                for token in [".pdf", "documentdelivery", "datasheet"]
                            ):
                                browser.close()
                                return full_url
                    except Exception:
                        continue
                # Fallback to whole-page HTML search
                try:
                    html = page.content()
                    match = re.search(
                        r'href=["\']([^"\']*(?:DocumentDelivery|datasheet|\.pdf)[^"\']*)["\']',
                        html,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        browser.close()
                        return normalize_url(match.group(1), page.url or product_url)
                except Exception:
                    import logging

                    logging.exception("Exception in datasheet candidate extraction:")
    except Exception:
        import logging

        logging.exception("Exception in get_mouser_datasheet_url:")

    # Static requests fallback
    try:
        import requests

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response = requests.get(product_url, headers=headers, timeout=timeout, allow_redirects=True)
        html = response.text or ""
        match = re.search(
            r'href=["\']([^"\']*(?:DocumentDelivery|datasheet|\.pdf)[^"\']*)["\']',
            html,
            flags=re.IGNORECASE,
        )
        if match:
            return normalize_url(match.group(1), response.url or product_url)
    except Exception:
        import logging

        logging.exception("Exception in static requests fallback for datasheet URL:")

    cprint(
        "[INFO]\tWarning: Mouser fallback could not find datasheet link on product page",
        silent=silent,
    )
    return ""


def fetch_part_info(part_number: str) -> dict:
    """Fetch part data from API"""

    from wrapt_timeout_decorator import timeout

    setup_environment()
    part_info = {}

    @timeout(dec_timeout=20)
    def search_timeout():
        try:
            request = MouserPartSearchRequest("partnumber")
            request.part_search(part_number)
        except FileNotFoundError as e:
            error_message = repr(e.args[0])
            error_message = error_message.strip("'")
            from ..common.tools import cprint

            cprint(f"[INFO] Warning: {error_message}", silent=False)
        finally:
            # Mouser 0.1.6 API update: single part list is returned, instead of dict
            pass

    # Query part number
    try:
        part: dict = search_timeout()
    except Exception:
        part = None

    if not part:
        return part_info

    # Check for empty response
    empty = True
    for _key, value in part.items():
        if value:
            empty = False
            break
    if empty:
        return part_info

    category, subcategory = find_categories(part)
    try:
        part_info["category"] = category
        part_info["subcategory"] = subcategory
    except Exception:
        part_info["category"] = ""
        part_info["subcategory"] = ""

    headers = SEARCH_HEADERS

    for key in part:
        if key in headers:
            part_info[key] = part[key]

    # Parameters
    part_info["parameters"] = {}
    [parameter_key, name_key, value_key] = PARAMETERS_MAP

    for parameter in range(len(part[parameter_key])):
        parameter_name = part[parameter_key][parameter][name_key]
        parameter_value = part[parameter_key][parameter][value_key]
        # Append to parameters dictionary
        part_info["parameters"][parameter_name] = parameter_value

    # Pricing
    part_info["pricing"] = {}
    [pricing_key, qty_key, price_key, currency_key] = PRICING_MAP

    for price_break in part[pricing_key]:
        quantity = price_break[qty_key]
        price = price_break[price_key]
        part_info["pricing"][quantity] = price

    if part[pricing_key]:
        part_info["currency"] = part[pricing_key][0][currency_key]
    else:
        part_info["currency"] = "USD"

    # Extra search fields
    if settings.CONFIG_MOUSER.get("EXTRA_FIELDS", None):
        for extra_field in settings.CONFIG_MOUSER["EXTRA_FIELDS"]:
            if part.get(extra_field, None):
                part_info["parameters"][extra_field] = part[extra_field]
            else:
                from ..common.tools import cprint

                cprint(
                    f'[INFO]\tWarning: Extra field "{extra_field}" not found in search results',
                    silent=False,
                )

    return part_info


def test_api() -> bool:
    """Test method for API"""

    test_success = True
    expected = {
        "Description": "MOSFETs P-channel 1.25W",
        "MouserPartNumber": "621-DMP2066LSN-7",
        "Manufacturer": "Diodes Incorporated",
        "ManufacturerPartNumber": "DMP2066LSN-7",
    }

    test_part = fetch_part_info("DMP2066LSN-7")

    if not test_part:
        # Unsucessful search
        test_success = False
    else:
        # Check content of response
        for key, value in expected.items():
            if test_part[key] != value:
                print(f'"{test_part[key]}" <> "{value}"')
                test_success = False
                break

    return test_success
