"""Barramento de eventos da coleta.

A rodada de agosto/2026 ficou dois dias parada sem ninguém perceber: o
driver **detectava** o rate limit e o bloqueio, mas engolia o sinal dentro
de um `print` e de um log dentro da própria VM. Quem olhava de fora só via
"a coleta não anda".

Este módulo é o conserto: todo evento que muda o que um humano precisa
saber vira uma linha em `data/<run_id>/events.jsonl`. O painel lê esse
arquivo; não há protocolo de rede, banco nem daemon no meio. Append-only e
uma linha por evento, igual ao resto do projeto (`exchanges.jsonl`,
`conversations.jsonl`).

Regra de ouro: **emitir evento nunca pode derrubar a coleta**. Toda escrita
é best-effort; se o disco encher ou o arquivo sumir, a coleta continua e o
painel é que fica cego.

Uso:

    from llmbias_tse import events

    events.configurar(store.dir, plataforma="gemini")   # uma vez por processo
    events.emit(events.TURNO_OK, conversa="gemini_P001_voto", turno=3)
    events.alerta(events.BLOQUEIO, "rate limit não liberou", esperas=6)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Tipos de evento. São strings simples de propósito: o painel não deve ter de
# importar este módulo para entender o arquivo, e uma rodada antiga tem de
# continuar legível quando a lista mudar.
# --------------------------------------------------------------------------

COLETA_INICIADA = "coleta_iniciada"
COLETA_ENCERRADA = "coleta_encerrada"

CONVERSA_INICIADA = "conversa_iniciada"
CONVERSA_CONCLUIDA = "conversa_concluida"
CONVERSA_ABORTADA = "conversa_abortada"

TURNO_OK = "turno_ok"
TURNO_ERRO = "turno_erro"

RATE_LIMIT = "rate_limit"            # modal detectado; esperando passar
RATE_LIMIT_LIBERADO = "rate_limit_liberado"
BLOQUEIO = "bloqueio"                # desistiu de esperar o limite
ENVIO_FALHOU = "envio_falhou"        # mensagem não chegou a ser postada
CHAT_ISOLADO_FALHOU = "chat_isolado_falhou"  # modo anônimo não confirmou
LOGIN_PERDIDO = "login_perdido"

PRECISA_HUMANO = "precisa_humano"    # genérico, quando nada mais descreve

# Níveis. `alerta` é o que acorda alguém; o painel destaca a plataforma e é
# daqui que sai a notificação para o Slack/WhatsApp.
INFO = "info"
AVISO = "aviso"
ALERTA = "alerta"

# Eventos que, por si só, significam "esta plataforma parou até alguém agir".
# O painel usa esta lista para montar a fila de "precisa de humano".
TIPOS_QUE_PEDEM_HUMANO = frozenset({
    BLOQUEIO, CHAT_ISOLADO_FALHOU, LOGIN_PERDIDO, PRECISA_HUMANO,
})

ARQUIVO = "events.jsonl"

_lock = threading.Lock()
_caminho: Path | None = None
_contexto: dict = {}


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def configurar(run_dir: str | Path | None = None, **contexto) -> Path | None:
    """Aponta o barramento para `<run_dir>/events.jsonl` e fixa os campos que
    se repetem em todo evento do processo (tipicamente `plataforma`).

    Sem `run_dir`, tenta a variável `LLMBIAS_EVENTS` (é assim que o container
    de coleta configura o runner sem alterar a linha de comando). Sem nenhum
    dos dois, o barramento fica mudo e `emit` vira no-op.
    """
    global _caminho, _contexto
    if run_dir is None:
        env = os.environ.get("LLMBIAS_EVENTS")
        run_dir = Path(env).parent if env else None
    with _lock:
        if run_dir is None:
            _caminho = None
        else:
            d = Path(run_dir)
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            _caminho = d / ARQUIVO
        _contexto = {k: v for k, v in contexto.items() if v is not None}
    return _caminho


def contexto(**campos) -> None:
    """Acrescenta/atualiza campos fixos (ex.: a conversa corrente)."""
    with _lock:
        for k, v in campos.items():
            if v is None:
                _contexto.pop(k, None)
            else:
                _contexto[k] = v


def emit(tipo: str, nivel: str = INFO, **campos) -> dict:
    """Registra um evento. Devolve o registro (útil em teste); nunca levanta."""
    reg = {"ts": _agora(), "tipo": tipo, "nivel": nivel}
    with _lock:
        reg.update(_contexto)
        reg.update(campos)
        caminho = _caminho
    if caminho is not None:
        try:
            with caminho.open("a", encoding="utf-8") as f:
                f.write(json.dumps(reg, ensure_ascii=False) + "\n")
        except Exception:
            pass  # painel cego é ruim; coleta interrompida é pior
    return reg


def aviso(tipo: str, mensagem: str = "", **campos) -> dict:
    return emit(tipo, nivel=AVISO, mensagem=mensagem or None, **campos)


def alerta(tipo: str, mensagem: str = "", **campos) -> dict:
    """Evento que exige ação humana. É o que o painel põe em vermelho."""
    return emit(tipo, nivel=ALERTA, mensagem=mensagem or None, **campos)


def ler(caminho: str | Path, *, tipos=None, niveis=None,
        desde: str | None = None, limite: int | None = None) -> list[dict]:
    """Lê `events.jsonl` com filtros. Linha corrompida é pulada, não derruba
    a leitura: o arquivo é escrito por processos que podem morrer no meio.

    `limite` devolve os N **mais recentes** (o arquivo cresce e o painel só
    mostra a cauda).
    """
    p = Path(caminho)
    if p.is_dir():
        p = p / ARQUIVO
    if not p.exists():
        return []
    tipos = set(tipos) if tipos else None
    niveis = set(niveis) if niveis else None
    out: list[dict] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    reg = json.loads(linha)
                except Exception:
                    continue
                if tipos and reg.get("tipo") not in tipos:
                    continue
                if niveis and reg.get("nivel") not in niveis:
                    continue
                if desde and (reg.get("ts") or "") < desde:
                    continue
                out.append(reg)
    except Exception:
        return out
    if limite is not None and len(out) > limite:
        return out[-limite:]
    return out
