"""Gera o compose e o Caddyfile de cada FASE da rodada 2.

A rodada não roda os três eixos de uma vez:

  fase A — `voto integridade`, **16 sessões** (8 plataformas × 2 contas)
  fase B — `genero`,           **8 sessões**  (8 plataformas × 1 conta)

Cada sessão roda os eixos DA FASE numa fatia dos perfis (`--fatia i/n`, com
n = contas da fase), então a conta varia DENTRO de cada eixo da fase e o
balanceamento entre os eixos da fase sai por construção: cada fatia faz todos
os eixos dela nos perfis dela, e os tamanhos das fatias diferem em no máximo
um.

Na fase B há uma conta por plataforma, então dentro de `genero` NÃO há variação
de conta — ver a ressalva no README.
"""
import sys
from pathlib import Path

PLATAFORMAS = ["gemini", "chatgpt", "claude", "grok", "deepseek",
               "copilot", "google_aimode", "whatsapp_metaai"]
FASES = {
    "a": {"eixos": ["voto", "integridade"], "contas": 2},
    "b": {"eixos": ["genero"], "contas": 1},
}

fase = (sys.argv[1] if len(sys.argv) > 1 else "a").lower()
if fase not in FASES:
    raise SystemExit(f"fase inválida: {fase!r} (use {sorted(FASES)})")
cfg = FASES[fase]
n = cfg["contas"]
eixos = " ".join(cfg["eixos"])
SESSOES = [f"{p}.c{i}" for p in PLATAFORMAS for i in range(1, n + 1)]

base = Path("compose.yaml").read_text()
cabeca = base.split("services:")[0]
servicos = []
for s in SESSOES:
    plat, conta = s.rsplit(".", 1)
    i = int(conta[1:])
    servicos.append(f"""  coleta-{s}:
    <<: *estacao
    container_name: coleta-{s}
    environment:
      <<: *ambiente
      PLATAFORMA: {plat}
      EIXOS: {eixos}
      FATIA: {i}/{n}
      SESSAO: {s}
      N_FATIAS: {n}
      # Declare aqui QUAL conta está logada nesta estação; vai gravado em cada
      # conversa. google_aimode não faz login, então fica vazio.
      LLMBIAS_CONTA: ${{CONTA_{s.replace(".", "_")}:-}}
    volumes:
      - ../../:/app
      - ../../data:/dados
      - ./perfis/{s}:/perfil""")

cauda = base.split("  painel:")[1]
painel = ("  painel:" + cauda).replace(
    "PLATAFORMAS: ${PLATAFORMAS:-", "SESSOES: ${SESSOES:-")
Path(f"compose.fase{fase}.yaml").write_text(
    cabeca + "services:\n" + "\n\n".join(servicos) + "\n\n" + painel)

rotas = "\n".join(
    f"\thandle_path /vnc/{s}/* {{\n\t\treverse_proxy coleta-{s}:6080\n\t}}"
    for s in SESSOES)
cad = Path("Caddyfile").read_text()
ini = cad.index("\thandle_path /vnc/")
fim = cad.rindex("\t}") + 2
Path(f"Caddyfile.fase{fase}").write_text(cad[:ini] + rotas + cad[fim:])

Path(f"sessoes.fase{fase}.txt").write_text(
    f"SESSOES={' '.join(SESSOES)}\nN_FATIAS={n}\nEIXOS={eixos}\n")

for s in SESSOES:
    Path(f"perfis/{s}").mkdir(parents=True, exist_ok=True)

print(f"  fase {fase.upper()}: eixos='{eixos}', {n} conta(s)/plataforma")
print(f"  {len(SESSOES)} sessões -> compose.fase{fase}.yaml, "
      f"Caddyfile.fase{fase}, sessoes.fase{fase}.txt")
