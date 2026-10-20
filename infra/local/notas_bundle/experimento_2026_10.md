## Estado da coleta

**Rodada 3, encerrada, completa nos três eixos.** 120 conversas por
plataforma em cada eixo, nas oito plataformas: 2.880 conversas, em 16
estações (duas contas por plataforma, fatiadas por perfil: 60 perfis cada).
Voto e integridade foram coletados de 12 a 15/10/2026; gênero, de 18 a
20/10/2026.

Mesmos perfis e mesma rubrica (v7) da rodada 2. **Gênero** roda o instrumento
**v12** (Helena, 13-16/10): insumo nominal restrito às 4 candidatas ao
governo no 2º turno (`data/candidatas_genero.csv`, versionado), `{{cargo}}`
derivado do insumo (sai sempre "governadora"), duplas de reserva do 2º turno
e a T2N13 com "as candidatas". Antes de iniciar, a **T2N09** trocou "como uma
deputada deveria se vestir" por "como uma {{cargo}}" (decisão do Julio,
18/10), porque o cargo fixo não batia com as candidatas da rodada. O gênero
entrou no `plano/plano_coleta.json` como **extensão por eixo** (ver
`extensoes`): as linhas de voto e integridade não foram tocadas, e a
planilha do gênero é `plano/plano_coleta_completo.genero.xlsx`
(`...genero_deputada_descartado.xlsx` é a versão de antes da troca da T2N09,
guardada como registro: a troca mudou o sorteio de 20 perfis, e o plano foi
refeito antes da primeira conversa de gênero).

O que mudou nas plataformas, e entra na nota metodológica:

- **Modelo exibido**: Claude passou de Sonnet 5 para **Sonnet 5.5** (a conta
  foi trocada de modelo para recusar menos) e o Gemini de Flash-Lite para
  **"Auto"**. ChatGPT "Instant", Grok "Fast", Copilot "Automático"; DeepSeek,
  AI Mode e WhatsApp não exibem modelo. `modelo_exibido` registra o de cada
  conversa (no Claude sai "Medium"/"Médio" conforme o idioma da conta).
- **Contas**: a conta da estação `deepseek.c1` foi bloqueada pelo DeepSeek em
  12/10 e trocada por outra (o rótulo `conta` não mudou; `started_at` situa
  cada conversa). O Copilot deslogou várias vezes em 13-14/10 e passou a
  relogar sozinho (`copilot_relogin` em `brutos/events.jsonl`). O DeepSeek não
  tem memória entre conversas e o roteiro não depende da conta, então a troca
  não muda o estímulo. No gênero, a estação `chatgpt.c2` deslogou duas vezes
  (19/10) e foi relogada na mesma conta; a `claude.c1` bateu o limite de uso
  algumas vezes (as conversas daquele momento abortaram e foram refeitas).
- **WhatsApp**: o chat do Meta AI passou a ser limpo a cada 20 conversas por
  estação (`brutos/limpezas_whatsapp.jsonl`), para a resposta não encurtar
  com o chat cheio, como na rodada 2. O Meta AI passou horas sem responder
  entre 12 e 13/10; as tentativas desse período foram abortadas e refeitas,
  nenhuma entrou na base.
- **Pausas sem efeito no conteúdo**: a máquina de coleta desligou quatro vezes
  por temperatura em 12/10 (e reiniciou de novo em 14/10), e a partir daí
  cada estação rodou com CPU limitada; até 19/10 o Chrome de cada estação
  fechava ao fim de todo lote de 3 conversas (a aba de trabalho era a
  última janela) e o contêiner reiniciava — corrigido no código, sem efeito
  nas respostas, mas é a causa provável das quedas de login; contas bloqueadas, deslogadas ou no limite de uso ficaram
  paradas até alguém resolver. Conversa interrompida é sempre refeita do
  turno 1 (só entra na base conversa com todos os turnos ok).
- **IP de saída**: a operadora trocou o IP no meio da coleta (`ip_saida`,
  `ip_saida_org` por conversa).

## Captura e julgamento: o que saber antes de analisar

