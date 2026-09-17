"""Estado da coleta, lido do disco.

O painel não conversa com os coletores: ele lê o mesmo diretório de rodada
que eles escrevem (`data/<run_id>/`). Não há protocolo, porta, fila nem
banco entre os dois. Isso é possível porque todas as estações rodam no
mesmo host; foi justamente o que a mudança de VMs na Azure para containers
na terranave comprou.

Fontes, todas append-only ou write-once:
  - `conversations/*.json` : o que já foi coletado (verdade sobre progresso)
  - `events.jsonl`         : o que aconteceu (rate limit, bloqueio, erro)
  - `plano_coleta.json`    : o que era para ser coletado (o alvo)
  - `control/<plat>.json`  : o que o painel mandou fazer (pausa, play)
"""

from __future__ import annotations

import json
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


def progresso(run_dir, plataforma: str, pln=None) -> dict:
    """Conta conversas completas e incompletas de uma plataforma.

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
            perfil = rec.get("perfil_id") or rec.get("profile_id")
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
    }


def definir_controle(run_dir, plataforma: str, estado: str,
                     motivo=None) -> dict:
    if estado not in ESTADOS:
        raise ValueError(f"estado inválido: {estado!r} (use {ESTADOS})")
    d = Path(run_dir) / "control"
    d.mkdir(parents=True, exist_ok=True)
    reg = {"estado": estado, "motivo": motivo, "em": _agora().isoformat()}
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


def estacao(run_dir, plataforma: str, pln: dict, evs: list) -> dict:
    """Tudo que o painel mostra de UMA plataforma."""
    prog = progresso(run_dir, plataforma, pln)
    ctl = controle(run_dir, plataforma)

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
        "plataforma": plataforma,
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


def resumo(run_dir, plataformas: list, horas: int = 24) -> dict:
    run_dir = Path(run_dir)
    pln = plano(run_dir)
    idx = _eventos_por_plataforma(run_dir, horas)
    estacoes = [estacao(run_dir, p, pln, idx.get(p, [])) for p in plataformas]
    total = sum(e["progresso"]["completas"] for e in estacoes)
    alvo = (pln.get("alvo") or 0) * len(plataformas)
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
        "precisam_humano": [e["plataforma"] for e in estacoes
                            if e["precisa_humano"]],
        "estacoes": estacoes,
    }
