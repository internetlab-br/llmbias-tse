"""Helpers genéricos de interação e captura de resposta.

As UIs das ferramentas mudam com frequência e fazem *streaming* da
resposta (o texto cresce token a token). Em vez de depender de um botão
"parar de gerar" específico de cada uma, a estratégia aqui é genérica e
robusta: ler o texto do último balão de resposta repetidamente e
considerar a resposta **pronta quando o texto para de mudar** por alguns
segundos (estabilização), com um teto de tempo.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

# Frases que aparecem no modal de rate limit ("Too many requests"). Usamos
# FRASES INTEIRAS e específicas (não palavras soltas como "rate limit"): o
# bundle do ChatGPT tem templates/JSON ocultos com esses termos, então
# procurar no text_content do body dava FALSO-POSITIVO (achava o modal mesmo
# sem ele na tela). `get_by_text` casa só com texto de elementos renderizados,
# não com <script>/template oculto — e o `.count()` funciona com janela
# ocluída (é count do locator base, não `.last`).
RATE_LIMIT_MARKERS = (
    "Too many requests",
    "making requests too quickly",
    "temporarily limited access",
)


# Avisos DA PLATAFORMA que podem ser capturados no lugar da resposta do modelo.
# Não são conteúdo: são erro ou bloqueio renderizados dentro do fluxo da
# conversa. Gravá-los como resposta contamina a base EM SILÊNCIO — foi o que
# aconteceu em 28/08/2026, quando o ChatGPT devolveu "Our systems have detected
# unusual activity coming from your system" e cinco conversas fecharam com
# todos os turnos `ok=True` carregando o aviso dentro. Como `_conv_done` só
# olha `ok`, a retomada nunca as teria refeito.
BLOCK_MARKERS = (
    "unusual activity",
    "something went wrong",
    "network error",
    "error in message stream",
    "conversation not found",
    "please try again later",
    # Google (busca / AI Mode). A frase do Google é "unusual TRAFFIC", que o
    # marcador "unusual activity" do ChatGPT NÃO casa — e a perna do AI Mode
    # roda deslogada, que é exatamente a condição em que o Google interpõe a
    # verificação de robô. Sem estas entradas o aviso entraria na base como
    # resposta do modelo, com ok=True.
    "unusual traffic",
    "tráfego incomum",
    "systems have detected unusual",
    "not a robot",
    "não é um robô",
)


class PlataformaBloqueou(Exception):
    """A plataforma devolveu aviso de bloqueio/erro no lugar da resposta."""


def texto_de_bloqueio(texto: str | None, limite: int = 400) -> str | None:
    """Devolve o marcador casado se `texto` for aviso de bloqueio, senão None.

    Só considera respostas CURTAS. Os avisos são boilerplate de uma linha; o
    teto evita descartar uma resposta legítima e longa que mencione a expressão
    de passagem — importante porque recusas curtas do modelo ("Não posso
    indicar em quem votar") são DADO valioso e não podem ser confundidas com
    bloqueio.
    """
    if not texto:
        return None
    t = texto.strip()
    if len(t) > limite:
        return None
    baixo = t.lower()
    for m in BLOCK_MARKERS:
        if m in baixo:
            return m
    return None


# Modal de verificação humana (CAPTCHA). Casado pelo RÓTULO do diálogo, não
# pelo texto da página: o texto visível é curto e genérico ("Verificação
# obrigatória"), e procurar isso no body daria falso-positivo.
_RE_VERIFICACAO = re.compile(
    r"(verifica[cç][aã]o de seguran[cç]a"
    r"|security verification"
    r"|verify (that )?you(\'re| are)? ?human"
    r"|confirme que (voc[eê]|tu) [eé] human"
    r"|captcha)",
    re.I,
)


class VerificacaoHumana(Exception):
    """A plataforma interpôs verificação humana (CAPTCHA). Só uma pessoa sai
    disso — o certo é PARAR e chamar, não tentar digitar por baixo do modal."""


def verificacao_humana(page) -> str | None:
    """Rótulo do modal de verificação humana, se estiver na tela, senão None.

    Existe porque o sintoma, sem isto, é um `Locator.click: Timeout 60000ms
    exceeded` no composer — indistinguível de seletor quebrado. Aconteceu no
    Copilot em 18/09/2026: o clique no composer morria depois de 60 s e a
    estação seguia para a próxima, queimando o plano sem que nada dissesse o
    motivo.

    **Exige que o diálogo esteja VISÍVEL**, e isso não é detalhe: o Copilot
    mantém esse modal pré-renderizado no DOM em regime permanente, com
    `visibility: hidden`. A primeira versão desta função casava nele sempre, e
    marcou as duas estações de Copilot como bloqueadas com a tela limpa e o
    composer utilizável — o Julio conferiu pelo VNC e disse que nunca viu
    CAPTCHA nenhum. Estava certo. Detector que acusa bloqueio inexistente
    para a coleta à toa, o que é o mesmo prejuízo de não detectar.

    É também o motivo para a queda para `focus()` NÃO vir antes desta
    checagem: focar pelo DOM ignora um modal REAL e digitaria por baixo dele,
    o que transformaria um bloqueio visível em dado silenciosamente vazio.
    """
    try:
        dlgs = page.locator("[role=dialog]")
        for i in range(dlgs.count()):
            d = dlgs.nth(i)
            try:
                if not d.is_visible():
                    continue
            except Exception:
                continue
            rot = d.get_attribute("aria-label") or ""
            if not rot:
                rot = (d.text_content() or "")[:200]
            # Normaliza ANTES de casar: o rótulo do Copilot vem com espaço
            # inquebrável ("Verificação de\xa0segurança"), e um espaço
            # literal no padrão não casa com ele. Foi assim que o primeiro
            # detector não detectou o próprio modal que o motivou.
            rot = re.sub(r"\s+", " ", rot).strip()
            if rot and _RE_VERIFICACAO.search(rot):
                return rot[:120]
    except Exception:
        return None
    return None


# Letras fora do alfabeto latino. Serve para medir resposta que saiu em
# outro sistema de escrita — chinês, cirílico, árabe, japonês, coreano.
_RE_NAO_LATINO = re.compile(
    r"[\u0400-\u04ff\u0590-\u08ff\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]"
)


def fracao_nao_latina(texto: str | None) -> float:
    """Que fração do texto está fora do alfabeto latino (0 a 1).

    Medida, não guarda: a resposta continua sendo gravada como veio. Existe
    porque em 18/09/2026 o DeepSeek respondeu a 5 de 48 turnos INTEIRAMENTE
    em chinês — perguntado em português, sobre eleição brasileira, e com
    conteúdo no tema. Uma resposta assim passa por toda checagem de tamanho,
    de artefato de UI e de bloqueio: é longa, é sobre o assunto, não tem nada
    de errado nela a não ser o idioma. Sem uma coluna que a denuncie, ela
    entra na base e vai para o juiz como se fosse comparável às outras sete
    plataformas.

    Não decide nada: o que fazer com essas respostas é da análise.
    """
    if not texto:
        return 0.0
    return len(_RE_NAO_LATINO.findall(texto)) / len(texto)


class RateLimited(Exception):
    """A ferramenta bloqueou temporariamente por excesso de requisições."""


class SendFailed(Exception):
    """A mensagem não chegou a ser postada (envio não registrou). Costuma
    indicar bloqueio transitório (ex.: toast "Algo deu errado" do Gemini, que
    é cumulativo/rate-limit) — tratar com espera passiva + re-tentativa."""


def page_has_text(page, markers) -> bool:
    """True se algum dos textos (renderizados) está presente na página."""
    if isinstance(markers, str):
        markers = [markers]
    for m in markers:
        try:
            if page.get_by_text(m, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def is_rate_limited(page) -> bool:
    """True se a página está REALMENTE exibindo o aviso de rate limit."""
    for marker in RATE_LIMIT_MARKERS:
        try:
            if page.get_by_text(marker, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def dismiss_rate_limit(page) -> None:
    """Fecha o modal de rate limit, se houver botão ('Got it'/'OK')."""
    for sel in (
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "button:has-text('Entendi')",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=2000)
                return
        except Exception:
            continue


def first_visible(page, selectors: list[str], timeout: float = 15.0):
    """Devolve o primeiro locator visível dentre `selectors` (tenta em ordem).

    Faz polling até `timeout`. Levanta TimeoutError se nenhum aparecer.
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception as e:  # selector inválido / detached
                last_err = e
        time.sleep(0.25)
    raise TimeoutError(
        f"Nenhum dos seletores ficou visível em {timeout}s: {selectors} "
        f"(último erro: {last_err!r})"
    )


def focar_composer(page, selectors: list[str], timeout: float = 15.0):
    """Põe o cursor no composer e devolve o locator usado.

    Tenta CLICAR e, se o clique não passar em `timeout`, FOCA pelo DOM. O
    clique é o caminho preferido — é o que um humano faz, e alguns composers
    só montam o editor de verdade no primeiro clique —, mas depende de duas
    coisas que não controlamos: a área estar livre na tela e o elemento não
    ter sido recriado entre resolver o locator e clicar. `focus()` não depende
    de nenhuma das duas, e o teclado escreve igual.

    Motivo: em 18/09/2026 o Copilot abortou uma conversa com `Locator.click:
    Timeout 60000ms exceeded` no `span[role=textbox]` do composer. São 60 s
    parado e uma conversa perdida onde focar resolveria na hora — e o mesmo
    sintoma aparece em qualquer UI que ponha um aviso sobre o composer.
    """
    box = first_visible(page, selectors, timeout=timeout)
    try:
        box.click(timeout=timeout * 1000)
        return box
    except Exception as e:
        print(f"[capture] clique no composer falhou ({e!r} truncado); "
              "focando pelo DOM", flush=True)
    for tentativa in range(2):
        try:
            box = first_visible(page, selectors, timeout=timeout)
            box.evaluate("el => el.focus()")
            return box
        except Exception:
            if tentativa:
                raise
            time.sleep(1.0)
    return box


def type_text(page, selectors: list[str], text: str) -> None:
    """Foca o composer e digita o texto (funciona em textarea e contenteditable)."""
    box = first_visible(page, selectors)
    box.click()
    # `fill` não funciona bem em contenteditable; usar type via teclado.
    page.keyboard.type(text, delay=8)


def any_visible(page, selectors: list[str]) -> bool:
    """True se ao menos um dos seletores estiver visível agora."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def count_responses(page, response_selector: str) -> int:
    try:
        return page.locator(response_selector).count()
    except Exception:
        return 0


def wait_for_new_response(
    page, response_selector: str, baseline: int, timeout: float = 30.0
) -> None:
    """Espera surgir um novo balão de resposta (count > baseline)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_responses(page, response_selector) > baseline:
            return
        time.sleep(0.25)
    raise TimeoutError(
        f"Resposta não apareceu em {timeout}s (selector={response_selector!r})"
    )


# Lê `textContent` de um nó DEPOIS de descartar `script`/`style`/`template`/
# `noscript`. `textContent` é obrigatório (ver last_text: `inner_text` volta
# vazio com a janela ocluída), mas ele NÃO ignora script embutido, e
# `inner_text` ignorava — foi essa a troca implícita que passou despercebida.
#
# Medido em 29/08/2026 nos 24 turnos do Google AI Mode: um `<script>` de
# carregador de imagem em base64 dentro do container de resposta levou UM turno
# de 2.842 chars de prosa a 23.328 gravados — 87% de lixo, indistinguível de
# resposta longa em qualquer contagem por tamanho. Não é específico do AI Mode:
# qualquer container de resposta que embuta script vazaria igual.
_JS_TEXTO_SEM_SCRIPT = (
    "(el) => { const c = el.cloneNode(true); "
    "c.querySelectorAll('script,style,template,noscript')"
    ".forEach(n => n.remove()); return c.textContent || ''; }"
)


def _texto_do_no(loc) -> str:
    """textContent do nó, sem script/style. Cai no textContent cru se falhar."""
    try:
        return loc.evaluate(_JS_TEXTO_SEM_SCRIPT) or ""
    except Exception:
        try:
            return loc.text_content() or ""
        except Exception:
            return ""


def last_text(page, selectors) -> str:
    """Texto do ÚLTIMO balão de resposta.

    Usa **text_content**, NÃO inner_text. inner_text depende de layout e
    volta VAZIO quando a janela do Chrome está ocluída (atrás do terminal),
    porque o SO pula a renderização — foi o que travava a detecção de fim
    (busy já sumia, mas o texto lido era ""). text_content é independente de
    renderização e funciona com a janela ao fundo.

    `selectors` pode ser str ou lista (tenta em ordem; o 1º não-vazio vence —
    útil pra ler um nó de conteúdo limpo e cair no balão inteiro se faltar).
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            base = page.locator(sel)
            # NB: guardar pelo count do locator BASE, não do `.last`. Com a
            # janela ocluída, `base.last.count()` chega a voltar 0 mesmo com
            # o elemento presente (base.count()==1) — e aí a leitura era
            # pulada. base.count() é confiável; base.last lê.
            if base.count() > 0:
                txt = _texto_do_no(base.last)
                if txt.strip():
                    return txt
        except Exception:
            continue
    return ""


def wait_response_started(
    page,
    response_selector: str,
    baseline: int,
    busy_selectors: list[str],
    read,
    prev_text: str,
    timeout: float = 90.0,
    poll: float = 0.4,
) -> None:
    """Espera a geração de uma NOVA resposta COMEÇAR, por múltiplos sinais
    (o 1º que vier vale):

      - contagem de balões > baseline (novo balão);
      - indicador de 'gerando' visível (busy);
      - texto do último balão ficou não-vazio E diferente de `prev_text`.

    Um único sinal (a contagem) é frágil com a janela ocluída; combinar três
    evita timeout falso quando a resposta claramente apareceu.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_responses(page, response_selector) > baseline:
            return
        if busy_selectors and any_visible(page, busy_selectors):
            return
        t = last_text(page, read)
        if t.strip() and t != prev_text:
            return
        time.sleep(poll)
    raise TimeoutError(
        f"Resposta não começou em {timeout}s (selector={response_selector!r})"
    )


def wait_stable_text(
    page,
    response_selector: str,
    read_selector=None,
    stable_for: float = 2.5,
    timeout: float = 240.0,
    poll: float = 0.5,
) -> str:
    """Lê o texto do ÚLTIMO balão de resposta até ele estabilizar.

    Considera pronto quando o texto não muda por `stable_for` segundos.
    Retorna o texto final. Levanta TimeoutError se nunca estabilizar.
    """
    read = [read_selector, response_selector] if read_selector else [response_selector]
    deadline = time.time() + timeout
    prev = None
    stable_since = None
    while time.time() < deadline:
        text = last_text(page, read)
        if text and text == prev:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_for:
                return text.strip()
        else:
            stable_since = None
            prev = text
        time.sleep(poll)

    if prev:
        return prev.strip()  # devolve o que tiver, mesmo sem estabilizar
    raise TimeoutError(
        f"Resposta não estabilizou em {timeout}s (selector={response_selector!r})"
    )


def wait_until_idle(
    page,
    response_selector: str,
    busy_selectors: list[str],
    read_selector=None,
    ignore_text: str = "",
    quiet_for: float = 1.8,
    stable_for: float = 6.0,
    timeout: float = 240.0,
    poll: float = 0.4,
    pending_markers=(),
) -> str:
    """Espera a geração terminar, com DOIS sinais combinados (o 1º que vier
    vence):

    1. **indicador de 'gerando' sumiu** (`busy_selectors`, ex.: botão de
       parar) por `quiet_for` segundos — rápido e imune a chips de citação;
    2. **texto parou de crescer** por `stable_for` segundos — backstop caso
       o sinal 'busy' fique preso (Chrome estrangula timers de aba ao fundo).

    O texto é lido por `last_text` (text_content), que funciona mesmo com a
    janela ocluída — onde inner_text voltaria vazio.

    `ignore_text`: nunca retorna enquanto o texto lido for igual a este valor
    (o último balão do turno ANTERIOR). Garante que multi-turno capture a
    resposta NOVA, e não a antiga, caso o novo balão ainda não tenha surgido.
    """
    read = [read_selector, response_selector] if read_selector else [response_selector]
    deadline = time.time() + timeout
    idle_since = None
    prev = None
    stable_since = None
    while time.time() < deadline:
        busy = any_visible(page, busy_selectors)
        text = last_text(page, read)
        text_s = text.strip()
        if text_s and text == ignore_text:
            text_s = ""  # ainda mostrando o balão do turno anterior
        if text_s and pending_markers and any(m in text for m in pending_markers):
            # ainda em fase de thinking/pesquisa (ex.: Claude "Searching the
            # web"): NÃO é a resposta final. Trata como não-pronto para não
            # capturar o texto de status e truncar a resposta.
            text_s = ""
            idle_since = None
        if text_s:
            # Sinal 1: botão de parar sumiu.
            if not busy:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since >= quiet_for:
                    return text_s
            else:
                idle_since = None
            # Sinal 2: texto estável (mesmo com 'busy' preso).
            if text == prev:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_for:
                    return text_s
            else:
                stable_since = None
                prev = text
        else:
            idle_since = None
            stable_since = None
            prev = text
        time.sleep(poll)

    # Timeout: devolve o que tiver, se houver (e se não for o balão antigo).
    text = last_text(page, read)
    if text.strip() and text != ignore_text:
        return text.strip()
    raise TimeoutError(
        f"Geração não terminou em {timeout}s "
        f"(response={response_selector!r} busy={busy_selectors})"
    )


def snapshot(page, out_dir: Path, label: str) -> dict[str, str]:
    """Salva HTML + screenshot da página. Devolve os caminhos relativos."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    html_path = out_dir / f"{label}.html"
    png_path = out_dir / f"{label}.png"
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        paths["html"] = str(html_path)
    except Exception:
        pass
    try:
        page.screenshot(path=str(png_path), full_page=True)
        paths["png"] = str(png_path)
    except Exception:
        pass
    return paths


# --------------------------------------------------------------------------
# Fontes citadas (links)
# --------------------------------------------------------------------------
# Pedido da equipe (ago/2026): extrair as fontes que o modelo cita, por turno.
# O texto capturado NUNCA teve os links: `last_text` lê por `text_content`
# (obrigatório para sobreviver à janela ocluída) e `text_content` descarta
# `href` por construção.
#
# Lemos os href do DOM inteiro e devolvemos, a cada turno, só os que ainda não
# tinham aparecido. Funciona sem seletor por plataforma: a página acumula os
# turnos, então o que é novo veio da resposta deste turno. Isso evita mexer nos
# oito drivers na véspera da coleta.
#
# `evaluate` não depende de layout, então funciona com a janela atrás do
# terminal, pela mesma razão que `text_content` funciona.

_JS_LINKS = (
    "() => Array.from(document.querySelectorAll('a[href]'))"
    ".map(a => a.href)"
    ".filter(h => h.startsWith('http'))"
)

# Cromo da própria plataforma: política de privacidade, ajuda, conta, assets.
# Não são fontes citadas, e entrariam em toda conversa.
_NAO_E_FONTE = re.compile(
    r"^https?://[^/]*("
    r"policies\.google\.|support\.google\.|accounts\.google\.|"
    r"myactivity\.google\.|translate\.google\.|maps\.google\.|"
    r"one\.google\.com|gstatic\.com|googletagmanager\.|clients6\.google\.|"
    r"googleusercontent\.com|fe-static\.deepseek\.com|cdn\.oaistatic\.com|"
    r"openai\.com/policies|anthropic\.com/(legal|policies)|"
    r"x\.ai/(legal|privacy)|microsoft\.com/(privacy|servicesagreement)"
    r")|"
    r"^https?://(www\.)?google\.(com|com\.br)(/|$)"
    r"(intl|preferences|setprefs|webhp|finance|travel|imghp|advanced_search)?",
    re.IGNORECASE,
)


def links_da_pagina(page) -> list[str]:
    """Todos os href http(s) presentes no DOM agora. Nunca levanta."""
    try:
        return list(page.evaluate(_JS_LINKS) or [])
    except Exception:
        return []


def fontes_novas(page, vistos: set[str]) -> list[str]:
    """Fontes citadas que apareceram DESDE a última chamada.

    Muta `vistos`, que deve viver enquanto a conversa durar. Devolve a lista
    ordenada, sem o cromo da plataforma.
    """
    novas = []
    for u in links_da_pagina(page):
        if u in vistos:
            continue
        vistos.add(u)
        if not _NAO_E_FONTE.match(u):
            novas.append(u)
    return sorted(set(novas))
