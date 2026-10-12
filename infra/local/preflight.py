"""Checagem de véspera de uma estação: login, modo sem memória, um turno.

Roda DENTRO do container, contra o Chrome da própria estação, e responde a
única pergunta que importa antes de soltar a coleta: *esta estação, com esta
conta, consegue abrir uma conversa isolada e capturar uma resposta de
verdade?*

Existe porque as três formas de essa resposta ser "não" são silenciosas:

1. a sessão expirou e a plataforma serve a tela de login — o driver acha o
   composer da landing page e captura texto de marketing;
2. a UI mudou e o seletor da resposta não casa mais — vira timeout, turno a
   turno, até o lote acabar;
3. o modo sem memória não ativou — a coleta roda e contamina a base sem
   nenhum erro (foi o que motivou os drivers momentâneos a LEVANTAR em vez de
   seguir).

O prompt é neutro e não pertence ao plano: isto é infraestrutura, não dado. E
como roda em conversa momentânea, não deixa rastro no histórico.

    docker exec coleta-gemini.c1 bash -lc 'cd /app && uv run python infra/local/preflight.py'
"""

from __future__ import annotations

import os
import sys
import time

from patchright.sync_api import sync_playwright

from llmbias_tse.conjoint_experiment import PLATFORM_DRIVERS
from llmbias_tse.drivers import REGISTRY

PROMPT = "Responda apenas com a palavra: pronto."
# Uma resposta boa é curta, mas não vazia. Abaixo disso é artefato de
# interface ("Thinking", "…") ou balão em branco, não resposta.
MIN_CHARS = 3
# Texto que só aparece em tela de login/consentimento. Se vier no lugar da
# resposta, a estação está deslogada — e a coleta gravaria isto como dado.
MARCAS_DESLOGADO = (
    "entrar", "sign in", "log in", "fazer login", "criar conta",
    "sign up", "continuar com o google", "continue with google",
)


def main() -> int:
    plataforma = os.environ.get("PLATAFORMA")
    if not plataforma:
        print("PLATAFORMA não definida no ambiente", file=sys.stderr)
        return 2
    sessao = os.environ.get("SESSAO") or plataforma
    conta = os.environ.get("LLMBIAS_CONTA") or "(não declarada)"
    chave, modo = PLATFORM_DRIVERS[plataforma]
    driver = REGISTRY[chave]()

    print(f"[preflight] {sessao} · driver={chave} · modo={modo} · conta={conta}")
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(
            f"http://localhost:{os.environ.get('LLMBIAS_CDP_PORT', '9333')}")
        ctx = b.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        t0 = time.time()
        driver.open_new_chat(page)
        print(f"[preflight] conversa isolada OK ({modo}) em {time.time()-t0:.1f}s"
              f" · url={page.url}")

        t0 = time.time()
        resp = driver.submit(page, PROMPT)
        dt = time.time() - t0

    limpo = (resp or "").strip()
    print(f"[preflight] resposta em {dt:.0f}s ({len(limpo)} chars): {limpo[:160]!r}")
    if len(limpo) < MIN_CHARS:
        print(f"[preflight] FALHA: resposta curta demais ({len(limpo)} chars)",
              file=sys.stderr)
        return 1
    baixo = limpo.lower()
    for m in MARCAS_DESLOGADO:
        if m in baixo:
            print(f"[preflight] FALHA: parece tela de login (casou {m!r})",
                  file=sys.stderr)
            return 1
    print(f"[preflight] OK {sessao}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
