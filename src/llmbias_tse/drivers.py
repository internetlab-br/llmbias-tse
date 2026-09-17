"""Drivers por ferramenta: como abrir um chat novo, digitar e ler a resposta.

Cada UI tem seus próprios seletores. Eles MUDAM com frequência — quando
um driver parar de capturar, ajuste os seletores aqui (e olhe o snapshot
HTML salvo em data/<run>/artifacts/... para achar o seletor certo).

Estratégia comum (BaseDriver.send):
    1. navega para a URL de "chat novo" (conversa limpa por prompt);
    2. digita o prompt no composer;
    3. envia (Enter);
    4. espera surgir um balão de resposta novo e estabilizar o texto;
    5. devolve o texto.

WhatsApp Web é suficientemente diferente para sobrescrever send().
"""

from __future__ import annotations

import os
import random
import re
import time

from . import capture, events


def _has_leaf_text(page, needles: list[str]) -> bool:
    """True se algum nó-folha visível contém (case-insensitive) uma das
    substrings. Usado para confirmar indicadores de modo (ex.: banner de
    conversa temporária/privada). Robusto a ocultação: varre o texto do DOM,
    não a tela."""
    lowered = [n.lower() for n in needles]
    try:
        return bool(page.evaluate(
            r"""(needles) => {
              const els = document.querySelectorAll('*');
              for (const el of els) {
                if (el.children.length === 0) {
                  const t = (el.textContent || '').trim().toLowerCase();
                  if (t && needles.some(n => t.includes(n))) return true;
                }
              }
              return false;
            }""",
            lowered,
        ))
    except Exception:
        return False


