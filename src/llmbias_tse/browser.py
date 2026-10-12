"""Lança o Google Chrome real numa porta de debug (CDP) com perfil
persistente, e conecta o Patchright a ele.

Padrão herdado do projeto monitor-italia: o Chrome roda como processo
separado, numa porta (`--remote-debugging-port`), com `--user-data-dir`
dedicado. Assim os cookies/sessões **persistem** — você loga **uma vez**
em cada ferramenta (ChatGPT, Gemini, Claude, Meta AI, WhatsApp Web) e o
script conecta na aba já logada quantas vezes quiser, sem relançar o
browser nem refazer login.

Fluxo:
    1. `launch_chrome()`  -> abre o Chrome na porta CDP (9333 por padrão).
    2. (manual) você faz login em cada ferramenta.
    3. `connect()`        -> Patchright conecta via CDP e devolve o context.

No Linux o Chrome não abre na sessão gráfica: ele ganha um Xvfb só dele, e o
login do passo 2 é feito por VNC. O motivo está em `xvfb.py`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import xvfb

# Diretório do perfil persistente (cookies, logins). Fora do versionamento.
PROFILE_DIR = Path(os.environ.get("LLMBIAS_PROFILE", "./tmp/profile")).resolve()
# 9333 por padrão para coexistir com outros projetos que usam 9222.
CDP_PORT = int(os.environ.get("LLMBIAS_CDP_PORT", "9333"))
CDP_URL = f"http://localhost:{CDP_PORT}"

# Caminhos comuns do Chrome por plataforma; sobrescreva com CHROME_PATH.
_CHROME_BY_PLATFORM = {
    "win32": [
        rf"{os.environ.get('ProgramFiles', '')}\Google\Chrome\Application\chrome.exe",
        rf"{os.environ.get('ProgramFiles(x86)', '')}\Google\Chrome\Application\chrome.exe",
        rf"{os.environ.get('LOCALAPPDATA', '')}\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ],
}

# Nomes procurados no PATH (cobre instalações fora dos caminhos padrão,
# incl. flatpak/asdf/nix e o `google-chrome` de distros diversas).
_CHROME_ON_PATH = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
]


def _chrome_candidates() -> list[str]:
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    cands = [os.environ.get("CHROME_PATH"), *_CHROME_BY_PLATFORM.get(plat, [])]
    return [c for c in cands if c]


def find_chrome() -> str:
    for cand in _chrome_candidates():
        if Path(cand).exists():
            return cand
    for name in _CHROME_ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "Google Chrome não encontrado. Instale o Chrome ou defina "
        "CHROME_PATH=/caminho/para/chrome"
    )


def _xauthority_do_xwayland(display: str) -> str | None:
    """Cookie X do Xwayland que está servindo `display` agora.

    Numa sessão Wayland (GNOME), cada sessão gráfica sobe um Xwayland com um
    arquivo de autorização novo em $XDG_RUNTIME_DIR. Um terminal aberto antes
    de reiniciar a sessão (reconexão remota, logout/login) fica com o
    XAUTHORITY da sessão velha; o arquivo antigo continua existindo, mas a
    chave não vale mais e o Chrome morre com "Missing X server or $DISPLAY"
    (precedido de "Invalid MIT-MAGIC-COOKIE-1 key"). Por isso não basta
    checar se o caminho existe: lemos o `-auth` do processo vivo.
    """
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().decode().split("\0")
        except OSError:
            continue  # processo morreu no meio da varredura
        if not args or not args[0].endswith("Xwayland") or display not in args:
            continue
        if "-auth" in args and args.index("-auth") + 1 < len(args):
            return args[args.index("-auth") + 1]
    return None


def _chrome_env() -> dict[str, str]:
    """Ambiente para o Chrome, com o XAUTHORITY corrigido quando obsoleto."""
    env = dict(os.environ)
    display = env.get("DISPLAY")
    if not sys.platform.startswith("linux") or not display:
        return env
    atual = _xauthority_do_xwayland(display)
    if atual and atual != env.get("XAUTHORITY"):
        print(f"[browser] XAUTHORITY da sessão atual: {atual}")
        env["XAUTHORITY"] = atual
    return env


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class Sessao:
    """O Chrome da coleta e, quando existe, o display virtual que o hospeda.

    Guardar os dois juntos é o que permite ao `launch` derrubar tudo de uma
    vez ao sair, sem deixar Xvfb ou x11vnc órfãos.
    """

    chrome: subprocess.Popen | None
    display: xvfb.DisplayVirtual | None = None

    @property
    def reaproveitada(self) -> bool:
        """True quando encontramos um Chrome já rodando e não subimos nada."""
        return self.chrome is None

    def esperar(self) -> None:
        if self.chrome is not None:
            self.chrome.wait()
            return
        while True:  # Chrome é de outro processo; só seguramos o terminal
            time.sleep(60)

    def encerrar(self) -> None:
        if self.chrome is not None and self.chrome.poll() is None:
            self.chrome.terminate()
            try:
                self.chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.chrome.kill()
        if self.display is not None:
            self.display.encerrar()


def launch_chrome(wait_ready: bool = True, com_vnc: bool = False) -> Sessao:
    """Abre o Chrome na porta CDP com o perfil persistente.

    Se a porta já estiver aberta (Chrome já rodando), não faz nada.

    No Linux o Chrome vai para um Xvfb dedicado por padrão, em vez da sessão
    gráfica atual: mapear janela numa sessão remota Wayland com NVIDIA
    derruba o gnome-shell inteiro. O porquê está em `xvfb.py`. Para voltar ao
    comportamento antigo, `LLMBIAS_XVFB=0`.
    """
    if _port_open(CDP_PORT):
        print(f"[browser] Chrome já está na porta {CDP_PORT} — reutilizando.")
        return Sessao(chrome=None)

    chrome = find_chrome()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    virtual = xvfb.iniciar(com_vnc=com_vnc) if xvfb.usar_xvfb() else None
    env = virtual.env() if virtual is not None else _chrome_env()

    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # 1280x900 mantém o layout desktop das UIs, que é o que os seletores
        # em drivers.py assumem — não caia para largura de tablet/mobile.
        # (Este tamanho fixo entrou como tentativa de evitar a queda da sessão
        # remota. Não era essa a causa; ficou porque o layout importa.)
        "--window-size=1280,900",
        "--window-position=0,0",
        # Não estrangular abas em segundo plano/ocultas. Sem isto, o Chrome
        # atrasa os timers da página quando a janela não está visível, e a
        # detecção de fim-de-resposta (sumiço do botão de parar) trava até o
        # timeout. Com estas flags a coleta roda em velocidade total mesmo
        # com a janela atrás de outras.
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    print(f"[browser] Lançando Chrome: porta={CDP_PORT} perfil={PROFILE_DIR}")
    try:
        proc = subprocess.Popen(args, env=env)
    except Exception:
        if virtual is not None:
            virtual.encerrar()
        raise

    if wait_ready:
        for _ in range(40):  # ~20s
            if _port_open(CDP_PORT):
                print(f"[browser] CDP pronto em {CDP_URL}")
                break
            time.sleep(0.5)
        else:
            print("[browser] AVISO: porta CDP não respondeu a tempo.")
    return Sessao(chrome=proc, display=virtual)


def connect(playwright):
    """Conecta o Patchright ao Chrome via CDP e devolve (browser, context).

    `playwright` é o objeto retornado por `sync_playwright().start()` /
    `with sync_playwright() as playwright`.
    """
    if not _port_open(CDP_PORT):
        raise RuntimeError(
            f"Nada escutando em {CDP_URL}. Rode `uv run llmbias-tse launch` "
            "primeiro e faça login nas ferramentas."
        )
    browser = playwright.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, ctx


def context_for_session(browser, default_ctx, session: str):
    """Devolve o context para a sessão pedida.

    - "logged_in": o context persistente (perfil com seus logins).
    - "anon":      um context isolado novo (sem cookies = deslogado),
                   equivalente a uma janela anônima. O chamador deve
                   fechá-lo (`ctx.close()`) ao terminar.
    """
    if session == "anon":
        return browser.new_context()
    return default_ctx


def main_launch() -> int:
    """Entry point do subcomando `launch`: abre o Chrome e segura o terminal."""
    sessao = launch_chrome(com_vnc=True)
    virtual = sessao.display
    print()
    print("=" * 60)
    print("Chrome aberto com perfil persistente.")
    if virtual is not None and virtual.vnc_port:
        print()
        porta = virtual.vnc_port
        print(f"Ele está num display virtual ({virtual.display}), fora da sessão")
        print("gráfica. Para ver a tela e fazer login, na sua máquina rode")
        print(f"    ssh -N -L {porta}:127.0.0.1:{porta} nave   # ou `lan`, na rede local")
        print(f"e aponte o cliente VNC para localhost:{porta}.")
        print("(No Windows: llmbias-vnc, que faz as duas coisas.)")
    print()
    print("Faça login (uma vez) nas ferramentas que vai testar:")
    print("  - ChatGPT:   https://chatgpt.com")
    print("  - Gemini:    https://gemini.google.com")
    print("  - Claude:    https://claude.ai")
    print("  - Meta AI:   https://www.meta.ai")
    print("  - WhatsApp:  https://web.whatsapp.com  (escaneie o QR)")
    print()
    print("Depois, em outro terminal: uv run llmbias-tse run")
    print("Mantenha este terminal aberto (fecha o Chrome ao sair). Ctrl+C p/ sair.")
    print("=" * 60)
    try:
        sessao.esperar()
    except KeyboardInterrupt:
        print("\n[browser] Encerrando.")
    finally:
        sessao.encerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main_launch())
