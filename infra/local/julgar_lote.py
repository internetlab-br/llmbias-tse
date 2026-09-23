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

# Teto por lote, POR PROVEDOR, em itens E em tokens de entrada. Os dois
# limites existem e morderam de formas diferentes:
#
#  - TAMANHO DO PEDIDO: o prompt médio tem 28 mil chars, então 2.000 itens dão
#    ~56 MB num pedido só, acima do que o lote inline do Google aceita;
#  - TOKENS ENFILEIRADOS: a openai recusou o lote de 1.632 itens com
#    `token_limit_exceeded` — a organização aceita 5 milhões de tokens
#    enfileirados por modelo, e aquele lote tinha ~12,3 milhões. A recusa foi
#    limpa (0 completados, nada cobrado), mas custou a fila.
#
# Por isso o corte é pelo que vier primeiro. Os tetos de token ficam com folga
# sobre o limite conhecido, porque a conta por token é estimativa.
FATIA = {"google": 400, "anthropic": 2000, "openai": 2000}
FATIA_PADRAO = 400
TOKENS_MAX = {"openai": 4_000_000, "anthropic": 30_000_000,
              "google": 30_000_000}
TOKENS_MAX_PADRAO = 4_000_000
# Tokens de entrada por turno, medidos por provedor (ver `custo_juiz.py`).
TOKENS_POR_ITEM = {"flash": 6475, "sonnet": 13351, "luna": 7546}
# Teto de saída que a submissão declara. Na openai ele ENTRA na conta de
# tokens enfileirados, então precisa entrar no fatiamento também — foi o que
# faltou para as três fatias de 530 itens recusadas: 530 × (7.546 + 4.000) =
# 6,1 M contra um teto de 5 M. Tem de casar com `max_output_tokens` de
# `judge_batch._submeter_openai`.
RESERVA_SAIDA = {"openai": 2000}