class BaseDriver:
    name: str = "base"
    new_chat_url: str = ""
    # Seletores do composer (tentados em ordem; textarea ou contenteditable).
    composer_selectors: list[str] = []
    # Se setado, CLICA este botão pra enviar (em vez de apertar Enter). Útil
    # em UIs onde Enter não submete (ex.: Grok).
    submit_selector: str = ""
    # Seletor que casa com TODOS os balões de resposta do assistente
    # (usado pra CONTAR balões e detectar um novo).
    response_selector: str = ""
    # Seletor do NÓ DE CONTEÚDO de onde extrair o texto (mais limpo que o
    # balão inteiro, sem rótulos de botões). Se vazio, usa response_selector.
    content_selector: str = ""
    # Seletor das mensagens DO USUÁRIO (pra evitar reenviar prompt já postado
    # numa re-tentativa após rate limit). Vazio = não checa.
    user_selector: str = ""
    # Seletores visíveis ENQUANTO a resposta é gerada (ex.: botão de parar).
    # Quando somem, a geração terminou. Se vazio, cai no fallback de
    # estabilidade-por-texto (menos robusto).
    busy_selectors: list[str] = []
    # Marcadores de texto de FASE INTERMEDIÁRIA (thinking/pesquisa) que NÃO são
    # a resposta final. Enquanto o texto lido contiver algum deles, a detecção
    # de fim NÃO retorna (evita capturar "Searching the web"/status e truncar).
    pending_markers: list[str] = []
    # Tempo extra após carregar a página antes de digitar.
    settle_s: float = 1.5
    # Teto de espera pela resposta (ferramentas com busca na web são lentas).
    response_timeout: float = 240.0
    # Teto de espera pela resposta COMEÇAR a aparecer (novo balão / texto novo).
    # Mensagens longas demoram mais pra disparar; sobrescreva se precisar.
    start_timeout: float = 90.0
    # Quantas vezes re-tentar um turno que falhou por timeout (resposta não
    # começou/terminou). O guard `user_selector` evita repostar a mensagem se
    # ela já foi parar no chat; sem `user_selector`, mantenha 0.
    submit_retries: int = 0

    def open_new_chat(self, page) -> None:
        page.goto(self.new_chat_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.settle_s)

    def submit(self, page, prompt: str, max_limit_waits: int | None = None) -> str:
        """Envia UM prompt no chat ATUAL (sem abrir chat novo) e captura a
        resposta. É o tijolo das conversas multi-turno.

        Resiliente a rate limit ("Too many requests"): quando detecta o modal,
        ESPERA **passivamente** (fecha o modal e dorme, com backoff
        exponencial) e só então re-tenta enviar. NÃO recarrega a página pra
        testar: recarregar refaz requisições (lista de conversas etc.) e
        **mantém o limite vivo** — o jeito de sair é ficar quieto e esperar.
        """
        # Paciência de rate-limit configurável por env (para runs autônomos que
        # ciclam plataformas: falhar rápido numa bloqueada e ir para a próxima).
        if max_limit_waits is None:
            max_limit_waits = int(os.environ.get("LLMBIAS_MAX_LIMIT_WAITS", "6"))
        backoff = float(os.environ.get("LLMBIAS_RL_BACKOFF", "90"))
        attempt = 0
        retries_left = self.submit_retries
        # Nº de balões do usuário ANTES deste turno. Numa re-tentativa, se a
        # contagem subiu, a mensagem já entrou — não reposta (count é imune a
        # colapso de texto e a prefixos repetidos entre turnos).
        user_baseline = (capture.count_responses(page, self.user_selector)
                         if self.user_selector else 0)
        while True:
            if capture.is_rate_limited(page):
                attempt += 1
                if attempt > max_limit_waits:
                    events.alerta(
                        events.BLOQUEIO,
                        f"rate limit não liberou após {attempt-1} esperas",
                        driver=self.name, esperas=attempt - 1,
                    )
                    raise capture.RateLimited(
                        f"rate limit não liberou após {attempt-1} esperas"
                    )
                capture.dismiss_rate_limit(page)
                time.sleep(2.0)  # deixa o modal fechar
                print(f"[driver:{self.name}] rate limit — aguardando "
                      f"{int(backoff)}s sem requisições (espera "
                      f"{attempt}/{max_limit_waits})")
                events.aviso(events.RATE_LIMIT,
                             f"esperando {int(backoff)}s",
                             driver=self.name, espera=attempt,
                             de=max_limit_waits, backoff_s=int(backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 600.0)
                continue
            try:
                resposta = self.limpar_resposta(
                    self._submit_once(page, prompt, user_baseline)
                )
                # O que a plataforma devolveu pode não ser resposta do modelo,
                # e sim um aviso de bloqueio renderizado no fluxo da conversa.
                # Gravar isso como resposta contamina a base em silêncio, então
                # o turno FALHA — e não se re-tenta, porque re-tentar sob
                # bloqueio só reforça o bloqueio.
                marcador = capture.texto_de_bloqueio(resposta)
                if marcador is not None:
                    raise capture.PlataformaBloqueou(
                        f"a plataforma devolveu aviso de bloqueio "
                        f"({marcador!r}) no lugar da resposta"
                    )
                if attempt:
                    events.emit(events.RATE_LIMIT_LIBERADO,
                                driver=self.name, esperas=attempt)
                return resposta
            except capture.PlataformaBloqueou:
                raise
            except Exception as e:
                if capture.is_rate_limited(page):
                    continue  # virou rate limit: volta pro topo e espera
                if retries_left > 0:
                    n = self.submit_retries - retries_left  # 0,1,2,...
                    retries_left -= 1
                    # Backoff exponencial: bloqueios transitórios do Gemini
                    # ("Algo deu errado") são cumulativos — esperar passivamente
                    # deixa resetar. NÃO recarrega a página (manteria o limite).
                    wait = min(15.0 * (2 ** n), 120.0)
                    print(f"[driver:{self.name}] turno falhou ({e!r}); "
                          f"aguardando {int(wait)}s antes de re-tentar "
                          f"({retries_left} restantes)")
                    events.aviso(events.ENVIO_FALHOU, repr(e),
                                 driver=self.name, espera_s=int(wait),
                                 tentativas_restantes=retries_left)
                    time.sleep(wait)
                    continue  # _submit_once re-checa already_sent p/ não repostar
                raise

    # Teto de espera pelo fim da geração anterior, antes de digitar o turno
    # seguinte, e teto do clique no botão de enviar.
    pre_send_wait_s: float = 45.0
    submit_click_timeout_ms: int = 15000
    # Faixa (ms) do atraso por tecla, sorteada a cada pedaço digitado.
    # Ajustável por `LLMBIAS_TYPE_DELAY_MS` ("min,max").
    type_delay_ms: tuple[float, float] = tuple(
        float(x) for x in os.environ.get("LLMBIAS_TYPE_DELAY_MS", "10,30").split(",")
    )

    def limpar_resposta(self, texto: str) -> str:
        """Remove cromo de UI que o seletor de conteúdo arrasta junto.

        Identidade por padrão: só os drivers cujo container de resposta engloba
        rodapé/botões da própria interface precisam sobrescrever. Roda ANTES da
        checagem de aviso de bloqueio, para o cromo não mascarar nem disparar um
        marcador.
        """
        return texto

    def _digitar(self, page, texto: str) -> None:
        """Digita com cadência variável.

        Antes era `keyboard.type(texto, delay=8)`: 8 ms constantes por tecla,
        ~1500 palavras/min, sem variância, sem pausa e sem correção. Em
        28/08/2026 o ChatGPT sinalizou a conta por "unusual activity" depois de
        761 turnos com esse padrão. Aqui o texto sai em pedaços, com atraso por
        tecla sorteado a cada pedaço e pausas entre eles, maiores em fim de
        frase.

        Continua BEM mais rápido que um humano: é compromisso entre reduzir a
        assinatura de automação e manter a coleta viável, não imitação. Um
        humano levaria ~3 min na mensagem mediana de 848 chars; aqui leva ~20 s.

        Cadência apertada em 29/08/2026 (era 18-55 ms/tecla, pedaços de 12-40,
        pausas de 0,08-0,30 s: ~40 s na mensagem mediana). O que a assinatura
        de automação de 28/08 tinha não era lentidão, era CONSTÂNCIA — 8 ms
        fixos, sem variância nem pausa. A variância e as pausas de fim de frase
        continuam aqui; só o patamar subiu, de ~330 para ~600 palavras/min.
        Para voltar ao ritmo antigo numa plataforma sensível, sem editar
        código: `LLMBIAS_TYPE_DELAY_MS=18,55`.
        """
        lo, hi = self.type_delay_ms
        restante = texto
        while restante:
            n = min(len(restante), random.randint(20, 60))
            pedaco, restante = restante[:n], restante[n:]
            page.keyboard.type(pedaco, delay=random.uniform(lo, hi))
            pausa = random.uniform(0.04, 0.16)
            if pedaco.rstrip().endswith((".", "?", "!", ":", ";")):
                pausa += random.uniform(0.15, 0.50)
            time.sleep(pausa)

    def _aguardar_ocioso(self, page) -> bool:
        """Espera o indicador de 'gerando' sumir. True se ficou ocioso."""
        if not self.busy_selectors:
            return True
        fim = time.time() + self.pre_send_wait_s
        while time.time() < fim:
            try:
                if not capture.any_visible(page, self.busy_selectors):
                    return True
            except Exception:
                return True  # não dá para medir: segue e deixa o clique decidir
            time.sleep(0.5)
        return False

    def _perform_send(self, page, prompt: str) -> None:
        """Foca o composer, LIMPA (Ctrl+A/Delete) e digita o prompt, e envia.
        Limpar antes evita texto duplicado caso um envio anterior não tenha
        submetido (o texto fica no composer e um novo type() concatenaria).

        ESPERA a geração anterior terminar antes de digitar. No Gemini o MESMO
        botão alterna de rótulo — "Enviar mensagem" quando pode enviar, "Parar
        resposta" enquanto gera —, então clicar logo após digitar procurava um
        seletor que não existia e queimava o timeout padrão (60s) antes de
        estourar. Com `submit_retries=4` e backoff, um turno assim custava
        minutos E a conversa. O sinal para saber que ainda está gerando já
        estava declarado em `busy_selectors`; só não era consultado aqui.
        """
        self._aguardar_ocioso(page)
        for tentativa in range(2):
            box = capture.first_visible(page, self.composer_selectors)
            box.click()
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
            except Exception:
                pass
            self._digitar(page, prompt)
            if not self.submit_selector:
                page.keyboard.press("Enter")
                return
            # O botão de enviar do Gemini só é RENDERIZADO quando há texto no
            # composer (medido: 0 ocorrências com o composer vazio, 1 depois de
            # digitar). Então ele aparecer é a confirmação de que a digitação
            # registrou — e ele NÃO aparecer significa que o texto se perdeu,
            # tipicamente quando a página ainda está reassentando logo após a
            # resposta anterior. Antes disto o código clicava às cegas e
            # queimava o timeout padrão (60s) procurando um botão que nunca
            # existiria; agora redigita uma vez, que é o que de fato resolve.
            try:
                page.locator(self.submit_selector).first.wait_for(
                    state="visible", timeout=self.submit_click_timeout_ms
                )
            except Exception:
                if tentativa == 0:
                    continue  # o texto não entrou: redigita
                # Última cartada. Em drivers cujo composer submete por Enter
                # isto resolve; onde não submete, `submit()` re-tenta o turno.
                page.keyboard.press("Enter")
                return
            page.locator(self.submit_selector).first.click(
                timeout=self.submit_click_timeout_ms
            )
            return

    def _submit_once(self, page, prompt: str, user_baseline: int = 0) -> str:
        read = self.content_selector or None
        read_list = [read, self.response_selector] if read else [self.response_selector]
        # Texto do último balão ANTES de enviar (resposta do turno anterior,
        # ou "" num chat novo) — usado pra detectar a resposta NOVA e não
        # capturar a antiga por engano.
        prev_text = capture.last_text(page, read_list)
        baseline = capture.count_responses(page, self.response_selector)
        # Numa re-tentativa, se o nº de balões do usuário subiu acima do
        # baseline (medido em submit() antes do 1º envio), a mensagem já entrou
        # — não reenvia, só espera a resposta. Evita prompt duplicado.
        already_sent = bool(
            self.user_selector
            and capture.count_responses(page, self.user_selector) > user_baseline
        )
        if not already_sent:
            self._perform_send(page, prompt)
            # Confirma que a mensagem POSTOU (balão do usuário novo). O envio
            # pode não registrar quando há bloqueio transitório (toast "Algo deu
            # errado" do Gemini, cumulativo/rate-limit). Reenviar algumas vezes
            # resolve hiccups; se mesmo assim não postar, FALHA RÁPIDO
            # (SendFailed) — muito melhor que esperar o start_timeout estourar:
            # o submit() então espera com backoff e re-tenta.
            if self.user_selector:
                def _registered() -> bool:
                    # A mensagem POSTOU se: (a) subiu a contagem de balões do
                    # usuário, OU (b) a geração começou (botão parar/busy visível
                    # — ex.: ChatGPT "pensando" antes de escrever), OU (c) o texto
                    # da resposta mudou. Vários sinais evitam o falso SendFailed
                    # quando o modelo demora a renderizar o balão do usuário.
                    try:
                        if (capture.count_responses(page, self.user_selector)
                                > user_baseline):
                            return True
                    except Exception:
                        pass
                    for bs in self.busy_selectors:
                        try:
                            if page.locator(bs).count() > 0:
                                return True
                        except Exception:
                            pass
                    try:
                        t = capture.last_text(page, read_list)
                        if t and t != prev_text:
                            return True
                    except Exception:
                        pass
                    return False

                posted = _registered()
                tries = 0
                while not posted and tries < 3:
                    time.sleep(3.0)
                    posted = _registered()
                    if posted:
                        break
                    self._perform_send(page, prompt)  # limpa e reenvia
                    tries += 1
                if not posted:
                    raise capture.SendFailed(
                        "mensagem não postou após reenvios (possível bloqueio "
                        "transitório / rate limit)"
                    )
        capture.wait_response_started(
            page, self.response_selector, baseline, self.busy_selectors,
            read_list, prev_text, timeout=self.start_timeout,
        )
        if self.busy_selectors:
            return capture.wait_until_idle(
                page, self.response_selector, self.busy_selectors,
                read_selector=read, ignore_text=prev_text,
                timeout=self.response_timeout,
                pending_markers=self.pending_markers,
            )
        return capture.wait_stable_text(
            page, self.response_selector, read_selector=read,
            timeout=self.response_timeout,
        )

    def send(self, page, prompt: str) -> str:
        """Conversa de um turno só: abre chat novo e envia um prompt."""
        self.open_new_chat(page)
        return self.submit(page, prompt)

    def conversation_url(self, page) -> str:
        return page.url


class ChatGPT(BaseDriver):
    name = "chatgpt"
    new_chat_url = "https://chatgpt.com/"
    composer_selectors = [
        "#prompt-textarea",
        "div[contenteditable='true']#prompt-textarea",
        "textarea[data-testid='prompt-textarea']",
        "textarea",
    ]
    response_selector = "[data-message-author-role='assistant']"
    # Texto limpo do markdown da resposta (sem rótulos de botões da toolbar).
    content_selector = "[data-message-author-role='assistant'] .markdown"
    user_selector = "[data-message-author-role='user']"
    # Confirmado por instrumentação: stop-button visível durante a geração;
    # some em ~7s mesmo em respostas longas. Teto menor que o default p/ um
    # turno travado não queimar 4 min (cai no fallback que devolve o parcial).
    busy_selectors = [
        "button[data-testid='stop-button']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Parar']",
    ]
    response_timeout = 150.0


class ChatGPTMomentary(ChatGPT):
    """ChatGPT em **conversa temporária** (não salva no histórico, não usa nem
    atualiza a memória). Ativada pela query `?temporary-chat=true`: cada
    `open_new_chat` navega para essa URL, o que abre uma conversa temporária
    LIMPA. Confirmado ao vivo (jul/2026): aparece o botão aria-label "Turn off
    temporary chat" e o banner "Temporary Chat".

    Se a confirmação falhar, LEVANTA erro (melhor abortar a conversa do que
    rodá-la em modo normal, o que salvaria no histórico e usaria a memória —
    contaminando o experimento)."""

    name = "chatgpt_momentary"
    new_chat_url = "https://chatgpt.com/?temporary-chat=true"
    _confirm_needles = ["temporary chat", "conversa temporária",
                        "chat temporário"]

    def _momentary_active(self, page) -> bool:
        # Rótulo do cabeçalho: só existe no modo temporário e independe de
        # idioma. Controle negativo (ago/2026): 0 no modo normal, 2 no
        # temporário, 0 ao voltar.
        try:
            if page.locator("[data-testid='temporary-chat-label']").count() > 0:
                return True
        except Exception:
            pass
        # NB: o prefixo 'Turn off'/'Desativar' é o que DISCRIMINA. O botão de
        # LIGAR existe no modo NORMAL com aria-label 'Temporary chat' /
        # 'Chat temporário'; casar só 'chat temporário' aqui daria falso
        # positivo e a coleta rodaria em modo normal (salva no histórico, usa
        # memória) sem nada na base denunciando — o campo `mode` do registro é
        # rótulo fixo do PLATFORM_DRIVERS, não medição.
        try:
            off = page.locator(
                "button[aria-label*='Turn off temporary' i], "
                "button[aria-label*='Desativar conversa temporária' i], "
                "button[aria-label*='Desativar chat temporário' i]"
            )
            if off.count() > 0:
                return True
        except Exception:
            pass
        return _has_leaf_text(page, self._confirm_needles)

    def open_new_chat(self, page) -> None:
        page.goto(self.new_chat_url, wait_until="domcontentloaded",
                  timeout=60000)
        time.sleep(self.settle_s)
        deadline = time.time() + 12
        while time.time() < deadline:
            if self._momentary_active(page):
                return
            time.sleep(0.4)
        raise RuntimeError(
            "ChatGPT: conversa temporária não confirmada (?temporary-chat=true) "
            "— abortando para não salvar no histórico/memória"
        )


class Gemini(BaseDriver):
    name = "gemini"
    new_chat_url = "https://gemini.google.com/app"
    composer_selectors = [
        "div.ql-editor[contenteditable='true']",
        "rich-textarea div[contenteditable='true']",
        "textarea",
    ]
    response_selector = "message-content, .model-response-text"
    # Mensagem do usuário (balão à direita). Usado pra não repostar numa
    # re-tentativa (comparação por CONTAGEM em _submit_once).
    user_selector = ".query-text"
    # Enviar pelo BOTÃO, não por Enter: confirmado por instrumentação (jun/2026)
    # que o Enter cru NÃO submete a partir do 2º turno (composer perde o atalho
    # após a resposta anterior), o que truncava as conversas. O botão tem
    # aria-label "Enviar mensagem" (vira "Parar" durante a geração).
    submit_selector = "button[aria-label='Enviar mensagem']"
    busy_selectors = [
        "button[aria-label*='Stop']",
        "button[aria-label*='Parar']",
        "button.send-button.stop",
    ]
    settle_s = 2.0


class GeminiMomentary(Gemini):
    """Gemini web em **conversa momentânea** (chat anônimo, sem memória entre
    conversas). É o modo usado no experimento de conjoint para que o Gemini
    não acumule contexto de uma conversa para a outra.

    `open_new_chat` navega para /app (chat limpo) e GARANTE que o modo
    momentâneo esteja ativo (idempotente: detecta o banner "conversas
    momentâneas"; se não estiver, clica o botão). Confirmado por inspeção
    (jun/2026): o botão tem aria-label "Conversa momentânea" e, ao ativar,
    aparece um banner com o texto "conversas momentâneas".
    """

    name = "gemini_momentary"
    _toggle_selector = "button[aria-label='Conversa momentânea']"
    # Mensagens longas/indiretas às vezes demoram pra disparar e a resposta do
    # Gemini pode ser longa; folgas maiores que o default evitam o timeout
    # observado no lote preliminar ("resposta não começou em 90s" / "não
    # terminou em 240s").
    # Flash-Lite começa a responder em poucos segundos; se não começou em 45s,
    # é bloqueio transitório — falha rápido e re-tenta (com backoff) em vez de
    # queimar 150s por turno.
    start_timeout = 45.0
    response_timeout = 120.0
    # Mais re-tentativas com backoff: as falhas são bloqueios transitórios
    # ("Algo deu errado", cumulativo) que liberam com a espera. O guard de
    # contagem em _submit_once evita repostar quando a mensagem já entrou.
    submit_retries = 4

    def _momentary_active(self, page) -> bool:
        try:
            return bool(page.evaluate(
                r"""() => {
                  const els = document.querySelectorAll('*');
                  for (const el of els) {
                    if (el.children.length === 0) {
                      const t = (el.textContent || '').trim().toLowerCase();
                      if (t.includes('conversas momentâneas')) return true;
                    }
                  }
                  return false;
                }"""
            ))
        except Exception:
            return False

    def open_new_chat(self, page) -> None:
        """Navega para /app e ATIVA o modo momentâneo, CONFIRMANDO o banner.

        O modo momentâneo NÃO persiste entre navegações (cada goto /app volta
        ao modo normal, que salva no histórico), então é preciso clicar o
        toggle a cada conversa. O botão demora a aparecer no /app recém-carregado
        — esperamos por ele e só então clicamos; depois confirmamos o banner
        "conversas momentâneas". Se não confirmar após várias tentativas,
        LEVANTAMOS erro (melhor abortar a conversa do que vazá-la ao histórico
        e contaminar o experimento com memória entre conversas)."""
        page.goto(self.new_chat_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.settle_s)
        last_err = None
        for _ in range(4):
            if self._momentary_active(page):
                return
            try:
                btn = capture.first_visible(page, [self._toggle_selector],
                                            timeout=20)
                btn.click(timeout=5000)
            except Exception as e:  # botão não apareceu / clique falhou
                last_err = e
            # confirma o banner (pode levar 1–2 s pra renderizar)
            deadline = time.time() + 6
            while time.time() < deadline:
                if self._momentary_active(page):
                    return
                time.sleep(0.3)
        raise RuntimeError(
            "não consegui ativar a conversa momentânea após 4 tentativas "
            f"(último erro: {last_err!r}) — abortando para não vazar ao "
            "histórico"
        )


class Claude(BaseDriver):
    name = "claude"
    new_chat_url = "https://claude.ai/new"
    composer_selectors = [
        "div[contenteditable='true'].ProseMirror",
        "div[contenteditable='true']",
    ]
    # DOM atual (jun/2026): cada resposta do assistente vem num wrapper
    # `div[data-is-streaming]` cujo atributo é "true" durante a geração e vira
    # "false" ao terminar — sinal de "ocupado" muito mais confiável que o
    # antigo botão de parar. O texto limpo fica em `div.font-claude-response`.
    response_selector = "div[data-is-streaming]"
    content_selector = "div.font-claude-response"
    user_selector = "[data-testid='user-message']"
    busy_selectors = [
        "div[data-is-streaming='true']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Parar']",
    ]
    # Fase de thinking/pesquisa do Claude: enquanto o texto lido for isto, não é
    # a resposta final (evita truncar em "Searching the web"). / são
    # os ícones de status (Pondering/Searching) do Claude.
    pending_markers = ["Searching the web", "", ""]
    settle_s = 2.0


class ClaudeMomentary(Claude):
    """Claude em **conversa incognito** (não salva no histórico nem usa/atualiza
    a memória). Ativada pelo botão aria-label "Use incognito" (ícone fantasma no
    topo direito); ao ativar, a URL vira `.../new?incognito=`, o botão passa a
    "Exit incognito" e aparecem os textos "You're incognito" / "Incognito chat".
    Confirmado ao vivo (jul/2026). Não persiste entre navegações: reativa a cada
    conversa. Se não confirmar, LEVANTA erro (não roda em modo normal)."""

    name = "claude_momentary"
    _incognito_needles = ["incognito", "incógnito"]

    def _momentary_active(self, page) -> bool:
        # Sinal INEQUÍVOCO: a URL vira .../new?incognito= ao ativar. Checar
        # texto da página dava falso-positivo (o botão/tooltip "Use incognito"
        # contém "incognito" mesmo no modo normal), fazendo o driver PULAR a
        # ativação e rodar em modo normal (salvando no histórico).
        try:
            if "incognito=" in (page.url or "").lower():
                return True
        except Exception:
            pass
        # Reforço: só considera ativo se houver banner explícito de estado.
        return _has_leaf_text(page, ["you're incognito", "incognito chat",
                                     "conversa incógnita", "chat incógnito"])

    def _dismiss_modal(self, page) -> None:
        """Fecha nudges/modais do Claude (ex.: 'claude-code-nudge') cujo backdrop
        sobrepõe o composer e intercepta o clique/digitação."""
        try:
            page.keyboard.press("Escape")
            time.sleep(0.2)
        except Exception:
            pass
        # Fallback: remove o backdrop fixo que intercepta os eventos de ponteiro.
        try:
            page.evaluate(
                r"""() => {
                  const kill = [];
                  document.querySelectorAll('[role=presentation],[data-base-ui-backdrop],[data-open]').forEach(el => {
                    const c = (el.className || '') + '';
                    if (/backdrop|inset-0/.test(c) && getComputedStyle(el).position === 'fixed') kill.push(el);
                  });
                  kill.forEach(el => el.remove());
                }"""
            )
        except Exception:
            pass

    def submit(self, page, prompt: str, max_limit_waits: int | None = None) -> str:
        self._dismiss_modal(page)
        return super().submit(page, prompt, max_limit_waits)

    def open_new_chat(self, page) -> None:
        page.goto(self.new_chat_url, wait_until="domcontentloaded",
                  timeout=60000)
        time.sleep(self.settle_s)
        last_err = None
        for _ in range(4):
            if self._momentary_active(page):
                return
            self._dismiss_modal(page)  # nudge/modal pode interceptar o clique
            try:
                page.get_by_role("button", name="Use incognito").first.click(
                    timeout=5000)
            except Exception as e:
                last_err = e
            deadline = time.time() + 6
            while time.time() < deadline:
                if self._momentary_active(page):
                    return
                time.sleep(0.3)
        raise RuntimeError(
            "Claude: conversa incognito não confirmada (URL sem ?incognito=) "
            f"(último erro: {last_err!r}) — abortando para não salvar no "
            "histórico/memória"
        )


class Grok(BaseDriver):
    name = "grok"
    new_chat_url = "https://grok.com/"
    # Composer é um contenteditable dentro de [data-testid=chat-input] (a
    # <textarea> é oculta, só pra a11y). Enter NÃO submete → usa o botão.
    composer_selectors = [
        "[data-testid='chat-input'] [contenteditable='true']",
        "[data-testid='chat-input']",
        "div[role='textbox']",
    ]
    submit_selector = "[data-testid='chat-submit']"
    response_selector = "[data-testid='assistant-message']"
    # Texto limpo da resposta (exclui o prefixo "Thought for Xs").
    content_selector = "[data-testid='assistant-message'] .response-content-markdown"
    user_selector = "[data-testid='user-message']"
    # Botão de parar durante a geração (candidatos); se não casar, cai no
    # backstop de estabilidade de texto do wait_until_idle.
    busy_selectors = [
        "[data-testid='chat-stop']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Parar']",
    ]
    settle_s = 2.0
    response_timeout = 180.0

    def open_new_chat(self, page) -> None:
        page.goto(self.new_chat_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.settle_s)
        self._dismiss_age_gate(page)
        capture.first_visible(page, self.composer_selectors, timeout=30)

    def _dismiss_age_gate(self, page) -> None:
        """Grok pede confirmação de idade (Confirm your age -> Continue ->
        Birth Year -> Save). Fluxo de 2 passos; fecha se aparecer. Detecta por
        texto RENDERIZADO (get_by_text), não pelo body inteiro (evita falso
        positivo com strings ocultas do bundle)."""
        for _ in range(4):
            try:
                gate = (page.get_by_text("Birth Year", exact=False).count() > 0
                        or page.get_by_text("confirm your age", exact=False).count() > 0)
            except Exception:
                gate = False
            if not gate:
                return
            clicked = False
            for sel in ("button:has-text('Continue')", "button:has-text('Save')"):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=2000)
                        time.sleep(1.3)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                return


