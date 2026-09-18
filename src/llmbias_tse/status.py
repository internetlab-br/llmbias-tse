"""Estado da coleta, lido do disco.

O painel não conversa com os coletores: ele lê o mesmo diretório de rodada
que eles escrevem (`data/<run_id>/`). Não há protocolo, porta, fila nem
banco entre os dois. Isso é possível porque todas as estações rodam no
mesmo host; foi justamente o que a mudança de VMs na Azure para containers
no host residencial comprou.

Fontes, todas append-only ou write-once:
  - `conversations/*.json` : o que já foi coletado (verdade sobre progresso)
  - `events.jsonl`         : o que aconteceu (rate limit, bloqueio, erro)
  - `plano_coleta.json`    : o que era para ser coletado (o alvo)
  - `control/<plat>.json`  : o que o painel mandou fazer (pausa, play)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import events

ESTADOS = ("rodando", "pausado", "precisa_humano", "parado")

# Turnos por eixo quando não há plano de coleta na rodada. É a especificação
# do instrumento (sete no ranqueamento, dez nos demais), repetida aqui para o
# painel conseguir contar antes de o plano existir.
TURNOS_PADRAO = {"voto": 7, "genero": 10, "integridade": 10}


def _ler_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def plano(run_dir) -> dict:
    """Alvo por plataforma e turnos esperados por conversa, tirados do plano.

    Sem plano (rodada ainda não planejada), devolve alvo zero e o painel
    mostra o progresso sem denominador, em vez de inventar um alvo.
    """
    d = _ler_json(Path(run_dir) / "plano_coleta.json") or {}
    conversas = d.get("conversas") or []
    turnos = {}
    perfis_eixo = set()
    for c in conversas:
        eixo = c.get("eixo")
        perfil = c.get("perfil_id")
        # O plano é por perfil x eixo e vale para TODAS as plataformas (mesmo
        # estímulo), então o alvo de uma plataforma é o plano deduplicado.
        chave = f"{perfil}_{eixo}"
        turnos[chave] = c.get("turnos") or TURNOS_PADRAO.get(eixo, 10)
        perfis_eixo.add((perfil, eixo))

    alvo_por_eixo = {}
    for _, eixo in perfis_eixo:
        if eixo:
            alvo_por_eixo[eixo] = alvo_por_eixo.get(eixo, 0) + 1

    return {
        "alvo": len(perfis_eixo),
        "turnos_esperados": turnos,
        "alvo_por_eixo": alvo_por_eixo,
        "eixos": d.get("eixos") or sorted(alvo_por_eixo),
    }


def partir_sessao(sessao: str) -> tuple[str, str | None]:
    """`"gemini.voto"` -> `("gemini", "voto")`; `"gemini"` -> `("gemini", None)`.

    A SESSÃO é a unidade de coleta da rodada 2: uma estação por (plataforma,
    eixo), 8x3 = 24, cada uma com sua conta, seu perfil de Chrome e sua tela
    remota. Separador é PONTO porque nome de plataforma tem underscore
    (`whatsapp_metaai`, `google_aimode`) e `whatsapp_metaai_voto` não se
    separa sem ambiguidade.

    Partir por eixo, e não rodar três contas sobre o mesmo eixo, é o que evita
    colisão: o resume indexa por `{plataforma}_{perfil}_{eixo}`, então duas
    sessões no mesmo eixo disputariam as mesmas conversas.
    """
    if "." in sessao:
        plat, suf = sessao.rsplit(".", 1)
        return plat, suf
    return sessao, None


_RE_CONTA = re.compile(r"^c(\d+)$")


def _perfis_do_run(run_dir) -> list[str]:
    """Ids dos perfis do run, do `profiles.json`. É a lista que o runner
    fatia, então painel e runner precisam ler a MESMA fonte."""
    d = _ler_json(Path(run_dir) / "profiles.json")
    if isinstance(d, dict):
        d = d.get("profiles") or d.get("perfis") or []
    return sorted(x.get("id") for x in (d or []) if isinstance(x, dict) and x.get("id"))


def fatia_da_sessao(sessao: str, n_fatias: int) -> tuple[int, int] | None:
    """`"gemini.c2"` com n=3 -> `(2, 3)`; sufixo que não é conta -> `None`.

    O sufixo `cN` marca a CONTA/fatia da sessão. Cada fatia roda os TRÊS eixos
    num terço dos perfis, para a conta variar dentro de cada eixo — se cada
    conta pegasse um eixo, a conta ficaria colada no eixo e os dois efeitos não
    se separariam."""
    _, suf = partir_sessao(sessao)
    m = _RE_CONTA.match(suf or "")
    return (int(m.group(1)), n_fatias) if m else None


def perfis_da_fatia(perfis: list[str], i: int, n: int) -> set[str]:
    """Mesma regra de `conjoint_experiment._fatiar_perfis`: ordena e reparte
    por resto. Precisa ser idêntica, senão o painel mede um alvo que o runner
    não vai cumprir."""
    return {p for k, p in enumerate(sorted(perfis)) if k % n == i - 1}


def progresso(run_dir, plataforma: str, pln=None, eixos=None,
              perfis=None) -> dict:
    """Conta conversas completas e incompletas de uma plataforma.

    `eixos` restringe a contagem (e o alvo) aos eixos dados — é o que faz uma
    sessão por eixo ver só o próprio trabalho.

    Completa = tem todos os turnos previstos e todos deram `ok`. É o mesmo
    critério do `_conv_done` do orquestrador, para o painel e a retomada
    nunca discordarem sobre o que já está feito.
    """
    run_dir = Path(run_dir)
    pln = pln if pln is not None else plano(run_dir)
    esperados = pln.get("turnos_esperados") or {}

    completas = {}
    incompletas = {}
    turnos_ok = 0
    fins = []
    conv_dir = run_dir / "conversations"
    if conv_dir.exists():
        for p in conv_dir.glob(plataforma + "_*.json"):
            rec = _ler_json(p)
            if not rec:
                continue
            eixo = rec.get("eixo") or p.stem.split("_")[-1]
            if eixos and eixo not in eixos:
                continue
            # o registro da conversa guarda o perfil em `profile.id`; as duas
            # outras formas existem em runs antigos
            perfil = (rec.get("perfil_id") or rec.get("profile_id")
                      or (rec.get("profile") or {}).get("id"))
            if perfis is not None and perfil not in perfis:
                continue  # conversa de outra fatia
            turns = rec.get("turns") or []
            turnos_ok += sum(1 for t in turns if t.get("ok"))
            alvo_turnos = (esperados.get(f"{perfil}_{eixo}")
                           or TURNOS_PADRAO.get(eixo, 10))
            if len(turns) == alvo_turnos and all(t.get("ok") for t in turns):
                completas[eixo] = completas.get(eixo, 0) + 1
                fim = _parse_ts(rec.get("finished_at"))
                if fim:
                    fins.append(fim)
            else:
                incompletas[eixo] = incompletas.get(eixo, 0) + 1

    total = sum(completas.values())
    if perfis is not None:
        # fatia: alvo = perfis desta fatia x eixos do plano. `is not None` e
        # não truthiness: fatia vazia (menos perfis que fatias) tem alvo ZERO,
        # e cair no alvo da plataforma inteira faria o painel mostrar a sessão
        # como atrasada para sempre.
        eixos_sessao = eixos or (pln.get("eixos") or [])
        alvo = len(perfis) * len(eixos_sessao)
    elif eixos:
        # o alvo da sessão é o do(s) eixo(s) dela, não o da plataforma inteira
        por_eixo = pln.get("alvo_por_eixo") or {}
        alvo = sum(por_eixo.get(e, 0) for e in eixos)
    else:
        alvo = pln.get("alvo") or 0

    # Ritmo pela janela recente, não pela média desde o início: uma plataforma
    # que rodou bem ontem e travou hoje tem de aparecer como travada.
    janela_h = 6
    corte = _agora() - timedelta(hours=janela_h)
    recentes = [f for f in fins if f >= corte]
    por_hora = round(len(recentes) / janela_h, 2) if recentes else 0.0
    restam = max(alvo - total, 0) if alvo else 0
    eta_h = round(restam / por_hora, 1) if por_hora and restam else None

    return {
        "completas": total,
        "alvo": alvo,
        "pct": round(100 * total / alvo, 1) if alvo else None,
        "incompletas": sum(incompletas.values()),
        "por_eixo": completas,
        "incompletas_por_eixo": incompletas,
        "turnos_ok": turnos_ok,
        "conversas_por_hora": por_hora,
        "eta_horas": eta_h,
        "ultima_conclusao": max(fins).isoformat() if fins else None,
    }


def controle(run_dir, plataforma: str) -> dict:
    d = _ler_json(Path(run_dir) / "control" / f"{plataforma}.json") or {}
    estado = d.get("estado") or "rodando"
    return {
        "estado": estado if estado in ESTADOS else "rodando",
        "motivo": d.get("motivo"),
        "em": d.get("em"),
        # Eixos DECLARADOS pelo runner (ver `definir_controle`). Sem repassar
        # aqui, o alvo da sessão sai calculado com os eixos do plano inteiro.
        "eixos": d.get("eixos") or None,
    }


def definir_controle(run_dir, plataforma: str, estado: str,
                     motivo=None, eixos=None) -> dict:
    """Escreve o estado desejado da sessão. `eixos` é DECLARADO pelo runner.

    Por que os eixos vivem aqui: a sessão pode rodar um subconjunto dos eixos
    do plano (fase 2 roda `voto integridade` em 16 sessões e depois `genero`
    em 8). Sem isso o painel calcularia o alvo com os TRÊS eixos do plano e
    mostraria toda sessão como eternamente atrasada. O runner é quem sabe, e
    declara; as ações do painel (play/pausar) NÃO apagam o que ele declarou.
    """
    if estado not in ESTADOS:
        raise ValueError(f"estado inválido: {estado!r} (use {ESTADOS})")
    d = Path(run_dir) / "control"
    d.mkdir(parents=True, exist_ok=True)
    anterior = _ler_json(d / f"{plataforma}.json") or {}
    reg = {"estado": estado, "motivo": motivo, "em": _agora().isoformat()}
    eixos = eixos if eixos is not None else anterior.get("eixos")
    if eixos:
        reg["eixos"] = list(eixos)
    # Escrita atômica: o runner lê este arquivo entre lotes e não pode pegar
    # um JSON pela metade.
    tmp = d / f".{plataforma}.json.tmp"
    tmp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / f"{plataforma}.json")
    return reg


def _eventos_por_plataforma(run_dir, horas: int = 24) -> dict:
    desde = (_agora() - timedelta(hours=horas)).isoformat()
    por_plat = {}
    for e in events.ler(run_dir, desde=desde):
        por_plat.setdefault(e.get("plataforma") or "?", []).append(e)
    return por_plat


def estacao(run_dir, sessao: str, pln: dict, evs: list) -> dict:
    """Tudo que o painel mostra de UMA sessão (`plataforma` ou `plataforma.eixo`)."""
    plataforma, suf = partir_sessao(sessao)
    # O CONTROLE é por sessão, não por plataforma: sessões da mesma plataforma
    # precisam poder ser pausadas e retomadas em separado. Lido ANTES porque o
    # alvo depende dos eixos que a sessão declarou aqui.
    ctl = controle(run_dir, sessao)
    n_fatias = int(os.environ.get("N_FATIAS", "3"))
    fat = fatia_da_sessao(sessao, n_fatias)
    if fat:
        # sessão por CONTA: roda os eixos que DECLAROU, numa fatia dos perfis
        perfis = perfis_da_fatia(_perfis_do_run(run_dir), *fat)
        # Ordem de precedência: o que o runner DECLAROU > os eixos da FASE
        # (env `EIXOS`, que o compose da fase define) > os eixos do plano.
        # Sem o nível da fase, uma sessão que ainda não começou aparece com
        # alvo dos três eixos do plano — 150 em vez de 100 na fase A.
        eixos_decl = (ctl.get("eixos")
                      or [e for e in os.environ.get("EIXOS", "").split() if e]
                      or None)
        prog = progresso(run_dir, plataforma, pln, eixos=eixos_decl,
                         perfis=perfis)
        eixo = None
    else:
        # sessão por EIXO (forma alternativa) ou plataforma inteira
        eixo = suf
        eixos = [eixo] if eixo else None
        prog = progresso(run_dir, plataforma, pln, eixos=eixos)

    alertas = [e for e in evs if e.get("nivel") == events.ALERTA]
    ultimo_alerta = alertas[-1] if alertas else None
    ultimo_evento = evs[-1] if evs else None
    ult_ts = _parse_ts(ultimo_evento.get("ts")) if ultimo_evento else None
    parado_min = round((_agora() - ult_ts).total_seconds() / 60) if ult_ts else None

    # "Precisa de humano" é a única métrica em cima da qual a equipe age, então
    # é decidida aqui e não na tela. Um alerta está PENDENTE até algo o
    # desmentir, e duas coisas desmentem:
    #   - uma conversa concluída depois dele (a plataforma voltou a andar);
    #   - alguém ter apertado "resolvido" no painel depois dele (logou de
    #     novo, trocou de conta, destravou).
    # Sem a segunda, o cartão ficaria vermelho para sempre depois do primeiro
    # bloqueio, e um painel que nunca fica verde deixa de ser lido.
    marcos = [_parse_ts(e.get("ts")) for e in evs
              if e.get("tipo") == events.CONVERSA_CONCLUIDA]
    if ctl["estado"] == "rodando":
        marcos.append(_parse_ts(ctl.get("em")))
    marcos = [m for m in marcos if m]

    alerta_pendente = False
    if ultimo_alerta:
        ta = _parse_ts(ultimo_alerta.get("ts")) or _agora()
        alerta_pendente = not any(m > ta for m in marcos)
    precisa = ctl["estado"] == "precisa_humano" or alerta_pendente

    return {
        "sessao": sessao,
        "plataforma": plataforma,
        "eixo": eixo,
        "estado": ctl["estado"],
        "motivo": ctl["motivo"],
        "precisa_humano": precisa,
        "progresso": prog,
        "rate_limits_24h": sum(1 for e in evs
                               if e.get("tipo") == events.RATE_LIMIT),
        "erros_24h": sum(1 for e in evs
                         if e.get("tipo") in (events.TURNO_ERRO,
                                              events.ENVIO_FALHOU)),
        "alertas_24h": len(alertas),
        "ultimo_alerta": ultimo_alerta,
        "ultimo_evento": ultimo_evento,
        "parado_ha_min": parado_min,
    }


def nucleo_pareado(run_dir, plataformas: list, eixos=None) -> dict:
    """Perfis COMPLETOS em TODAS as plataformas, por eixo.

    É o número que governa a comparação entre plataformas: um perfil só entra
    na comparação se as oito o coletaram. Cada estação percorre sua fatia em
    ordem de id, então o conjunto completo de uma plataforma é um PREFIXO da
    mesma lista — e a interseção sai naturalmente. O que abre buraco no
    prefixo é conversa que falhou e ainda não foi refeita, e é exatamente isso
    que este número expõe: a plataforma mais lenta define o N comparável, e
    uma plataforma adiantada não compensa outra atrasada.
    """
    run_dir = Path(run_dir)
    pln = plano(run_dir)
    plats = sorted({partir_sessao(s)[0] for s in plataformas})
    eixos = eixos or (pln.get("eixos") or [])
    por_plat_eixo: dict[tuple[str, str], set] = {}
    conv_dir = run_dir / "conversations"
    if conv_dir.exists():
        esperados = pln.get("turnos_esperados") or {}
        for f in conv_dir.glob("*.json"):
            rec = _ler_json(f)
            if not rec:
                continue
            plat, eixo = rec.get("platform"), rec.get("eixo")
            perfil = (rec.get("perfil_id") or rec.get("profile_id")
                      or (rec.get("profile") or {}).get("id"))
            if not (plat and eixo and perfil):
                continue
            turns = rec.get("turns") or []
            alvo = (esperados.get(f"{perfil}_{eixo}")
                    or TURNOS_PADRAO.get(eixo, 10))
            if len(turns) == alvo and all(t.get("ok") for t in turns):
                por_plat_eixo.setdefault((plat, eixo), set()).add(perfil)
    out = {}
    for e in eixos:
        conjuntos = [por_plat_eixo.get((p, e), set()) for p in plats]
        comum = set.intersection(*conjuntos) if conjuntos else set()
        out[e] = {
            "n": len(comum),
            "por_plataforma": {p: len(por_plat_eixo.get((p, e), set()))
                               for p in plats},
            "gargalo": min(
                ((len(por_plat_eixo.get((p, e), set())), p) for p in plats),
                default=(0, None))[1],
        }
    return {"plataformas": plats, "por_eixo": out}


def resumo(run_dir, plataformas: list, horas: int = 24) -> dict:
    run_dir = Path(run_dir)
    pln = plano(run_dir)
    idx = _eventos_por_plataforma(run_dir, horas)
    # `plataformas` aceita sessões (`plataforma.eixo`). Os eventos são
    # indexados por PLATAFORMA, então três sessões da mesma plataforma vêem os
    # mesmos eventos — aceitável: o que distingue uma da outra é o progresso e
    # o controle, e o driver não sabe em qual sessão está.
    estacoes = [estacao(run_dir, s, pln, idx.get(partir_sessao(s)[0], []))
                for s in plataformas]
    total = sum(e["progresso"]["completas"] for e in estacoes)
    alvo = sum(e["progresso"]["alvo"] for e in estacoes)
    return {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "gerado_em": _agora().isoformat(),
        "plano": {
            "alvo_por_plataforma": pln.get("alvo"),
            "eixos": pln.get("eixos"),
            "alvo_por_eixo": pln.get("alvo_por_eixo"),
        },
        "total": {"completas": total, "alvo": alvo,
                  "pct": round(100 * total / alvo, 1) if alvo else None},
        "nucleo_pareado": nucleo_pareado(run_dir, plataformas),
        "precisam_humano": [e["sessao"] for e in estacoes
                            if e["precisa_humano"]],
        "estacoes": estacoes,
    }
