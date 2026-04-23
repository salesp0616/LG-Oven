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
    result = []

    with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(8000)

        items = page.locator('div[class*="product"]')
        count = items.count()

        for i in range(min(5, count)):
            try:
                txt = items.nth(i).inner_text()
                lines = txt.split("\n")

                model = lines[0] if len(lines) > 0 else ""
                price = next((l for l in lines if "$" in l), "")

                result.append({
                    "model": model,
                    "price": price.replace("$", "").replace(",", "")
                })
            except:
                pass

        browser.close()

    return result


def write(data):
    creds = Credentials.from_service_account_info(
    json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(SHEET_ID).worksheet("List")

    today = datetime.now().strftime("%Y.%m.%d")

    rows = []
    for i, d in enumerate(data, 1):
        rows.append([
            today,        # Date
            i,            # Rank
            "",           # Knob O/X
            d["model"],   # Model
            "",           # P/N
            d["price"],   # Price
            "",           # Promotion
            "",           # Promotion %
            "",           # Total
            "",           # WOW
            ""            # Note
        ])

    sheet.append_rows(rows)


def main():
    data = scrape()
    write(data)


if __name__ == "__main__":
    main()
