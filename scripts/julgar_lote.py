"""Julga a base em LOTE, com o painel de três juízes.

Fluxo, uma etapa por vez (o lote leva de minutos a horas, então nada aqui
bloqueia por padrão):

    preparar   monta os prompts e sorteia a amostra dos juízes secundários
    submeter   manda os lotes e guarda os ids
    status     pergunta aos provedores como está cada lote
    coletar    baixa os resultados de quem já terminou
    montar     agrega tudo em `anotacoes.jsonl`, pronto para a análise

Tudo é retomável: o estado vive em `<dir>/estado.json` e cada etapa só faz o
que falta. Os resultados brutos ficam em `resultados_<juiz>.jsonl`, então
recoletar não custa nada e remontar não depende de rede.

Desenho da amostra: o juiz principal julga 100% da base; os secundários julgam
uma fração, sorteada de forma ESTRATIFICADA por (plataforma, eixo) e
sistemática dentro do estrato (ordena por id e pega de k em k). Assim a amostra
de concordância tem a mesma composição da base e é reproduzível sem semente.

Uso típico:

    uv run python scripts/julgar_lote.py preparar \\
        --conversas ../llmbias-tse-reports/data/processed/conversas_corrigidas.jsonl \\
        --dir data/julgamento_2026_08 --modo conversa --amostra 0.2
    uv run python scripts/julgar_lote.py submeter --dir data/julgamento_2026_08 --confirmar
    uv run python scripts/julgar_lote.py status   --dir data/julgamento_2026_08
    uv run python scripts/julgar_lote.py coletar  --dir data/julgamento_2026_08
    uv run python scripts/julgar_lote.py montar   --dir data/julgamento_2026_08
"""
from __future__ import annotations

import argparse
import json
import time
import collections
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from llmbias_tse import judge, judge_batch as jb
from llmbias_tse.judges import JUIZES_LOTE, JUIZES_LOTE_POR_KEY
from llmbias_tse.rubrics import RUBRICS

# O juiz que roda em 100% da base; os demais entram só na amostra.
JUIZ_PRINCIPAL = "gemini"

# O lote do Gemini vai com as requisições EMBUTIDAS no corpo da chamada, e o
# corpo tem teto de tamanho. 1.679 prompts de ~42 mil chars dariam ~70 MB num
# pedido só; partimos em blocos e guardamos uma lista de ids. Anthropic e
# OpenAI recebem tudo de uma vez (upload de arquivo), então usam bloco único.
TAMANHO_BLOCO = {"google": 150}


# --------------------------------------------------------------------- estado
def _estado_path(d: Path) -> Path:
    return d / "estado.json"


def carregar_estado(d: Path) -> dict:
    p = _estado_path(d)
    return json.loads(p.read_text()) if p.exists() else {}


def salvar_estado(d: Path, est: dict) -> None:
    _estado_path(d).write_text(json.dumps(est, ensure_ascii=False, indent=2))


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ conversas
def ler_conversas(caminhos: list[Path]) -> list[dict]:
    out, vistos = [], set()
    for c in caminhos:
        linhas = c.read_text().splitlines() if c.suffix == ".jsonl" else []
        registros = ([json.loads(l) for l in linhas if l.strip()] if linhas
                     else [json.loads(c.read_text())])
        for r in registros:
            cid = r.get("conversation_id")
            if cid and cid not in vistos:
                vistos.add(cid)
                out.append(r)
    return out


def amostrar(conversas: list[dict], fracao: float) -> set[str]:
    """Amostra estratificada por (plataforma, eixo), sistemática no estrato."""
    if fracao >= 1.0:
        return {c["conversation_id"] for c in conversas}
    estratos: dict[tuple, list[str]] = defaultdict(list)
    for c in conversas:
        estratos[(c.get("platform"), c.get("eixo"))].append(c["conversation_id"])
    escolhidos: set[str] = set()
    passo = round(1 / fracao)
    for chave, ids in sorted(estratos.items()):
        ids.sort()
        # começa deslocado por estrato para não pegar sempre os mesmos perfis
        deslocamento = (hash(chave) % passo + passo) % passo
        escolhidos.update(ids[deslocamento::passo])
    return escolhidos


