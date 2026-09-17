# Rodada 2 — 24 sessões (8 plataformas × 3 contas)

Cada sessão é uma estação: conta própria, perfil de Chrome próprio, tela remota
própria e arquivo de controle próprio. O id da sessão é `plataforma.cN`.

## O desenho: fatiar PERFIS, não eixos

Cada conta roda os **três eixos** numa **fatia dos perfis** (`--fatia i/3`).

Fatiar por eixo seria mais simples, mas colaria a conta no eixo: toda conversa
de `voto` viria de uma conta e toda de `genero` de outra, e os dois efeitos não
se separariam. Na rodada 1 o efeito de conta no Meta AI foi grande — a recusa
evasiva "Boa pergunta, acesse tse.jus.br" apareceu em 69% dos turnos de uma
conta e em **0%** dos 804 turnos de outra.

Fatiando por perfil, a conta varia **dentro** de cada eixo. E três propriedades
saem de graça:

- **sem colisão**: as fatias são disjuntas por construção (ordena por id,
  reparte por resto), sem as máquinas combinarem nada entre si;
- **balanceamento garantido**: cada fatia faz todos os eixos dos perfis dela,
  então nenhum eixo pode terminar com mais conversas que outro. Não depende de
  ninguém monitorar;
- **resume intacto**: a conversa continua indexada por
  `{plataforma}_{perfil}_{eixo}`.

## Registrar a conta

Cada estação declara a conta logada em `LLMBIAS_CONTA`, e isso vai gravado no
campo `conta` de **cada conversa**. No `.env`:

```sh
CONTA_whatsapp_metaai_c1=esim_A
CONTA_whatsapp_metaai_c2=esim_B
CONTA_whatsapp_metaai_c3=esim_C
```

Sem isso a conta só se reconstrói por timestamp depois — foi o que precisou ser
feito na rodada 1. `google_aimode` não faz login: deixe vazio.

## Subir

```sh
# 1. SESSOES no .env (o painel lê daqui)
cat sessoes.txt >> .env          # ou cole a linha SESSOES=...

# 2. subir as 24 + painel + caddy
docker compose -f compose.24.yaml up -d

# 3. trocar as rotas do Caddy para as 24 telas
cp Caddyfile.24 Caddyfile && docker compose -f compose.24.yaml restart caddy
```

Regenerar os dois arquivos depois de mexer no `compose.yaml` base:
`python3 gera_24.py`.

## Login: 24 vezes, uma por sessão

As estações **não** começam a coletar sozinhas (`AUTO_INICIAR=0`). Cada uma
precisa de login pela tela remota:

```
http://<host>:8090/vnc/gemini.voto/
http://<host>:8090/vnc/gemini.genero/
...
```

Três contas por plataforma: uma por eixo. Qual conta em qual sessão é decisão
da equipe — registre, porque a conta é variável de incômodo (na rodada 1 o
comportamento do Meta AI variou muito entre contas).

## Capacidade medida (17/09/2026, terranave: 16 cores, 62 GB)

| | |
|---|---|
| Por estação | 375 MB em média, 1,2 GB no pico (WhatsApp) |
| 24 estações | 8,8 GB em média, 29 GB no pior caso |
| CPU | ~1% por estação coletando |

A máquina aguenta. O gargalo esperado é a **cota do Gemini**: 24 sessões fazem
~24 chamadas/min do agente-usuário. Por isso a coleta usa `GEMINI_API_KEY2`,
separada da chave de uso geral — um rate limit da coleta não derruba o resto.
O painel conta `rate_limits_24h` por sessão, então isso aparece em vez de
degradar em silêncio.

## Voltar ao modo da rodada 1

Sem `SESSOES` (e sem `SESSAO` nas estações), tudo se comporta como antes: uma
estação por plataforma rodando os três eixos em série, com `compose.yaml`.
