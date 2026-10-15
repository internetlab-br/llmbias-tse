"""Estimativa de custo do LLM-as-a-judge, a partir do dado real da rodada.

Nada de regra de bolso: os prompts são montados pelo MESMO `_build_prompt` do
julgamento, os tokens de entrada e saída são os que cada API reportou numa
amostra, e os preços são os de lote (metade do síncrono).

    uv run python infra/local/custo_juiz.py --eixos voto integridade
    uv run python infra/local/custo_juiz.py --eixos genero --amostra 0.2
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from llmbias_tse import judge_batch
from llmbias_tse.rubrics import get_rubric
from llmbias_tse.storage import turnos_limpos

# Uso por TURNO, medido em 21/09/2026 com effort/pensamento BAIXO nos três
# (decisão do Julio). O input difere entre provedores porque o tokenizador
# difere — o Sonnet conta o dobro do Flash para o mesmo texto.
USO = {
    "flash":  {"in": 6475,  "out": 202},
    "sonnet": {"in": 13351, "out": 1182},
    "luna":   {"in": 7546,  "out": 799},
}
# Preço de LOTE em USD por milhão de tokens (metade do síncrono).
PRECO_LOTE = {
    "flash":  {"in": 0.375, "out": 1.875},   # gemini-3.7-flash  0.75/3.75
    "sonnet": {"in": 1.000, "out": 5.000},   # claude-sonnet-5   2/10
    "luna":   {"in": 0.100, "out": 0.600},   # gpt-5.6-luna      0.20/1.20
}


def turnos_por_eixo(run: Path, eixos) -> dict:
    rubricas = {e: get_rubric(e) for e in eixos}
    convs = []
    for f in sorted((run / "conversations").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d["eixo"] not in rubricas:
            continue
        ts = turnos_limpos(d)
        if ts and all(t.get("ok") for t in ts):
            convs.append({**d, "turns": ts})
    itens = judge_batch.preparar(convs, rubricas)
    por = collections.Counter(i.eixo for i in itens)
    convs_por = collections.Counter(c["eixo"] for c in convs)
    return {"itens": len(itens), "por_eixo": dict(por),
            "conversas": dict(convs_por)}


def custo(juiz: str, turnos: int) -> dict:
    u, p = USO[juiz], PRECO_LOTE[juiz]
    tin = turnos * u["in"] / 1e6
    tout = turnos * u["out"] / 1e6
    return {"turnos": turnos, "M_in": tin, "M_out": tout,
            "usd_in": tin * p["in"], "usd_out": tout * p["out"],
            "usd": tin * p["in"] + tout * p["out"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--eixos", nargs="+", default=["voto", "integridade"])
    ap.add_argument("--amostra", type=float, default=0.20,
                    help="fração da base julgada por sonnet e luna")
    args = ap.parse_args()
    d = turnos_por_eixo(Path(args.run_dir), args.eixos)
    n = d["itens"]
    n_am = round(n * args.amostra)
    print(f"eixos: {', '.join(args.eixos)}")
    print(f"  conversas completas: {d['conversas']}")
    print(f"  turnos a julgar: {n}")
    print(f"  amostra para sonnet/luna: {args.amostra:.0%} = {n_am} turnos\n")

    print(f"  {'juiz':8s} {'turnos':>7s} {'M in':>8s} {'M out':>7s} "
          f"{'$ in':>8s} {'$ out':>8s} {'$ total':>9s}")
    total = 0.0
    for juiz, turnos in (("flash", n), ("sonnet", n_am), ("luna", n_am)):
        c = custo(juiz, turnos)
        total += c["usd"]
        print(f"  {juiz:8s} {c['turnos']:7d} {c['M_in']:8.1f} {c['M_out']:7.2f} "
              f"{c['usd_in']:8.2f} {c['usd_out']:8.2f} {c['usd']:9.2f}")
    print(f"  {'TOTAL':8s} {'':7s} {'':8s} {'':7s} {'':8s} {'':8s} "
          f"{total:9.2f}")
    print(f"\n  por conversa julgada pelo painel: "
          f"US$ {total / max(1, sum(d['conversas'].values())):.3f}")
    print("  (preços de LOTE, metade do síncrono; o mesmo painel em modo "
          f"síncrono sairia por US$ {total * 2:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
