"""Recupera o fim das respostas truncadas a partir do HTML salvo do turno.

Em alguns turnos o texto gravado é um PREFIXO do que a página tinha: a
captura fechou antes de o balão terminar de crescer. O artefato guarda o DOM
do instante da captura, então o pedaço que falta está lá.

Decisão do time (21/09/2026): recuperar do artefato em vez de recoletar —
recoleta no WhatsApp é caríssima em tempo.

O registro BRUTO não é reescrito. A recuperação sai numa camada própria,
`recuperados.jsonl`, com o texto recuperado, quantos chars são, e de qual
arquivo vieram; `storage.turnos_limpos` a aplica na leitura, que é por onde o
juiz e a base enxergam. Assim a afirmação "este turno foi corrigido" tem
prova, e desfazer é apagar um arquivo.

    uv run python infra/local/recuperar_artefato.py --plataforma whatsapp_metaai
    uv run python infra/local/recuperar_artefato.py --plataforma whatsapp_metaai --aplicar
"""

from __future__ import annotations

import argparse
import html
import json
import re
import statistics
from pathlib import Path

FUNCAO = (r"(o|a|os|as|um|uma|uns|umas|de|do|da|dos|das|em|no|na|nos|nas|por|"
          r"pelo|pela|para|com|sem|que|se|e|ou|mas|como|ao|à|aos|às|é|são|"
          r"seu|sua|meu|minha|este|esta|esse|essa|aquele|mais|muito|já|não)")
RE_CORTE = re.compile(rf"\b{FUNCAO}\s*$", re.I)
# Onde a mensagem acaba e começa o cromo do WhatsApp Web. Sem este limite a
# recuperação arrastaria "WhatsApp Yesterday *Make decisions fun with polls*"
# para dentro da resposta do modelo.
RE_FIM = re.compile(
    r"wds-ic-|WhatsApp (Yesterday|Wednesday|Today|Monday|Tuesday|Thursday"
    r"|Friday|Saturday|Sunday)|Type a message|Digite uma mensagem"
    r"|\bSee details\b|\d{2}:\d{2}\s*$")
# Cauda usada para achar o ponto de junção. Curta demais casa em qualquer
# lugar; longa demais não casa por causa de espaço normalizado.
CAUDA = 45


def _texto_do_html(caminho: Path) -> str:
    bruto = caminho.read_text(encoding="utf-8", errors="ignore")
    return html.unescape(re.sub(r"<[^>]+>", "\n", bruto))


def recuperar(run: Path, cid: str, turno: int, gravado: str):
    h = run / "artifacts" / cid / f"turn_{turno:02d}" / "ok.html"
    if not h.exists():
        return None, "sem artefato"
    txt = _texto_do_html(h)
    cauda = gravado.rstrip()[-CAUDA:]
    i = txt.find(cauda)
    if i < 0:
        return None, "cauda não encontrada no HTML"
    resto = txt[i + len(cauda):]
    m = RE_FIM.search(resto)
    cont = resto[:m.start() if m else 500]
    cont = re.sub(r"\s+", " ", cont).strip()
    # Piso de 20 chars. Abaixo disso a "continuação" não é frase: num caso o
    # gravado terminava em "Essa" e o HTML dava "história 1" — o DOM também
    # estava cortado ali, e o "1" é marca de citação. Melhor deixar o turno
    # marcado como cortado do que emendar meia palavra.
    if len(cont) < 20:
        return None, "continuação curta demais para ser frase"
    return cont, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--plataforma", required=True)
    ap.add_argument("--aplicar", action="store_true",
                    help="escreve recuperados.jsonl")
    args = ap.parse_args()
    run = Path(args.run_dir)

    achados, falhas = [], {}
    for f in sorted((run / "conversations").glob(f"{args.plataforma}_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for t in (d.get("turns") or []):
            g = (t.get("response") or "").rstrip()
            if not (t.get("ok") and RE_CORTE.search(g)):
                continue
            cont, erro = recuperar(run, d["conversation_id"], t["turn"], g)
            if erro:
                falhas[erro] = falhas.get(erro, 0) + 1
                continue
            achados.append({
                "conversation_id": d["conversation_id"],
                "turno": t["turn"],
                "chars_gravados": len(g),
                "chars_recuperados": len(cont),
                "continuacao": cont,
                "fonte": str(Path("artifacts") / d["conversation_id"]
                             / f"turn_{t['turn']:02d}" / "ok.html"),
                "criterio": "texto gravado é prefixo do texto no HTML salvo",
            })

    print(f"=== {args.plataforma} ===")
    print(f"  recuperáveis: {len(achados)}")
    for k, v in sorted(falhas.items()):
        print(f"  {k}: {v}")
    if achados:
        v = [a["chars_recuperados"] for a in achados]
        print(f"  chars a recuperar: mediana {int(statistics.median(v))}, "
              f"total {sum(v)}, máx {max(v)}")
        print("\n  amostra:")
        for a in achados[:3]:
            print(f"    {a['conversation_id']} T{a['turno']}: "
                  f"+{a['chars_recuperados']} chars -> "
                  f"{a['continuacao'][:90]!r}")
    if not args.aplicar:
        print("\n(nada escrito; rode com --aplicar)")
        return 0
    alvo = run / "recuperados.jsonl"
    antigos = []
    if alvo.exists():
        antigos = [json.loads(l) for l in alvo.open(encoding="utf-8")]
        chaves = {(a["conversation_id"], a["turno"]) for a in achados}
        antigos = [a for a in antigos
                   if (a["conversation_id"], a["turno"]) not in chaves]
    with alvo.open("w", encoding="utf-8") as fh:
        for a in antigos + achados:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    print(f"\n{len(achados)} recuperações escritas em {alvo} "
          f"({len(antigos)} de antes mantidas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
