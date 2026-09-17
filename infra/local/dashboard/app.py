"""Painel de coleta.

Lê o diretório da rodada e serve o estado das estações. Não fala com os
containers: quem quer mandar parar/retomar escreve o arquivo de controle que
o `roda.sh` lê entre lotes. Por isso o painel não precisa do socket do Docker
nem de SSH para nada do dia a dia, o que é o que permite expô-lo para o time
sem transformar o botão de pausa numa porta de execução remota.

O acesso à tela de cada estação (o "abrir o browser") é feito pelo Caddy, que
faz proxy de `/vnc/<plataforma>/` para o noVNC daquele container. O painel só
publica o link.

    uv run --extra painel uvicorn infra.local.dashboard.app:app --port 8000
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from llmbias_tse import events, status

RUN_DIR = Path(os.environ.get("RUN_DIR", "data/experimento_2026_09"))
# SESSOES é a lista da rodada 2: uma entrada por (plataforma, eixo), no
# formato `plataforma.eixo` (ex.: `whatsapp_metaai.voto`). Aceita também o
# nome da plataforma sozinho, que é a forma da rodada 1 — uma estação por
# plataforma rodando os três eixos em série.
SESSOES = [s for s in os.environ.get(
    "SESSOES",
    os.environ.get("PLATAFORMAS", "")).split() if s]
PLATAFORMAS = SESSOES or [p for p in os.environ.get(
    "PLATAFORMAS",
    "gemini chatgpt claude grok deepseek copilot google_aimode whatsapp_metaai",
).split() if p]

AQUI = Path(__file__).parent
app = FastAPI(title="Painel de coleta llmbias-tse")


@app.get("/api/status")
def api_status(horas: int = 24):
    return status.resumo(RUN_DIR, PLATAFORMAS, horas=horas)


@app.get("/api/eventos")
def api_eventos(plataforma: str | None = None, nivel: str | None = None,
                limite: int = 200):
    evs = events.ler(RUN_DIR, niveis=[nivel] if nivel else None, limite=None)
    if plataforma:
        evs = [e for e in evs if e.get("plataforma") == plataforma]
    return {"eventos": evs[-limite:], "total": len(evs)}


# `{plataforma}` aceita a SESSÃO (`plataforma.eixo`): o controle é por sessão,
# senão pausar o WhatsApp pararia as três de uma vez.
@app.post("/api/estacao/{plataforma:path}/{acao}")
def api_acao(plataforma: str, acao: str, motivo: str | None = None):
    """Ações do painel. Todas são idempotentes e se resumem a escrever o
    estado desejado; o runner converge para ele no início do próximo lote.

    `resolver` é a que fecha o ciclo do alerta: alguém logou de novo, trocou a
    conta ou destravou a plataforma, e declara isso. Sem ela o painel ficaria
    vermelho para sempre depois do primeiro bloqueio.
    """
    if plataforma not in PLATAFORMAS:
        raise HTTPException(404, f"plataforma desconhecida: {plataforma}")
    destino = {
        "play": "rodando",
        "retomar": "rodando",
        "resolver": "rodando",
        "pausar": "pausado",
        "parar": "parado",
        "chamar": "precisa_humano",
    }.get(acao)
    if destino is None:
        raise HTTPException(400, f"ação desconhecida: {acao}")

    reg = status.definir_controle(RUN_DIR, plataforma, destino, motivo)
    events.configurar(RUN_DIR, plataforma=plataforma)
    events.emit(
        events.PRECISA_HUMANO if destino == "precisa_humano" else "acao_painel",
        nivel=events.ALERTA if destino == "precisa_humano" else events.INFO,
        acao=acao, estado=destino, mensagem=motivo or None, origem="painel",
    )
    return reg


@app.get("/api/saude")
def api_saude():
    return {"ok": True, "run_dir": str(RUN_DIR), "existe": RUN_DIR.exists(),
            "plataformas": PLATAFORMAS}


@app.get("/")
def index():
    return FileResponse(AQUI / "static" / "index.html")


app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
