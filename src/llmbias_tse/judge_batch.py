"""Julgamento em LOTE (Batch API), 50% mais barato que o modo síncrono.

Julgar não é sensível a latência: as conversas já estão coletadas e o resultado
só é usado na análise. Os três provedores cobram metade do preço para
processamento assíncrono, então o painel inteiro do experimento sai pela metade
sem tocar no instrumento — nem no prompt, nem no modelo, nem na rubrica.

Fluxo (igual nos três provedores, os detalhes é que mudam):

    itens = preparar(conversas, rubricas)   # um item por (conversa, turno)
    lote  = submeter(juiz, itens)           # devolve o id do lote
    ...                                     # espera (minutos a horas)
    res   = coletar(juiz, lote)             # {custom_id: Extracao}
    anotar_do_lote(...)                     # remonta as anotações

O `custom_id` carrega `conversation_id` e o número do turno, porque os
resultados voltam FORA DE ORDEM — nunca casar por posição.

Estado do suporte (ago/2026):
  - anthropic: `messages.batches`, aceita `output_config` (saída estruturada
    validada) nos params — testado.
  - openai:    `batches` sobre um arquivo JSONL de requisições.
  - google:    `batches` (o SDK expõe create/get/list).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import judge
from .judges import Juiz, _anthropic_client, _openai_client

# Separador do custom_id. A Anthropic exige `^[a-zA-Z0-9_-]{1,64}$`, então o
# separador tem de ser um desses caracteres: usamos "__" (o conversation_id usa
# "_" simples, nunca duplo, então a volta é inequívoca).
SEP = "__"
CUSTOM_ID_MAX = 64


@dataclass(frozen=True)
class ItemLote:
    """Uma unidade de julgamento: o prompt de um turno de uma conversa."""
    custom_id: str
    conversation_id: str
    turno: int
    eixo: str
    prompt: str


def preparar(conversas: list[dict], rubricas: dict) -> list[ItemLote]:
    """Monta um item por (conversa, turno avaliável), com o prompt já pronto.

    Usa exatamente o mesmo `_build_prompt` do modo síncrono — o lote não pode
    julgar por um prompt diferente, senão os resultados não são comparáveis.
    """
    itens: list[ItemLote] = []
    for rec in conversas:
        eixo = rec.get("eixo")
        rubric = rubricas.get(eixo)
        if rubric is None:
            continue
        turns = rec.get("turns", [])
        cid = rec["conversation_id"]
        for i, t in enumerate(turns):
            resposta = (t.get("response") or "").strip()
            if not (t.get("ok") and resposta):
                continue
            prompt = judge._build_prompt(
                rubric, judge._format_prev(turns, i), t.get("prompt", ""),
                resposta,
            )
            n = t.get("turn")
            custom_id = f"{cid}{SEP}{n}"
            if len(custom_id) > CUSTOM_ID_MAX:
                raise ValueError(
                    f"custom_id longo demais ({len(custom_id)} > "
                    f"{CUSTOM_ID_MAX}): {custom_id!r}"
                )
            itens.append(ItemLote(
                custom_id=custom_id, conversation_id=cid, turno=n,
                eixo=eixo, prompt=prompt,
            ))
    return itens


def desmontar_id(custom_id: str) -> tuple[str, int]:
    """Volta do custom_id para (conversation_id, turno)."""
    cid, _, n = custom_id.rpartition(SEP)
    return cid, int(n)


def _estritar(no):
    """Torna o schema ESTRITO, recursivamente.

    Os dois provedores exigem, em modo estrito, que todo objeto declare
    `additionalProperties: false` e liste TODAS as propriedades em `required`
    — o `model_json_schema()` do Pydantic não faz nem um nem outro (campos com
    default ficam fora de `required`).
    """
    if isinstance(no, dict):
        if no.get("type") == "object" and "properties" in no:
            no["additionalProperties"] = False
            no["required"] = list(no["properties"].keys())
        for v in no.values():
            _estritar(v)
    elif isinstance(no, list):
        for v in no:
            _estritar(v)
    return no


def _schema() -> dict:
    return _estritar(judge.Extracao.model_json_schema())


def _validar(cid: str, txt: str, juiz_key: str) -> judge.Extracao | None:
    """Valida o JSON de um item do lote. NUNCA falha em silêncio.

    Alguns provedores devolvem `null` dentro das listas quando não há achado
    (visto no Gemini: `{"achados": [null]}`), o que não valida contra o schema.
    Limpamos os nulos antes de validar; qualquer outra falha é REPORTADA, e não
    engolida — um item perdido em silêncio vira uma anotação a menos sem que
    ninguém perceba.
    """
    try:
        bruto = json.loads(txt)
    except Exception as e:  # noqa: BLE001
        print(f"[lote:{juiz_key}] {cid}: JSON inválido ({e})")
        return None
    if isinstance(bruto, dict):
        for chave in ("achados", "resistencia"):
            v = bruto.get(chave)
            if isinstance(v, list):
                bruto[chave] = [x for x in v if x is not None]
    try:
        return judge.Extracao.model_validate(bruto)
    except Exception as e:  # noqa: BLE001
        print(f"[lote:{juiz_key}] {cid}: não valida contra o schema ({e})")
        return None


# --------------------------------------------------------------------------
# Submissão
# --------------------------------------------------------------------------

def _pensamento(juiz: Juiz) -> int:
    """Orçamento de pensamento do lote — o MESMO do modo síncrono."""
    from .judges import _orcamento_pensamento
    return _orcamento_pensamento(juiz)


def submeter(juiz: Juiz, itens: list[ItemLote]) -> str:
    if juiz.provider == "anthropic":
        return _submeter_anthropic(juiz, itens)
    if juiz.provider == "openai":
        return _submeter_openai(juiz, itens)
    if juiz.provider == "google":
        return _submeter_google(juiz, itens)
    raise NotImplementedError(
        f"lote ainda não implementado para {juiz.provider!r} "
        f"(use o modo síncrono para esse juiz)"
    )


def _submeter_google(juiz: Juiz, itens: list[ItemLote]) -> str:
    from google.genai import types as T

    from . import llm

    client = llm.get_client()
    reqs = [
        {
            "contents": [{"role": "user", "parts": [{"text": it.prompt}]}],
            # o custom_id volta em `inlined_responses[].metadata`
            "metadata": {"custom_id": it.custom_id},
            # MESMA configuração do modo síncrono (`llm.generate_structured`):
            # o schema vai como o modelo Pydantic, não como JSON Schema cru —
            # com o schema cru o Gemini devolveu `{"achados":[null]}` e saídas
            # truncadas em 120k chars. `max_output_tokens` limita a fuga.
            "config": T.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=judge.Extracao,
                max_output_tokens=8192,
                # Effort baixo também no lote — senão o síncrono e o
                # assíncrono julgariam com configuração diferente e os
                # resultados deixariam de ser comparáveis.
                thinking_config=T.ThinkingConfig(
                    thinking_budget=_pensamento(juiz)),
            ),
        }
        for it in itens
    ]
    job = client.batches.create(model=juiz.model, src=reqs)
    return job.name


def _submeter_anthropic(juiz: Juiz, itens: list[ItemLote]) -> str:
    client = _anthropic_client()
    reqs = [
        {
            "custom_id": it.custom_id,
            "params": {
                "model": juiz.model,
                "max_tokens": 16000,
                # Sem `thinking` quando o effort é baixo (ver `judges`).
                **({"thinking": {"type": "adaptive"}}
                   if _pensamento(juiz) else {}),
                "output_config": {
                    "effort": juiz.effort,
                    "format": {
                        "type": "json_schema",
                        "schema": _schema(),
                    },
                },
                "messages": [{"role": "user", "content": it.prompt}],
            },
        }
        for it in itens
    ]
    lote = client.messages.batches.create(requests=reqs)
    return lote.id


def _submeter_openai(juiz: Juiz, itens: list[ItemLote]) -> str:
    import io

    client = _openai_client()
    linhas = []
    for it in itens:
        linhas.append(json.dumps({
            "custom_id": it.custom_id,
            "method": "POST",
            "url": "/v1/responses",
            "body": {
                "model": juiz.model,
                "reasoning": {"effort": juiz.effort},
                "input": [{"role": "user", "content": it.prompt}],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "extracao",
                        "schema": _schema(),
                    }
                },
            },
        }, ensure_ascii=False))
    buf = io.BytesIO("\n".join(linhas).encode("utf-8"))
    buf.name = "lote.jsonl"
    arq = client.files.create(file=buf, purpose="batch")
    lote = client.batches.create(
        input_file_id=arq.id, endpoint="/v1/responses",
        completion_window="24h",
    )
    return lote.id


# --------------------------------------------------------------------------
# Estado e coleta
# --------------------------------------------------------------------------

def estado(juiz: Juiz, lote_id: str) -> str:
    """Estado do lote, normalizado: 'em_andamento' | 'pronto' | 'erro'."""
    if juiz.provider == "anthropic":
        b = _anthropic_client().messages.batches.retrieve(lote_id)
        return "pronto" if b.processing_status == "ended" else "em_andamento"
    if juiz.provider == "openai":
        b = _openai_client().batches.retrieve(lote_id)
        if b.status == "completed":
            return "pronto"
        if b.status in ("failed", "expired", "cancelled"):
            return "erro"
        return "em_andamento"
    if juiz.provider == "google":
        from . import llm
        j = llm.get_client().batches.get(name=lote_id)
        st = str(getattr(j, "state", ""))
        if "SUCCEEDED" in st or "PARTIALLY" in st:
            return "pronto"
        if any(x in st for x in ("FAILED", "CANCELLED", "EXPIRED")):
            return "erro"
        return "em_andamento"
    raise NotImplementedError(juiz.provider)


def coletar(juiz: Juiz, lote_id: str) -> dict[str, judge.Extracao]:
    """Resultados do lote, por `custom_id`.

    Os resultados voltam FORA DE ORDEM nos dois provedores — por isso o
    dicionário é indexado pelo custom_id, nunca pela posição.
    """
    out: dict[str, judge.Extracao] = {}
    if juiz.provider == "anthropic":
        for r in _anthropic_client().messages.batches.results(lote_id):
            if r.result.type != "succeeded":
                continue
            for bloco in r.result.message.content:
                txt = getattr(bloco, "text", None)
                if not txt:
                    continue
                ext = _validar(r.custom_id, txt, juiz.key)
                if ext is not None:
                    out[r.custom_id] = ext
                break
        return out
    if juiz.provider == "openai":
        client = _openai_client()
        b = client.batches.retrieve(lote_id)
        if not b.output_file_id:
            return out
        conteudo = client.files.content(b.output_file_id).text
        for linha in conteudo.splitlines():
            if not linha.strip():
                continue
            d = json.loads(linha)
            cid = d.get("custom_id")
            corpo = (d.get("response") or {}).get("body") or {}
            txt = _texto_openai(corpo)
            if cid and txt:
                ext = _validar(cid, txt, juiz.key)
                if ext is not None:
                    out[cid] = ext
        return out
    if juiz.provider == "google":
        from . import llm
        j = llm.get_client().batches.get(name=lote_id)
        dest = getattr(j, "dest", None)
        for r in (getattr(dest, "inlined_responses", None) or []):
            meta = getattr(r, "metadata", None) or {}
            cid = meta.get("custom_id") if isinstance(meta, dict) else None
            resp = getattr(r, "response", None)
            if not cid or resp is None or getattr(r, "error", None):
                continue
            txt = getattr(resp, "text", None)
            if not txt:
                continue
            ext = _validar(cid, txt, juiz.key)
            if ext is not None:
                out[cid] = ext
        return out
    raise NotImplementedError(juiz.provider)


def _texto_openai(corpo: dict) -> str | None:
    """O texto da resposta do /v1/responses, onde quer que ele esteja."""
    if corpo.get("output_text"):
        return corpo["output_text"]
    for item in corpo.get("output") or []:
        for c in item.get("content") or []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                return c["text"]
    return None


def aguardar(juiz: Juiz, lote_id: str, *, intervalo: float = 30.0,
             teto_s: float = 24 * 3600) -> str:
    """Espera o lote terminar (bloqueante). Devolve o estado final."""
    fim = time.time() + teto_s
    while time.time() < fim:
        st = estado(juiz, lote_id)
        if st != "em_andamento":
            return st
        time.sleep(intervalo)
    return "em_andamento"
