import os
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

URL = "https://www.lg.com/us/search?q=oven&tab=product"


def scrape():
    products = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )

        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(8000)

        cards = page.locator('a[href*="/us/"]')
        count = cards.count()

        for i in range(count):
            try:
                text = cards.nth(i).inner_text()

                # 🔥 필터 (핵심)
                if "Compare" in text:
                    continue
                if "$" not in text:
                    continue

                lines = [l.strip() for l in text.split("\n") if l.strip()]

                model = ""
                price = ""

                for line in lines:
                    if "$" in line:
                        price = line.replace("$", "").replace(",", "").strip()
                    elif len(line) > 10 and "cu." in line:
                        model = line

                if model and price:
                    products.append({
                        "model": model,
                        "price": price
                    })

            except:
                continue

        browser.close()

    return products[:5]


def write(data):
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet("List")

    today = datetime.now().strftime("%Y.%m.%d")

    rows = []

    for i, d in enumerate(data, 1):
        rows.append([
            today,     # Date
            i,         # Rank
            "",        # Knob O/X (유지)
            d["model"],
            "",        # P/N
            float(d["price"]),
            "", "", "", "", ""
        ])

    if rows:
        sheet.append_rows(rows, value_input_option="USER_ENTERED")


def main():
    data = scrape()
    write(data)


if __name__ == "__main__":
    main()
