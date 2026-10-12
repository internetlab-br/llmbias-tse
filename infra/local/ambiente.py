"""Registra o ambiente da coleta, estação por estação, para o relatório.

O que a rodada precisa poder afirmar depois: de qual IP as chamadas saíram,
por qual operadora, em que máquina, com qual versão do Chrome e qual conta.
Sem isso, "a plataforma respondeu assim" fica sem o contexto que decide se a
observação vale para um usuário comum — plataformas tratam IP residencial e
IP de nuvem de forma diferente, e é justamente a diferença que muda CAPTCHA,
verificação de segurança e limite de uso.

Escreve `<run_dir>/ambiente.json`. Fica em `data/`, que não é versionado: o
repositório é público e o IP da coleta é da máquina de quem coletou.

    uv run python infra/local/ambiente.py
    uv run python infra/local/ambiente.py --run-dir data/experimento_2026_09
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import platform
import subprocess
from pathlib import Path


def _no_container(nome: str, script: str) -> str:
    try:
        r = subprocess.run(
            ["docker", "exec", nome, "bash", "-lc", script],
            capture_output=True, text=True, timeout=60)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"ERRO: {e!r}"


def _estacoes() -> list[str]:
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                       capture_output=True, text=True)
    nomes = [n for n in r.stdout.split() if n.startswith("coleta-")]
    return sorted(n for n in nomes if n not in ("coleta-caddy", "coleta-painel"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)

    estacoes = {}
    for c in _estacoes():
        sessao = c.removeprefix("coleta-")
        # Medido de dentro do container, que compartilha a pilha de rede do
        # Chrome daquela estação. Medir do host provaria outra coisa.
        # Python do venv, NÃO `uv run` (ver o comentário em vigia.py).
        bruto = _no_container(
            c, "cd /app && /home/coleta/venv/bin/python -c "
               "\"from llmbias_tse import rede; import json; "
               "print(json.dumps(rede.ip_saida()))\"")
        try:
            r = json.loads(bruto.splitlines()[-1])
        except Exception:
            r = None
        estacoes[sessao] = {
            "ip": (r or {}).get("ip"),
            "org": (r or {}).get("org"),
            "conta": _no_container(c, "echo -n \"$LLMBIAS_CONTA\"") or None,
            "chrome": _no_container(
                c, "google-chrome --version 2>/dev/null | head -1"),
            "erro": None if r else bruto[:200],
        }
        print(f"  {sessao:24s} {str(estacoes[sessao]['ip']):16s} "
              f"{estacoes[sessao]['org'] or ''}")

    ips = collections.Counter(e["ip"] for e in estacoes.values() if e["ip"])
    reg = {
        "medido_em": datetime.datetime.now(
            datetime.timezone.utc).astimezone().isoformat(),
        "maquina": platform.node(),
        "sistema": f"{platform.system()} {platform.release()}",
        "ips_de_saida": dict(ips),
        # Uma rodada com mais de um IP de saída não é erro (rota que mudou,
        # VPN que subiu), mas tem de ser declarada: o IP é um fator que afeta
        # o que se mede, e virou coluna da base por isso.
        "ip_unico": len(ips) == 1,
        "estacoes": estacoes,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    alvo = run_dir / "ambiente.json"
    alvo.write_text(json.dumps(reg, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\nIPs de saída: {dict(ips)}")
    print(f"único: {reg['ip_unico']}")
    print(f"escrito em {alvo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
