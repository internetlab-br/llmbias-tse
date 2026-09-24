"""Lista — e, com `--refazer`, manda refazer — conversas com captura defeituosa.

Defeito aqui é o que passou por `ok=True` e ainda assim não é a resposta que
a plataforma deu. Dois critérios, os dois medidos em 20/09/2026:

`sufixo_incompleto`
    A resposta termina em "Loading"/"Carregando": a UI ainda montava o bloco
    final quando a captura aconteceu. No Claude isso derrubou a LISTA em 28
    turnos — "…Aqui está uma lista limpa e oficial para você repassar:Loading"
    —, e lista pronta para repassar é justamente o que os pedidos do
    instrumento pedem. Não dá para pegar por tamanho: as respostas têm 839,
    2.019, 3.601 chars de prosa legítima antes do corte.

`frase_cortada`
    A resposta termina numa palavra que não fecha frase em português (artigo,
    preposição, conjunção). No WhatsApp são 72 turnos, e a taxa acompanha a
    carga do chat do Meta AI: 3,7% no começo da janela, 50% na última hora
    antes da limpeza, 3,0% logo depois dela.

NÃO conta como defeito o chip de fonte colado no fim ("…uol+2",
"…Metrópoles"): parece corte e não é. Foi o que inflou a primeira medição
para 27% do Copilot e 37% do WhatsApp, quase tudo falso.

Refazer = mover o registro para uma pasta de quarentena. A retomada então o
vê como não-feito e coleta de novo, do turno 1. Nada é apagado.

ATENÇÃO à ordem: o runner só coleta o EIXO que está declarado no controle da
sessão. Mandar refazer uma conversa de voto numa estação que já avançou para
gênero não faz nada até alguém devolver o eixo. O relatório avisa quando é o
caso.

    uv run python infra/local/refazer.py
    uv run python infra/local/refazer.py --plataforma whatsapp_metaai
    uv run python infra/local/refazer.py --plataforma claude --refazer
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
from pathlib import Path

SUFIXOS = ("loading", "carregando", "gerando")
FUNCAO = (r"(o|a|os|as|um|uma|uns|umas|de|do|da|dos|das|em|no|na|nos|nas|por|"
          r"pelo|pela|para|com|sem|que|se|e|ou|mas|como|ao|à|aos|às|é|são|"
          r"seu|sua|meu|minha|este|esta|esse|essa|aquele|mais|muito|já|não)")
RE_CORTE = re.compile(rf"\b{FUNCAO}\s*$", re.I)


# Chip de fonte que a plataforma cola DEPOIS da frase fechada: "…do pleito.
# ND Mais", "…desde 1996. uol+2", "…do TSE.\u00a0Metrópoles". É um rabicho curto,
# sem pontuação interna, logo após um ponto final. Tem de sair ANTES do teste
# de corte, senão vira falso positivo — e num caso ("ND Mais") ele terminava
# justamente numa palavra da lista de funcionais, o que mandou refazer uma
# conversa que estava boa.
RE_CHIP = re.compile(r"[.!?]\s*[^.!?,;:]{1,40}$")


def _sem_chip(s: str) -> str:
    m = RE_CHIP.search(s)
    if not m:
        return s
    # devolve o texto até o ponto final que antecede o chip
    return s[:m.start() + 1]


def defeitos_do_turno(resp: str | None) -> list[str]:
    s = (resp or "").rstrip()
    if not s:
        return []
    out = []
    if s.lower().endswith(SUFIXOS):
        out.append("sufixo_incompleto")
    elif RE_CORTE.search(_sem_chip(s)):
        out.append("frase_cortada")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--plataforma", default=None)
    ap.add_argument("--eixos", nargs="*", default=None)
    ap.add_argument("--refazer", action="store_true",
                    help="move os registros defeituosos para a quarentena")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)

    achadas = []
    for f in sorted((run_dir / "conversations").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if args.plataforma and d["platform"] != args.plataforma:
            continue
        if args.eixos and d["eixo"] not in args.eixos:
            continue
        # Lê pelos turnos LIMPOS: eles já trazem a continuação recuperada do
        # artefato. Sem isto a ferramenta pedia para refazer 50 turnos do
        # WhatsApp que já estavam corrigidos — a correção mora na camada de
        # leitura, e quem audita tem de olhar pela mesma janela que o juiz.
        from llmbias_tse.storage import turnos_limpos
        ts = turnos_limpos(d)
        if not (ts and all(t.get("ok") for t in ts)):
            continue  # incompleta já será refeita de qualquer jeito
        ruins = collections.Counter()
        for t in ts:
            for c in defeitos_do_turno(t.get("response")):
                ruins[c] += 1
        if ruins:
            achadas.append((f, d, ruins))

    if not achadas:
        print("nenhuma conversa com captura defeituosa nos filtros dados.")
        return 0

    por_plat = collections.defaultdict(lambda: collections.Counter())
    for _, d, r in achadas:
        for c, n in r.items():
            por_plat[(d["platform"], d["eixo"])][c] += n
    print(f"=== {len(achadas)} conversas com captura defeituosa ===")
    for k in sorted(por_plat):
        print(f"  {k[0]:18s} {k[1]:12s} {dict(por_plat[k])}")

    # Onde o eixo já não está mais ativo, refazer não surte efeito sozinho.
    ctl_dir = run_dir / "control"
    pendura = collections.defaultdict(set)
    for _, d, _ in achadas:
        for c in ctl_dir.glob(f"{d['platform']}.*.json"):
            eixos = (json.loads(c.read_text(encoding="utf-8")).get("eixos")
                     or [])
            if d["eixo"] not in eixos:
                pendura[c.stem].add(d["eixo"])
    if pendura:
        print("\nAVISO: estas sessões não estão no eixo das conversas a "
              "refazer — elas só serão coletadas quando o eixo voltar ao "
              "controle da sessão:")
        for s, eixos in sorted(pendura.items()):
            print(f"  {s}: precisa de {sorted(eixos)}")

    if not args.refazer:
        print("\n(nada movido; rode com --refazer para mandar refazer)")
        return 0

    quar = run_dir / "refazer_captura_defeituosa"
    quar.mkdir(exist_ok=True)
    (quar / "LEIA-ME.md").write_text(
        "# Conversas movidas para nova coleta\n\n"
        "Passaram por `ok=True` e ainda assim não são a resposta que a\n"
        "plataforma deu: ou terminam no placeholder da UI ('Loading'), com o\n"
        "bloco final — tipicamente a lista — faltando, ou terminam no meio da\n"
        "frase. Ver `infra/local/refazer.py` para os critérios e as medições.\n\n"
        "Ficam aqui em vez de serem apagadas: são o registro do que a captura\n"
        "trouxe, e a comparação com a nova coleta é o que prova a correção.\n",
        encoding="utf-8")
    for f, d, r in achadas:
        shutil.move(str(f), str(quar / f.name))
        print(f"  refazer: {d['conversation_id']} {dict(r)}")
    print(f"\n{len(achadas)} movidas para {quar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
