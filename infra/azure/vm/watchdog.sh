#!/usr/bin/env bash
# Vigia a coleta. $1=plataforma  $2..=eixos (opcional)
PLAT="$1"; shift
EIXOS="$*"
LOG="$HOME/watchdog.log"
cd "$HOME/llmbias-tse" || exit 1
export PATH="$HOME/.local/bin:$PATH"
ts() { date -Is; }

SAIDA_PROG=$(uv run python progresso.py "$PLAT" $EIXOS 2>/dev/null)
FEITAS=$(echo "$SAIDA_PROG" | grep -oP 'COMPLETAS=\K[0-9]+'); FEITAS=${FEITAS:-0}
ALVO=$(echo "$SAIDA_PROG" | grep -oP 'ALVO=\K[0-9]+'); ALVO=${ALVO:-360}
[ "$FEITAS" -ge "$ALVO" ] && exit 0

if ! timeout 5 bash -c "</dev/tcp/127.0.0.1/1080" 2>/dev/null; then
  echo "$(ts) ALERTA: tunel SOCKS 1080 FORA DO AR" >> "$LOG"
else
  IP=$(curl -s --max-time 20 --socks5-hostname 127.0.0.1:1080 https://ipinfo.io/ip 2>/dev/null)
  # IP_ESPERADO vem do ambiente: o IP residencial nao pode ficar no repo, e
  # ele muda sozinho de tempos em tempos.
  [ -n "${IP_ESPERADO:-}" ] && [ -n "$IP" ] && [ "$IP" != "$IP_ESPERADO" ]     && echo "$(ts) ALERTA: IP de saida inesperado '$IP'" >> "$LOG"
fi

if ! curl -s --max-time 10 http://localhost:9333/json/version >/dev/null 2>&1; then
  echo "$(ts) ALERTA: CDP 9333 fora do ar (systemd deve religar)" >> "$LOG"
else
  ABAS=$(curl -s --max-time 10 http://localhost:9333/json/list 2>/dev/null | grep -c '"type": "page"')
  if [ "${ABAS:-0}" -lt 2 ]; then
    echo "$(ts) aviso: so ${ABAS} aba(s); reabrindo reserva" >> "$LOG"
    for i in 1 2 3; do curl -s -X PUT "http://localhost:9333/json/new?about:blank" >/dev/null 2>&1; done
  fi
fi

if ! tmux has-session -t coleta 2>/dev/null; then
  echo "$(ts) ALERTA: runner caiu com ${FEITAS}/${ALVO} - religando" >> "$LOG"
  tmux new-session -d -s coleta "$HOME/llmbias-tse/roda_lotes.sh $PLAT $EIXOS"
fi
