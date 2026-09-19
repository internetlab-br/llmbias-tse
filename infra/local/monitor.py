"""Panorama das 16 estações num só lugar, para acompanhar a rodada.

Lê o mesmo diretório que o painel, mas responde às perguntas que o painel não
responde de relance: quantas conversas já fecharam por sessão e por eixo,
quais estão INCOMPLETAS (e por quê), qual o núcleo pareado, e se alguma
estação está "rodando" sem runner.

    uv run python infra/local/monitor.py
    uv run python infra/local/monitor.py --run-dir data/experimento_2026_09
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from llmbias_tse import status


def _conversas(run_dir: Path):
    for f in sorted((run_dir / "conversations").glob("*.json")):
        try:
            yield json.load(f.open(encoding="utf-8"))
        except Exception:
            continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)

    sessoes = collections.Counter()
    eixos = collections.Counter()
    modelos = collections.Counter()
    contas = collections.Counter()
    ruins = []
    for d in _conversas(run_dir):
        ts = d.get("turns") or []
        ok = [t for t in ts if t.get("ok")]
        completa = bool(ts) and len(ok) == len(ts)
        chave = d.get("sessao") or d["platform"]
        if completa:
            sessoes[chave] += 1
            eixos[(d["platform"], d["eixo"])] += 1
            modelos[(d["platform"], d.get("modelo_exibido"))] += 1
            contas[(chave, d.get("conta"))] += 1
        else:
            motivo = d.get("error") or next(
                (t.get("error") for t in ts if not t.get("ok")), "sem turnos")
            ruins.append((d["conversation_id"], f"{len(ok)}/{len(ts)}",
                          str(motivo)[:90]))

    print("=== CONVERSAS COMPLETAS POR SESSÃO ===")
    for k, v in sorted(sessoes.items()):
        print(f"  {k:24s} {v}")
    print(f"  {'TOTAL':24s} {sum(sessoes.values())}")

    print("\n=== POR PLATAFORMA × EIXO ===")
    plats = sorted({k[0] for k in eixos})
    todos = sorted({k[1] for k in eixos})
    print(f"  {'plataforma':20s}" + "".join(f"{e:>14s}" for e in todos))
    for p in plats:
        print(f"  {p:20s}" + "".join(f"{eixos[(p, e)]:>14d}" for e in todos))

    print("\n=== MODELO QUE A UI EXIBIA ===")
    for k, v in sorted(modelos.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        print(f"  {k[0]:18s} {str(k[1]):28s} {v}")

    print("\n=== NÚCLEO PAREADO (perfis completos em TODAS as plataformas) ===")
    nuc = status.nucleo_pareado(run_dir, sorted(plats) or None)
    for eixo, d in nuc["por_eixo"].items():
        if not any(d["por_plataforma"].values()):
            continue
        print(f"  {eixo}: n={d['n']} · gargalo={d['gargalo']}")
        print("     " + "  ".join(f"{k}={v}"
                                  for k, v in sorted(d["por_plataforma"].items())))

    print(f"\n=== INCOMPLETAS ({len(ruins)}) — a retomada as refaz ===")
    for c, t, m in ruins[:20]:
        print(f"  {c:44s} {t:>6s} {m}")
    if len(ruins) > 20:
        print(f"  ... e outras {len(ruins) - 20}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
