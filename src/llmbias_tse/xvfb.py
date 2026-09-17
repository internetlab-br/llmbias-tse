"""Servidor X dedicado (Xvfb) para o Chrome da coleta.

Por que isto existe: numa sessão remota Wayland com GPU NVIDIA
(gnome-remote-desktop no Ubuntu 24.04), mapear a janela do Chrome derruba o
`gnome-shell` inteiro. É SIGSEGV em `meta_context_get_backend()` chamado de
`meta_wayland_egl_stream_new()`, com `buffer->compositor` NULL. O bug é do
mutter (Launchpad #2127249), não do Chrome: desligar a GPU do lado do
navegador não adianta, porque quem quebra é o compositor ao realizar o
buffer da janela nova. Na prática, cada `launch` derrubava a sessão RDP.

A saída é não mapear janela nenhuma na sessão do GNOME: o Chrome ganha um
servidor X só dele (Xvfb), invisível para o mutter. De quebra, a coleta
deixa de depender de ter sessão remota aberta e pode rodar com ninguém
conectado.

Para o login manual do `launch`, um `x11vnc` preso ao localhost expõe esse
display; você chega nele pelo mesmo túnel SSH que já usa para o RDP.

Variáveis de ambiente:
    LLMBIAS_XVFB      auto (padrão) | 1 | 0. `0` usa a sessão gráfica atual.
    LLMBIAS_SCREEN    resolução do display virtual (padrão 1280x900).
    LLMBIAS_VNC_PORT  porta do x11vnc no localhost (padrão 5901).
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SCREEN = os.environ.get("LLMBIAS_SCREEN", "1280x900")
VNC_PORT = int(os.environ.get("LLMBIAS_VNC_PORT", "5901"))

_MODO = os.environ.get("LLMBIAS_XVFB", "auto").strip().lower()
_NAO = {"0", "false", "no", "nao", "não", "off"}
_SIM = {"1", "true", "yes", "sim", "on"}

# Longe de :0/:1, que são das sessões de verdade (Xorg/Xwayland).
_FAIXA_DISPLAY = range(77, 100)

# Sem gerenciador de janelas o Chrome abre sem decoração e popups de login
# (OAuth, QR do WhatsApp) podem nascer sem foco. Qualquer um destes resolve;
# nenhum é obrigatório.
_GERENCIADORES = ["openbox", "matchbox-window-manager", "i3", "fluxbox"]


def usar_xvfb() -> bool:
    """Decide se o Chrome vai para um Xvfb próprio.

    `LLMBIAS_XVFB=0` força a sessão gráfica atual (útil num desktop local,
    quando você quer ver a janela de verdade). `=1` exige o Xvfb e falha se
    não houver. O padrão (`auto`) usa o Xvfb sempre que ele estiver
    instalado, no Linux.
    """
    if _MODO in _NAO:
        return False
    if not sys.platform.startswith("linux"):
        return False
    if _MODO in _SIM:
        return True
    return shutil.which("Xvfb") is not None


def _display_livre() -> int:
    for n in _FAIXA_DISPLAY:
        if Path(f"/tmp/.X11-unix/X{n}").exists() or Path(f"/tmp/.X{n}-lock").exists():
            continue
        return n
    raise RuntimeError(
        f"nenhum display X livre em :{_FAIXA_DISPLAY.start}-{_FAIXA_DISPLAY.stop - 1}"
    )


def _cookie_xauth(numero: int) -> str:
    """Cria um arquivo de autorização só para este display.

    Sem isto, o socket do Xvfb aceita qualquer processo local. O `-nolisten
    tcp` já tira a rede da jogada; o cookie fecha o resto.
    """
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    arq = base / f"llmbias-Xauthority.{numero}"
    arq.unlink(missing_ok=True)
    arq.touch(mode=0o600)
    subprocess.run(
        ["xauth", "-f", str(arq), "add", f":{numero}", ".", secrets.token_hex(16)],
        check=True,
        capture_output=True,
    )
    return str(arq)


@dataclass
class DisplayVirtual:
    """Um Xvfb vivo, mais os processos auxiliares presos a ele."""

    numero: int
    xauthority: str
    vnc_port: int | None = None
    _procs: list[subprocess.Popen] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f":{self.numero}"

    def env(self) -> dict[str, str]:
        """Ambiente para processos que devem enxergar este display."""
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        env["XAUTHORITY"] = self.xauthority
        # WAYLAND_DISPLAY herdado faria o Chrome preferir falar Wayland com o
        # mutter, que é exatamente o que estamos evitando aqui.
        env.pop("WAYLAND_DISPLAY", None)
        return env

    def encerrar(self) -> None:
        for proc in reversed(self._procs):
            if proc.poll() is None:
                proc.terminate()
        prazo = time.monotonic() + 5
        for proc in reversed(self._procs):
            try:
                proc.wait(timeout=max(0.0, prazo - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
        self._procs.clear()
        Path(self.xauthority).unlink(missing_ok=True)


def _esperar_socket(proc: subprocess.Popen, numero: int, prazo_s: float = 10.0) -> None:
    socket_x = Path(f"/tmp/.X11-unix/X{numero}")
    limite = time.monotonic() + prazo_s
    while time.monotonic() < limite:
        if socket_x.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"Xvfb morreu ao subir (código {proc.returncode})")
        time.sleep(0.2)
    raise TimeoutError(f"Xvfb não abriu o socket de :{numero} em {prazo_s:.0f}s")


def _silencioso() -> dict:
    return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def iniciar(com_vnc: bool = False) -> DisplayVirtual:
    """Sobe um Xvfb (e, se pedido, o x11vnc) e devolve o display."""
    if not shutil.which("Xvfb"):
        raise FileNotFoundError(
            "Xvfb não encontrado. Instale com:\n"
            "    sudo apt install xvfb x11vnc\n"
            "Ou use a sessão gráfica atual com LLMBIAS_XVFB=0 (atenção: em "
            "sessão remota Wayland com NVIDIA isso derruba o gnome-shell)."
        )

    numero = _display_livre()
    xauth = _cookie_xauth(numero)
    virtual = DisplayVirtual(numero=numero, xauthority=xauth)

    print(f"[display] Xvfb em :{numero} ({SCREEN})")
    xvfb = subprocess.Popen(
        [
            "Xvfb",
            f":{numero}",
            "-screen",
            "0",
            f"{SCREEN}x24",
            "-nolisten",
            "tcp",
            "-auth",
            xauth,
        ],
        **_silencioso(),
    )
    virtual._procs.append(xvfb)
    try:
        _esperar_socket(xvfb, numero)
        gerenciador = next(
            (c for c in (shutil.which(g) for g in _GERENCIADORES) if c), None
        )
        if gerenciador:
            print(f"[display] Gerenciador de janelas: {Path(gerenciador).name}")
            virtual._procs.append(
                subprocess.Popen([gerenciador], env=virtual.env(), **_silencioso())
            )
        if com_vnc:
            _iniciar_vnc(virtual)
    except Exception:
        virtual.encerrar()
        raise
    return virtual


def _iniciar_vnc(virtual: DisplayVirtual) -> None:
    x11vnc = shutil.which("x11vnc")
    if not x11vnc:
        print("[display] AVISO: x11vnc ausente, seguindo sem acesso visual.")
        print("[display]        sudo apt install x11vnc")
        return
    virtual._procs.append(
        subprocess.Popen(
            [
                x11vnc,
                "-display",
                virtual.display,
                "-auth",
                virtual.xauthority,
                # Só o localhost: quem chega de fora tem que passar pelo
                # túnel SSH, que já é autenticado por chave.
                "-localhost",
                "-rfbport",
                str(VNC_PORT),
                "-nopw",
                "-forever",
                "-shared",
                "-noxdamage",
                "-quiet",
            ],
            **_silencioso(),
        )
    )
    virtual.vnc_port = VNC_PORT
    print(f"[display] x11vnc escutando em 127.0.0.1:{VNC_PORT}")