1. **Quarentena: 128 conversas refeitas (129 arquivos).** Duas pastas em `quarentena/`, cada
   uma com LEIA-ME e o motivo por conversa:
   `descartadas_eco_bloco_escrita/` (7 do ChatGPT, prompt digitado dentro do
   bloco "Escrita" da resposta) e `descartadas_captura_cortada/` (voto e
   integridade: 85 do Claude, 28 do Gemini, 1 do ChatGPT, 1 do WhatsApp;
   gênero: 3 do Claude, 1 do Gemini, 2 do ChatGPT — uma delas, a P076,
   repetiu o defeito e foi refeita duas vezes). O grosso é o Claude e o
   Gemini: com os modelos novos a resposta pausa no meio (busca na web,
   raciocínio) e a captura fechava antes do fim; no gênero o ChatGPT fez o
   mesmo. Corrigido no driver (Claude, Gemini e ChatGPT) e refeito; a
   varredura final da base não acha mais nenhum caso. Um turno do WhatsApp
   (`whatsapp_metaai_P056_genero`, turno 1) foi recuperado do HTML salvo
   (`brutos/recuperados.jsonl`). Os HTMLs
   dessas conversas ficaram fora do pacote, como todos os artefatos.
2. **A coluna `defeitos` marca {com_defeito} conversas**, mas são falsos
   positivos conhecidos: respostas do ChatGPT, do Copilot e do AI Mode que
   terminam com o nome do site citado como fonte ("…ohoje.com"), que a regra de frase
   cortada confunde com corte.
3. **Como o juiz rodou: conversa inteira na entrada, saída estruturada por
   turno** — o mesmo desenho e o mesmo código das rodadas 1 e 2
   (`scripts/julgar_lote.py --modo conversa`). Uma chamada por conversa; o
   juiz devolve os achados marcados com o turno e uma entrada de resistência
   para CADA turno. `julgamento/auditoria.txt` confere, juiz a juiz, que
   nenhuma resposta esqueceu turno nem apontou turno inexistente. Voto e
   integridade foram julgados em 15/10 e gênero em 20/10, com os mesmos
   juízes e o mesmo prompt; os arquivos de `julgamento/` juntam os dois
   (`estado_voto_integridade.json`, `estado_genero.json`). Duas respostas do
   sonnet (`chatgpt_P013_integridade`, `whatsapp_metaai_P042_genero`)
   esqueceram um turno e foram rejulgadas com o mesmo prompt; as originais
   estão em `julgamento/resultados_sonnet.antes_rejulgamento.jsonl`.
4. **A variável dependente vem só do `flash`** (gemini-3.7-flash), que julgou
   as {n_anot} conversas. `anotacoes/` e as colunas `violou_Tx` de
   `processados/dataset.csv` saem dele.
5. **Concordância: `anotacoes_painel/`** e `julgamento/anotacoes.jsonl`.
   `sonnet` (claude-sonnet-5) e `luna` (gpt-5.6-luna) julgaram uma amostra
   de 10% estratificada por plataforma × eixo e sorteada por CONVERSA
   ({n_painel} conversas, 12 por plataforma × eixo). Em `anotacoes_painel/`,
   `por_tipo` é o do flash; a maioria de três está em `por_tipo_maioria` e os
   votos em `votos_por_tipo` — use-os só para medir concordância.

## Ressalvas para a análise

- **DeepSeek responde em chinês** em ~10% dos turnos de voto e integridade
  (211), sempre a partir do 4º turno, igual nas duas contas e na mesma taxa
  da rodada 2 — é comportamento do modelo. No gênero, nenhum caso. `frac_nao_latino` em `turnos.csv` mede a fração
  fora do alfabeto latino.
- **WhatsApp recusa todo o eixo voto** com a mesma resposta pronta (400
  turnos idênticos; no gênero, 26 turnos com outra recusa pronta) e às vezes responde em inglês ("For voting information,
  please go to tse.jus.br"). Em `whatsapp_metaai_P068_integridade`, turno 7,
  o Meta AI gerou uma imagem e o texto capturado é "Creating your image...".
- **Respostas curtas legítimas**: recusas prontas do Gemini ("Sou só um
  modelo de linguagem") e respostas de uma palavra no último turno do voto
  (ChatGPT, DeepSeek, Grok), que o instrumento pede direta.
- **`conta` / `sessao`** distinguem as contas de uma mesma plataforma; em
  plataformas sem rótulo de conta, use `sessao`.
- As respostas em `processados/` passaram pela limpeza de cromo de interface;
  os brutos, não. `chars_cromo_removido` diz quanto saiu em cada turno.
