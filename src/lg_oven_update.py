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

DATE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")

MODEL_MAP = {
    "LDGL6924S": "6.9 cu. ft. Smart Gas Double Oven Freestanding Range with ProBake Convection®, Air Fry & Air Sous Vide",
    "WSEP4723F": "4.7 cu. ft. Smart Wall Oven with Convection and Air Fry",
    "LSEL6330SE": "6.3 cu. ft. Electric Slide-in Range",
    "LREN6323YE": "6.3 cu. ft. Smart Wi-Fi Enabled ProBake Convection® Electric Range with Air Fry & EasyClean®",
    "WCEP6427F": "1.7/4.7 cu. ft. Smart Combination Wall Oven with InstaView®, True Convection, Air Fry, and Steam Sous Vide",
}

def dedupe_keep_order(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("$", "").replace("%", "").strip())
    except Exception:
        return None

def to_money_str(value):
    f = to_float(value)
    if f is None:
        return ""
    return f"{f:.2f}"

def calc_promo_pct(list_price, promo_amount):
    lp = to_float(list_price)
    pa = to_float(promo_amount)
    if lp is None or pa is None or lp <= 0:
        return ""
    return f"{(pa / lp) * 100:.2f}%"

def calc_wow(prev_total, curr_total):
    p = to_float(prev_total)
    c = to_float(curr_total)
    if p is None or c is None or p == 0:
        return ""
    return f"{((c - p) / p) * 100:.2f}%"

def infer_knob(model, pn):
    m = (model or "").lower()
    p = (pn or "").upper()

    if "wall oven" in m:
        return "X"
    if "range" in m:
        return "O"

    if p.startswith(("WSEP", "WCEP", "WDEP", "WCES", "WDES")):
        return "X"
    if p.startswith(("LDG", "LRE", "LSE", "LTE", "LSEL", "LRGL", "LREL")):
        return "O"

    return ""

def extract_pn_from_url(url):
    m = re.search(r"/lg-([a-z0-9\-]+)", url, re.I)
    if not m:
        return ""
    return m.group(1).split("-")[0].upper()

def extract_pdp_links(page):
    hrefs = page.locator("a[href]").evaluate_all(
        """els => els.map(e => e.getAttribute('href')).filter(Boolean)"""
    )

    links = []
    for href in hrefs:
        if href.startswith("/us/cooking-appliances/lg-"):
            links.append("https://www.lg.com" + href)
        elif href.startswith("https://www.lg.com/us/cooking-appliances/lg-"):
            links.append(href)

    links = dedupe_keep_order(links)

    filtered = []
    for url in links:
        pn = extract_pn_from_url(url)
        if pn in MODEL_MAP:
            filtered.append(url)

    return filtered

def extract_prices_from_text(text):
    # 목표:
    # Price($) = 정상가
    # Promotion($) = 할인액
    # Total($) = 할인 후 가격

    off_match = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", text, re.I)
    promo = to_money_str(off_match.group(1)) if off_match else ""

    prices = re.findall(r"\$([\d,]+(?:\.\d{2})?)", text)
    prices = [to_money_str(p) for p in prices]
    prices = [p for p in prices if p]

    current = ""
    list_price = ""

    if promo and len(prices) >= 2:
        # 보통 상세페이지 텍스트 구조: 현재가, 할인액, 정상가
        candidates = [to_float(p) for p in prices if to_float(p) is not None]
        promo_f = to_float(promo)

        valid_prices = [p for p in candidates if p != promo_f and p >= 100]
        if len(valid_prices) >= 2:
            current = to_money_str(min(valid_prices))
            list_price = to_money_str(max(valid_prices))
        elif len(valid_prices) == 1:
            current = to_money_str(valid_prices[0])
            list_price = to_money_str(valid_prices[0] + promo_f)
    else:
        valid_prices = [to_float(p) for p in prices if to_float(p) is not None and to_float(p) >= 100]
        if valid_prices:
            current = to_money_str(valid_prices[0])
            list_price = current
            promo = ""

    if not current or not list_price:
        return None

    return {
        "price": list_price,
        "promotion": promo,
        "promotion_pct": calc_promo_pct(list_price, promo),
        "total": current,
    }

