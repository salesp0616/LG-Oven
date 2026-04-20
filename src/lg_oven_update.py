
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TZ = ZoneInfo(os.getenv("TZ", "Asia/Seoul"))
SEARCH_URL = os.getenv("SEARCH_URL", "https://www.lg.com/us/search?q=oven&tab=product")
TOP_N = int(os.getenv("TOP_N", "5"))
SHEET_ID = os.getenv("SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
KNOB_OVERRIDES_JSON = os.getenv("KNOB_OVERRIDES_JSON", "").strip()

LIST_HEADERS = [
    "Date", "Rank", "Knob O/X", "Model", "P/N", "Price($)",
    "Promotion($)", "Promotion(%)", "Total($)", "WOW", "Note"
]
RAW_HEADERS = [
    "CapturedAt(KST)", "Rank", "Title", "P/N", "CurrentPrice",
    "OriginalPrice", "Promotion", "ProductURL"
]
LOG_HEADERS = ["Timestamp(KST)", "Level", "Message"]
CONTROL_HEADERS = ["Key", "Value"]
OVERRIDE_HEADERS = ["P/N", "Knob O/X"]
GUIDE_TEXT = [
    ["This workbook is maintained by GitHub Actions + Playwright."],
    ["List: weekly top 1~5 snapshot"],
    ["Raw_Last_Run: raw scrape output"],
    ["Run_Log: execution history"],
    ["Overrides: force knob value per model (O/X)"],
    ["Control: runtime config snapshot"],
]

@dataclass
class Product:
    rank: int
    title: str
    pn: str
    current_price: float
    original_price: float
    promotion: float
    url: str
    knob: str

    @property
    def promo_pct(self) -> float:
        if self.original_price <= 0:
            return 0.0
        return round(self.promotion / self.original_price, 2)

    @property
    def total(self) -> float:
        return round(self.current_price, 2)


def now_kst() -> datetime:
    return datetime.now(TZ)


def fmt_date_kst() -> str:
    return now_kst().strftime("%Y.%m.%d")


def fmt_ts_kst() -> str:
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{fmt_ts_kst()}] {msg}", flush=True)


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9][0-9,]*\.?[0-9]*)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.lg.com" + url
    return url


def parse_knob(title: str, pn: str, overrides: Dict[str, str]) -> str:
    if pn in overrides and overrides[pn] in {"O", "X"}:
        return overrides[pn]
    t = (title or "").lower()
    if "wall oven" in t or "combination wall oven" in t or "combo wall oven" in t:
        return "X"
    return "O"


def extract_product_urls(page, top_n: int) -> List[str]:
    # Try direct product-card style extraction first.
    js = """
    () => {
      const out = [];
      const seen = new Set();

      function push(u) {
        if (!u) return;
        const url = u.startsWith('http') ? u : ('https://www.lg.com' + u);
        if (seen.has(url)) return;
        if (!/\\/us\\//.test(url)) return;
        if (!/(oven|range|lg-)/i.test(url)) return;
        seen.add(url);
        out.push(url);
      }

      const containers = Array.from(document.querySelectorAll('a[href]'));
      for (const a of containers) {
        const href = a.getAttribute('href') || '';
        const txt = (a.innerText || '').trim();
        if (!href) continue;
        if (/\\/us\\//.test(href) && /(oven|range|wall oven|slide-in|freestanding|combination)/i.test(txt + ' ' + href)) {
          push(href);
        }
      }
      return out;
    }
    """
    urls = page.evaluate(js) or []
    final = []
    seen = set()
    for url in urls:
        nu = normalize_url(url)
        if nu in seen:
            continue
        seen.add(nu)
        final.append(nu)
        if len(final) >= max(top_n * 3, 10):
            break
    return final


def wait_stable(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)


def scrape_detail(page, url: str, rank: int, overrides: Dict[str, str]) -> Optional[Product]:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    wait_stable(page)

    title = page.locator("h1").first.text_content(timeout=5000) or ""
    title = re.sub(r"\s+", " ", title).strip()

    body_text = page.locator("body").inner_text(timeout=5000)
    pn = ""
    pn_match = re.search(r"\b([A-Z]{3,}[A-Z0-9]{4,})\b", body_text)
    if pn_match:
        pn = pn_match.group(1).strip()

    price_candidates = []
    texts = page.locator("body").inner_text(timeout=5000).splitlines()
    merged = " ".join(texts)

    # collect explicit monetary phrases
    for m in re.finditer(r"\$[0-9][0-9,]*(?:\.[0-9]{2})?", merged):
        price_candidates.append(parse_price(m.group(0)))

    price_candidates = [p for p in price_candidates if p is not None]
    current_price = 0.0
    original_price = 0.0
    promotion = 0.0

    # Prefer OFF pattern when present
    off_match = re.search(
        r"\$([0-9][0-9,]*(?:\.[0-9]{2})?)\s+OFF\s+\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
        merged,
        re.I,
    )
    if off_match:
        promotion = float(off_match.group(1).replace(",", ""))
        original_price = float(off_match.group(2).replace(",", ""))
        current_price = round(original_price - promotion, 2)

    # Fallback: choose smallest plausible sale and largest plausible original
    if current_price <= 0 and price_candidates:
        uniq = sorted(set(price_candidates))
        if len(uniq) >= 2:
            current_price = uniq[0]
            original_price = uniq[-1]
            promotion = round(max(original_price - current_price, 0), 2)
        else:
            current_price = uniq[0]
            original_price = uniq[0]
            promotion = 0.0

    if not title or not pn or current_price <= 0:
        return None

    knob = parse_knob(title, pn, overrides)

    return Product(
        rank=rank,
        title=title,
        pn=pn,
        current_price=current_price,
        original_price=original_price if original_price > 0 else current_price,
        promotion=promotion,
        url=url,
        knob=knob,
    )


