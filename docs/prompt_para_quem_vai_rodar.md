# Prompt para quem vai rodar a coleta

Cole o bloco abaixo no Claude, aberto na pasta do projeto.
**Troque `NOME_DA_PLATAFORMA` pela sua** antes de enviar.

Plataformas possíveis (escreva exatamente assim):
`chatgpt` · `gemini` · `claude` · `deepseek` · `grok` · `copilot` ·
`google_aimode` · `whatsapp_metaai`

---

```
Você vai rodar a minha parte da coleta de dados do projeto llmbias-tse
(auditoria de plataformas de IA para as eleições de 2026, LabDados + InternetLab).

MINHA PLATAFORMA: NOME_DA_PLATAFORMA

O que já está feito:
- o repositório está nesta pasta e eu já rodei `uv venv` e `uv sync`
- o Chrome já está aberto (rodei `uv run llmbias-tse launch`) e eu já
  fiz login na minha plataforma, com ela aberta numa aba

O que eu preciso que você faça, nesta ordem:

1. Confirme que o plano de coleta está no lugar. Tem que existir a pasta
   `data/experimento_2026_08/` com o arquivo `plano_coleta.json` dentro.
   Se não existir, me avise e PARE: eu preciso descompactar o zip que o
   Julio mandou. NÃO gere um plano novo, porque o plano precisa ser
   idêntico ao de todo mundo.

2. Confirme que o Chrome está no ar na porta 9333 e que a minha
   plataforma está logada. Abra uma aba de teste com o driver dela antes
   de começar; se não logar, me avise e PARE.

3. IMPORTANTE: antes de começar, abra 2 ou 3 abas extras em branco no
   Chrome e deixe abertas. O Chrome fecha sozinho quando fica sem
   nenhuma aba, e no fim de cada lote o script fecha a aba que estava
   usando. As abas extras impedem isso.

4. Rode a coleta em LOTES, não tudo de uma vez, para que uma falha custe
   pouco. Use este comando, aumentando o limite a cada lote:

     uv run python -m llmbias_tse conjoint --run-id experimento_2026_08 \
       --platforms NOME_DA_PLATAFORMA \
       --phase generate --per-platform-limit 20 \
       --turn-delay 5 --conv-delay 12

   São 360 conversas no total (120 perfis × 3 eixos). Cada conversa leva
   uns 10 minutos, então isso é longo: rode em lotes de 20, confira e
   siga. Pode rodar em background e me avisar do progresso.

5. Se der erro, NÃO recomece do zero. O mesmo comando com o mesmo
   `--run-id` pula as conversas já concluídas e refaz só as que faltam.
   Casos comuns:
   - "Nada escutando em http://localhost:9333" -> o Chrome fechou. Me
     avise para eu abrir de novo, e confira as abas extras do passo 3.
   - "Too many requests" ou bloqueio da plataforma -> ESPERE, não fique
     recarregando a página. Recarregar mantém o bloqueio vivo. Espere
     uns 30 minutos e rode o mesmo comando de novo.
   - Uma conversa que aborta sozinha é normal e é retomável; não tente
     consertar na unha.

6. Quando as 360 conversas estiverem completas, confira quantas ficaram
   boas e me diga o número. Uma conversa só conta como pronta se tem
   todos os turnos com ok=true.

7. No fim, gere a base e compacte a pasta para eu subir no Drive:

     uv run python -m llmbias_tse conjoint --run-id experimento_2026_08 \
       --platforms NOME_DA_PLATAFORMA --phase judge

   Se essa etapa pedir chave de API que eu não tenho, PULE ela e me
   avise: o julgamento pode ser feito depois, centralizado. O que não
   pode faltar são as conversas.

   Depois compacte `data/experimento_2026_08/` inteira num zip com o
   nome da minha plataforma, por exemplo
   `experimento_2026_08_NOME_DA_PLATAFORMA.zip`.

Regras que valem o tempo todo:
- NÃO edite o conteúdo do experimento: nada de mexer em prompts,
  rubricas, instrumentos ou no plano de coleta. Se algo parecer errado,
  me avise em vez de corrigir.
- NÃO rode outras plataformas além da minha.
- NÃO use a minha conta para mais nada enquanto a coleta roda, porque
  isso conta para o limite de uso e derruba a coleta.
- Me diga o que está acontecendo conforme avança.
```

---

## Notas para o Julio (não vão para a equipe)

- **Distribuir o zip `data/PLANO_experimento_2026_08.zip`** para cada
  pessoa. Ela descompacta dentro de `data/`, ficando
  `data/experimento_2026_08/plano_coleta.json`. Isso é o que garante que
  todo mundo rode exatamente as mesmas 360 conversas: o runner lê o plano
  persistido em vez de sortear de novo, então uma diferença de commit
  entre as máquinas não muda o estímulo.
- **O `--phase judge` precisa de chave de API** (`GEMINI_API_KEY` e, para
  o painel, `ANTHROPIC_API_KEY` e `OPENAI_API_KEY` no `.env`). A maioria
  não vai ter. Por isso o passo 7 manda pular e avisar. Julgar depois,
  centralizado e em lote, é mais barato de qualquer forma.
- **Grok e WhatsApp** têm as restrições que você citou: Grok exige conta
  paga, e o WhatsApp exige conta pessoal, com o chat do Meta AI aberto à
  mão antes de rodar (o driver não abre o chat, só dá `/reset-all-ais`).
- **Cada pessoa numa conta separada.** Duas pessoas na mesma conta
  compartilham o limite de uso e derrubam as duas coletas.