class GrokMomentary(Grok):
    """Grok em **conversa privada** (não aparece no histórico nem é usada para
    treinar). O toggle é o link `a[aria-label='Switch to Private Chat']` (texto
    "Private"); ao ativar, aparece o badge "Private" (aria-label passa a
    "Switch to Default Chat") e o banner "This chat won't appear in your
    history and will not be used to train models." Confirmado ao vivo
    (jul/2026). O modo NÃO persiste entre carregamentos: reativa a cada
    conversa. Se não confirmar, LEVANTA erro (não roda em modo normal)."""

    name = "grok_momentary"
    _switch_to_private = "a[aria-label='Switch to Private Chat']"
    _private_needles = [
        "won't appear in your history",
        "will not be used to train",
        "não aparecerá no seu histórico",
        "não será usada para treinar",
    ]

    def _momentary_active(self, page) -> bool:
        # Indicador positivo: badge "Switch to Default Chat" OU banner privado.
        try:
            if page.locator("[aria-label='Switch to Default Chat']").count() > 0:
                return True
        except Exception:
            pass
        return _has_leaf_text(page, self._private_needles)

    def open_new_chat(self, page) -> None:
        page.goto(self.new_chat_url, wait_until="domcontentloaded",
                  timeout=60000)
        time.sleep(self.settle_s)
        self._dismiss_age_gate(page)
        capture.first_visible(page, self.composer_selectors, timeout=30)
        last_err = None
        for _ in range(4):
            if self._momentary_active(page):
                return
            try:
                page.locator(self._switch_to_private).first.click(timeout=5000)
            except Exception as e:
                last_err = e
            deadline = time.time() + 6
            while time.time() < deadline:
                if self._momentary_active(page):
                    return
                time.sleep(0.3)
        raise RuntimeError(
            "Grok: conversa privada não confirmada "
            f"(último erro: {last_err!r}) — abortando para não salvar no "
            "histórico"
        )


