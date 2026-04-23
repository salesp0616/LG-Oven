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
BANNED_MODEL_TEXT = {
    "skip to main content",
    "compare",
    "learn more",
    "shop now",
    "find a dealer",
    "add to cart",
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


def infer_knob(model: str, pn: str) -> str:
    m = (model or "").lower()
    p = (pn or "").upper()

    # 1순위: 모델명 기준
    if "wall oven" in m:
        return "X"
    if "range" in m:
        return "O"

    # 2순위: P/N prefix 기준
    if p.startswith(("WSEP", "WCEP", "WDEP", "WCES", "WDES")):
        return "X"
    if p.startswith(("LDG", "LRE", "LSE", "LTE", "LSEL", "LRGL", "LREL")):
        return "O"

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


def iter_json_objects(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_json_objects(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_json_objects(item)


def extract_jsonld_products(html: str):
    scripts = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    )

    products = []
    for raw in scripts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        for obj in iter_json_objects(data):
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") == "Product":
                products.append(obj)
    return products


def choose_product_json(products, pn: str):
    if not products:
        return None

    # 1순위: sku/mpn/model이 pn과 일치
    for p in products:
        candidates = [
            str(p.get("sku", "")),
            str(p.get("mpn", "")),
            str(p.get("model", "")),
        ]
        if pn and any(c.upper() == pn.upper() for c in candidates if c):
            return p

    # 2순위: name에 pn이 포함
    for p in products:
        name = str(p.get("name", ""))
        if pn and pn.upper() in name.upper():
            return p

    # 3순위: 첫 번째 Product
    return products[0]


def extract_price_from_offers(offers):
    if isinstance(offers, dict):
        for key in ["price", "salePrice", "currentPrice", "lowPrice", "highPrice"]:
            if key in offers:
                v = to_money_str(offers.get(key))
                if v:
                    return v
    elif isinstance(offers, list):
        for off in offers:
            if isinstance(off, dict):
                for key in ["price", "salePrice", "currentPrice", "lowPrice", "highPrice"]:
                    if key in off:
                        v = to_money_str(off.get(key))
                        if v:
                            return v
    return ""


def extract_off_amount(body_text: str):
    m = re.search(r"\$([\d,]+(?:\.\d{2})?)\s+OFF", body_text, re.I)
    if m:
        return to_money_str(m.group(1))
    return ""


def extract_model_from_product_json(product, pn: str):
    name = str(product.get("name", "")).strip()
    if not name:
        return ""

    low = name.lower()
    if low in BANNED_MODEL_TEXT:
        return ""

    if len(name) < 10:
        return ""

    # pn만 덩그러니 있으면 모델명으로 인정 안 함
    if pn and name.upper() == pn.upper():
        return ""

    return name


def parse_product_page(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(2500)
    except PlaywrightTimeoutError:
        return None

    pn = extract_pn_from_url(url)
    html = page.content()
    body_text = page.locator("body").inner_text(timeout=30000)

    products = extract_jsonld_products(html)
    product = choose_product_json(products, pn)

    if not product:
        return None

    model = extract_model_from_product_json(product, pn)
    if not model:
        return None

    current_price = extract_price_from_offers(product.get("offers", {}))
    promo_amount = extract_off_amount(body_text)

    if not current_price:
        return None

    total_price = current_price  # 할인 후가
    list_price = ""

    if promo_amount:
        cp = to_float(current_price)
        pa = to_float(promo_amount)
        if cp is not None and pa is not None:
            list_price = f"{cp + pa:.2f}"

    if not list_price:
        list_price = current_price
        promo_amount = ""

    # 최종 검증
    lp = to_float(list_price)
    tp = to_float(total_price)
    if lp is None or tp is None:
        return None
    if lp < 100 or tp < 100:
        return None
    if lp < tp:
        # 혹시 뒤집혔으면 스왑
        list_price, total_price = total_price, list_price

    return {
        "url": url,
        "pn": pn,
        "model": model,
        "price": to_money_str(list_price),           # Price($) = 정상가
        "promotion": to_money_str(promo_amount),     # Promotion($) = 할인액
        "promotion_pct": calc_promo_pct(list_price, promo_amount),
        "total": to_money_str(total_price),          # Total($) = 할인 후가
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
        for url in links[:15]:
            item = parse_product_page(page, url)
            if not item:
                continue
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
            today,               # Date
            i,                   # Rank
            infer_knob(r["model"], r["pn"]),  # Knob O/X
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