# ------------------------------------------------------------------- preparar
def cmd_preparar(a: argparse.Namespace) -> None:
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    conversas = ler_conversas([Path(x) for x in a.conversas])
    print(f"{len(conversas)} conversas lidas")

    if a.apenas:
        alvo_ids = {x.strip() for x in Path(a.apenas).read_text().split()
                    if x.strip() and x.strip() != "conversation_id"}
        conversas = [c for c in conversas if c["conversation_id"] in alvo_ids]
        print(f"  restrito a {len(conversas)} conversas de {a.apenas}")

    if a.amostra_de:
        # Reaproveita a amostra de uma rodada anterior. Numa REJULGA de
        # subconjunto, sortear de novo trocaria as conversas da amostra e a
        # concordância passaria a ser medida em outro conjunto.
        anterior = json.loads((Path(a.amostra_de) / "estado.json").read_text())
        amostra = set(anterior.get("amostra") or [])
        print(f"  amostra herdada de {a.amostra_de}: {len(amostra)} ids")
    else:
        amostra = amostrar(conversas, a.amostra)
    est = carregar_estado(d)
    est.update({
        "modo": a.modo,
        "fracao_amostra": a.amostra,
        "preparado_em": agora(),
        "fontes": [str(x) for x in a.conversas],
        "amostra": sorted(amostra),
    })
    est.setdefault("lotes", {})

    for juiz in JUIZES_LOTE:
        if a.juizes and juiz.key not in a.juizes:
            continue
        alvo = (conversas if juiz.key == JUIZ_PRINCIPAL
                else [c for c in conversas if c["conversation_id"] in amostra])
        itens = jb.preparar(alvo, RUBRICS, modo=a.modo)
        arq = d / f"itens_{juiz.key}.jsonl"
        with arq.open("w") as fh:
            for it in itens:
                fh.write(json.dumps({
                    "custom_id": it.custom_id,
                    "conversation_id": it.conversation_id,
                    "turno": it.turno,
                    "eixo": it.eixo,
                    "prompt": it.prompt,
                }, ensure_ascii=False) + "\n")
        chars = sum(len(it.prompt) for it in itens)
        est["lotes"].setdefault(juiz.key, {})
        est["lotes"][juiz.key].update({
            "modelo": juiz.model, "effort": juiz.effort,
            "n_itens": len(itens), "chars_prompt": chars,
            "conversas": len({it.conversation_id for it in itens}),
        })
        print(f"  {juiz.key:7s} {juiz.model:20s} {len(itens):5d} itens  "
              f"{chars/1e6:6.1f} M chars  -> {arq.name}")
    salvar_estado(d, est)
    print(f"\nestado em {_estado_path(d)}")


def _itens(d: Path, juiz_key: str) -> list[jb.ItemLote]:
    arq = d / f"itens_{juiz_key}.jsonl"
    return [
        jb.ItemLote(**json.loads(l)) for l in arq.read_text().splitlines()
        if l.strip()
    ]


# ------------------------------------------------------------------- submeter
def cmd_submeter(a: argparse.Namespace) -> None:
    d = Path(a.dir)
    est = carregar_estado(d)
    modo = est["modo"]
    if not a.confirmar:
        print("SEM --confirmar: nada foi enviado. Itens que iriam:")
        for k, v in est["lotes"].items():
            print(f"  {k:7s} {v['n_itens']:5d} itens ({v['modelo']})")
        return
    for juiz in JUIZES_LOTE:
        if a.juizes and juiz.key not in a.juizes:
            continue
        info = est["lotes"].get(juiz.key)
        if not info:
            continue
        if info.get("lote_id"):
            print(f"  {juiz.key}: já submetido ({info['lote_id']}), pulando")
            continue
        if not juiz.disponivel():
            print(f"  {juiz.key}: sem chave ({juiz.env_key}), pulando")
            continue
        itens = _itens(d, juiz.key)
        if a.limite:
            itens = itens[: a.limite]
        bloco = TAMANHO_BLOCO.get(juiz.provider, len(itens)) or len(itens)
        blocos = [itens[i:i + bloco] for i in range(0, len(itens), bloco)]
        print(f"  {juiz.key}: submetendo {len(itens)} itens em "
              f"{len(blocos)} lote(s)...", flush=True)
        ids = list(info.get("lote_ids") or [])
        for n, parte in enumerate(blocos, 1):
            if n <= len(ids):
                continue  # já foi; retomada não reenvia
            lote_id = jb.submeter(juiz, parte, modo=modo)
            ids.append(lote_id)
            info.update({"lote_ids": ids, "submetido_em": agora(),
                         "n_submetidos": sum(len(b) for b in blocos[:n])})
            salvar_estado(d, est)
            print(f"    [{n}/{len(blocos)}] {len(parte)} itens -> {lote_id}",
                  flush=True)


