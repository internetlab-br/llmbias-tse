# -*- coding: utf-8 -*-
"""Guarda do insumo das perguntas nominais (smoke de 17/09).

Sem data/candidatas_genero.csv, o planejamento seguia com 42 avisos, o
sorteio da candidata era pulado em silêncio e a coleta abortava horas
depois com KeyError('candidata') no primeiro turno nominal — o eixo de
gênero inteiro parava. As duas camadas testadas aqui:

  1. `plan_round` FALHA de imediato, com mensagem que diz o que fazer,
     quando uma alternativa nominal não tem insumo;
  2. se um placeholder escapar mesmo assim, a renderização da ficha erra
     com mensagem legível (alternativa + placeholder), não KeyError.
"""
from dataclasses import replace

import pytest

from llmbias_tse.instrument import ficha, plan_round, sortear_temas
from llmbias_tse.instrumentos import INSTRUMENTO_GENERO

PIDS = ["P01", "P02"]


def _temas():
    return sortear_temas(INSTRUMENTO_GENERO, PIDS, seed=3)


def test_plan_round_falha_sem_insumo_nominal():
    sem_insumo = replace(INSTRUMENTO_GENERO, candidatas=())
    with pytest.raises(RuntimeError) as exc:
        plan_round(sem_insumo, PIDS, seed=3, temas_por_perfil=_temas())
    msg = str(exc.value)
    assert "Insumo das perguntas nominais ausente" in msg
    assert "candidatas_genero.csv" in msg


@pytest.mark.skipif(not INSTRUMENTO_GENERO.candidatas,
                    reason="sem data/candidatas_genero.csv nesta máquina")
def test_plan_round_com_insumo_renderiza_sem_placeholder():
    rot, _ = plan_round(INSTRUMENTO_GENERO, PIDS, seed=3,
                        temas_por_perfil=_temas())
    fichas = [ficha(INSTRUMENTO_GENERO, t) for p in PIDS for t in rot[p]]
    assert any("PERGUNTA NOMINAL" in f for f in fichas)
    assert not any("{candidata" in f or "{cargo" in f for f in fichas)


@pytest.mark.skipif(not INSTRUMENTO_GENERO.candidatas,
                    reason="sem data/candidatas_genero.csv nesta máquina")
def test_ficha_erra_legivel_quando_falta_placeholder():
    rot, _ = plan_round(INSTRUMENTO_GENERO, PIDS, seed=3,
                        temas_por_perfil=_temas())
    alvo = None
    for p in PIDS:
        for t in rot[p]:
            for q in t.perguntas:
                if (q.pedido is not None and q.pedido.nominal
                        and "{candidata" in q.pedido.texto_pedido):
                    alvo = replace(t, perguntas=(replace(q, exemplares=()),))
                    break
            if alvo:
                break
        if alvo:
            break
    assert alvo is not None
    with pytest.raises(RuntimeError) as exc:
        ficha(INSTRUMENTO_GENERO, alvo)
    msg = str(exc.value)
    assert "placeholder" in msg and "KeyError" not in msg
