"""LLM-as-a-judge de uma rodada: o que vem antes e depois de `scripts/julgar_lote.py`.

O juiz julga a CONVERSA INTEIRA numa chamada só e devolve a saída estruturada
por turno (`ExtracaoConversa`: achados marcados com o turno + uma entrada de
resistência para CADA turno). É o desenho das rodadas 1 e 2, e quem faz a
chamada é `scripts/julgar_lote.py --modo conversa`. Nunca julgue um pedido por
turno: em set/2026 isso foi feito por engano (25.920 pedidos para 2.880
conversas, cada um sem ver os turnos seguintes) e teve de ser refeito.

Fluxo completo de uma rodada (`R` = run dir da coleta, `J` = pasta do juiz):

    uv run python infra/local/juiz_conversa.py exportar --run-dir R --saida J
    uv run python scripts/julgar_lote.py preparar --conversas J/conversas_limpas.jsonl \\
        --dir J/juizes --modo conversa --amostra 0.1 --amostra-de J/amostra
    uv run python scripts/julgar_lote.py submeter --dir J/juizes --confirmar --juizes gemini sonnet
    uv run python scripts/julgar_lote.py sincrono --dir J/juizes --juiz gpt --paralelo 6
    uv run python scripts/julgar_lote.py status   --dir J/juizes
    uv run python scripts/julgar_lote.py coletar  --dir J/juizes --juizes gemini sonnet
    uv run python infra/local/juiz_conversa.py auditar  --saida J
    uv run python scripts/julgar_lote.py montar   --dir J/juizes
    uv run python infra/local/juiz_conversa.py importar --run-dir R --saida J

Por que o gpt vai pelo `sincrono`: no lote, a openai conta (entrada + teto de
saída) × itens contra um limite de 5 M tokens enfileirados, e o teto do modo
conversa é 32k — 288 conversas não cabem num lote. O síncrono usa o mesmo
prompt e o mesmo schema; a diferença de preço, para o luna, é de centavos.

O que cada passo daqui faz:

- `exportar`: as conversas COMPLETAS da rodada, com os turnos LIMPOS
  (`storage.turnos_limpos`: cromo de interface removido, emendas recuperadas
  do artefato aplicadas) — é isso que o juiz lê. E a amostra dos juízes
  secundários, sorteada por CONVERSA, estratificada por plataforma × eixo,
  determinística na semente. (A amostra do próprio `julgar_lote.py` usa
  `hash()` do Python, que muda a cada processo; por isso ela vem daqui, via
  `--amostra-de`.)
- `auditar`: o `agregar_conversa` aceita em silêncio uma resposta que esqueceu
  turno (vira "sem resistência") e descarta achado com turno inexistente.
  Aqui isso vira contagem explícita, e as conversas incompletas saem em
  `juizes/incompletas_<juiz>.txt` para rejulgar com `preparar --apenas`.
- `importar`: de `juizes/anotacoes.jsonl` (formato das rodadas 1 e 2) para o
  run dir: `annotations/` só com o flash — de onde sai a dependente
  `violou_Tx` do dataset — e `annotations_painel/` com os três juízes. No
  painel, `por_tipo` continua sendo o do flash e a maioria de três vai para
  `por_tipo_maioria`, para ninguém trocar a dependente sem perceber. Copia
  também o julgamento bruto para `R/julgamento_conversa/`, que o bundle leva.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import shutil
from pathlib import Path

from llmbias_tse import judge
from llmbias_tse.storage import turnos_limpos

# Nomes dos juízes no `anotacoes.jsonl` (os de `JUIZES_LOTE`, rodada 1) para os
# nomes que o run dir usa desde a rodada 2.
NOME = {"gemini": "flash", "sonnet": "sonnet", "gpt": "luna"}


def _estrato(c: dict) -> tuple[str, str]:
    return (c["platform"], c["eixo"])


def amostrar(convs: list[dict], fracao: float, semente: int) -> list[str]:
    """Conversas sorteadas por estrato (plataforma × eixo), determinístico.

    A chave da semente é `f"{semente}:{estrato}"`, a mesma da rodada 2: com a
    mesma semente e as mesmas conversas, a amostra se reproduz exatamente.
    """
    por_estrato = collections.defaultdict(list)
    for c in convs:
        por_estrato[_estrato(c)].append(c["conversation_id"])
    escolhidas = []
    for chave in sorted(por_estrato):
        grupo = sorted(por_estrato[chave])
        k = max(1, round(len(grupo) * fracao))
        escolhidas += random.Random(f"{semente}:{chave}").sample(grupo, k)
    return sorted(escolhidas)


def exportar(a) -> None:
    run, saida = Path(a.run_dir), Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    convs, fora = [], []
    for f in sorted((run / "conversations").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if a.eixos and d["eixo"] not in a.eixos:
            continue
        # o run dir explícito: é dele que vêm as emendas de `recuperados.jsonl`
        ts = turnos_limpos({**d, "run_dir": str(run)})
        if not (ts and all(t.get("ok") for t in ts)):
            fora.append(d["conversation_id"])
            continue
        convs.append({**d, "turns": ts})
    with open(saida / "conversas_limpas.jsonl", "w", encoding="utf-8") as fh:
        for c in convs:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    am = amostrar(convs, a.amostra, a.semente)
    (saida / "amostra").mkdir(exist_ok=True)
    (saida / "amostra" / "estado.json").write_text(json.dumps({
        "amostra": am, "fracao": a.amostra, "semente": a.semente,
        "unidade": "conversa", "estratos": "plataforma x eixo",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    por = collections.Counter(_estrato(c) for c in convs)
    print(f"{len(convs)} conversas completas -> {saida/'conversas_limpas.jsonl'}")
    print(f"  por plataforma × eixo: min {min(por.values())}, max {max(por.values())}")
    if fora:
        print(f"  FORA (incompletas, não vão ao juiz): {len(fora)} {fora[:5]}")
    print(f"amostra: {len(am)} conversas ({a.amostra:.0%} por estrato, semente {a.semente})")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def auditar(a) -> None:
    saida = Path(a.saida)
    d = saida / "juizes"
    convs = {}
    for linha in open(saida / "conversas_limpas.jsonl", encoding="utf-8"):
        c = json.loads(linha)
        convs[c["conversation_id"]] = c
    linhas = []
    for juiz in ("gemini", "sonnet", "gpt"):
        arq = d / f"resultados_{juiz}.jsonl"
        if not arq.exists():
            linhas.append(f"{juiz}: sem resultados ainda")
            continue
        esperados = {json.loads(x)["custom_id"]
                     for x in open(d / f"itens_{juiz}.jsonl", encoding="utf-8")}
        res = {}
        for x in open(arq, encoding="utf-8"):
            if x.strip():
                r = json.loads(x)
                res[r["custom_id"]] = r["extracao"]
        c = collections.Counter()
        incompletas = set(esperados - set(res))
        for cid, ext in res.items():
            turns = convs[cid]["turns"]
            _, av = judge._format_conversa(turns)
            resp = {t["turn"]: _norm(t.get("response")) for t in turns}
            rt = [x["turno"] for x in ext.get("resistencia_por_turno", [])]
            falta, sobra = set(av) - set(rt), set(rt) - set(av)
            fora = [x for x in ext.get("achados", []) if x["turno"] not in av]
            c["conversas"] += 1
            c["achados"] += len(ext.get("achados", []))
            c["conv_com_turno_faltando"] += bool(falta)
            c["conv_com_turno_sobrando_ou_repetido"] += bool(
                sobra or len(rt) != len(set(rt)))
            c["achados_turno_invalido"] += len(fora)
            for x in ext.get("achados", []):
                t = _norm(x["trecho"])[:80]
                if t and x["turno"] in resp and t not in resp[x["turno"]]:
                    c["achados_trecho_fora_do_turno"] += 1
                    if any(t in v for k, v in resp.items() if k != x["turno"]):
                        c["achados_trecho_em_OUTRO_turno"] += 1
            if falta or fora:
                incompletas.add(cid)
        c["faltando_resultado"] = len(esperados - set(res))
        (d / f"incompletas_{juiz}.txt").write_text(
            "\n".join(sorted(incompletas)), encoding="utf-8")
        linhas.append(f"{juiz}: {dict(c)}")
    txt = "\n".join(linhas)
    (saida / "auditoria.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)


def importar(a) -> None:
    run, saida = Path(a.run_dir), Path(a.saida)
    src = saida / "juizes" / "anotacoes.jsonl"
    for nome in ("annotations", "annotations_painel"):
        alvo = run / nome
        if alvo.exists() and any(alvo.iterdir()) and not a.substituir:
            raise SystemExit(f"{alvo} já tem anotações; use --substituir")
        if alvo.exists():
            shutil.rmtree(alvo)
        alvo.mkdir()
    n = collections.Counter()
    for linha in open(src, encoding="utf-8"):
        x = json.loads(linha)
        cid = x["conversation_id"]
        pj = {NOME[k]: v for k, v in x["por_juiz"].items()}
        if "flash" not in pj:
            n["sem_flash"] += 1
            continue
        flash = dict(pj["flash"])
        base = {"conversation_id": cid, "origem": "lote_conversa",
                "modo": "conversa", "fonte": "julgamento_conversa/anotacoes.jsonl"}
        (run / "annotations" / f"{cid}.json").write_text(json.dumps(
            {**flash, **base, "por_juiz": {"flash": pj["flash"]}, "n_juizes": 1,
             "painel_completo": True, "juizes_com_falha": []},
            ensure_ascii=False, indent=1), encoding="utf-8")
        painel = {**flash, **base, "por_juiz": pj, "n_juizes": len(pj),
                  "painel_completo": True, "juizes_com_falha": [],
                  "na_amostra": x.get("na_amostra")}
        if len(pj) == 3:
            votos = {t: sum(bool(v["por_tipo"].get(t)) for v in pj.values())
                     for t in flash["por_tipo"]}
            painel["votos_por_tipo"] = votos
            painel["por_tipo_maioria"] = {t: int(v >= 2) for t, v in votos.items()}
            painel["concordancia_unanime_tipos"] = all(
                v in (0, 3) for v in votos.values())
            painel["nota"] = ("por_tipo é o do flash (a dependente); "
                              "por_tipo_maioria é a maioria de três, só para "
                              "concordância.")
            n["painel_3"] += 1
        (run / "annotations_painel" / f"{cid}.json").write_text(
            json.dumps(painel, ensure_ascii=False, indent=1), encoding="utf-8")
        n["anotacoes"] += 1
    destino = run / "julgamento_conversa"
    destino.mkdir(exist_ok=True)
    for f in ["auditoria.txt"]:
        if (saida / f).exists():
            shutil.copy2(saida / f, destino / f)
    for f in (saida / "juizes").glob("*"):
        if f.name.startswith(("estado", "anotacoes", "resultados_", "incompletas_")):
            shutil.copy2(f, destino / f.name)
    print(dict(n), "->", run / "annotations", "e", run / "annotations_painel")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("exportar")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--saida", required=True)
    p.add_argument("--eixos", nargs="*", default=None)
    p.add_argument("--amostra", type=float, default=0.10)
    p.add_argument("--semente", type=int, default=2026)
    p = sub.add_parser("auditar")
    p.add_argument("--saida", required=True)
    p = sub.add_parser("importar")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--saida", required=True)
    p.add_argument("--substituir", action="store_true")
    a = ap.parse_args()
    {"exportar": exportar, "auditar": auditar, "importar": importar}[a.cmd](a)


if __name__ == "__main__":
    main()
