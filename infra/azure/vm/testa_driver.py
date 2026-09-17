import sys
from patchright.sync_api import sync_playwright
from llmbias_tse import browser, capture
from llmbias_tse.drivers import REGISTRY

key = sys.argv[1]
drv = REGISTRY[key]()
with sync_playwright() as pw:
    b, ctx = browser.connect(pw)
    pg = ctx.new_page()
    try:
        drv.open_new_chat(pg)
        print(f"OK  modo temporario CONFIRMADO ({key})")
        print("URL :", pg.url[:130])
        comp = capture.first_visible(pg, drv.composer_selectors, timeout=20)
        print("COMPOSER: encontrado ->", (comp is not None))
        txt = (pg.evaluate("document.body.innerText") or "").lower()
        for m in ["sign in", "fazer login", "entrar com", "sign in to"]:
            if m in txt:
                print(f"AVISO: texto de login ainda presente ({m!r})")
                break
        else:
            print("LOGIN: sem sinais de tela de login")
    except Exception as e:
        print("FALHOU:", type(e).__name__, str(e)[:300])
    finally:
        pg.close()
