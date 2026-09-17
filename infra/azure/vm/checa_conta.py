import sys, re
from patchright.sync_api import sync_playwright
from llmbias_tse import browser

with sync_playwright() as pw:
    b, ctx = browser.connect(pw)
    pg = ctx.new_page()
    try:
        pg.goto("https://myaccount.google.com/", timeout=90000, wait_until="domcontentloaded")
        pg.wait_for_timeout(6000)
        txt = pg.evaluate("document.body.innerText") or ""
        mails = sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", txt)))
        if mails:
            for m in mails[:3]:
                u, d = m.split("@")
                print(f"CONTA: {u[:3]}{'*'*max(0,len(u)-3)}@{d}")
        else:
            print("CONTA: nao identificada; titulo =", pg.title()[:80])
    finally:
        pg.close()
