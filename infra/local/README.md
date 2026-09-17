# Coleta multiplataforma num host só

Uma estação por plataforma de IA, todas num host só, com painel central e
acesso ao browser de cada uma pelo navegador.

Isto roda em **qualquer máquina numa conexão residencial** com Docker: um
desktop parado, um mini-PC, um servidor caseiro. O que ela precisa ter é RAM
(cerca de 2 GB por plataforma) e uma conexão que não seja de datacenter. No
nosso caso é um servidor com 16 cores e 62 GB, mas três plataformas cabem
folgadas em 8 GB.

## Por que não é na nuvem

O desenho anterior (`infra/azure/`) punha cada plataforma numa VM da Azure e
fazia o Chrome de lá sair pelo IP de casa através de um túnel SOCKS reverso.
O túnel existia por um motivo só: as plataformas bloqueiam IP de datacenter.
Ele foi também a peça que quebrou, quando o IP residencial mudou e derrubou a
coleta por dois dias.

Rodando numa máquina que **já está** numa conexão residencial, o egress já é
o IP certo. O túnel deixa de existir, e com ele somem a regra de NSG presa a
um IP, a chave SSH em cada nó, o serviço systemd por VM e a quota da Azure.
O que a nuvem dava e não se perde é o acesso remoto para o time: quem entrega
isso é o `cloudflared`, que expõe **entrada**, que é o que ele realmente faz.

Não desaparece o risco de todas as plataformas saírem pelo mesmo IP
residencial. Para distribuir, veja "Escalar para fora" no fim.

## Peças

```
navegador do time
      |
   cloudflared  (Cloudflare Access: quem pode entrar)
      |
    caddy :8080
      |------- /            -> painel (FastAPI)
      |------- /vnc/<plat>/ -> noVNC daquela estação
      |
  coleta-gemini   coleta-chatgpt   coleta-claude   ... (uma por plataforma)
   Xvfb + x11vnc + Chrome(CDP) + runner, perfil próprio em ./perfis/<plat>
      |
   /dados/<run_id>/   <- conversas, events.jsonl, control/
                         o painel lê ESTE diretório; não há protocolo no meio
```

| Arquivo | O que é |
|---|---|
| `Dockerfile` | imagem da estação: Chrome, Xvfb, x11vnc, noVNC, uv |
| `entrypoint.sh` | sobe tela virtual, VNC e Chrome; **não** começa a coletar sozinho |
| `roda.sh` | runner em lotes, obedecendo ao arquivo de controle do painel |
| `compose.yaml` | as 8 estações + painel + caddy |
| `Caddyfile` | entrada única: painel na raiz, telas em `/vnc/<plataforma>/` |
| `progresso.py` | progresso de uma plataforma em texto (usado pelo runner) |
| `dashboard/` | o painel (FastAPI + uma página) |

No pacote, e não aqui, ficam as duas peças que a coleta usa direto:
`llmbias_tse/events.py` (o barramento de eventos) e `llmbias_tse/status.py`
(a leitura do estado, compartilhada entre painel e runner).

## Subir

```bash
cd infra/local
mkdir -p perfis/gemini                     # antes do up: bind mount que nao
                                           # existe, o Docker cria como root
echo "UID=$(id -u)" >> .env                # o container escreve em data/ pelo
echo "GID=$(id -g)" >> .env                # bind mount: uid tem de casar
docker compose build
docker compose up -d caddy painel          # painel primeiro, para acompanhar
docker compose up -d coleta-gemini         # uma estação por vez, na primeira vez
```

Abra `http://localhost:8080`. A estação sobe **parada**: clique em
"abrir browser", logue na conta pela tela remota e só então dê play. Estação
nova sem login logaria erro em loop se começasse sozinha.

Variáveis (via `.env` ao lado do `compose.yaml`):

```bash
RUN_ID=experimento_2026_09       # diretório da rodada em data/
EIXOS=voto genero integridade
LOTE=20                          # conversas por lote
IP_ESPERADO=                     # trava de segurança; vazio desliga
PORTA=8080
```

`IP_ESPERADO` (descubra o seu com `curl -s https://api.ipify.org`) é o que
sobrou do fail-closed do desenho da Azure. Lá ele
protegia contra o túnel cair; aqui protege contra alguém subir este compose
numa VM de nuvem ou com VPN ligada e queimar as contas. O IP residencial muda
sozinho de tempos em tempos, então quando a estação abortar por isso,
confirme que é só o IP novo e atualize.

## Operar

O painel mostra, por estação: conversas completas sobre o alvo do plano,
ritmo por hora e ETA, incompletas, rate limits e erros nas últimas 24h, e há
quanto tempo saiu o último evento. Esse último campo é o que denuncia a
estação que diz "rodando" mas está parada.

No alto fica a fila de **precisa de humano**, que é a única coisa em cima da
qual alguém age. Uma estação entra na fila quando:

- o runner desistiu (3 lotes sem nenhuma conversa nova), ou
- houve um alerta (`bloqueio`, `chat_isolado_falhou`, `login_perdido`) que
  nenhuma conversa concluída depois desmentiu.

Sai da fila quando volta a produzir sozinha, ou quando alguém resolve e
clica em **resolvido**. Sem esse botão o cartão ficaria vermelho para sempre
depois do primeiro bloqueio, e painel que nunca fica verde ninguém lê.

Botões: `pausar`/`retomar` escrevem `control/<plataforma>.json`, que o runner
lê entre lotes. O painel não tem socket do Docker nem SSH, então o botão de
pausa não é uma porta de execução remota.

## O que ainda falta

Este é o esqueleto. Antes de rodar a coleta cheia:

1. **Login das contas** nos perfis, uma vez cada, pela tela remota.
2. **Plano da rodada** (`llmbias-tse conjoint --phase plan`) no `RUN_ID` novo,
   senão o painel mostra progresso sem denominador.
3. **Cloudflare Access na frente**, se for expor para fora. Sem ele, quem
   chega na porta 8080 opera contas logadas. Em localhost, não é necessário.
4. **Teste de carga real**: 8 Chromes simultâneos cabem em RAM, mas o
   comportamento sob concorrência (rate limit correlacionado entre plataformas
   saindo do mesmo IP) só se descobre rodando.

O painel notifica na tela, não por fora. Se a sua equipe não fica de olho,
vale plugar um webhook no evento de nível `alerta`; o `events.jsonl` já
carrega tudo o que uma notificação precisaria.

## Escalar para fora

Se 8 plataformas no mesmo IP residencial começarem a levantar suspeita, ou se
o host ficar apertado, dá para mandar estações para fora sem trocar de
arquitetura: suba o mesmo container numa VM e aponte `PROXY_SOCKS` para um
exit node residencial. Prefira **Tailscale** (`tailscale up
--advertise-exit-node` na casa de alguém do laboratório) ao SSH reverso do
desenho antigo: a conexão é de saída dos dois lados, sobrevive a troca de IP e
a NAT, e reconecta sozinha. Distribuir as saídas entre algumas casas também é
a mitigação natural do risco de correlacionar todas as contas num IP só.

O painel continua onde está: ele lê o diretório da rodada, e o que muda é só
de onde as conversas chegam nesse diretório.
