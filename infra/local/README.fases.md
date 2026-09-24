# Rodada 2 em duas fases

A rodada não roda os três eixos de uma vez:

| fase | eixos | sessões | fatias |
|---|---|---|---|
| **A** | `voto` + `integridade` | **16** (8 plataformas × 2 contas) | `--fatia i/2` |
| **B** | `genero` | **8** (8 plataformas × 1 conta) | `--fatia 1/1` |

Cada sessão roda os eixos **da fase** numa **fatia dos perfis**, então a conta
varia DENTRO de cada eixo da fase, e o balanceamento entre os eixos da fase sai
por construção: cada fatia faz todos os eixos dela nos perfis dela, e as fatias
diferem em no máximo um perfil.

Com 100 perfis: fase A = 50 perfis × 2 eixos por fatia = **200 conversas por
plataforma**; fase B = 100 perfis × 1 eixo = **100 por plataforma**.

## Subir

```sh
python3 gera_fases.py a          # ou b
cat sessoes.fasea.txt >> .env    # SESSOES, N_FATIAS e EIXOS da fase
docker compose -f compose.fasea.yaml up -d
cp Caddyfile.fasea Caddyfile && docker compose -f compose.fasea.yaml restart caddy
```

Login em cada sessão pela tela remota: `http://<host>:8090/vnc/<plataforma>.cN/`.
Declare a conta logada no `.env` (`CONTA_gemini_c1=...`) — ela vai gravada no
campo `conta` de cada conversa.

## Ressalva de desenho da fase B

Na fase B há **uma conta por plataforma**, então dentro de `genero` **não há
variação de conta**: a conta fica constante por plataforma e os dois efeitos
não se separam nesse eixo. Na fase A, com duas contas por plataforma, a conta
varia dentro de `voto` e de `integridade`.

Isso importa porque na rodada 1 o efeito de conta foi grande no Meta AI (a
recusa evasiva apareceu em 69% dos turnos de uma conta e em 0% dos 804 de
outra). Duas saídas, se a equipe quiser fechar isso:

1. rodar a fase B em **duas ondas** nas mesmas 8 estações (`--fatia 1/2` com a
   conta A, depois `--fatia 2/2` com a conta B) — dobra o relógio, não o custo;
2. aceitar e **controlar na análise** pelo campo `conta`, que agora vem gravado
   em cada conversa.

## Como o painel sabe o alvo de cada sessão

O runner **declara** seus eixos no arquivo de controle (`marcar`), e o painel
calcula `alvo = perfis da fatia × eixos declarados`. Sem isso o alvo sairia com
os três eixos do plano e toda sessão da fase A apareceria eternamente atrasada
(50 × 3 = 150 em vez de 100).
