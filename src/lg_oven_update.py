import os
import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

URL = "https://www.lg.com/us/search?q=oven&tab=product"

def scrape():
    data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, timeout=60000)
        page.wait_for_timeout(6000)

        items = page.locator('div[class*="product"]')
        count = items.count()

        print(f"[DEBUG] product count: {count}")

        for i in range(min(5, count)):
            try:
                txt = items.nth(i).inner_text()
                data.append(txt.strip())
            except:
                pass

        browser.close()

    if not data:
        data = ["NO DATA"]

    return data


def write(data):
    print(f"[DEBUG] SHEET_ID: {SHEET_ID}")
    print(f"[DEBUG] DATA: {data}")

    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    )
    client = gspread.authorize(creds)

    sheet = client.open_by_key(SHEET_ID).worksheet("List")

    today = datetime.now().strftime("%Y.%m.%d")

    rows = []
    for i, d in enumerate(data, 1):
        rows.append([today, i, "", d, "", "", "", "", "", ""])

    print(f"[DEBUG] ROWS: {rows}")

    sheet.append_rows(rows)

    print("[SUCCESS] WRITTEN TO SHEET")


def main():
    data = scrape()
    write(data)


if __name__ == "__main__":
    main()
