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

# UM runner por sessão, e só um. Duas instâncias na mesma estação dirigem o
# MESMO Chrome: as conversas se intercalam na mesma aba e a captura mistura
# turnos de duas conversas — dado corrompido sem nenhum erro. Aconteceu em
# 18/09/2026 nas duas estações de WhatsApp, onde o chat do Meta AI é único e
# compartilhado, que é o pior caso possível.
#
# `flock` não solta o arquivo enquanto o processo vive, e o solta sozinho se
# ele morrer — não fica trava órfã para alguém limpar à mão.
TRAVA="/tmp/roda.${SESSAO}.lock"
exec 9>"$TRAVA"
if ! flock -n 9; then
  echo "[roda] já existe runner para $SESSAO (trava $TRAVA); saindo" >&2
  exit 0
fi

estado() { jq -r '.estado // "rodando"' "$CONTROLE" 2>/dev/null || echo rodando; }
motivo_atual() { jq -r '.motivo // ""' "$CONTROLE" 2>/dev/null || echo ""; }
marcar() {  # marcar <estado> [motivo]
  # Declara também os EIXOS desta sessão. O painel precisa deles para calcular
  # o alvo: a sessão pode rodar um subconjunto do plano (a fase 2 roda
  # `voto integridade` em 16 sessões e depois `genero` em 8), e sem isso o
  # alvo sairia com os três eixos do plano e toda sessão apareceria atrasada.
  #
  # `runner_visto_em` é o batimento: prova de que existe PROCESSO por trás do
  # estado. Sem ele o painel mostrava "rodando" só porque alguém clicou em
  # play — foi o que aconteceu em 18/09, com as 16 sessões "rodando" e nenhum
  # runner no ar, porque o runner só subia com AUTO_INICIAR=1 e ninguém lia o
  # arquivo de controle. Estado sem processo é a mentira mais cara do painel.
  local tmp; tmp="$(mktemp)"
  jq -n --arg e "$1" --arg m "${2:-}" --arg t "$(date -Is)" \
     --arg x "$EIXOS" \
     '{estado:$e, motivo:(if $m=="" then null else $m end), em:$t,
       runner_visto_em:$t,
       eixos:($x | split(" ") | map(select(length>0)))}' > "$tmp"
  mv "$tmp" "$CONTROLE"
}
bater() {  # renova o batimento sem mexer no estado nem no motivo
  marcar "$(estado)" "$(motivo_atual)"
}
dormir_batendo() {  # dormir_batendo <segundos>
  # Espera longa TEM de bater no meio. A espera de 15 min depois de um lote
  # sem progresso é decisão do runner, não morte dele — mas sem batimento ela
  # é indistinguível de morte, e o painel marcou 12 estações vivas como "fora
  # do ar" (18/09/2026). Um painel que acusa o que está funcionando deixa de
  # ser lido, e aí não acusa o que quebrou.
  local resta="$1"
  while [ "$resta" -gt 0 ]; do
    bater
    if [ "$resta" -gt 60 ]; then sleep 60; resta=$((resta - 60));
    else sleep "$resta"; resta=0; fi
  done
}
evento() {  # evento <tipo> <nivel> <mensagem>
  # SESSAO, não PLATAFORMA: o painel agrupa os eventos por este campo e há
  # duas estações por plataforma na fase 2.
  python3 - "$RUN_DIR" "$SESSAO" "$1" "$2" "${3:-}" <<'PY'
import json, sys, datetime, pathlib
run, sessao, tipo, nivel, msg = sys.argv[1:6]
reg = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "tipo": tipo, "nivel": nivel, "plataforma": sessao, "origem": "runner"}
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

# PRESERVA o estado: o runner sobe junto com o container, e forçar "rodando"
# aqui faria uma estação parada de propósito (esperando login, conta trocada,
# alvo atingido) voltar a coletar sozinha a cada reinício. Só declara os eixos
# e o batimento.
bater
evento coleta_iniciada info "runner de $SESSAO no ar (estado: $(estado))"
SEM_PROGRESSO=0

while true; do
  case "$(estado)" in
    pausado)        bater; sleep 10; continue ;;
    precisa_humano) bater; sleep 30; continue ;;
    # `parado` ESPERA, não encerra. Antes o runner saía, e o play do painel
    # passava a não ter ninguém para obedecer: o estado virava "rodando" e
    # nada acontecia. Ficar de vigia custa um processo dormindo.
    parado)         bater; sleep 15; continue ;;
  esac

  bater  # batimento fresco antes de um lote que pode levar horas
  ANTES="$(completas)"; ANTES="${ANTES:-0}"
  uv run python -m llmbias_tse conjoint \
      --run-id "$RUN_ID" --platforms "$PLATAFORMA" --eixos $EIXOS \
      --phase generate --per-platform-limit "$LOTE" \
      ${FATIA:+--fatia "$FATIA"} \
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
    dormir_batendo 900
  else
    SEM_PROGRESSO=0
    dormir_batendo 30
  fi
done