# ------------------------------------------------------------------- síncrono
def cmd_sincrono(a: argparse.Namespace) -> None:
    """Roda um juiz pela API normal, sem lote.

    Existe por causa da OpenAI: com a chave de projeto (`sk-proj-`) a Batch API
    aceita o upload do arquivo, marca-o como `processed`, e depois recusa o
    lote com "Cannot find file … or organization does not have access to it" —
    inclusive num lote de UMA linha trivial. É configuração da conta, não do
    payload. O modo síncrono usa o mesmo prompt e o mesmo schema; muda só o
    preço (não tem o desconto de 50%) e a latência.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from llmbias_tse import judges

    d = Path(a.dir)
    est = carregar_estado(d)
    modo = est["modo"]
    esquema = jb.esquema_do_modo(modo)
    juiz = JUIZES_LOTE_POR_KEY[a.juiz]
    itens = _itens(d, juiz.key)
    if a.limite:
        itens = itens[: a.limite]

    arq = d / f"resultados_{juiz.key}.jsonl"
    feitos = set()
    if arq.exists():  # retomável: não repete o que já voltou
        for l in arq.read_text().splitlines():
            if l.strip():
                feitos.add(json.loads(l)["custom_id"])
    pendentes = [it for it in itens if it.custom_id not in feitos]
    print(f"  {juiz.key}: {len(pendentes)} pendentes de {len(itens)} "
          f"({len(feitos)} já feitos)")

    def um(it):
        # 429 é regime normal aqui: o prompt tem ~10 mil tokens e o teto da
        # conta é 200 mil por minuto, então oito chamadas em paralelo já
        # esbarram. Recuar e tentar de novo sai mais barato que serializar.
        espera = 5.0
        for tentativa in range(6):
            try:
                return it.custom_id, judges.extrair(juiz, it.prompt, esquema)
            except Exception as e:  # noqa: BLE001
                if "rate_limit" not in str(e).lower() and "429" not in str(e):
                    raise
                if tentativa == 5:
                    raise
                time.sleep(espera)
                espera *= 1.8
        raise RuntimeError("inalcançável")

    ok = falhas = 0
    with arq.open("a") as fh, ThreadPoolExecutor(max_workers=a.paralelo) as ex:
        futuros = {ex.submit(um, it): it for it in pendentes}
        for fut in as_completed(futuros):
            it = futuros[fut]
            try:
                cid, ext = fut.result()
            except Exception as e:  # noqa: BLE001
                falhas += 1
                print(f"    {it.custom_id}: FALHOU ({type(e).__name__}: {e})")
                continue
            fh.write(json.dumps({"custom_id": cid,
                                 "extracao": ext.model_dump()},
                                ensure_ascii=False) + "\n")
            fh.flush()
            ok += 1
            if ok % 25 == 0:
                print(f"    {ok}/{len(pendentes)}", flush=True)
    info = est["lotes"].setdefault(juiz.key, {})
    info.update({"via": "sincrono", "n_colhidos": len(feitos) + ok,
                 "colhido_em": agora(), "falhas": falhas})
    salvar_estado(d, est)
    print(f"  {juiz.key}: {ok} novos, {falhas} falhas -> {arq.name}")


# --------------------------------------------------------------------- status
def cmd_status(a: argparse.Namespace) -> None:
    d = Path(a.dir)
    est = carregar_estado(d)
    for juiz in JUIZES_LOTE:
        info = est["lotes"].get(juiz.key) or {}
        ids = info.get("lote_ids") or []
        if not ids:
            print(f"  {juiz.key:7s} não submetido")
            continue
        estados = []
        for lote_id in ids:
            try:
                estados.append(jb.estado(juiz, lote_id))
            except Exception as e:  # noqa: BLE001
                estados.append(f"erro({type(e).__name__})")
        # o lote só está pronto quando TODOS os blocos estão
        if any(x == "em_andamento" for x in estados):
            st = "em_andamento"
        elif any(x.startswith("erro") for x in estados):
            st = "erro"
        else:
            st = "pronto"
        conta = collections.Counter(estados)
        colhidos = info.get("n_colhidos")
        extra = (f" | colhidos {colhidos}/{info.get('n_submetidos')}"
                 if colhidos else "")
        print(f"  {juiz.key:7s} {st:14s} {len(ids)} bloco(s) "
              f"{dict(conta)}{extra}")
        info["estado"] = st
        info["estado_blocos"] = estados
    salvar_estado(d, est)


# -------------------------------------------------------------------- coletar
def cmd_coletar(a: argparse.Namespace) -> None:
    d = Path(a.dir)
    est = carregar_estado(d)
    modo = est["modo"]
    for juiz in JUIZES_LOTE:
        if a.juizes and juiz.key not in a.juizes:
            continue
        info = est["lotes"].get(juiz.key) or {}
        ids = info.get("lote_ids") or []
        if not ids:
            continue
        res: dict = {}
        pendentes = 0
        for lote_id in ids:
            if jb.estado(juiz, lote_id) == "em_andamento":
                pendentes += 1
                continue
            res.update(jb.coletar(juiz, lote_id, modo=modo))
        if pendentes:
            print(f"  {juiz.key}: {pendentes} bloco(s) ainda em andamento "
                  f"(coletando os {len(ids) - pendentes} prontos)")
        if not res:
            continue
        arq = d / f"resultados_{juiz.key}.jsonl"
        with arq.open("w") as fh:
            for cid, ext in res.items():
                fh.write(json.dumps({"custom_id": cid,
                                     "extracao": ext.model_dump()},
                                    ensure_ascii=False) + "\n")
        info.update({"n_colhidos": len(res), "colhido_em": agora(),
                     "estado": "em_andamento" if pendentes else "pronto"})
        salvar_estado(d, est)
        falta = (info.get("n_submetidos") or 0) - len(res)
        aviso = f"  ({falta} sem resultado)" if falta > 0 else ""
        print(f"  {juiz.key}: {len(res)} resultados -> {arq.name}{aviso}")


# -------------------------------------------------------------------- mesclar
def cmd_mesclar(a: argparse.Namespace) -> None:
    """Traz os resultados de uma REJULGA para o diretório principal.

    Quando os artifacts de uma perna chegam depois, ou quando a recuperação
    melhora, um subconjunto de conversas precisa ser julgado de novo. Em vez de
    rejulgar a base inteira, a rodada nova cobre só o que mudou e os resultados
    são sobrepostos aqui, casando por `custom_id`.
    """
    destino, origem = Path(a.dir), Path(a.de)
    for juiz in JUIZES_LOTE:
        arq_o = origem / f"resultados_{juiz.key}.jsonl"
        if not arq_o.exists():
            continue
        novos = {}
        for linha in arq_o.read_text().splitlines():
            if linha.strip():
                reg = json.loads(linha)
                novos[reg["custom_id"]] = linha
        arq_d = destino / f"resultados_{juiz.key}.jsonl"
        antigos, substituidos = [], 0
        if arq_d.exists():
            for linha in arq_d.read_text().splitlines():
                if not linha.strip():
                    continue
                cid = json.loads(linha)["custom_id"]
                if cid in novos:
                    substituidos += 1
                    continue
                antigos.append(linha)
        with arq_d.open("w") as fh:
            for linha in antigos:
                fh.write(linha + "\n")
            for linha in novos.values():
                fh.write(linha + "\n")
        print(f"  {juiz.key}: {len(novos)} da rodada nova "
              f"({substituidos} substituídos), {len(antigos)} preservados, "
              f"total {len(antigos) + len(novos)}")
    # a base de conversas mudou junto: o `montar` tem de ler a versão nova
    est_d = carregar_estado(destino)
    est_o = carregar_estado(origem)
    est_d["fontes"] = est_o.get("fontes", est_d.get("fontes"))
    est_d["mesclado_de"] = str(origem)
    est_d["mesclado_em"] = agora()
    salvar_estado(destino, est_d)
    print(f"\nfontes atualizadas para {est_d['fontes']}")


# --------------------------------------------------------------------- montar
def cmd_montar(a: argparse.Namespace) -> None:
    d = Path(a.dir)
    est = carregar_estado(d)
    modo = est["modo"]
    if modo != "conversa":
        raise SystemExit("montar só implementado para o modo conversa")

    conversas = {c["conversation_id"]: c
                 for c in ler_conversas([Path(x) for x in est["fontes"]])}
    esquema = jb.esquema_do_modo(modo)

    por_juiz: dict[str, dict[str, dict]] = {}
    for juiz in JUIZES_LOTE:
        arq = d / f"resultados_{juiz.key}.jsonl"
        if not arq.exists():
            continue
        anot: dict[str, dict] = {}
        for linha in arq.read_text().splitlines():
            if not linha.strip():
                continue
            reg = json.loads(linha)
            cid = reg["custom_id"]
            rec = conversas.get(cid)
            if rec is None:
                continue
            rubric = RUBRICS.get(rec.get("eixo"))
            if rubric is None:
                continue
            _, avaliaveis = judge._format_conversa(rec.get("turns", []))
            ext = esquema.model_validate(reg["extracao"])
            anot[cid] = judge.agregar_conversa(ext, rubric, avaliaveis)
        por_juiz[juiz.key] = anot
        print(f"  {juiz.key}: {len(anot)} conversas anotadas")

    saida = d / "anotacoes.jsonl"
    n = 0
    with saida.open("w") as fh:
        for cid, rec in sorted(conversas.items()):
            anots = {k: v[cid] for k, v in por_juiz.items() if cid in v}
            if not anots:
                continue
            fh.write(json.dumps({
                "conversation_id": cid,
                "platform": rec.get("platform"),
                "eixo": rec.get("eixo"),
                "perfil_id": (rec.get("profile") or {}).get("perfil_id"),
                "conta": rec.get("conta"),
                "n_juizes": len(anots),
                "na_amostra": cid in set(est.get("amostra") or []),
                "por_juiz": anots,
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"\n{n} conversas -> {saida}")


# ----------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preparar")
    p.add_argument("--conversas", nargs="+", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--modo", default="conversa", choices=["conversa", "turno"])
    p.add_argument("--amostra", type=float, default=0.2)
    p.add_argument("--juizes", nargs="*", default=None)
    p.add_argument("--apenas", default=None,
                   help="arquivo com os conversation_id a julgar (um por linha)")
    p.add_argument("--amostra-de", dest="amostra_de", default=None,
                   help="diretório de uma rodada anterior, para herdar a amostra")
    p.set_defaults(fn=cmd_preparar)

    p = sub.add_parser("submeter")
    p.add_argument("--dir", required=True)
    p.add_argument("--juizes", nargs="*", default=None)
    p.add_argument("--limite", type=int, default=None,
                   help="manda só os N primeiros itens (teste de fumaça)")
    p.add_argument("--confirmar", action="store_true",
                   help="sem esta flag nada é enviado (o lote custa dinheiro)")
    p.set_defaults(fn=cmd_submeter)

    p = sub.add_parser("sincrono")
    p.add_argument("--dir", required=True)
    p.add_argument("--juiz", required=True)
    p.add_argument("--limite", type=int, default=None)
    p.add_argument("--paralelo", type=int, default=4)
    p.set_defaults(fn=cmd_sincrono)

    p = sub.add_parser("mesclar")
    p.add_argument("--dir", required=True, help="diretório principal (destino)")
    p.add_argument("--de", required=True, help="diretório da rejulga (origem)")
    p.set_defaults(fn=cmd_mesclar)

    for nome, fn in (("status", cmd_status), ("coletar", cmd_coletar),
                     ("montar", cmd_montar)):
        p = sub.add_parser(nome)
        p.add_argument("--dir", required=True)
        if nome != "status":
            p.add_argument("--juizes", nargs="*", default=None)
        p.set_defaults(fn=fn)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
