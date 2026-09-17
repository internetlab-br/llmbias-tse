#!/usr/bin/env bash
# Runner da coleta de UMA plataforma, em lotes, obedecendo ao painel.
#
# Diferença para o `roda_lotes.sh` da Azure: aqui existe um arquivo de
# controle que o painel escreve (`control/<plataforma>.json`). O runner o lê
# entre lotes, então "pausar" e "retomar" viram botão sem precisar de SSH,
# de docker exec, nem de socket do Docker no painel.
set -uo pipefail

PLATAFORMA="${PLATAFORMA:?}"
RUN_ID="${RUN_ID:-experimento_2026_09}"
DADOS="${DADOS:-/dados}"
EIXOS="${EIXOS:-voto genero integridade}"
LOTE="${LOTE:-20}"
TURN_DELAY="${TURN_DELAY:-5}"
CONV_DELAY="${CONV_DELAY:-12}"

RUN_DIR="${DADOS}/${RUN_ID}"
# SESSAO é a unidade de controle da rodada 2 (`plataforma.eixo`). Sem ela,
# pausar uma sessão pausaria as três da mesma plataforma. Vazia = rodada 1,
# uma estação por plataforma.
SESSAO="${SESSAO:-$PLATAFORMA}"
CONTROLE="${RUN_DIR}/control/${SESSAO}.json"
export LLMBIAS_EVENTS="${RUN_DIR}/events.jsonl"

mkdir -p "$(dirname "$CONTROLE")"
[ -f "$CONTROLE" ] || echo '{"estado": "rodando"}' > "$CONTROLE"

estado() { jq -r '.estado // "rodando"' "$CONTROLE" 2>/dev/null || echo rodando; }
marcar() {  # marcar <estado> [motivo]
  local tmp; tmp="$(mktemp)"
  jq -n --arg e "$1" --arg m "${2:-}" --arg t "$(date -Is)" \
     '{estado:$e, motivo:(if $m=="" then null else $m end), em:$t}' > "$tmp"
  mv "$tmp" "$CONTROLE"
}
evento() {  # evento <tipo> <nivel> <mensagem>
  python3 - "$RUN_DIR" "$PLATAFORMA" "$1" "$2" "${3:-}" <<'PY'
import json, sys, datetime, pathlib
run, plat, tipo, nivel, msg = sys.argv[1:6]
reg = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "tipo": tipo, "nivel": nivel, "plataforma": plat, "origem": "runner"}
if msg:
    reg["mensagem"] = msg
try:
    with (pathlib.Path(run) / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")
except Exception:
    pass
PY
}
# `^` é essencial: sem ele o padrão casa também em INCOMPLETAS=, a função
# devolve DUAS linhas ("0\n3") e a comparação de progresso abaixo quebra com
# "esperava expressão de número inteiro" — deixando a guarda de 3 lotes sem
# progresso sem funcionar.
completas() {
  uv run python infra/local/progresso.py "$PLATAFORMA" \
      --run-dir "$RUN_DIR" --eixos $EIXOS 2>/dev/null \
      | grep -oP '^COMPLETAS=\K[0-9]+'
}

alvo() {
  uv run python infra/local/progresso.py "$PLATAFORMA" \
      --run-dir "$RUN_DIR" --eixos $EIXOS 2>/dev/null \
      | grep -oP '^COMPLETAS=[0-9]+ ALVO=\K[0-9]+'
}

evento coleta_iniciada info "runner de $SESSAO no ar"
SEM_PROGRESSO=0

while true; do
  case "$(estado)" in
    pausado)        sleep 10; continue ;;
    precisa_humano) sleep 30; continue ;;
    parado)         evento coleta_encerrada info "parado pelo painel"; exit 0 ;;
  esac

  ANTES="$(completas)"; ANTES="${ANTES:-0}"
  uv run python -m llmbias_tse conjoint \
      --run-id "$RUN_ID" --platforms "$PLATAFORMA" --eixos $EIXOS \
      --phase generate --per-platform-limit "$LOTE" \
      --turn-delay "$TURN_DELAY" --conv-delay "$CONV_DELAY"
  DEPOIS="$(completas)"; DEPOIS="${DEPOIS:-0}"

  # ALVO ATINGIDO não é impedimento. Sem esta checagem, a estação que termina
  # produz lotes sem conversa nova por definição, bate os 3 lotes e grita
  # `precisa_humano` — alarme falso que, com 24 sessões, afoga o painel e
  # esconde os alarmes de verdade. (Visto no smoke de 17/09: o WhatsApp
  # apareceu "com erro" em 3/3.)
  ALVO_ATUAL="$(alvo)"; ALVO_ATUAL="${ALVO_ATUAL:-0}"
  if [ "$ALVO_ATUAL" -gt 0 ] && [ "$DEPOIS" -ge "$ALVO_ATUAL" ]; then
    marcar parado "alvo atingido (${DEPOIS}/${ALVO_ATUAL})"
    evento coleta_encerrada info "alvo atingido: ${DEPOIS}/${ALVO_ATUAL}"
    exit 0
  fi

  if [ "$DEPOIS" -le "$ANTES" ]; then
    SEM_PROGRESSO=$((SEM_PROGRESSO + 1))
    # Três lotes sem uma conversa nova não é lentidão, é impedimento: conta
    # deslogada, seletor quebrado ou bloqueio duro. Parar e CHAMAR alguém é
    # melhor do que continuar girando e queimando a conta, que foi o que a
    # rodada de agosto fez por dois dias.
    if [ "$SEM_PROGRESSO" -ge 3 ]; then
      marcar precisa_humano "3 lotes sem progresso (parado em ${DEPOIS} conversas)"
      evento precisa_humano alerta "3 lotes sem progresso em ${DEPOIS} conversas"
      continue
    fi
    evento envio_falhou aviso "lote sem progresso ($SEM_PROGRESSO/3)"
    sleep 900
  else
    SEM_PROGRESSO=0
    sleep 30
  fi
done
