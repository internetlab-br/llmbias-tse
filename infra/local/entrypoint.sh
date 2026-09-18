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
# mesmo compose numa VM de nuvem (ou ligar uma VPN no host residencial), a coleta
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
# Lock do Xvfb sobrevive ao restart do container (o filesystem persiste), e o
# Xvfb novo morre com "Server is already active for display N". Junto com a
# checagem de CDP que agora aborta de verdade, isso fazia a estação entrar em
# loop de restart sem nunca recuperar. Ninguém mais usa este display aqui.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
Xvfb ":${DISPLAY_NUM}" -screen 0 "$TELA" -nolisten tcp -auth "$XAUTH" &
# Espera a tela ACEITAR conexão, em vez de dormir um tempo fixo. Com `sleep 3`
# o Chrome subia antes do Xvfb em máquina carregada (5 estações subindo
# juntas) e morria com "Missing X server or $DISPLAY", deixando a estação sem
# navegador — e o entrypoint seguia como se tivesse dado certo.
for _ in $(seq 1 60); do
  if xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1; then
  log "ERRO: Xvfb :${DISPLAY_NUM} não respondeu em 30s. Abortando."
  exit 1
fi
log "Xvfb :${DISPLAY_NUM} (${TELA})"

x11vnc -display ":${DISPLAY_NUM}" -auth "$XAUTH" -rfbport "$VNC_PORT" \
       -nopw -forever -shared -noxdamage -quiet &
sleep 1
websockify --web=/usr/share/novnc "0.0.0.0:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" &
log "noVNC em :${NOVNC_PORT}"

# Lock obsoleto do perfil: o Chrome grava SingletonLock com o hostname do
# container. Como o hostname muda a cada recriação, o Chrome novo acha que o
# perfil está em uso "em outro computador" e se recusa a abrir. Só um
# container usa cada perfil, então remover é seguro — e sem isto toda
# recriação de estação nasce sem navegador.
rm -f "$PERFIL/SingletonLock" "$PERFIL/SingletonSocket" "$PERFIL/SingletonCookie"

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

CDP_OK=0
for _ in $(seq 60); do
  if curl -sf "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null; then
    CDP_OK=1; break
  fi
  sleep 0.5
done
# Antes isto seguia em frente e logava "CDP pronto" mesmo com o Chrome morto,
# e a estação aparecia saudável no painel sem ter navegador. Falha tem de
# parecer falha: o container morre e o `restart: unless-stopped` re-tenta.
if [ "$CDP_OK" != "1" ]; then
  log "ERRO: Chrome não abriu o CDP em :${CDP_PORT} (veja o log acima). Abortando."
  exit 1
fi
log "CDP pronto em :${CDP_PORT}"

# Dependências do projeto (repo vem montado em /app, então instala em runtime
# e não na imagem: assim mexer no código não exige rebuild).
uv sync --frozen 2>/dev/null || uv sync || log "AVISO: uv sync falhou"

# O runner sobe SEMPRE. Ele obedece ao arquivo de controle — fica dormindo
# enquanto a sessão está parada/pausada e começa quando o painel manda play.
# Antes ele só subia com AUTO_INICIAR=1, e sem ele o botão de play escrevia
# "rodando" num arquivo que ninguém lia: o painel mostrava as 16 sessões
# coletando com zero processo de coleta no ar.
#
# Prefere a cópia do repositório montado à da imagem, pelo mesmo motivo do
# `uv sync` em runtime: corrigir o runner não deveria exigir rebuild.
RODA=/usr/local/bin/roda.sh
[ -x /app/infra/local/roda.sh ] && RODA=/app/infra/local/roda.sh
# Roda uma CÓPIA, nunca o arquivo do repositório. O bash lê o script do disco
# por offset de byte enquanto executa, então reescrever o arquivo por baixo de
# um processo em andamento corrompe o parse e o shell morre calado. Foi o que
# aconteceu em 18/09/2026: uma correção no `roda.sh` derrubou o runner de 14
# das 16 estações, cada uma no meio de um lote, e o único sinal foi o painel
# dizendo "runner fora do ar" — que eu levei um tempo para acreditar.
# Caminho ÚNICO por execução. Um caminho fixo só empurra o problema: copiar
# por cima de uma cópia que já está rodando corrompe o parse do mesmo jeito.
ATIVO="/tmp/roda-ativo.$(date +%s).$$.sh"
cp "$RODA" "$ATIVO" && chmod +x "$ATIVO"
log "subindo o runner (cópia de $RODA); espera o play do painel se estiver parado"
"$ATIVO" &

wait "$CHROME_PID"
