#!/usr/bin/env bash
# Coleta em lotes de 20. $1=plataforma  $2..=eixos (opcional).
PLAT="$1"; shift
EIXOS="$*"
cd "$HOME/llmbias-tse" || exit 1
export PATH="$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1
LOG="$HOME/coleta_${PLAT}.log"
SEM_PROGRESSO=0
ARG_EIXOS=""
[ -n "$EIXOS" ] && ARG_EIXOS="--eixos $EIXOS"

completas() { uv run python progresso.py "$PLAT" ${EIXOS:+$EIXOS} 2>/dev/null | grep -oP 'COMPLETAS=\K[0-9]+'; }
ALVO=$(uv run python progresso.py "$PLAT" ${EIXOS:+$EIXOS} 2>/dev/null | grep -oP 'ALVO=\K[0-9]+')
ALVO=${ALVO:-360}

echo "===== RUNNER iniciado $(date -Is) plataforma=$PLAT eixos='${EIXOS:-todos}' alvo=$ALVO =====" >> "$LOG"
while true; do
  ANTES=$(completas); ANTES=${ANTES:-0}
  if [ "$ANTES" -ge "$ALVO" ]; then
    echo "===== COMPLETO: $ANTES/$ALVO em $(date -Is) =====" >> "$LOG"; break
  fi
  echo "----- lote inicio $(date -Is) completas=$ANTES/$ALVO -----" >> "$LOG"
  uv run python -m llmbias_tse conjoint --run-id experimento_2026_08 \
    --platforms "$PLAT" $ARG_EIXOS --phase generate --per-platform-limit 20 \
    --turn-delay 5 --conv-delay 12 >> "$LOG" 2>&1
  RC=$?
  DEPOIS=$(completas); DEPOIS=${DEPOIS:-0}
  echo "----- lote fim $(date -Is) rc=$RC completas=$DEPOIS (+$((DEPOIS-ANTES))) -----" >> "$LOG"
  if [ "$DEPOIS" -le "$ANTES" ]; then
    SEM_PROGRESSO=$((SEM_PROGRESSO+1))
    echo "!!! lote sem progresso ($SEM_PROGRESSO/3) - esperando 30min" >> "$LOG"
    [ "$SEM_PROGRESSO" -ge 3 ] && { echo "!!! PARANDO $(date -Is)" >> "$LOG"; break; }
    sleep 1800
  else
    SEM_PROGRESSO=0; sleep 60
  fi
done
