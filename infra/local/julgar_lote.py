"""Lança e acompanha o julgamento em LOTE de uma rodada.

Três passos, cada um idempotente e com estado em disco
(`<run>/lotes_juiz.json`), para que a queda do terminal não custe um lote:

    lancar    prepara os itens, sorteia a amostra e submete
    estado    consulta cada lote nos provedores
    coletar   baixa os resultados e escreve as anotações

Desenho do painel (decisão do Julio): `flash` julga a base INTEIRA, `sonnet` e
`luna` julgam uma AMOSTRA — o suficiente para medir concordância sem pagar
três juízes em tudo. A amostra é estratificada por plataforma × eixo: uma
amostra aleatória simples poderia concentrar numa plataforma, e a concordância
entre juízes é justamente o que não pode depender de qual plataforma caiu nela.
Sorteio determinístico na semente, para ser refeito igual.

Os lotes são FATIADOS: 16 mil itens de ~28 mil chars não cabem num pedido só
em nenhum dos provedores.

    uv run python infra/local/julgar_lote.py lancar --eixos voto integridade --amostra 0.10
    uv run python infra/local/julgar_lote.py estado
    uv run python infra/local/julgar_lote.py coletar
"""

from __future__ import annotations

import argparse
import collections
import json
import random
from pathlib import Path

from llmbias_tse import judge, judge_batch, judges
from llmbias_tse.rubrics import get_rubric
from llmbias_tse.storage import turnos_limpos

# Itens por lote, POR PROVEDOR. O limite é por tamanho, não só por contagem:
# o prompt médio tem 28 mil chars, então 2.000 itens dão ~56 MB num pedido só
# — acima do que o lote inline do Google aceita. Conservador de propósito: um
# lote recusado no fim da fila custa a espera inteira.
FATIA = {"google": 400, "anthropic": 2000, "openai": 2000}
FATIA_PADRAO = 400


