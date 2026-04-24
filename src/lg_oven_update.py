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


def calc_wow_formula(row_num):
    return (
        f'=IFERROR((I{row_num}-LOOKUP(2,1/(TRIM(TO_TEXT($E$5:E{row_num-1}))=TRIM(TO_TEXT(E{row_num}))),$I$5:I{row_num-1}))/'
        f'LOOKUP(2,1/(TRIM(TO_TEXT($E$5:E{row_num-1}))=TRIM(TO_TEXT(E{row_num}))),$I$5:I{row_num-1}),"")'
    )


def note_formula(row_num):
    return (
        f'=IFERROR(IF(B{row_num}=LOOKUP(2,1/(TRIM(TO_TEXT($E$5:E{row_num-1}))=TRIM(TO_TEXT(E{row_num}))),$B$5:B{row_num-1}),'
        f'"SAME","Ranking change"),"")'
    )


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
                "pn": row[4],
            })

    return rows


def delete_existing_today_rows(sheet, today):
    existing = get_existing_valid_rows(sheet)
    target_rows = [r["sheet_row"] for r in existing if r["date"] == today]

    for row_num in sorted(target_rows, reverse=True):
        sheet.delete_rows(row_num)


def clean_money(raw):
    if not raw:
        return ""
    raw = raw.replace(",", "").replace("$", "").strip()
    f = to_float(raw)
    if f is None:
        return ""
    return f"{f:.2f}"


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
        prices = [p for p in prices if p and to_float(p) and to_float(p) >= 100]

        off_match = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", segment_text, re.I)
        promotion = clean_money(off_match.group(1)) if off_match else ""

        current_price = ""
        list_price = ""

        # PLP 구조 기준:
        # 현재가가 먼저 나오고, OFF 옆에 정상가가 뒤따름
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
            "price": list_price,        # 정상가
            "promotion": promotion,     # 할인액
            "total": current_price,     # 할인 후 가격
        })

    # 중복 제거, 화면 순서 유지
    seen = set()
    out = []
    for item in found:
        if item["pn"] in seen:
            continue
        seen.add(item["pn"])
        out.append(item)

    return out[:5]


def scrape_top5():
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

    print(f"[DEBUG] parsed rows: {rows}")
    return rows


def write_to_list(rows):
    client = get_client()
    sheet = client.open_by_key(SHEET_ID).worksheet("List")
    today = datetime.now().strftime("%Y.%m.%d")

    delete_existing_today_rows(sheet, today)

    start_row = len(sheet.get_all_values()) + 1

    values = []
    for i, r in enumerate(rows, start=1):
        row_num = start_row + i - 1

        promotion_pct_formula = f'=IFERROR(G{row_num}/F{row_num},"")'
        total_formula = f'=IFERROR(F{row_num}-G{row_num},"")'

        values.append([
            today,
            i,
            infer_knob(r["model"], r["pn"]),
            r["model"],
            r["pn"],
            r["price"],
            r["promotion"],
            promotion_pct_formula,
            total_formula,
            calc_wow_formula(row_num),
            note_formula(row_num),
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
