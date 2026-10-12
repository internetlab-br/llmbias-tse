"""Vigia das estações: religa o que caiu, e chama humano no que não dá para religar.

Duas coisas derrubam uma estação, e elas pedem respostas opostas:

- o runner morreu (container reiniciado, script editado por baixo, kill) —
  religar resolve, e ninguém precisa ser acordado para isso;
- a plataforma interpôs verificação humana, pediu login de novo ou trocou a
  UI — religar só queima conversas do plano, e o certo é PARAR e chamar.

Sem alguém olhando, as duas viram a mesma coisa: progresso que não anda. O
vigia separa, age no primeiro caso e declara o segundo no painel.

Religar é seguro de repetir: o `roda.sh` tem `flock`, então uma segunda
instância sai sozinha em vez de dirigir o mesmo Chrome em paralelo.

    uv run python infra/local/vigia.py                 # uma passada
    uv run python infra/local/vigia.py --loop 180      # a cada 3 min
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import time
from pathlib import Path

RUNNER = r"^bash /tmp/roda-ativo"
CONJOINT = r"^/home/coleta/venv/bin/python -m llmbias_tse conjoint"


def _sh(cmd: list[str], timeout: float = 60) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _exec(nome: str, script: str, timeout: float = 90) -> str:
    return _sh(["docker", "exec", nome, "bash", "-lc", script], timeout)


def estacoes() -> list[str]:
    nomes = _sh(["docker", "ps", "--format", "{{.Names}}"]).split()
    return sorted(n for n in nomes if n.startswith("coleta-")
                  and n not in ("coleta-caddy", "coleta-painel"))


def conta_proc(nome: str, padrao: str) -> int:
    out = _exec(nome, f"ps -eo cmd | grep -cE '{padrao}'")
    try:
        return int(out.splitlines()[-1])
    except Exception:
        return 0


def religar(nome: str) -> bool:
    """Sobe o runner de uma CÓPIA em caminho único.

    Cópia porque o bash lê o script do disco por offset enquanto executa;
    caminho único porque copiar por cima de uma cópia em uso corrompe igual.
    """
    # `-e EIXOS_SEGUINTES`: o runner lê a fila do arquivo de controle quando
    # ela existe, mas uma estação que ainda não avançou não a tem gravada. Sem
    # repassar aqui, o vigia religava com a fila VAZIA e a estação encerrava
    # em "alvo atingido" em vez de emendar o gênero.
    import os as _os
    seg = _os.environ.get("EIXOS_SEGUINTES", "genero")
    _sh(["docker", "exec", "-d", "-e", f"EIXOS_SEGUINTES={seg}", nome,
         "bash", "-lc",
         'A=/tmp/roda-ativo.$(date +%s).$$.sh; '
         'cp /app/infra/local/roda.sh "$A" && chmod +x "$A" && cd /app && '
         'nohup setsid "$A" >> /dados/runner.$SESSAO.log 2>&1 &'])
    time.sleep(6)
    return conta_proc(nome, RUNNER) >= 1


def verificacao(nome: str) -> str | None:
    """Rótulo do modal de verificação humana na tela daquela estação."""
    # Python do venv, NÃO `uv run`: `uv run` toma o lock do ambiente que a
    # coleta está usando e pode reescrevê-lo — sondar não pode arriscar o que
    # está sendo sondado.
    out = _exec(nome, 'cd /app && /home/coleta/venv/bin/python -c "'
                      'from patchright.sync_api import sync_playwright;'
                      'from llmbias_tse import capture;'
                      'pw=sync_playwright().start();'
                      'b=pw.chromium.connect_over_cdp(\\"http://localhost:9333\\");'
                      'print(\\"VERIF=\\" + str(capture.verificacao_humana('
                      'b.contexts[0].pages[0])))"')
    for linha in out.splitlines():
        if linha.startswith("VERIF="):
            v = linha[len("VERIF="):].strip()
            return None if v in ("None", "") else v
    return None


def chamar(porta: int, sessao: str, motivo: str) -> None:
    _sh(["curl", "-s", "-X", "POST", "-o", "/dev/null",
         f"http://127.0.0.1:{porta}/api/estacao/{sessao}/chamar",
         "--data-urlencode", f"motivo={motivo}", "-G"])


def passada(porta: int, log: Path) -> dict:
    agora = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    acoes = []
    for nome in estacoes():
        sessao = nome.removeprefix("coleta-")
        vivo = conta_proc(nome, RUNNER) >= 1
        coletando = conta_proc(nome, CONJOINT) >= 1
        if vivo and coletando:
            continue
        # Antes de religar, pergunta se o problema é humano. Religar por cima
        # de um CAPTCHA só gasta conversas do plano com resposta vazia.
        v = verificacao(nome)
        if v:
            chamar(porta, sessao, f"{v} — resolva pela tela e clique em resolvido")
            acoes.append({"sessao": sessao, "acao": "chamou_humano", "motivo": v})
            continue
        if not vivo:
            ok = religar(nome)
            acoes.append({"sessao": sessao,
                          "acao": "religou" if ok else "falhou_ao_religar"})
        else:
            # Runner vivo sem lote em curso é normal entre lotes; só registra.
            acoes.append({"sessao": sessao, "acao": "entre_lotes"})
    reg = {"em": agora, "acoes": acoes}
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    for a in acoes:
        print(f"[vigia {agora}] {a['sessao']}: {a['acao']}"
              + (f" ({a.get('motivo')})" if a.get("motivo") else ""), flush=True)
    return reg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8090)
    ap.add_argument("--log", default="data/experimento_2026_09/vigia.jsonl")
    ap.add_argument("--loop", type=int, default=0,
                    help="segundos entre passadas; 0 = uma passada só")
    args = ap.parse_args()
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    while True:
        passada(args.porta, log)
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
