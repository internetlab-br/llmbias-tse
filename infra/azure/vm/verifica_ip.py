import sys
from patchright.sync_api import sync_playwright
from llmbias_tse import browser

with sync_playwright() as pw:
    b, ctx = browser.connect(pw)
    pg = ctx.new_page()
    try:
        pg.goto("https://ipinfo.io/json", timeout=60000)
        txt = pg.evaluate("document.body.innerText")
        print("CHROME VE:", " ".join(txt.split())[:300])
    finally:
        pg.close()
