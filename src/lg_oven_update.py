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


def calc_wow_value(prev_total, curr_total):
    prev = to_float(prev_total)
    curr = to_float(curr_total)

    if prev is None or curr is None or prev == 0:
        return ""

    return f"{((curr - prev) / prev) * 100:.2f}%"


def build_note_value(prev_by_pn, pn, current_rank):
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
            calc_promo_pct(r["price"], r["promotion"]),
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