class DeepSeek(BaseDriver):
    name = "deepseek"
    new_chat_url = "https://chat.deepseek.com/"
    # Composer: uma <textarea> única (Enter envia; Shift+Enter quebra linha).
    # NB: o id "chat-input" saiu do DOM (jun/2026) — usar a textarea direto.
    composer_selectors = [
        "textarea",
        "textarea#chat-input",
    ]
    # Balão do assistente: o corpo em markdown é `.ds-markdown` (confirmado por
    # inspeção jun/2026 — captura o texto limpo da resposta). Conta um por turno.
    response_selector = ".ds-markdown"
    content_selector = ".ds-markdown"
    # DeepSeek não expõe botão de "parar" com seletor estável; cai no backstop
    # de estabilidade de texto (wait_stable_text) com busy vazio.
    busy_selectors: list[str] = []
    settle_s = 2.5
    response_timeout = 180.0


class GoogleAIMode(BaseDriver):
    """Google AI Mode (aba "Modo IA" da busca, `udm=50`).

    Sem modo anônimo/incognito próprio: cada conversa navega para uma URL
    AI Mode NOVA (`?udm=50` sem `q`), o que abre uma thread limpa (o contexto
    só persiste DENTRO da conversa, via composer de follow-up). Isolamento
    entre conversas = navegação fresca (análogo ao DeepSeek). Fica salvo na
    Atividade da conta Google — não há como evitar na UI (jul/2026).

    Seletores confirmados por inspeção (ago/2026), voláteis (classes ofuscadas
    do Google): composer é a `textarea` visível (placeholder "Pergunte o que
    quiser"); a prosa da resposta fica em `div.n6owBd` (um por turno). Se
    quebrar, reinspecione com scratchpad/google_inspect*.py.
    """

    name = "google_aimode"
    new_chat_url = "https://www.google.com/search?udm=50"
    # A página tem 2-3 textareas: as OCULTAS (w=0,h=0) vêm ANTES no DOM, e
    # first_visible só olha `.first` — por isso mira a VISÍVEL via `:visible`
    # (o composer de follow-up "Pergunte o que quiser").
    composer_selectors = [
        "textarea[placeholder*='Pergunte']:visible",
        "textarea:visible",
    ]
    # Resposta COMPLETA do turno. `div.n6owBd` só pega fragmentos (intro +
    # pergunta de fechamento); `div.mZJni` é o container da prosa inteira da
    # resposta (um por turno). Classe ofuscada → volátil (reinspecione se
    # quebrar: scratchpad/google_inspect5.py).
    response_selector = "div.mZJni"
    content_selector = "div.mZJni"
    busy_selectors: list[str] = []      # sem botão de "parar" estável
    settle_s = 3.0
    response_timeout = 240.0            # AI Mode faz busca na web, é lento
    start_timeout = 120.0

    # Rodapé fixo que o AI Mode renderiza DENTRO de `div.mZJni` (aviso de erro
    # + botões de copiar/compartilhar + termos), ~700 chars colados no fim de
    # TODA resposta. Medido no smoke de 29/08: os 7 turnos, sem exceção.
    # Não é cosmético — vai inteiro para o prompt do juiz, e "A IA pode cometer
    # erros. Por isso, cheque as respostas" é justamente o tipo de ressalva que
    # o bloco de resistência da rubrica (R1-R3) mede. Deixar passar seria
    # creditar ao modelo uma cautela que é boilerplate da página.
    #
    # Corte por marcador, não por seletor: no DOM ao vivo o rodapé é um nó
    # irmão (`div.alk4p.q4PqPb`, vazio enquanto o turno gera), mas no HTML do
    # turno concluído ele aparece dentro do mesmo filho da prosa — nenhum
    # seletor CSS separa os dois nos dois estados.
    # Duas variantes, e as DUAS aparecem: o aviso de erro nem sempre precede o
    # bloco de compartilhamento. Na 1a conversa de produção o turno 6 trouxe
    # "Compartilhar link público" SEM o aviso antes — casar só o aviso deixou
    # 392 chars de diálogo de compartilhamento dentro da resposta.
    # Variantes do rodapé, e TODAS aparecem. Levantadas sobre os 24 turnos já
    # coletados (7 do voto e 10 do gênero no smoke, 7 da 1ª conversa de
    # produção), não por leitura da página:
    #   - o aviso genérico ("A IA pode cometer erros"), 15 turnos;
    #   - o aviso JURÍDICO ("As respostas da IA podem conter erros. Para
    #     orientação jurídica, consulte um profissional"), 7 turnos — o AI Mode
    #     o injeta em tema legal, e eleição é tema legal, então ele vai ser
    #     comum nesta coleta;
    #   - o diálogo de compartilhamento SEM aviso antes, 2 turnos.
    # Casar só o primeiro deixava os outros dois dentro da resposta.
    _rodape_marcadores = (
        "A IA pode cometer erros",
        "As respostas da IA podem conter erros",
        "AI responses may include mistakes",
        "AI responses may contain errors",
        "Compartilhar link público",
        "Share public link",
        # 3º diálogo, achado só em 30/08 com 530 turnos na base: o de FEEDBACK
        # (avaliar a resposta). Aparece em 1 turno em 530 — raro porque só
        # entra no textContent quando a UI o monta, mas quando entra traz ~450
        # chars, incluindo "Política de Privacidade e nossos Termos de
        # Serviço". A raridade é o problema: com 24 turnos de amostra ele não
        # aparecia, e foi por isso que a correção anterior passou por completa.
        "Boa respostaResposta ruim",
        "Good responseBad response",
        "Uma cópia desta conversa",
        "A copy of this conversation",
    )
    # Rótulos de botão que encostam no fim do texto sem rodapé antes.
    _rotulos_finais = ("Copiar", "Copy", "Saiba mais", "Learn more",
                       "Compartilhar", "Share", "Exportar", "Export")

    def limpar_resposta(self, texto: str) -> str:
        if not texto:
            return texto
        cortes = [i for i in (texto.find(m) for m in self._rodape_marcadores)
                  if i > 0]
        # `i > 0` de propósito: se o rodapé abre o texto (i == 0) não sobrou
        # prosa nenhuma, e devolver "" faria o turno passar por resposta vazia
        # em vez de falhar. Nesse caso devolve como veio e o turno falha.
        if cortes:
            texto = texto[:min(cortes)]
        # Só no FIM, e só rótulo exato: um "Copiar" no meio da prosa é do
        # modelo e fica. Em laço porque vêm colados ("Saiba maisCopiar").
        mudou = True
        while mudou:
            mudou = False
            texto = texto.rstrip().rstrip(" ").rstrip()
            for r in self._rotulos_finais:
                if texto.endswith(r):
                    texto = texto[:-len(r)]
                    mudou = True
        return texto.rstrip()

    def open_new_chat(self, page) -> None:
        """Navega para uma thread AI Mode fresca e espera o composer."""
        page.goto(self.new_chat_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.settle_s)
        # o composer visível pode estar abaixo da dobra → garante presença
        capture.first_visible(page, self.composer_selectors, timeout=30)


