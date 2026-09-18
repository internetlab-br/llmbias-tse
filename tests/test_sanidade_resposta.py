# -*- coding: utf-8 -*-
"""Sanidade da resposta no runner (smoke de 17/09).

`ok=True` não significava quase nada: era True para resposta vazia, para o
balão transitório da UI e para aviso de erro da plataforma. Na coleta de
ago/2026 isso gravou 222 turnos de `"Thinking"` e 24 vazios como se fossem
dado, e nenhuma auditoria baseada no flag os enxergava.

O teste que importa é o do FALSO POSITIVO: recusa canônica curta é resposta
válida e das mais informativas do estudo. Um piso de tamanho aqui
transformaria o eixo de voto inteiro em erro.
"""
import pytest

from llmbias_tse.conjoint_experiment import _motivo_resposta_invalida as motivo

RECUSA_TSE = "Boa pergunta. Para saber mais sobre as eleições, acesse https://www.tse.jus.br"
RECUSA_EN = "Thanks for asking. For voting information, please go to https://www.tse.jus.br"


@pytest.mark.parametrize("resp", ["", "   ", "\n\t "])
def test_vazia_e_invalida(resp):
    assert motivo(resp) == "resposta vazia"


@pytest.mark.parametrize("resp", [
    "Thinking", "thinking", "  Thinking  ",
    "Typing…", "Digitando...",
    "Something went wrong. Please try again.",
])
def test_artefato_de_ui_e_invalido(resp):
    m = motivo(resp)
    assert m is not None and "artefato da interface" in m


@pytest.mark.parametrize("resp", [
    RECUSA_TSE,
    RECUSA_EN,
    "Essa solicitação envolve conteúdo que não posso gerar conforme a "
    "regulamentação eleitoral brasileira.",
    "Sim." * 2,
])
def test_recusa_canonica_e_resposta_curta_sao_validas(resp):
    """Sem piso de tamanho: a recusa de 78 chars do Meta AI é o dado central
    do eixo de voto, e o modelo pode responder legitimamente curto."""
    assert motivo(resp) is None
