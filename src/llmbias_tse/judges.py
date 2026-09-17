"""Painel de juízes: o mesmo prompt do LLM as a judge rodado em três modelos.

Decisão de 21/08/2026 (Julio): em vez de um juiz só, o experimento roda **três
juízes independentes** e a concordância entre eles vira dado. Isso ataca a
preocupação levantada pelo Luiz — a de que a qualidade do juiz só seria
mensurável com N muito grande: com três modelos, a divergência entre eles é
observável em toda conversa, sem depender de anotação humana.

Painel padrão (`JUIZES_PADRAO`):
  - `flash`     google/gemini-3.7-flash
  - `sonnet`    anthropic/claude-sonnet-5    (effort high)
  - `luna`      openai/gpt-5.6-luna          (reasoning high)

Cada provedor expõe a MESMA interface: recebe o prompt já montado por
`judge._build_prompt` e devolve o objeto `Extracao` validado. O prompt é
idêntico para os três — o que varia é só o modelo, para que a divergência seja
atribuível ao juiz e não ao estímulo.

Chaves, do `.env`: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.
Um juiz sem chave é PULADO com aviso, não quebra a rodada.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

from . import llm


@dataclass(frozen=True)
class Juiz:
    """Um juiz do painel: rótulo curto, provedor e modelo."""
    key: str
    provider: str      # "google" | "anthropic" | "openai"
    model: str
    effort: str = "high"

    @property
    def env_key(self) -> str:
        return {
            "google": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }[self.provider]

    def disponivel(self) -> bool:
        load_dotenv()
        return bool(os.environ.get(self.env_key))


JUIZES_PADRAO: tuple[Juiz, ...] = (
    # Painel da rodada 2 (set/2026): flash + sonnet + luna, os mesmos que o
    # `llmbias-tse-reports` registra como tendo julgado a base. O painel antigo
    # (gemini-3.1-pro + opus + gpt-5.6-sol) custava ~$1/conversa; este fica em
    # torno de um quinto disso, com o mesmo desenho de 3 juízes por turno.
    Juiz("flash", "google", os.environ.get("LLMBIAS_JUIZ_FLASH",
                                           "gemini-3.7-flash")),
    # Sonnet, não Opus, a partir da rodada 2 (set/2026): o painel de 3 juízes
    # por turno custava ~$1/conversa e o Opus respondia por metade disso. A
    # CHAVE mudou junto com o modelo de propósito — ela vai para `por_juiz` no
    # dado, e deixá-la como "opus" rodando Sonnet gravaria um rótulo falso de
    # qual modelo julgou.
    Juiz("sonnet", "anthropic", os.environ.get("LLMBIAS_JUIZ_SONNET",
                                               "claude-sonnet-5")),
    Juiz("luna", "openai", os.environ.get("LLMBIAS_JUIZ_LUNA",
                                          "gpt-5.6-luna")),
)

JUIZES_POR_KEY = {j.key: j for j in JUIZES_PADRAO}


# --------------------------------------------------------------------------
# Clientes (um por provedor, memoizados)
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _anthropic_client():
    import anthropic
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY ausente no .env")
    return anthropic.Anthropic(api_key=key)


@lru_cache(maxsize=1)
def _openai_client():
    import openai
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY ausente no .env")
    return openai.OpenAI(api_key=key)


# --------------------------------------------------------------------------
# Extração estruturada, uma função por provedor
# --------------------------------------------------------------------------

def _google(prompt: str, schema, juiz: Juiz):
    # Reaproveita o wrapper que já existe (retry de erro transitório incluso).
    return llm.generate_structured(prompt, schema, temperature=0.1,
                                   model=juiz.model)


def _anthropic(prompt: str, schema, juiz: Juiz):
    client = _anthropic_client()
    # `messages.parse` valida a resposta contra o schema Pydantic. Thinking
    # adaptativo + effort são o recomendado no Opus 5; sem `temperature`
    # (removida nessa família).
    msg = client.messages.parse(
        model=juiz.model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": juiz.effort},
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    # A resposta validada vem no bloco de texto (`parsed_output`), não num
    # atributo no topo da mensagem.
    for bloco in msg.content:
        parsed = getattr(bloco, "parsed_output", None)
        if parsed is not None:
            return parsed
    raise RuntimeError(
        f"{juiz.key}: resposta sem parsed_output (stop_reason="
        f"{msg.stop_reason!r})"
    )


def _openai(prompt: str, schema, juiz: Juiz):
    client = _openai_client()
    resp = client.responses.parse(
        model=juiz.model,
        reasoning={"effort": juiz.effort},
        input=[{"role": "user", "content": prompt}],
        text_format=schema,
    )
    return resp.output_parsed


_DISPATCH = {"google": _google, "anthropic": _anthropic, "openai": _openai}


def extrair(juiz: Juiz, prompt: str, schema):
    """Roda UM juiz sobre um prompt já montado e devolve o objeto validado."""
    fn = _DISPATCH.get(juiz.provider)
    if fn is None:
        raise ValueError(f"provedor desconhecido: {juiz.provider!r}")
    return fn(prompt, schema, juiz)


def painel(keys: list[str] | None = None) -> list[Juiz]:
    """Os juízes pedidos que têm chave configurada (os demais saem com aviso)."""
    escolhidos = ([JUIZES_POR_KEY[k] for k in keys] if keys
                  else list(JUIZES_PADRAO))
    prontos = []
    for j in escolhidos:
        if j.disponivel():
            prontos.append(j)
        else:
            print(f"[juizes] {j.key} ({j.model}) PULADO: {j.env_key} ausente")
    return prontos