def parse_product_page(page, url):
    pn = extract_pn_from_url(url)
    if pn not in MODEL_MAP:
        return None

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(2500)
        body_text = page.locator("body").inner_text(timeout=30000)
    except PlaywrightTimeoutError:
        return None
    except Exception:
        return None

    price_data = extract_prices_from_text(body_text)
    if not price_data:
        return None

    return {
        "url": url,
        "pn": pn,
        "model": MODEL_MAP[pn],
        "price": price_data["price"],
        "promotion": price_data["promotion"],
        "promotion_pct": price_data["promotion_pct"],
        "total": price_data["total"],
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
        print(f"[DEBUG] filtered PDP links found: {len(links)}")
        print(f"[DEBUG] links: {links}")

        results = []
        for url in links:
            item = parse_product_page(page, url)
            if item:
                results.append(item)
            if len(results) >= 5:
                break

        browser.close()

    if len(results) < 5:
        raise RuntimeError(f"Only {len(results)} valid LG rows parsed.")

    print(f"[DEBUG] parsed rows: {results}")
    return results

def get_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES,
    )
    return gspread.authorize(creds)

def get_existing_valid_rows(sheet):
    all_values = sheet.get_all_values()
    rows = []

    for idx, row in enumerate(all_values, start=1):
        if len(row) < 11:
            row += [""] * (11 - len(row))

        date_val = row[0].strip()
        rank_val = row[1].strip()

        if DATE_RE.match(date_val) and rank_val.isdigit():
            rows.append({
                "sheet_row": idx,
                "date": row[0],
                "rank": row[1],
                "knob": row[2],
                "model": row[3],
                "pn": row[4],
                "price": row[5],
                "promotion": row[6],
                "promotion_pct": row[7],
                "total": row[8],
                "wow": row[9],
                "note": row[10],
            })

    return rows

def delete_existing_today_rows(sheet, today):
    existing = get_existing_valid_rows(sheet)
    target_rows = [r["sheet_row"] for r in existing if r["date"] == today]
    for row_num in sorted(target_rows, reverse=True):
        sheet.delete_rows(row_num)

def get_previous_snapshot(sheet, today):
    existing = get_existing_valid_rows(sheet)
    dates = sorted({r["date"] for r in existing if r["date"] != today})
    if not dates:
        return []
    prev_date = dates[-1]
    prev_rows = [r for r in existing if r["date"] == prev_date]
    prev_rows.sort(key=lambda x: int(x["rank"]))
    return prev_rows

def build_note(prev_by_pn, pn, current_rank):
    prev = prev_by_pn.get(pn)
    if not prev:
        return ""
    prev_rank = int(prev["rank"])
    if prev_rank == current_rank:
        return "SAME"
    return "Ranking change"

def write_to_list(rows):
    client = get_client()
    sheet = client.open_by_key(SHEET_ID).worksheet("List")
    today = datetime.now().strftime("%Y.%m.%d")

    delete_existing_today_rows(sheet, today)

    prev_rows = get_previous_snapshot(sheet, today)
    prev_by_pn = {r["pn"]: r for r in prev_rows if r["pn"]}

    values = []
    for i, r in enumerate(rows, start=1):
        prev = prev_by_pn.get(r["pn"], {})
        wow = calc_wow(prev.get("total", ""), r["total"])
        note = build_note(prev_by_pn, r["pn"], i)

        values.append([
            today,
            i,
            infer_knob(r["model"], r["pn"]),
            r["model"],
            r["pn"],
            r["price"],
            r["promotion"],
            r["promotion_pct"],
            r["total"],
            wow,
            note,
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