class Copilot(BaseDriver):
    """Microsoft Copilot consumer (`copilot.microsoft.com`), conta pessoal.

    Seletores confirmados por inspeção ao vivo (ago/2026): composer é a
    `textarea#userInput` (`data-testid='composer-input'`, placeholder "Message
    Copilot"); Enter envia. A resposta do assistente é `[data-testid=
    'ai-message']`, com o texto limpo em `[data-testid='ai-message-body']` (um
    por turno). Sem botão de "parar" com seletor estável → detecção de fim por
    estabilidade de texto (busy vazio).
    """

    name = "copilot"
    new_chat_url = "https://copilot.microsoft.com/"
    composer_selectors = [
        "textarea#userInput",
        "textarea[data-testid='composer-input']",
        "textarea",
    ]
    response_selector = "[data-testid='ai-message']"
    content_selector = "[data-testid='ai-message-body']"
    busy_selectors: list[str] = []      # sem stop-button estável
    settle_s = 2.5
    response_timeout = 180.0
    start_timeout = 90.0


class CopilotMomentary(Copilot):
    """Copilot em **chat temporário** (não salva no histórico nem usa/atualiza a
    memória). `open_new_chat` navega para `/chats/temporary`, que abre um chat
    temporário LIMPO (confirmado: 0 mensagens, botão "Exit temporary chat"
    presente). Idempotente e isolado por conversa.

    Se a confirmação falhar, LEVANTA erro (melhor abortar do que rodar em modo
    normal, salvando no histórico/memória e contaminando o experimento)."""

    name = "copilot_momentary"
    new_chat_url = "https://copilot.microsoft.com/chats/temporary"

    def _momentary_active(self, page) -> bool:
        try:
            if page.locator("button[title='Exit temporary chat' i]").count() > 0:
                return True
        except Exception:
            pass
        if "/chats/temporary" in (page.url or ""):
            return True
        return _has_leaf_text(page, ["temporary chat", "chat temporário"])

    def open_new_chat(self, page) -> None:
        page.goto(self.new_chat_url, wait_until="domcontentloaded",
                  timeout=60000)
        time.sleep(self.settle_s)
        deadline = time.time() + 12
        while time.time() < deadline:
            if self._momentary_active(page):
                # garante o composer presente antes de retornar
                try:
                    capture.first_visible(page, self.composer_selectors,
                                          timeout=10)
                except Exception:
                    pass
                return
            time.sleep(0.4)
        raise RuntimeError(
            "Copilot: chat temporário não confirmado (/chats/temporary) — "
            "abortando para não salvar no histórico/memória"
        )


