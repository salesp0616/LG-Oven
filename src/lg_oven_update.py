import os
import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials

# 환경 변수
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SEARCH_URL = os.getenv("SEARCH_URL")

def get_top5():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SEARCH_URL, timeout=60000)

        page.wait_for_timeout(5000)

        products = page.locator('a[href*="/us/cooking-appliances"]')

        items = []

        count = products.count()
        print(f"Total products found: {count}")

        for i in range(min(count, 15)):
            try:
                el = products.nth(i)
                text = el.inner_text()

                if len(text) < 10:
                    continue

                price_el = el.locator("text=$")
                price = price_el.first.inner_text() if price_el.count() > 0 else ""

                items.append({
                    "model": text.strip(),
                    "price": price.strip()
                })

            except:
                continue

        browser.close()

        print(f"Parsed items: {len(items)}")
        print(items[:5])

        return items[:5]

def write_to_sheet(data):
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(SHEET_ID).worksheet("List")

    today = datetime.now().strftime("%Y.%m.%d")

    rows = []
    for i, item in enumerate(data, start=1):
        rows.append([
            today,
            i,
            "",
            item["model"],
            "",
            item["price"],
            "",
            "",
            "",
            ""
        ])

    if rows:
        sheet.append_rows(rows)
        print("Sheet updated")
    else:
        print("No data to write")

def main():
    data = get_top5()
    write_to_sheet(data)
    print("SUCCESS")

if __name__ == "__main__":
    main()
