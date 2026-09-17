"""Progresso de uma plataforma, em texto. É o que o `roda.sh` consulta entre
lotes para decidir se houve avanço, e serve para conferir na mão.

    uv run python infra/local/progresso.py gemini --run-dir data/experimento_2026_09
"""

from __future__ import annotations

import argparse
import sys

from llmbias_tse import status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plataforma")
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--eixos", nargs="*", default=None)
    a = ap.parse_args(argv)

    pln = status.plano(a.run_dir)
    p = status.progresso(a.run_dir, a.plataforma, pln, eixos=a.eixos or None)
    ctl = status.controle(a.run_dir, a.plataforma)

    completas = p["completas"]
    alvo = p["alvo"]
    if a.eixos:
        completas = sum(p["por_eixo"].get(e, 0) for e in a.eixos)
        alvo = sum((pln.get("alvo_por_eixo") or {}).get(e, 0) for e in a.eixos)

    # A primeira linha é lida por grep no runner; manter o formato.
    print(f"COMPLETAS={completas} ALVO={alvo} "
          f"INCOMPLETAS={p['incompletas']} ESTADO={ctl['estado']}")
    print(f"  plataforma={a.plataforma} por_eixo={p['por_eixo']}")
    print(f"  ritmo={p['conversas_por_hora']}/h eta={p['eta_horas']}h "
          f"turnos_ok={p['turnos_ok']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
