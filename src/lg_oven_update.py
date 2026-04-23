
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

def clean_money(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\$([\d,]+(?:\.\d{2})?)", text)
    return m.group(1).replace(",", "") if m else ""

def dedupe_keep_order(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def extract_pdp_links(page):
    hrefs = page.locator("a[href]").evaluate_all(
        """els => els.map(e => e.getAttribute('href')).filter(Boolean)"""
    )
    full = []
    for href in hrefs:
        if not href:
            continue
        if href.startswith("/us/cooking-appliances/lg-"):
            full.append("https://www.lg.com" + href)
        elif href.startswith("https://www.lg.com/us/cooking-appliances/lg-"):
            full.append(href)
    links = dedupe_keep_order(full)
    return links

def parse_product_page(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3500)
    except PlaywrightTimeoutError:
        return None

    # Pull raw page text once, then parse.
    body_text = page.locator("body").inner_text(timeout=30000)

    # Model / P-N: prefer URL slug token after lg-
    pn = ""
    m = re.search(r"/lg-([a-z0-9\-]+)", url, re.I)
    if m:
        slug = m.group(1)
        # usually slug begins with the model/PN before the descriptive tail
        pn = slug.split("-")[0].upper()

    # Try to detect model from body text around the P/N if present
    model = ""
    if pn:
        patt = re.compile(rf"\b{re.escape(pn)}\b", re.I)
        if patt.search(body_text):
            model = pn

    # Price extraction: look for current price + optional off + original price
    current_price = ""
    promo_amount = ""
    total_price = ""

    # Strongest path: meta/product JSON often contains price
    html = page.content()
    price_candidates = []
    for pat in [
        r'"price"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?',
        r'"salePrice"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?',
        r'"currentPrice"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?',
    ]:
        for mm in re.finditer(pat, html, re.I):
            price_candidates.append(mm.group("v"))

    if price_candidates:
        current_price = price_candidates[0]

    # Visible text fallback
    if not current_price:
        m = re.search(r"\$([\d,]+(?:\.\d{2})?)", body_text)
        if m:
            current_price = m.group(1).replace(",", "")

    off = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", body_text, re.I)
    if off:
        promo_amount = off.group(1).replace(",", "")

    # original price often after OFF text
    if promo_amount:
        offs = re.search(r"OFF\s*\$([\d,]+(?:\.\d{2})?)", body_text, re.I)
        if offs:
            total_price = offs.group(1).replace(",", "")
    if not total_price and current_price and promo_amount:
        try:
            total_price = f"{float(current_price) + float(promo_amount):.2f}"
        except Exception:
            total_price = ""

    return {
        "url": url,
        "pn": pn,
        "model": model or pn,
        "price": current_price,
        "promotion": promo_amount,
        "total": total_price,
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
        for url in links[:10]:
            item = parse_product_page(page, url)
            if not item:
                continue
            if item["pn"] and item["price"]:
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
            today,            # Date
            i,                # Rank
            "",               # Knob O/X
            r["model"],       # Model
            r["pn"],          # P/N
            r["price"],       # Price($)
            r["promotion"],   # Promotion($)
            "",               # Promotion(%)
            r["total"],       # Total($)
            "",               # WOW
            "",               # Note
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
