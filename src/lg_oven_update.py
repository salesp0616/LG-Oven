import os
import json
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SEARCH_URL = "https://www.lg.com/us/search?q=oven&tab=product"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def dedupe_keep_order(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def to_money_str(value):
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}"
    except Exception:
        return ""


def calc_promo_pct(list_price, promo_amount):
    try:
        lp = float(list_price)
        pa = float(promo_amount)
        if lp <= 0:
            return ""
        return f"{(pa / lp) * 100:.2f}%"
    except Exception:
        return ""


def extract_pdp_links(page):
    hrefs = page.locator("a[href]").evaluate_all(
        """els => els.map(e => e.getAttribute('href')).filter(Boolean)"""
    )
    full = []
    for href in hrefs:
        if href.startswith("/us/cooking-appliances/lg-"):
            full.append("https://www.lg.com" + href)
        elif href.startswith("https://www.lg.com/us/cooking-appliances/lg-"):
            full.append(href)
    return dedupe_keep_order(full)


def extract_pn_from_url(url: str) -> str:
    m = re.search(r"/lg-([a-z0-9\-]+)", url, re.I)
    if not m:
        return ""
    slug = m.group(1)
    return slug.split("-")[0].upper()


def extract_product_name(body_text: str, pn: str) -> str:
    lines = [x.strip() for x in body_text.splitlines() if x.strip()]

    # P/N이 있는 줄은 제외
    clean_lines = []
    for line in lines:
        if pn and pn.upper() in line.upper():
            continue
        if "$" in line:
            continue
        if "OFF" in line.upper():
            continue
        if line.lower() in {
            "add to cart", "compare", "learn more", "find a dealer",
            "sold exclusively through authorized lg retail partners.",
        }:
            continue
        clean_lines.append(line)

    # 가장 긴 설명형 문장을 제품명으로 사용
    for line in clean_lines:
        if len(line) >= 15:
            return line

    return pn


def extract_prices(body_text: str, html: str):
    # visible text 기준
    current_price = ""
    promo_amount = ""
    list_price = ""

    # "$900.00 OFF" 패턴
    off_match = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", body_text, re.I)
    if off_match:
        promo_amount = off_match.group(1).replace(",", "")

    # 모든 가격 후보
    text_prices = re.findall(r"\$([\d,]+(?:\.\d{2})?)", body_text)
    text_prices = [p.replace(",", "") for p in text_prices]

    # JSON/HTML price fallback
    html_prices = re.findall(r'"price"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?', html, re.I)
    html_prices += re.findall(r'"salePrice"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?', html, re.I)
    html_prices += re.findall(r'"currentPrice"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?', html, re.I)

    candidates = text_prices + html_prices

    # 현재가 추정: 첫 번째 합리적 가격
    for price in candidates:
        try:
            if float(price) > 0:
                current_price = price
                break
        except Exception:
            pass

    # 정상가 추정
    if current_price and promo_amount:
        try:
            list_price = f"{float(current_price) + float(promo_amount):.2f}"
        except Exception:
            list_price = ""

    # OFF 뒤에 정상가가 직접 보이는 경우 우선
    off_list_match = re.search(r"OFF\s*\$([\d,]+(?:\.\d{2})?)", body_text, re.I)
    if off_list_match:
        list_price = off_list_match.group(1).replace(",", "")

    # 할인 없는 경우
    if current_price and not list_price:
        list_price = current_price
        promo_amount = ""

    return {
        "current_price": to_money_str(current_price),   # 할인 후가
        "promo_amount": to_money_str(promo_amount),     # 할인액
        "list_price": to_money_str(list_price),         # 정상가
    }


def parse_product_page(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)
    except PlaywrightTimeoutError:
        return None

    body_text = page.locator("body").inner_text(timeout=30000)
    html = page.content()

    pn = extract_pn_from_url(url)
    model = extract_product_name(body_text, pn)
    prices = extract_prices(body_text, html)

    if not pn or not prices["current_price"]:
        return None

    return {
        "url": url,
        "pn": pn,
        "model": model,
        # 시트 의미에 맞춤
        "price": prices["list_price"],         # Price($) = 정상가
        "promotion": prices["promo_amount"],   # Promotion($) = 할인액
        "total": prices["current_price"],      # Total($) = 할인 후가
        "promotion_pct": calc_promo_pct(prices["list_price"], prices["promo_amount"]),
    }


def scrape_top5():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)

        links = extract_pdp_links(page)
        print(f"[DEBUG] PDP links found: {len(links)}")

        results = []
        for url in links[:12]:
            item = parse_product_page(page, url)
            if not item:
                continue
            results.append(item)
            if len(results) >= 5:
                break

        browser.close()

    print(f"[DEBUG] parsed rows: {results}")
    return results


def get_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES,
    )
    return gspread.authorize(creds)


def write_to_list(rows):
    client = get_client()
    sheet = client.open_by_key(SHEET_ID).worksheet("List")
    today = datetime.now().strftime("%Y.%m.%d")

    values = []
    for i, r in enumerate(rows, start=1):
        values.append([
            today,               # Date
            i,                   # Rank
            "",                  # Knob O/X
            r["model"],          # Model
            r["pn"],             # P/N
            r["price"],          # Price($) = 정상가
            r["promotion"],      # Promotion($) = 할인액
            r["promotion_pct"],  # Promotion(%)
            r["total"],          # Total($) = 할인 후가
            "",                  # WOW
            "",                  # Note
        ])

    if not values:
        raise RuntimeError("No valid rows parsed from LG site.")

    sheet.append_rows(values, value_input_option="USER_ENTERED")
    print("[SUCCESS] rows appended to List")


def main():
    rows = scrape_top5()
    write_to_list(rows)


if __name__ == "__main__":
    main()
