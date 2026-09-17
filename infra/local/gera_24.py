"""Gera compose.24.yaml e Caddyfile.24 — uma estação por (plataforma, eixo).

24 sessões = 8 plataformas x 3 eixos. Cada uma tem conta, perfil de Chrome,
tela remota e arquivo de controle próprios; o que compartilham é o plano (mesmo
estímulo) e o diretório de dados.

Por que partir por EIXO e não rodar 3 contas no mesmo eixo: o resume indexa a
conversa por `{plataforma}_{perfil}_{eixo}`, então duas sessões no mesmo eixo
disputariam as mesmas conversas — trabalho duplicado e corrida de escrita.
"""
from pathlib import Path

PLATAFORMAS = ["gemini", "chatgpt", "claude", "grok", "deepseek",
               "copilot", "google_aimode", "whatsapp_metaai"]
EIXOS = ["voto", "genero", "integridade"]
N_FATIAS = 3
# Sessão = (plataforma, CONTA). Cada conta roda os TRÊS eixos numa fatia dos
# perfis, para a conta variar DENTRO de cada eixo. Se cada conta pegasse um
# eixo, a conta ficaria colada no eixo e os dois efeitos não se separariam —
# e na rodada 1 o efeito de conta no Meta AI foi grande (recusa evasiva em 69%
# dos turnos de uma conta e 0% de outra).
SESSOES = [f"{p}.c{i}" for p in PLATAFORMAS for i in range(1, N_FATIAS + 1)]

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
      EIXOS: {" ".join(EIXOS)}
      FATIA: {i}/{N_FATIAS}
      SESSAO: {s}
      N_FATIAS: {N_FATIAS}
      # Declare aqui QUAL conta está logada nesta estação; vai gravado em
      # cada conversa. google_aimode não faz login, então fica vazio.
      LLMBIAS_CONTA: ${{CONTA_{s.replace(".","_")}:-}}
    volumes:
      - ../../:/app
      - ../../data:/dados
      - ./perfis/{s}:/perfil""")

# painel e caddy: copiados do compose de 8, com SESSOES no lugar de PLATAFORMAS
cauda = base.split("  painel:")[1]
painel = ("  painel:" + cauda).replace(
    "PLATAFORMAS: ${PLATAFORMAS:-", "SESSOES: ${SESSOES:-")

Path("compose.24.yaml").write_text(
    cabeca + "services:\n" + "\n\n".join(servicos) + "\n\n" + painel)

rotas = "\n".join(
    f"\thandle_path /vnc/{s}/* {{\n\t\treverse_proxy coleta-{s}:6080\n\t}}"
    for s in SESSOES)
cad = Path("Caddyfile").read_text()
ini = cad.index("\thandle_path /vnc/")
fim = cad.rindex("\t}") + 2
Path("Caddyfile.24").write_text(cad[:ini] + rotas + cad[fim:])

print(f"  compose.24.yaml: {len(SESSOES)} estações")
print(f"  Caddyfile.24   : {len(SESSOES)} rotas de VNC")
print(f"  SESSOES={' '.join(SESSOES[:3])} ... ({len(SESSOES)} no total)")
