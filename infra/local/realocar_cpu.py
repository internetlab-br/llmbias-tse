"""Reparte a CPU entre as estações em proporção ao que FALTA a cada uma.

Para rodar pelo cron a cada 10 minutos:

    */10 * * * * cd /caminho/do/repo && /snap/bin/uv run python infra/local/realocar_cpu.py

Por que existe: em 12/10/2026 a máquina desligou quatro vezes por
temperatura com as 16 estações sem limite de CPU. A saída foi limitar todas
a 0,6 núcleo (ChatGPT a 2,0), o que segurou a CPU em ~60 °C. Mas um teto fixo
desperdiça o orçamento no fim da rodada: estação que já fechou a fatia segue
com 0,6 núcleo parado, e a atrasada continua presa no mesmo 0,6. Aqui o
ORÇAMENTO total fica fixo (o que já se mostrou seguro) e só a divisão muda.

Regras:
- falta de cada estação = ALVO - COMPLETAS da fatia dela (`progresso.py`);
- estação concluída ou pausada fica com o mínimo (o Chrome segue aberto);
- as demais dividem o resto do orçamento em proporção ao que falta, entre
  MIN e MAX por estação; o que sobra do teto MAX é redistribuído;
- FREIO TÉRMICO: com a CPU >= 78 °C o orçamento cai 25%; não se mexe em nada
  para cima enquanto estiver quente.

Cada decisão vai para `data/<RUN_ID>/realocacao_cpu.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
REPO = AQUI.parent.parent


def ler_env(chave: str) -> str:
    for linha in (AQUI / ".env").read_text().splitlines()[::-1]:
        if linha.startswith(f"{chave}="):
            return linha.split("=", 1)[1].strip()
    return ""


def temperatura() -> int | None:
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            if (z / "type").read_text().strip() == "x86_pkg_temp":
                return int((z / "temp").read_text()) // 1000
        except OSError:
            pass
    return None


def estacoes() -> list[str]:
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                         capture_output=True, text=True, check=True).stdout
    return sorted(n[len("coleta-"):] for n in out.split()
                  if n.startswith("coleta-") and "." in n)


def falta(run_dir: Path, sessao: str, eixos: list[str]) -> tuple[int, str]:
    plat, conta = sessao.rsplit(".", 1)
    i = int(conta.lstrip("c"))
    r = subprocess.run(
        [sys.executable, str(AQUI / "progresso.py"), plat, "--run-dir", str(run_dir),
         "--eixos", *eixos, "--fatia", f"{i}/2"],
        capture_output=True, text=True, cwd=REPO)
    m = re.search(r"COMPLETAS=(\d+) ALVO=(\d+).*?ESTADO=(\S+)", r.stdout)
    if not m:
        return 0, "?"
    return max(int(m[2]) - int(m[1]), 0), m[3]


# Piso de quem ainda coleta, por plataforma. O ChatGPT é a interface mais
# pesada: em 12/10, a 0,6 núcleo a mediana do turno foi de 71 s para 111 s, e
# só voltou ao normal (55 s) a partir de 1,2.
PISO_ATIVA = {"chatgpt": 1.2}


def repartir(faltas: dict[str, int], orcamento: float, minimo: float,
             maximo: float) -> dict[str, float]:
    cotas = {s: (max(minimo, PISO_ATIVA.get(s.rsplit(".", 1)[0], 0))
                 if f > 0 else minimo) for s, f in faltas.items()}
    ativas = {s: f for s, f in faltas.items() if f > 0}
    resto = orcamento - sum(cotas.values())
    # Enche em proporção ao que falta; quem bate no teto sai e o excedente
    # volta para as outras.
    while ativas and resto > 1e-6:
        tot = sum(ativas.values())
        cheias = {}
        for s, f in ativas.items():
            extra = resto * f / tot
            if cotas[s] + extra >= maximo:
                cheias[s] = maximo - cotas[s]
        if not cheias:
            for s, f in ativas.items():
                cotas[s] += resto * f / tot
            break
        for s, d in cheias.items():
            cotas[s] = maximo
            resto -= d
            del ativas[s]
    return {s: round(c, 2) for s, c in cotas.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orcamento", type=float, default=12.4,
                    help="núcleos somados para todas as estações (padrão: o "
                         "que já rodou em ~60 °C em 12/10)")
    ap.add_argument("--min", type=float, default=0.5)
    ap.add_argument("--max", type=float, default=3.0)
    ap.add_argument("--quente", type=int, default=78)
    ap.add_argument("--simular", action="store_true")
    a = ap.parse_args(argv)

    run_id = ler_env("RUN_ID")
    run_dir = REPO / "data" / run_id
    eixos = ["voto", "integridade"]  # eixos da fase em curso da rodada 3
    temp = temperatura()
    orcamento = a.orcamento * (0.75 if temp and temp >= a.quente else 1.0)

    faltas, estados = {}, {}
    for s in estacoes():
        f, estado = falta(run_dir, s, eixos)
        ctrl = run_dir / "control" / f"{s}.json"
        try:
            pedido = json.loads(ctrl.read_text()).get("estado")
        except Exception:
            pedido = None
        estados[s] = pedido or estado
        # Pausada ou à espera de humano: não está gastando CPU de coleta.
        faltas[s] = 0 if estados[s] in ("pausado", "parado", "precisa_humano") else f

    cotas = repartir(faltas, orcamento, a.min, a.max)
    quente = bool(temp and temp >= a.quente)
    for s, c in cotas.items():
        atual = None
        r = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}",
                            f"coleta-{s}"], capture_output=True, text=True)
        if r.stdout.strip().isdigit():
            atual = int(r.stdout.strip()) / 1e9
        if quente and atual is not None and c > atual:
            cotas[s] = c = atual  # quente: nunca sobe
        if not a.simular and atual is not None and abs(c - atual) >= 0.05:
            subprocess.run(["docker", "update", "--cpus", str(c), f"coleta-{s}"],
                           capture_output=True)

    reg = {"em": datetime.now().astimezone().isoformat(timespec="seconds"),
           "temp": temp, "orcamento": orcamento, "simulado": a.simular,
           "estacoes": {s: {"falta": faltas[s], "estado": estados[s],
                            "cpus": cotas[s]} for s in cotas}}
    if not a.simular:
        with open(run_dir / "realocacao_cpu.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(reg, ensure_ascii=False) + "\n")
    print(json.dumps(reg, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