class MetaAI(BaseDriver):
    name = "metaai"
    new_chat_url = "https://www.meta.ai/"
    composer_selectors = [
        "textarea",
        "div[contenteditable='true']",
    ]
    # Best-effort; ajuste após inspecionar o snapshot na 1ª execução.
    response_selector = "div[dir='auto']"
    settle_s = 2.5


class WhatsAppMetaAI(BaseDriver):
    """Meta AI dentro do WhatsApp Web.

    Requer QR escaneado uma vez (persiste no perfil) E a conversa do
    Meta AI **aberta manualmente** (chat ativo). O driver opera no chat
    ativo — não navega/busca (a busca do WhatsApp Web é volátil).

    Contexto limpo por prompt: como não há "chat novo" no WhatsApp, envia
    `/reset-all-ais` antes de cada prompt para zerar a memória do Meta AI.

    Seletores confirmados por inspeção (jun/2026); a UI muda — reinspecione
    com tmp/inspect_wa.py se quebrar.
    """

    name = "whatsapp_metaai"
    new_chat_url = "https://web.whatsapp.com/"
    # Regex do timestamp que o WhatsApp cola no fim do balão (ex.: "\n\n09:56").
    _ts_re = re.compile(r"\s*\d{1,2}:\d{2}(\s?[AaPp][Mm])?\s*$")
    # Composer do chat ativo (aria-label "Type a message to Meta AI").
    composer_selectors = [
        "[role='textbox'][data-tab='10']",
        "footer [role='textbox']",
    ]
    # Balões RECEBIDOS (do Meta AI). DOM ago/2026: as ENVIADAS (usuário) têm
    # `.copyable-text[data-pre-plain-text]` ("[hora] Nome:"); as RECEBIDAS (Meta
    # AI) têm `.copyable-text.selectable-text` SEM data-pre-plain-text. VOLÁTIL:
    # se quebrar, reinspecione (scratchpad wa_dom2.py) qual marca só as recebidas.
    response_selector = ".copyable-text.selectable-text"
    busy_selectors: list[str] = []  # WhatsApp não tem botão de "parar"
    reset_command = "/reset-all-ais"

    def open_new_chat(self, page) -> None:
        """No WhatsApp não há "chat novo": garante o chat ativo (Meta AI aberto
        à mão) e ZERA a memória do Meta AI UMA vez por conversa (`/reset-all-
        ais`). Assim cada conversa começa limpa, mas os turnos de uma conversa
        longa mantêm o contexto entre si."""
        self._force_focus(page)  # anti-throttle de render do WA Web (aba ocluída)
        if "web.whatsapp.com" not in page.url:
            page.goto(self.new_chat_url, wait_until="domcontentloaded", timeout=90000)
        capture.first_visible(page, self.composer_selectors, timeout=30)
        self.reset(page)

    def _send_raw(self, page, text: str) -> None:
        box = capture.first_visible(page, self.composer_selectors)
        box.click()
        self._digitar(page, text)
        page.keyboard.press("Enter")

    def reset(self, page) -> None:
        """Zera a memória do Meta AI e espera a confirmação chegar."""
        baseline = capture.count_responses(page, self.response_selector)
        self._send_raw(page, self.reset_command)
        try:
            capture.wait_response_started(
                page, self.response_selector, baseline, [],
                [self.response_selector], "", timeout=30,
            )
        except Exception:
            pass
        time.sleep(2.0)

    def _force_focus(self, page) -> None:
        """Faz o renderer tratar a página como SEMPRE focada/visível, mesmo com a
        janela do Chrome ocluída (atrás do terminal). O WhatsApp Web estrangula a
        renderização quando `document.hidden` (Page Visibility API), e as flags
        de linha de comando do Chrome não cobrem esse throttling no nível da app.
        `Emulation.setFocusEmulationEnabled` (CDP) resolve na raiz: sem isto, o
        balão recebido só entra no DOM no envio seguinte (atraso de 1 turno).
        Idempotente e barato; chamado uma vez por conversa."""
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception:
            pass
        # Reforço: sobrescreve a Page Visibility API para 'visible'/hidden=false.
        try:
            page.add_init_script(
                """Object.defineProperty(document,'hidden',{get:()=>false});
                   Object.defineProperty(document,'visibilityState',
                       {get:()=>'visible'});"""
            )
        except Exception:
            pass

    def _scroll_to_bottom(self, page) -> None:
        """Rola a lista de mensagens até o fim. O WhatsApp Web VIRTUALIZA a
        lista: um balão recebido novo só entra no DOM quando a view rola até
        ele. Sem isto, a resposta do Meta AI só era "vista" no envio seguinte
        (atraso de 1 turno)."""
        try:
            page.evaluate(
                r"""() => {
                  const main = document.querySelector('#main');
                  if (!main) return;
                  let el = main.querySelector('div[role=row]');
                  while (el && el !== main) {
                    const s = getComputedStyle(el);
                    if (/(auto|scroll)/.test(s.overflowY) &&
                        el.scrollHeight > el.clientHeight) {
                      el.scrollTop = el.scrollHeight;
                      return;
                    }
                    el = el.parentElement;
                  }
                }"""
            )
        except Exception:
            pass

    def _incoming_texts(self, page) -> list[str]:
        """Lista dos textos dos balões RECEBIDOS (do Meta AI), em ordem. Uma
        linha é ENVIADA (usuário) se tem `.copyable-text[data-pre-plain-text]`
        ("[hora] Nome:"); as recebidas não têm. Robusto a respostas idênticas
        (a contagem sobe mesmo que o texto se repita)."""
        try:
            return page.evaluate(
                r"""() => {
                  const main = document.querySelector('#main');
                  if (!main) return [];
                  const out = [];
                  for (const r of main.querySelectorAll('div[role=row]')) {
                    if (r.querySelector('.copyable-text[data-pre-plain-text]'))
                      continue; // enviada (usuário)
                    const cop = r.querySelector('.copyable-text.selectable-text')
                             || r.querySelector('span.selectable-text');
                    // textContent (NÃO innerText): innerText retorna "" com a
                    // janela ocluída (layout pulado) -> resposta some por 1 turno
                    const t = cop ? (cop.textContent || '').trim() : '';
                    if (t) out.push(t);
                  }
                  return out;
                }"""
            ) or []
        except Exception:
            return []

    def _last_incoming(self, page) -> str:
        xs = self._incoming_texts(page)
        return xs[-1] if xs else ""

    def _incoming_msgs(self, page) -> list[dict]:
        """Balões RECEBIDOS como {id, t}. O `data-id` (prefixo `false_`=recebida,
        `true_`=enviada) é ÚNICO e ESTÁVEL por mensagem, mesmo com texto idêntico
        (os refusals de voto repetem) e mesmo com a VIRTUALIZAÇÃO da lista (que
        faz a CONTAGEM oscilar para cima E para baixo). Detectar por id novo é o
        único sinal confiável aqui."""
        try:
            return page.evaluate(
                r"""() => {
                  const main = document.querySelector('#main');
                  if (!main) return [];
                  const out = [];
                  for (const r of main.querySelectorAll('div[role=row]')) {
                    const idEl = r.querySelector('[data-id]');
                    const id = idEl ? (idEl.getAttribute('data-id') || '') : '';
                    const outgoing = id.startsWith('true_') ||
                      !!r.querySelector('.copyable-text[data-pre-plain-text]');
                    if (outgoing) continue;              // enviada (usuário)
                    const cop = r.querySelector('.copyable-text.selectable-text')
                             || r.querySelector('span.selectable-text');
                    const t = cop ? (cop.textContent || '').trim() : '';
                    if (id || t) out.push({id, t});
                  }
                  return out;
                }"""
            ) or []
        except Exception:
            return []

    def submit(self, page, prompt: str) -> str:
        """Envia um prompt no chat do Meta AI e captura a resposta. Detecta a
        resposta NOVA pelo `data-id` (id de mensagem) que ainda não existia antes
        do envio: robusto a respostas idênticas (o Meta AI repete o redirect ao
        TSE) E à virtualização da lista (contagem não é monotônica). Sem reset
        aqui (é por conversa)."""
        try:
            page.bring_to_front()  # aba ativa (anti-throttle de render do WA Web)
        except Exception:
            pass
        before = {m["id"] for m in self._incoming_msgs(page) if m["id"]}
        self._send_raw(page, prompt)
        deadline = time.time() + self.response_timeout
        last_text = ""
        since = None
        saw_new = False
        while time.time() < deadline:
            try:
                page.bring_to_front()
            except Exception:
                pass
            self._scroll_to_bottom(page)  # mantém os balões novos renderizados
            msgs = self._incoming_msgs(page)
            # balão recebido mais recente cujo id NÃO existia antes do envio
            newmsg = None
            for m in reversed(msgs):
                if m["id"] and m["id"] not in before:
                    newmsg = m
                    break
            t = newmsg["t"] if (newmsg and newmsg["t"]) else ""
            if t:
                saw_new = True
                if t == last_text:
                    if since is None:
                        since = time.time()
                    elif time.time() - since >= 3.0:
                        break
                else:
                    since = None
                    last_text = t
            time.sleep(0.5)
        if os.environ.get("LLMBIAS_WA_DEBUG"):
            print(f"[WA] id_novo={saw_new} capturado={len(last_text)} chars: "
                  f"{last_text[:50]!r}", flush=True)
        return self._ts_re.sub("", last_text).strip()


# Registro: nome -> classe. A CLI usa essas chaves.
REGISTRY: dict[str, type[BaseDriver]] = {
    d.name: d
    for d in (ChatGPT, ChatGPTMomentary, Gemini, GeminiMomentary, Claude,
              ClaudeMomentary, Grok, GrokMomentary, DeepSeek, GoogleAIMode,
              Copilot, CopilotMomentary, MetaAI, WhatsAppMetaAI)
}

DEFAULT_TOOLS = ["chatgpt", "gemini", "claude", "metaai"]
