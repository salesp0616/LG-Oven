import os
import json
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

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

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("$", "").replace("%", "").strip())
    except Exception:
        return None

def money(value):
    f = to_float(value)
    return "" if f is None else f"{f:.2f}"

def calc_promo_pct(price, promo):
    p = to_float(price)
    g = to_float(promo)
    if p is None or g is None or p == 0:
        return ""
    return f"{(g / p) * 100:.2f}%"

def calc_wow_value(prev_total, curr_total):
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
        row = row + [""] * (11 - len(row))

        date_val = row[0].strip()
        rank_val = row[1].strip()

        if DATE_RE.match(date_val) and rank_val.isdigit():
            rows.append({
                "sheet_row": idx,
                "date": row[0].strip(),
                "rank": row[1].strip(),
                "knob": row[2].strip(),
                "model": row[3].strip(),
                "pn": row[4].strip(),
                "price": row[5].strip(),
                "promotion": row[6].strip(),
                "promotion_pct": row[7].strip(),
                "total": row[8].strip(),
                "wow": row[9].strip(),
                "note": row[10].strip(),
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

def build_note_value(prev_by_pn, pn, current_rank):
    prev = prev_by_pn.get(pn)
    if not prev:
        return ""

    prev_rank = int(prev["rank"])
    return "SAME" if prev_rank == current_rank else "Ranking change"

def clean_money(raw):
    if not raw:
        return ""
    return money(raw.replace(",", "").replace("$", "").strip())

def parse_plp_text(body_text):
    lines = [x.strip() for x in body_text.splitlines() if x.strip()]
    found = []

    for idx, line in enumerate(lines):
        pn = line.strip().upper()

        if pn not in MODEL_MAP:
            continue

        model = MODEL_MAP[pn]
        segment = lines[idx: idx + 30]
        segment_text = "\n".join(segment)

        prices = re.findall(r"\$([\d,]+(?:\.\d{2})?)", segment_text)
        prices = [clean_money(p) for p in prices]
        prices = [p for p in prices if p and to_float(p) is not None and to_float(p) >= 100]

        off_match = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", segment_text, re.I)
        promotion = clean_money(off_match.group(1)) if off_match else ""

        current_price = ""
        list_price = ""

        if prices:
            current_price = prices[0]

        if promotion and current_price:
            cp = to_float(current_price)
            pp = to_float(promotion)
            if cp is not None and pp is not None:
                list_price = f"{cp + pp:.2f}"

        if not list_price:
            if len(prices) >= 2:
                list_price = prices[1]
            elif current_price:
                list_price = current_price
                promotion = ""

        if not current_price or not list_price:
            continue

        found.append({
            "pn": pn,
            "model": model,
            "price": list_price,
            "promotion": promotion,
            "promotion_pct": calc_promo_pct(list_price, promotion),
            "total": current_price,
        })

    seen = set()
    out = []

    for item in found:
        if item["pn"] in seen:
            continue
        seen.add(item["pn"])
        out.append(item)

    return out[:5]

def scrape_lg():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(7000)

        body_text = page.locator("body").inner_text(timeout=30000)
        rows = parse_plp_text(body_text)

        browser.close()

    if len(rows) < 5:
        raise RuntimeError(f"Only {len(rows)} valid LG rows parsed.")

    return rows

def write_to_list(rows):
    client = get_client()
    sheet = client.open_by_key(SHEET_ID).worksheet("List")
    today = datetime.now().strftime("%Y.%m.%d")

    delete_existing_today_rows(sheet, today)

    existing = get_existing_valid_rows(sheet)
    next_row = max([r["sheet_row"] for r in existing], default=4) + 1

    prev_rows = get_previous_snapshot(sheet, today)
    prev_by_pn = {r["pn"]: r for r in prev_rows if r["pn"]}

    values = []

    for i, r in enumerate(rows, start=1):
        pn = r["pn"]
        prev = prev_by_pn.get(pn, {})

        wow = calc_wow_value(prev.get("total", ""), r["total"])
        note = build_note_value(prev_by_pn, pn, i)

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

    if len(values) != 5:
        raise RuntimeError(f"Expected 5 rows, got {len(values)} rows.")

    target_range = f"A{next_row}:K{next_row + len(values) - 1}"
    sheet.update(target_range, values, value_input_option="USER_ENTERED")

    print(f"[SUCCESS] rows written to {target_range}")

def main():
    rows = scrape_lg()
    write_to_list(rows)

if __name__ == "__main__":
    main()
