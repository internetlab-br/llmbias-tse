# Container de coleta: um por plataforma.
#
# Cada container é uma "estação" completa e isolada: tela virtual própria
# (Xvfb), acesso remoto próprio (x11vnc + noVNC), Chrome próprio com perfil
# persistente e o runner da coleta. Duas plataformas nunca compartilham
# perfil, porta CDP nem histórico.
#
# O que ele NÃO tem, e é a diferença central para o desenho da Azure: túnel.
# Rodando no host residencial (rede residencial), o egress do container já sai pelo
# IP de casa. O túnel SOCKS existia só para VM de nuvem tomar emprestado esse
# IP; aqui a peça mais frágil do sistema simplesmente não existe.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=pt_BR.UTF-8 \
    TZ=America/Sao_Paulo

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc x11-utils xdotool \
      novnc websockify \
      ca-certificates curl wget gnupg jq unzip tini \
      fonts-liberation fonts-noto-color-emoji language-pack-pt \
      python3 python3-venv tmux procps \
 && rm -rf /var/lib/apt/lists/*

# Google Chrome real (não Chromium): os seletores dos drivers foram escritos
# contra as UIs como o Chrome as renderiza, e algumas plataformas tratam
# Chromium de forma diferente.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Usuário sem privilégio: o Chrome roda com uma sessão logada em conta real,
# não há motivo para ser root.
#
# O UID vem de fora e precisa ser o MESMO do dono de `data/` no host: as
# conversas são escritas num bind mount, e um uid diferente do dono dá
# "permission denied" no meio da coleta. 1000 é o primeiro usuário na maioria
# das distribuições; confira com `id -u` e passe --build-arg UID=$(id -u).
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} coleta 2>/dev/null || true  && useradd -m -u ${UID} -g ${GID} coleta 2>/dev/null  || useradd -m -u ${UID} coleta
RUN mkdir -p /app /perfil /dados && chown -R coleta:coleta /app /perfil /dados

COPY entrypoint.sh roda.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/roda.sh

USER coleta
WORKDIR /app

# O venv fica DENTRO do container, e nao no repo montado: as estacoes
# compartilham o mesmo /app, e tres `uv sync` concorrentes no mesmo .venv se
# atropelam. Assim cada uma tem o seu e nenhuma mexe no ambiente do host.
ENV LLMBIAS_PROFILE=/perfil \
    LLMBIAS_CDP_PORT=9333 \
    DISPLAY=:77 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_PROJECT_ENVIRONMENT=/home/coleta/venv

EXPOSE 6080
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
