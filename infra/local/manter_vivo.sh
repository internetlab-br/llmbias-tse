#!/usr/bin/env bash
# Guarda da máquina da coleta, para rodar pelo cron A CADA MINUTO:
#
#     * * * * * /caminho/do/repo/infra/local/manter_vivo.sh
#
# Faz duas coisas, e as duas existem porque a máquina caiu duas vezes em
# 12/10/2026 (08:46 e 09:34) sem ninguém olhando:
#
# 1. Mantém o VIGIA no ar. Os contêineres voltam sozinhos depois de um reboot
#    (`restart: unless-stopped`), mas o vigia é um processo do host e não
#    voltava, então nenhuma estação coletava até alguém religá-lo à mão. Como o
#    cron também roda depois do boot, isto cobre o reboot e a morte do vigia.
# 2. Registra a SAÚDE da máquina (temperatura, carga, memória) em
#    `data/<RUN_ID>/saude_maquina.log`. As duas quedas não deixaram nada no
#    journal (nem OOM, nem alarme térmico, nem panic): foram cortes secos. Sem
#    série de temperatura não há como separar "a coleta esquenta a máquina até
#    ela desligar" de "pico de energia".
#
# Lê RUN_ID e EIXOS_SEGUINTES do `.env` da infra, os mesmos que os contêineres
# usam, para o vigia nunca apontar para uma rodada diferente da das estações.
set -uo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$AQUI/../.." && pwd)"
ENV="$AQUI/.env"
export PATH="$HOME/.local/bin:/snap/bin:/usr/local/bin:/usr/bin:/bin"

ler_env() { grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- | tr -d '\r'; }
RUN_ID="$(ler_env RUN_ID)"
[ -n "$RUN_ID" ] || exit 0
RUN_DIR="$REPO/data/$RUN_ID"
mkdir -p "$RUN_DIR"

# --- saúde -----------------------------------------------------------------
pkg=""
for z in /sys/class/thermal/thermal_zone*; do
  [ "$(cat "$z/type" 2>/dev/null)" = "x86_pkg_temp" ] && pkg=$(( $(cat "$z/temp") / 1000 ))
done
nvme=""
for h in /sys/class/hwmon/hwmon*; do
  [ "$(cat "$h/name" 2>/dev/null)" = "nvme" ] && nvme=$(( $(cat "$h/temp1_input") / 1000 ))
done
read -r l1 l5 _ < /proc/loadavg
mem=$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{printf "%.0f", 100*(t-a)/t}' /proc/meminfo)
echo "$(date -Is) cpu_pkg=${pkg}C nvme=${nvme}C load1=$l1 load5=$l5 mem=${mem}%" \
  >> "$RUN_DIR/saude_maquina.log"

# --- vigia -----------------------------------------------------------------
# Docker ainda subindo depois do boot: tenta no próximo minuto.
docker ps >/dev/null 2>&1 || exit 0
exec 9>"/tmp/manter_vivo.$RUN_ID.lock"
flock -n 9 || exit 0
if ! pgrep -f "[v]igia.py --loop" >/dev/null; then
  cd "$REPO" || exit 1
  echo "$(date -Is) vigia fora do ar; religando" >> "$RUN_DIR/vigia.log"
  EIXOS_SEGUINTES="$(ler_env EIXOS_SEGUINTES)" nohup setsid uv run python \
    infra/local/vigia.py --loop 180 --log "data/$RUN_ID/vigia.jsonl" \
    >> "$RUN_DIR/vigia.log" 2>&1 < /dev/null &
fi

# --- Chrome travado para a automação ---------------------------------------
# Visto três vezes em 12–14/10/2026 (Copilot c1, DeepSeek c2 duas vezes), sempre
# logo depois de um login manual: o Chrome responde no CDP, mas o
# `connect_over_cdp` do runner morre ("Connection closed while reading from
# the driver" / "Frame was detached"), cada lote sai "sem progresso" e o
# play no painel não adianta. Reiniciar o contêiner resolve e mantém o login
# (o perfil é volume). No máximo um reinício a cada 15 min por estação.
for log in "$REPO"/data/runner.*.log; do
  [ -f "$log" ] || continue
  sessao="${log##*/runner.}"; sessao="${sessao%.log}"
  ultimo=$(grep -a -n -E "connect_over_cdp: Connection closed|\] turno [0-9]+/" "$log" | tail -1)
  case "$ultimo" in *connect_over_cdp*) ;; *) continue ;; esac
  [ -n "$(find "$log" -mmin -10)" ] || continue
  marca="/tmp/manter_vivo.reinicio.$sessao"
  [ -n "$(find "$marca" -mmin -15 2>/dev/null)" ] && continue
  touch "$marca"
  echo "$(date -Is) $sessao: runner não conecta no Chrome; reiniciando o contêiner" \
    >> "$RUN_DIR/vigia.log"
  docker restart "coleta-$sessao" >/dev/null 2>&1
done

# --- coleta travada sem morrer -----------------------------------------------
# 19/10/2026: o processo da coleta do chatgpt.c1 ficou 6 h parado no meio de
# uma conversa (turno 6), vivo e sem erro, e o runner não percebe processo
# vivo. Estação "rodando", com `conjoint` no ar e sem NENHUM evento há 40 min:
# mata o `conjoint`; o runner abre o próximo lote e a conversa é refeita do
# turno 1. O turno mais longo previsto (Claude/Gemini) tem teto de 7 min.
tail -n 20000 "$RUN_DIR/events.jsonl" 2>/dev/null | python3 -c '
import sys, json, datetime as dt
ult = {}
for l in sys.stdin:
    try: d = json.loads(l)
    except Exception: continue
    if d.get("plataforma"): ult[d["plataforma"]] = d["ts"]
agora = dt.datetime.now(dt.timezone.utc)
for s, ts in ult.items():
    if (agora - dt.datetime.fromisoformat(ts)).total_seconds() > 2400:
        print(s)
' | while read -r sessao; do
  estado=$(python3 -c "import json;print(json.load(open('$RUN_DIR/control/$sessao.json')).get('estado') or 'rodando')" 2>/dev/null)
  [ "$estado" = "rodando" ] || continue
  docker exec "coleta-$sessao" pgrep -f "llmbias_tse conjoint" >/dev/null 2>&1 || continue
  marca="/tmp/manter_vivo.travada.$sessao"
  [ -n "$(find "$marca" -mmin -40 2>/dev/null)" ] && continue
  touch "$marca"
  echo "$(date -Is) $sessao: 40 min sem evento com a coleta no ar; matando o conjoint" \
    >> "$RUN_DIR/vigia.log"
  docker exec "coleta-$sessao" pkill -f "llmbias_tse conjoint" >/dev/null 2>&1
done
