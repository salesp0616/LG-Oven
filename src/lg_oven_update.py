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
        return f"{float(str(value).replace(',', '')):.2f}"
    except Exception:
        return ""


def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except Exception:
        return None


def calc_promo_pct(list_price, promo_amount):
    lp = to_float(list_price)
    pa = to_float(promo_amount)
    if lp is None or pa is None or lp == 0:
        return ""
    return f"{(pa / lp) * 100:.2f}%"


def calc_wow(prev_total, curr_total):
    p = to_float(prev_total)
    c = to_float(curr_total)
    if p is None or c is None or p == 0:
        return ""
    return f"{((c - p) / p) * 100:.2f}%"


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


def extract_product_name(page, pn: str) -> str:
    # 1순위: 상세페이지 h1
    try:
        h1 = page.locator("h1").first.inner_text(timeout=5000).strip()
        if h1 and len(h1) >= 10:
            return h1
    except Exception:
        pass

    # 2순위: 메타 타이틀
    try:
        title = page.locator('meta[property="og:title"]').first.get_attribute("content")
        if title:
            title = title.strip()
            if len(title) >= 10:
                return title
    except Exception:
        pass

    # 3순위: 페이지 본문에서 pn 포함 안 된 긴 문장 중 첫 번째
    try:
        body_text = page.locator("body").inner_text(timeout=10000)
        lines = [x.strip() for x in body_text.splitlines() if x.strip()]
        banned = {
            "compare",
            "learn more",
            "find a dealer",
            "add to cart",
            "shop now",
        }
        for line in lines:
            low = line.lower()
            if any(b in low for b in banned):
                continue
            if "$" in line:
                continue
            if pn and pn.upper() in line.upper():
                continue
            if len(line) >= 15:
                return line
    except Exception:
        pass

    return pn


def extract_prices(body_text: str, html: str):
    # 목표:
    # Price($) = 정상가(list/original)
    # Promotion($) = 할인액
    # Total($) = 할인 후가(current/sale)
    current_price = ""
    promo_amount = ""
    list_price = ""

    # 할인액
    off_match = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", body_text, re.I)
    if off_match:
        promo_amount = off_match.group(1).replace(",", "")

    # visible text 가격 후보
    visible_prices = re.findall(r"\$([\d,]+(?:\.\d{2})?)", body_text)
    visible_prices = [p.replace(",", "") for p in visible_prices]

    # html/json 가격 후보
    sale_price = ""
    current_price_json = ""
    generic_price = ""

    patterns = [
        (r'"salePrice"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?', "sale"),
        (r'"currentPrice"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?', "current"),
        (r'"price"\s*:\s*"?(?P<v>\d+(?:\.\d{2})?)"?', "price"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, html, re.I)
        if m:
            if kind == "sale":
                sale_price = m.group("v")
            elif kind == "current":
                current_price_json = m.group("v")
            elif kind == "price":
                generic_price = m.group("v")

    # 현재가 우선순위
    if sale_price:
        current_price = sale_price
    elif current_price_json:
        current_price = current_price_json
    elif visible_prices:
        current_price = visible_prices[0]
    elif generic_price:
        current_price = generic_price

    # 정상가 추정
    if promo_amount and current_price:
        try:
            list_price = f"{float(current_price) + float(promo_amount):.2f}"
        except Exception:
            list_price = ""

    # OFF 뒤에 정상가가 바로 표기되면 우선 사용
    off_list_match = re.search(r"OFF\s*\$([\d,]+(?:\.\d{2})?)", body_text, re.I)
    if off_list_match:
        list_price = off_list_match.group(1).replace(",", "")

    # 할인 없는 경우
    if current_price and not list_price:
        list_price = current_price
        promo_amount = ""

    return {
        "current_price": to_money_str(current_price),  # 할인 후가
        "promo_amount": to_money_str(promo_amount),    # 할인액
        "list_price": to_money_str(list_price),        # 정상가
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
    model = extract_product_name(page, pn)
    prices = extract_prices(body_text, html)

    if not pn or not prices["current_price"]:
        return None

    return {
        "url": url,
        "pn": pn,
        "model": model,
        "price": prices["list_price"],         # Price($) = 정상가
        "promotion": prices["promo_amount"],   # Promotion($) = 할인액
        "promotion_pct": calc_promo_pct(prices["list_price"], prices["promo_amount"]),
        "total": prices["current_price"],      # Total($) = 할인 후가
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


def get_existing_valid_rows(sheet):
    all_values = sheet.get_all_values()
    rows = []

    # 실제 시트 행번호를 유지하기 위해 enumerate 사용
    for idx, row in enumerate(all_values, start=1):
        if len(row) < 11:
            row += [""] * (11 - len(row))

        date_val = row[0].strip() if len(row) > 0 else ""
        rank_val = row[1].strip() if len(row) > 1 else ""

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
    target_row_numbers = [r["sheet_row"] for r in existing if r["date"] == today]

    # 아래에서부터 지워야 인덱스 안 꼬임
    for row_num in sorted(target_row_numbers, reverse=True):
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

    # 오늘 잘못 들어간 중복 데이터 방지
    delete_existing_today_rows(sheet, today)

    prev_rows = get_previous_snapshot(sheet, today)
    prev_by_pn = {r["pn"]: r for r in prev_rows if r["pn"]}

    values = []
    for i, r in enumerate(rows, start=1):
        prev = prev_by_pn.get(r["pn"], {})
        wow = calc_wow(prev.get("total", ""), r["total"])
        note = build_note(prev_by_pn, r["pn"], i)

        values.append([
            today,               # Date
            i,                   # Rank
            "",                  # Knob O/X
            r["model"],          # Model
            r["pn"],             # P/N
            r["price"],          # Price($)
            r["promotion"],      # Promotion($)
            r["promotion_pct"],  # Promotion(%)
            r["total"],          # Total($)
            wow,                 # WOW
            note,                # Note
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
