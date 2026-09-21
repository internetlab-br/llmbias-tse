"""Confere o texto GRAVADO contra o texto que está no HTML salvo do turno.

Responde a uma pergunta que só o artefato responde: a captura pegou tudo o
que a página tinha? Se o container da resposta no HTML tem mais texto do que
foi gravado, houve truncamento — e, ao contrário do "Loading" do Claude, um
truncamento que fecha frase não deixa rastro no texto.

Sem dependência nova: o parser é o `html.parser` da biblioteca padrão. Não
instalo nada no venv que a coleta está usando — `uv` toma o lock do ambiente
e já derrubou runner uma vez.

    uv run python infra/local/conferir_html.py --plataforma claude --amostra 60
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from html.parser import HTMLParser
from pathlib import Path

# Como achar o container da resposta no HTML de cada plataforma. Classe ou
# data-testid — o que for estável. Gemini e WhatsApp não têm um container de
# conteúdo separado do balão, então vão pelo balão mesmo.
ALVOS = {
    "claude": ("class", "font-claude-response"),
    "deepseek": ("class", "ds-markdown"),
    "google_aimode": ("class", "mZJni"),
    "chatgpt": ("class", "markdown"),
    "grok": ("class", "response-content-markdown"),
    "copilot": ("data-testid", "markdown-reply"),
    "gemini": ("class", "model-response-text"),
}
IGNORAR_TAGS = {"script", "style", "template", "noscript"}
# Elementos sem tag de fechamento. Sem tratá-los, a pilha de profundidade
# desanda e o extrator passa a juntar blocos que não são do mesmo container —
# foi o que fez ele "achar" a mensagem do usuário dentro da resposta.
VAZIAS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
          "meta", "param", "source", "track", "wbr"}


class Extrai(HTMLParser):
    """Texto de TODOS os elementos que casam com (atributo, valor)."""

    def __init__(self, atributo: str, valor: str):
        super().__init__(convert_charrefs=True)
        self.atributo, self.valor = atributo, valor
        self.profundidade = 0      # >0 = dentro de um alvo
        self.pilha: list[bool] = []
        self.ignorando = 0
        self.blocos: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in VAZIAS:
            return
        if tag in IGNORAR_TAGS:
            self.ignorando += 1
        d = dict(attrs)
        casa = self.valor in (d.get(self.atributo) or "")
        if casa and self.profundidade == 0:
            self.blocos.append([])
        self.pilha.append(casa)
        if casa:
            self.profundidade += 1

    def handle_startendtag(self, tag, attrs):
        return  # <br/>, <img/> — não abrem escopo

    def handle_endtag(self, tag):
        if tag in IGNORAR_TAGS and self.ignorando:
            self.ignorando -= 1
        if self.pilha:
            if self.pilha.pop():
                self.profundidade = max(0, self.profundidade - 1)

    def handle_data(self, data):
        if self.profundidade > 0 and not self.ignorando and self.blocos:
            self.blocos[-1].append(data)

    def ultimo(self) -> str:
        return "".join(self.blocos[-1]) if self.blocos else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--plataforma", required=True)
    ap.add_argument("--amostra", type=int, default=50)
    ap.add_argument("--semente", type=int, default=1)
    args = ap.parse_args()
    run = Path(args.run_dir)
    atributo, valor = ALVOS[args.plataforma]

    convs = sorted((run / "conversations").glob(f"{args.plataforma}_*.json"))
    random.Random(args.semente).shuffle(convs)
    faltas, comparados, sem_html = [], 0, 0
    perdas = []
    for f in convs:
        if comparados >= args.amostra:
            break
        d = json.loads(f.read_text(encoding="utf-8"))
        for t in (d.get("turns") or []):
            if not t.get("ok"):
                continue
            h = (run / "artifacts" / d["conversation_id"]
                 / f"turn_{t['turn']:02d}" / "ok.html")
            if not h.exists():
                sem_html += 1
                continue
            p = Extrai(atributo, valor)
            try:
                p.feed(h.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            no_html = _norm(p.ultimo())
            gravado = _norm(t.get("response"))
            if not no_html:
                continue
            comparados += 1
            # O gravado passa por `limpar_resposta`, então pode ser MENOR de
            # propósito (cromo removido). O que interessa é o contrário: HTML
            # com muito mais texto que o gravado.
            sobra = len(no_html) - len(gravado)
            if sobra > 200 and len(gravado) < 0.8 * len(no_html):
                perdas.append(sobra)
                if len(faltas) < 6:
                    faltas.append((d["conversation_id"], t["turn"],
                                   len(gravado), len(no_html),
                                   no_html[len(gravado):len(gravado) + 90]))
    print(f"=== {args.plataforma}: {comparados} turnos comparados "
          f"({sem_html} sem HTML) ===")
    if not perdas:
        print("  nenhum turno com texto sobrando no HTML — captura completa")
        return 0
    print(f"  turnos com texto SOBRANDO no HTML: {len(perdas)} "
          f"({100*len(perdas)/max(1,comparados):.1f}%)")
    print(f"  chars perdidos: mediana {int(statistics.median(perdas))}, "
          f"máx {max(perdas)}")
    for c in faltas:
        print(f"    {c[0]} T{c[1]}: gravado {c[2]} vs html {c[3]} — "
              f"faltou ...{c[4]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
