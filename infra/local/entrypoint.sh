#!/usr/bin/env bash
# Sobe a estação da plataforma: tela virtual, acesso remoto e Chrome com CDP.
#
# Depois disso NÃO inicia a coleta sozinho. A coleta só anda quando o painel
# (ou você) manda, porque a primeira coisa que uma estação nova precisa é de
# um humano logando na conta pela tela remota. Subir coletando daria um
# container em loop de erro antes do login existir.
set -euo pipefail

PLATAFORMA="${PLATAFORMA:?defina PLATAFORMA (ex.: gemini)}"
DISPLAY_NUM="${DISPLAY_NUM:-77}"
TELA="${TELA:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5901}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
CDP_PORT="${LLMBIAS_CDP_PORT:-9333}"
PERFIL="${LLMBIAS_PROFILE:-/perfil}"
RUN_ID="${RUN_ID:-experimento_2026_09}"
DADOS="${DADOS:-/dados}"
XAUTH="/tmp/.Xauthority.${DISPLAY_NUM}"

log() { echo "[$PLATAFORMA] $*"; }

export DISPLAY=":${DISPLAY_NUM}"
export XAUTHORITY="$XAUTH"
export LLMBIAS_EVENTS="${DADOS}/${RUN_ID}/events.jsonl"

mkdir -p "$PERFIL" "${DADOS}/${RUN_ID}/control"

# ---------------------------------------------------------------------------
# 1. Guarda de IP de saída.
#
# Na Azure o `start-browser.sh` era fail-closed contra o túnel cair. Aqui o
# risco mudou de forma: não há túnel para cair, mas se alguém subir este
# mesmo compose numa VM de nuvem (ou ligar uma VPN na terranave), a coleta
# passa a sair de um IP de datacenter e as contas queimam. Então a checagem
# continua, só que sobre o IP em si.
# ---------------------------------------------------------------------------
IP_SAIDA="$(curl -s --max-time 20 https://api.ipify.org || true)"
log "IP de saída: ${IP_SAIDA:-DESCONHECIDO}"
if [ -n "${IP_ESPERADO:-}" ] && [ "$IP_SAIDA" != "$IP_ESPERADO" ]; then
  log "ERRO: IP de saída '$IP_SAIDA' != esperado '$IP_ESPERADO'. Abortando."
  log "      (IP residencial muda sozinho; se foi só isso, atualize IP_ESPERADO)"
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Tela virtual + acesso remoto.
#    x11vnc só no loopback do container; quem publica é o Caddy/cloudflared.
# ---------------------------------------------------------------------------
touch "$XAUTH"; chmod 600 "$XAUTH"
Xvfb ":${DISPLAY_NUM}" -screen 0 "$TELA" -nolisten tcp -auth "$XAUTH" &
sleep 3
log "Xvfb :${DISPLAY_NUM} (${TELA})"

x11vnc -display ":${DISPLAY_NUM}" -auth "$XAUTH" -rfbport "$VNC_PORT" \
       -nopw -forever -shared -noxdamage -quiet &
sleep 1
websockify --web=/usr/share/novnc "0.0.0.0:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" &
log "noVNC em :${NOVNC_PORT}"

# ---------------------------------------------------------------------------
# 3. Chrome com CDP e perfil persistente.
#
# --no-sandbox: o sandbox do Chrome precisa de user namespaces que o Docker
# padrão não entrega. É aceitável aqui porque o container já é a fronteira de
# isolamento e as páginas visitadas são as próprias plataformas. Se quiser o
# sandbox de volta, rode com um perfil seccomp de Chrome e apague esta flag.
#
# As flags de anti-throttling são as mesmas da coleta em Windows: sem elas o
# Chrome atrasa timers de página não visível e a detecção de fim de resposta
# trava até o timeout. Aqui a tela é SEMPRE virtual, ou seja, nunca visível.
# ---------------------------------------------------------------------------
PROXY_ARG=""
[ -n "${PROXY_SOCKS:-}" ] && PROXY_ARG="--proxy-server=socks5://${PROXY_SOCKS} --proxy-bypass-list=<-loopback>"

google-chrome \
  --remote-debugging-port="${CDP_PORT}" \
  --user-data-dir="${PERFIL}" \
  --no-sandbox --disable-dev-shm-usage \
  $PROXY_ARG \
  --lang=pt-BR --no-first-run --no-default-browser-check \
  --window-size=1280,900 --window-position=0,0 \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-features=CalculateNativeWinOcclusion \
  about:blank &
CHROME_PID=$!

for _ in $(seq 40); do
  curl -sf "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null && break
  sleep 0.5
done
log "CDP pronto em :${CDP_PORT}"

# Dependências do projeto (repo vem montado em /app, então instala em runtime
# e não na imagem: assim mexer no código não exige rebuild).
uv sync --frozen 2>/dev/null || uv sync || log "AVISO: uv sync falhou"

if [ "${AUTO_INICIAR:-0}" = "1" ]; then
  log "AUTO_INICIAR=1: começando a coleta"
  /usr/local/bin/roda.sh &
else
  log "pronto. Logue na conta pela tela remota e dê play no painel."
fi

wait "$CHROME_PID"
