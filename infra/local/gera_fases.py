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
      # Eixo a emendar quando as DUAS contas da plataforma fecharem o
      # alvo dos eixos atuais. Ver o laço de `roda.sh`.
      EIXOS_SEGUINTES: ${{EIXOS_SEGUINTES:-}}
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
# O painel precisa de N_FATIAS e EIXOS: sem N_FATIAS ele assume 3 e calcula a
# FATIA ERRADA (1/3 em vez de 1/2), mostrando progresso de outros perfis; sem
# EIXOS, a sessão que ainda não começou aparece com o alvo dos três eixos do
# plano em vez dos da fase.
painel = painel.replace(
    "      SESSOES: ${SESSOES:-",
    f"      N_FATIAS: \"{n}\"\n      EIXOS: {eixos}\n      SESSOES: ${{SESSOES:-")
# Sub-rede EXPLÍCITA: a máquina de coleta tem VPN com rotas para
# 172.16.0.0/12 e 192.168.0.0/16, que são justamente os dois pools default do
# Docker — ele recusa alocar por cima e falha com "all predefined address
# pools have been fully subnetted". Fixar aqui torna a subida independente do
# estado da VPN. (A VPN é split tunnel: o egresso para a internet continua
# saindo pelo IP residencial, que é o que as plataformas veem.)
rede = """
networks:
  default:
    ipam:
      config:
        - subnet: 10.42.0.0/16
"""
Path(f"compose.fase{fase}.yaml").write_text(
    cabeca + "services:\n" + "\n\n".join(servicos) + "\n\n" + painel + rede)

rotas = "\n".join(
    f"\thandle_path /vnc/{s}/* {{\n\t\treverse_proxy coleta-{s}:6080\n\t}}"
    for s in SESSOES)
# Substitui APENAS o bloco de rotas /vnc/, preservando o que vem depois — a
# rota `/` do painel vem DEPOIS delas, e recortar até o último `}` do arquivo
# a comia: o Caddy respondia 200 com corpo vazio, e o painel parecia no ar.
cad = Path("Caddyfile.base").read_text() if Path("Caddyfile.base").exists() \
    else Path("Caddyfile").read_text()
ini = cad.index("\thandle_path /vnc/")
ult = cad.rindex("\thandle_path /vnc/")
fim = cad.index("\t}", cad.index("reverse_proxy", ult)) + len("\t}\n")
Path(f"Caddyfile.fase{fase}").write_text(cad[:ini] + rotas + "\n" + cad[fim:])

Path(f"sessoes.fase{fase}.txt").write_text(
    f"SESSOES={' '.join(SESSOES)}\nN_FATIAS={n}\nEIXOS={eixos}\n")

for s in SESSOES:
    Path(f"perfis/{s}").mkdir(parents=True, exist_ok=True)

print(f"  fase {fase.upper()}: eixos='{eixos}', {n} conta(s)/plataforma")
print(f"  {len(SESSOES)} sessões -> compose.fase{fase}.yaml, "
      f"Caddyfile.fase{fase}, sessoes.fase{fase}.txt")