def _carregar(run: Path, eixos):
    rubricas = {e: get_rubric(e) for e in eixos}
    convs = []
    for f in sorted((run / "conversations").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d["eixo"] not in rubricas:
            continue
        ts = turnos_limpos(d)
        if ts and all(t.get("ok") for t in ts):
            convs.append({**d, "turns": ts})
    return convs, rubricas


def _amostra(itens, fracao: float, semente: int):
    """Amostra estratificada por plataforma × eixo, determinística."""
    por_estrato = collections.defaultdict(list)
    for it in itens:
        plat = it.conversation_id.rsplit("_", 2)[0]
        por_estrato[(plat, it.eixo)].append(it)
    escolhidos = []
    for chave in sorted(por_estrato):
        grupo = sorted(por_estrato[chave], key=lambda i: i.custom_id)
        k = max(1, round(len(grupo) * fracao))
        escolhidos += random.Random(f"{semente}:{chave}").sample(grupo, k)
    return sorted(escolhidos, key=lambda i: i.custom_id)


def lancar(args) -> int:
    run = Path(args.run_dir)
    convs, rubricas = _carregar(run, args.eixos)
    itens = judge_batch.preparar(convs, rubricas)
    amostra = _amostra(itens, args.amostra, args.semente)
    print(f"conversas: {len(convs)} · itens: {len(itens)} · "
          f"amostra ({args.amostra:.0%}): {len(amostra)}")
    estr = collections.Counter(
        (i.conversation_id.rsplit('_', 2)[0], i.eixo) for i in amostra)
    print("  amostra por plataforma × eixo:")
    for k in sorted(estr):
        print(f"    {k[0]:18s} {k[1]:12s} {estr[k]}")

    plano = {"eixos": list(args.eixos), "amostra": args.amostra,
             "semente": args.semente, "itens_total": len(itens),
             "itens_amostra": len(amostra), "lotes": []}
    alvo = run / "lotes_juiz.json"
    if alvo.exists() and not args.forcar:
        raise SystemExit(
            f"{alvo} já existe — os lotes desta rodada já foram lançados. "
            f"Use `estado`/`coletar`, ou --forcar para relançar (paga de novo)."
        )

    for key, conjunto in (("flash", itens), ("sonnet", amostra),
                          ("luna", amostra)):
        j = judges.JUIZES_POR_KEY[key]
        if not j.disponivel():
            print(f"  {key}: SEM CHAVE — pulado")
            continue
        tam = FATIA.get(j.provider, FATIA_PADRAO)
        for ini in range(0, len(conjunto), tam):
            pedaco = conjunto[ini:ini + tam]
            lote = judge_batch.submeter(j, pedaco)
            plano["lotes"].append({
                "juiz": key, "lote": lote, "de": ini,
                "n": len(pedaco),
                "custom_ids": [i.custom_id for i in pedaco],
            })
            print(f"  {key:8s} fatia {ini//tam + 1}: {len(pedaco)} itens "
                  f"-> {lote}")
            _salvar(alvo, plano)
    _salvar(alvo, plano)
    print(f"\n{len(plano['lotes'])} lotes lançados; estado em {alvo}")
    return 0


def _salvar(alvo: Path, plano: dict) -> None:
    tmp = alvo.with_suffix(".tmp")
    tmp.write_text(json.dumps(plano, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(alvo)


def estado(args) -> int:
    run = Path(args.run_dir)
    plano = json.loads((run / "lotes_juiz.json").read_text(encoding="utf-8"))
    por_juiz = collections.defaultdict(collections.Counter)
    for L in plano["lotes"]:
        j = judges.JUIZES_POR_KEY[L["juiz"]]
        try:
            st = judge_batch.estado(j, L["lote"])
        except Exception as e:  # noqa: BLE001
            st = f"erro: {type(e).__name__}"
        por_juiz[L["juiz"]][st] += 1
        L["estado"] = st
    for k in sorted(por_juiz):
        print(f"  {k:8s} {dict(por_juiz[k])}")
    _salvar(run / "lotes_juiz.json", plano)
    prontos = sum(1 for L in plano["lotes"] if L.get("estado") == "pronto")
    print(f"\nprontos: {prontos}/{len(plano['lotes'])}")
    return 0


def coletar(args) -> int:
    run = Path(args.run_dir)
    plano = json.loads((run / "lotes_juiz.json").read_text(encoding="utf-8"))
    # {conversa: {juiz: {turno: Extracao}}}
    porconv = collections.defaultdict(lambda: collections.defaultdict(dict))
    faltando = collections.Counter()
    for L in plano["lotes"]:
        j = judges.JUIZES_POR_KEY[L["juiz"]]
        try:
            res = judge_batch.coletar(j, L["lote"])
        except Exception as e:  # noqa: BLE001
            print(f"  {L['juiz']} {L['lote']}: erro ao coletar "
                  f"({type(e).__name__}: {str(e)[:120]})")
            continue
        for cid_turno, ex in res.items():
            cid, n = judge_batch.desmontar_id(cid_turno)
            porconv[cid][L["juiz"]][n] = ex
        falta = set(L["custom_ids"]) - set(res)
        if falta:
            faltando[L["juiz"]] += len(falta)
        print(f"  {L['juiz']:8s} {L['lote'][:34]:36s} {len(res)}/{L['n']}")
    if faltando:
        print(f"\nitens sem resultado: {dict(faltando)} "
              f"(ficam sem aquele juiz na anotação, declarado em "
              f"`juizes_com_falha`)")

    anot_dir = run / "annotations"
    anot_dir.mkdir(exist_ok=True)
    escritas = 0
    for cid, prontos in sorted(porconv.items()):
        f = run / "conversations" / f"{cid}.json"
        if not f.exists():
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        rubric = get_rubric(rec["eixo"])
        juizes = [judges.JUIZES_POR_KEY[k] for k in prontos]
        anot = judge.annotate_panel(rec, rubric, juizes, prontos=prontos)
        anot["conversation_id"] = cid
        anot["origem"] = "lote"
        (anot_dir / f"{cid}.json").write_text(
            json.dumps(anot, ensure_ascii=False, indent=2), encoding="utf-8")
        escritas += 1
    print(f"\n{escritas} anotações escritas em {anot_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("acao", choices=["lancar", "estado", "coletar"])
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--eixos", nargs="+", default=["voto", "integridade"])
    ap.add_argument("--amostra", type=float, default=0.10)
    ap.add_argument("--semente", type=int, default=2026)
    ap.add_argument("--forcar", action="store_true")
    args = ap.parse_args()
    return {"lancar": lancar, "estado": estado, "coletar": coletar}[args.acao](args)


if __name__ == "__main__":
    raise SystemExit(main())
