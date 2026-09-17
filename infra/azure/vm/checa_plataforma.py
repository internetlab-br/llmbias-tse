import sys
from patchright.sync_api import sync_playwright
from llmbias_tse import browser

url = sys.argv[1]
with sync_playwright() as pw:
    b, ctx = browser.connect(pw)
    pg = ctx.new_page()
    try:
        pg.goto(url, timeout=90000, wait_until="domcontentloaded")
        pg.wait_for_timeout(6000)
        txt = (pg.evaluate("document.body.innerText") or "")[:400]
        print("URL FINAL:", pg.url[:140])
        print("TITULO   :", pg.title()[:120])
        low = txt.lower()
        marcas = [m for m in ["sign in","fazer login","entrar","unusual traffic",
                              "blocked","acesso negado","denied","captcha","verify"]
                  if m in low]
        print("SINAIS   :", marcas or "nenhum sinal de bloqueio/login")
        print("TEXTO    :", " ".join(txt.split())[:220])
    finally:
        pg.close()