def get_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON secret is empty.")
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID secret is empty.")
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def ensure_sheet(service, title: str, rows: int = 1000, cols: int = 20) -> None:
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if title in existing:
        return
    requests = [{
        "addSheet": {
            "properties": {
                "title": title,
                "gridProperties": {"rowCount": rows, "columnCount": cols}
            }
        }
    }]
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID, body={"requests": requests}
    ).execute()


def update_range(service, a1_range: str, values: List[List[str]]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=a1_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def append_range(service, a1_range: str, values: List[List[str]]) -> None:
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=a1_range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def get_values(service, a1_range: str) -> List[List[str]]:
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=a1_range
    ).execute()
    return res.get("values", [])


def ensure_workbook_structure(service) -> None:
    for title in ["List", "Raw_Last_Run", "Run_Log", "Control", "Overrides", "Guide"]:
        ensure_sheet(service, title)

    if not get_values(service, "List!A1:K1"):
        update_range(service, "List!A1:K1", [LIST_HEADERS])

    if not get_values(service, "Raw_Last_Run!A1:H1"):
        update_range(service, "Raw_Last_Run!A1:H1", [RAW_HEADERS])

    if not get_values(service, "Run_Log!A1:C1"):
        update_range(service, "Run_Log!A1:C1", [LOG_HEADERS])

    if not get_values(service, "Control!A1:B10"):
        update_range(
            service,
            "Control!A1:B4",
            [CONTROL_HEADERS, ["SEARCH_URL", SEARCH_URL], ["TOP_N", str(TOP_N)], ["TIMEZONE", str(TZ)]],
        )

    if not get_values(service, "Overrides!A1:B1"):
        update_range(service, "Overrides!A1:B1", [OVERRIDE_HEADERS])

    if not get_values(service, "Guide!A1:A10"):
        update_range(service, "Guide!A1:A6", GUIDE_TEXT)


def load_overrides(service) -> Dict[str, str]:
    result = {}
    rows = get_values(service, "Overrides!A2:B1000")
    for row in rows:
        if len(row) >= 2:
            pn = row[0].strip()
            knob = row[1].strip().upper()
            if pn and knob in {"O", "X"}:
                result[pn] = knob

    if KNOB_OVERRIDES_JSON:
        try:
            payload = json.loads(KNOB_OVERRIDES_JSON)
            if isinstance(payload, dict):
                for k, v in payload.items():
                    v = str(v).strip().upper()
                    if v in {"O", "X"}:
                        result[str(k).strip()] = v
        except Exception:
            pass
    return result


def fetch_previous_total_map(service) -> Dict[str, float]:
    rows = get_values(service, "List!A2:K2000")
    prev = {}
    for row in rows:
        if len(row) < 9:
            continue
        pn = row[4].strip()
        total = parse_price(str(row[8]))
        if pn and total is not None:
            prev[pn] = total
    return prev


def clear_raw(service) -> None:
    update_range(service, "Raw_Last_Run!A2:H1000", [[""]])


def write_raw(service, products: List[Product]) -> None:
    rows = []
    ts = fmt_ts_kst()
    for p in products:
        rows.append([
            ts, p.rank, p.title, p.pn,
            p.current_price, p.original_price, p.promotion, p.url
        ])
    update_range(service, "Raw_Last_Run!A2:H{}".format(len(rows) + 1), rows)


def append_log(service, level: str, message: str) -> None:
    append_range(service, "Run_Log!A:C", [[fmt_ts_kst(), level, message]])


def append_list(service, products: List[Product]) -> None:
    prev_map = fetch_previous_total_map(service)
    date_str = fmt_date_kst()
    rows = []
    for p in products:
        wow = ""
        note = ""
        prev = prev_map.get(p.pn)
        if prev and prev != 0:
            wow_val = round((p.total - prev) / prev, 4)
            wow = wow_val
            if wow_val > 0:
                note = "UP"
            elif wow_val < 0:
                note = "DOWN"
            else:
                note = "SAME"

        rows.append([
            date_str,
            p.rank,
            p.knob,
            p.title,
            p.pn,
            p.original_price,
            p.promotion,
            p.promo_pct,
            p.total,
            wow,
            note,
        ])
    append_range(service, "List!A:K", rows)


def scrape_products(overrides: Dict[str, str]) -> List[Product]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 2200},
        )
        page = context.new_page()
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        wait_stable(page)

        # Cookie dialog, if any.
        for selector in [
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
        ]:
            try:
                if page.locator(selector).count():
                    page.locator(selector).first.click(timeout=2000)
                    break
            except Exception:
                pass

        urls = extract_product_urls(page, TOP_N)
        products = []
        seen_pn = set()
        for url in urls:
            product = scrape_detail(page, url, len(products) + 1, overrides)
            if not product:
                continue
            if product.pn in seen_pn:
                continue
            seen_pn.add(product.pn)
            products.append(product)
            if len(products) >= TOP_N:
                break

        browser.close()

    if len(products) < TOP_N:
        raise RuntimeError(f"Top {TOP_N} scrape failed. Collected only {len(products)} products.")
    return products


def main() -> None:
    service = get_service()
    ensure_workbook_structure(service)
    overrides = load_overrides(service)

    try:
        products = scrape_products(overrides)
        clear_raw(service)
        write_raw(service, products)
        append_list(service, products)
        append_log(service, "SUCCESS", f"Captured {len(products)} products and appended to List.")
        log("SUCCESS")
    except Exception as e:
        append_log(service, "ERROR", str(e))
        log(f"ERROR: {e}")
        raise


if __name__ == "__main__":
    main()
