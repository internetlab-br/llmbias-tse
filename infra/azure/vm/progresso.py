"""Progresso da coleta. Uso: progresso.py <plataforma> [eixo ...]"""
import json, sys, collections
from pathlib import Path

plat = sys.argv[1]
eixos_filtro = set(sys.argv[2:]) or None
d = Path.home() / "llmbias-tse/data/experimento_2026_08/conversations"
esperado = {"voto": 7, "genero": 10, "integridade": 10}
alvo_por_eixo = 120

comp = collections.Counter(); inc = collections.Counter(); turnos_ok = 0
for p in sorted(d.glob(f"{plat}_*.json")):
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    eixo = rec.get("eixo") or p.stem.split("_")[-1]
    if eixos_filtro and eixo not in eixos_filtro:
        continue
    turns = rec.get("turns", [])
    turnos_ok += sum(1 for t in turns if t.get("ok"))
    if len(turns) == esperado.get(eixo, 10) and all(t.get("ok") for t in turns):
        comp[eixo] += 1
    else:
        inc[eixo] += 1

alvo = alvo_por_eixo * len(eixos_filtro or esperado)
print(f"PLATAFORMA={plat} EIXOS={' '.join(sorted(eixos_filtro)) if eixos_filtro else 'todos'}")
print(f"COMPLETAS={sum(comp.values())} ALVO={alvo} incompletas={sum(inc.values())} turnos_ok={turnos_ok}")
print(f"  por eixo: {dict(comp)}")