def _fatiar(conjunto, juiz, key):
    """Fatias que respeitam o teto de itens E o de tokens enfileirados."""
    max_itens = FATIA.get(juiz.provider, FATIA_PADRAO)
    max_tok = TOKENS_MAX.get(juiz.provider, TOKENS_MAX_PADRAO)
    por_item = (TOKENS_POR_ITEM.get(key, 8000)
                + RESERVA_SAIDA.get(juiz.provider, 0))
    # 70% do teto: a conta por item é estimativa (o prompt varia de 17 mil a
    # 87 mil chars), e um lote recusado custa a fila inteira.
    por_tok = max(1, int(0.7 * max_tok) // por_item)
    tam = min(max_itens, por_tok)
    return [conjunto[i:i + tam] for i in range(0, len(conjunto), tam)], tam


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


def _amostra(itens, fracao: float, semente: int, unidade: str = "conversa"):
    """Amostra estratificada por plataforma × eixo, determinística.

    A UNIDADE é a conversa, não o turno, e isso não é detalhe de gosto.

    O juiz avalia turno a turno — é o desenho, e a rubrica se aplica a cada
    resposta. Mas sortear TURNOS faz um juiz ver a conversa inteira e o outro
    ver 1 ou 2 turnos dela (medido em 22/09/2026: 14% dos turnos). Duas coisas
    quebram com isso:

      - a concordância por conversa passa a comparar julgamento completo com
        parcial, e deixa de ser interpretável;
      - `por_tipo`, que é a dependente do dataset, valeria "flash OU sonnet"
        nas conversas sorteadas e "flash sozinho" nas outras — dois
        instrumentos na mesma coluna.

    Sorteando CONVERSAS, todos os juízes vêem os mesmos turnos das mesmas
    conversas, e o painel é comparável em qualquer nível. O custo em tokens é
    o mesmo: o que muda é como os turnos se distribuem entre conversas.
    """
    if unidade == "turno":
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

    # por CONVERSA: sorteia conversas dentro de cada estrato e leva TODOS os
    # turnos das escolhidas.
    convs_por_estrato = collections.defaultdict(set)
    itens_por_conv = collections.defaultdict(list)
    for it in itens:
        plat = it.conversation_id.rsplit("_", 2)[0]
        convs_por_estrato[(plat, it.eixo)].add(it.conversation_id)
        itens_por_conv[it.conversation_id].append(it)
    escolhidos = []
    for chave in sorted(convs_por_estrato):
        grupo = sorted(convs_por_estrato[chave])
        k = max(1, round(len(grupo) * fracao))
        for cid in random.Random(f"{semente}:{chave}").sample(grupo, k):
            escolhidos += itens_por_conv[cid]
    return sorted(escolhidos, key=lambda i: i.custom_id)


def lancar(args) -> int:
    run = Path(args.run_dir)
    convs, rubricas = _carregar(run, args.eixos)
    itens = judge_batch.preparar(convs, rubricas)
    amostra = _amostra(itens, args.amostra, args.semente, args.unidade)
    n_convs_am = len({i.conversation_id for i in amostra})
    print(f"conversas: {len(convs)} · itens: {len(itens)} · "
          f"amostra ({args.amostra:.0%} por {args.unidade}): "
          f"{len(amostra)} turnos de {n_convs_am} conversas")
    estr = collections.Counter(
        (i.conversation_id.rsplit('_', 2)[0], i.eixo) for i in amostra)
    print("  amostra por plataforma × eixo:")
    for k in sorted(estr):
        print(f"    {k[0]:18s} {k[1]:12s} {estr[k]}")

    plano = {"eixos": list(args.eixos), "amostra": args.amostra,
             "semente": args.semente, "itens_total": len(itens),
             "itens_amostra": len(amostra), "lotes": []}
    alvo = run / args.plano
    if alvo.exists():
        # Relançar só um juiz (`--juizes luna`) preserva o que já está no ar.
        # É o caso de um provedor ter recusado o lote: o resto da fila não
        # pode ser jogado fora nem pago de novo.
        anterior = json.loads(alvo.read_text(encoding="utf-8"))
        if args.juizes:
            plano["lotes"] = [L for L in anterior["lotes"]
                              if L["juiz"] not in args.juizes]
            print(f"  preservando {len(plano['lotes'])} lote(s) de outros "
                  f"juízes")
        elif not args.forcar:
            raise SystemExit(
                f"{alvo} já existe — os lotes desta rodada já foram lançados. "
                f"Use `estado`/`coletar`, `--juizes X` para relançar um só, "
                f"ou --forcar (paga tudo de novo)."
            )

    for key, conjunto in (("flash", itens), ("sonnet", amostra),
                          ("luna", amostra)):
        if args.juizes and key not in args.juizes:
            continue
        j = judges.JUIZES_POR_KEY[key]
        if not j.disponivel():
            print(f"  {key}: SEM CHAVE — pulado")
            continue
        pedacos, tam = _fatiar(conjunto, j, key)
        print(f"  {key}: {len(pedacos)} lote(s) de até {tam} itens "
              f"(~{tam * TOKENS_POR_ITEM.get(key, 8000) / 1e6:.1f} M tokens)"
              + (" · SEQUENCIAL" if args.sequencial else ""))
        for k, pedaco in enumerate(pedacos, 1):
            # Sequencial: espera o lote anterior sair da fila antes de
            # submeter o próximo. Necessário na openai, onde o limite de 5
            # milhões de tokens enfileirados vale para o TOTAL em voo, não
            # por lote — quatro lotes de 4 M cada foram recusados dois a dois.
            if args.sequencial and plano["lotes"]:
                ultimo = plano["lotes"][-1]
                if ultimo["juiz"] == key:
                    print(f"    aguardando a fatia {k-1} sair da fila...")
                    judge_batch.aguardar(j, ultimo["lote"], intervalo=60.0,
                                         teto_s=args.espera_max)
            lote = judge_batch.submeter(j, pedaco)
            plano["lotes"].append({
                "juiz": key, "lote": lote, "de": (k - 1) * tam,
                "n": len(pedaco),
                "custom_ids": [i.custom_id for i in pedaco],
            })
            print(f"  {key:8s} fatia {k}: {len(pedaco)} itens -> {lote}")
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
    plano = json.loads((run / args.plano).read_text(encoding="utf-8"))
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
    _salvar(run / args.plano, plano)
    prontos = sum(1 for L in plano["lotes"] if L.get("estado") == "pronto")
    print(f"\nprontos: {prontos}/{len(plano['lotes'])}")
    return 0


def coletar(args) -> int:
    run = Path(args.run_dir)
    plano = json.loads((run / args.plano).read_text(encoding="utf-8"))
    # {conversa: {juiz: {turno: Extracao}}}
    porconv = collections.defaultdict(lambda: collections.defaultdict(dict))
    faltando = collections.Counter()
    for L in plano["lotes"]:
        # `--juizes` na coleta serve para fechar a base com quem já terminou.
        # É o caso de um juiz de cobertura completa estar pronto e os de
        # amostra ainda na fila: a dependente já pode ser escrita, e a
        # concordância entra depois.
        if args.juizes and L["juiz"] not in args.juizes:
            continue
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

    anot_dir = run / args.saida
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
    ap.add_argument("--unidade", choices=["conversa", "turno"],
                    default="conversa",
                    help="unidade da amostra de sonnet/luna (ver `_amostra`)")
    ap.add_argument("--forcar", action="store_true")
    ap.add_argument("--plano", default="lotes_juiz.json",
                    help="arquivo (no run dir) com os lotes desta rodada de "
                         "julgamento; um por rodada, para lançar o gênero sem "
                         "tocar no plano de voto+integridade")
    ap.add_argument("--saida", default="annotations",
                    help="pasta (no run dir) onde `coletar` grava. O dataset "
                         "do relatório lê `annotations/` só com o flash; o "
                         "painel de três juízes vai para outra pasta, porque "
                         "nas conversas da amostra ele muda a dependente "
                         "(maioria de três em vez do flash sozinho)")
    ap.add_argument("--juizes", nargs="*", default=None,
                    help="relança só estes juízes, preservando os demais")
    ap.add_argument("--sequencial", action="store_true",
                    help="espera cada lote sair da fila antes do próximo "
                         "(obrigatório na openai: o teto de tokens "
                         "enfileirados vale para o total em voo)")
    ap.add_argument("--espera-max", type=float, default=14400.0)
    args = ap.parse_args()
    return {"lancar": lancar, "estado": estado, "coletar": coletar}[args.acao](args)


if __name__ == "__main__":
    raise SystemExit(main())
