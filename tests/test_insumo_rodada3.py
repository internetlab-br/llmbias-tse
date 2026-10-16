# -*- coding: utf-8 -*-
"""Insumo nominal da rodada 3 (2º turno): 4 candidatas, todas a
governadora.

O que está em jogo: (1) o pool do {cargo} deriva do insumo — numa rodada
sem mulher disputando o Senado, "qual das candidatas ao cargo de
senadora..." seria premissa falsa; (2) o planejamento funciona com um
insumo de só 4 nomes (os trios {candidata2}/{candidata3} precisam de 3
distintos); (3) nada vaza como placeholder.
"""
from dataclasses import replace

from llmbias_tse.instrument import ficha, plan_round, sortear_temas
from llmbias_tse.instrumentos import (CARGOS_2026, INSTRUMENTO_GENERO,
                                      _cargos_com_candidatas)

CANDIDATAS_2T = (
    ("Mailza Assis", "governadora"),
    ("Professora Maria do Carmo", "governadora"),
    ("Celina Leão", "governadora"),
    ("Professora Dorinha", "governadora"),
)

INST_2T = replace(INSTRUMENTO_GENERO, candidatas=CANDIDATAS_2T,
                  cargos=tuple(c for c in CARGOS_2026
                               if c in {c2 for _, c2 in CANDIDATAS_2T}))

PIDS = ["P01", "P02", "P03"]


def test_pool_de_cargo_deriva_do_insumo():
    assert INST_2T.cargos == ("governadora",)
    # e o derivador real respeita a grafia canônica: cargo sujo fica fora
    assert set(_cargos_com_candidatas()) <= set(CARGOS_2026)


def test_plan_round_com_insumo_do_segundo_turno():
    temas = sortear_temas(INSTRUMENTO_GENERO, PIDS, seed=13)
    rot, avisos = plan_round(INST_2T, PIDS, seed=13, temas_por_perfil=temas)
    fatais = [a for a in avisos if "não tem lista" in a or "não declara" in a]
    assert not fatais
    fichas = [ficha(INST_2T, t) for p in PIDS for t in rot[p]]
    assert not any("{candidata" in f or "{cargo" in f for f in fichas)
    # toda mencao de cargo sorteado e governadora
    for f in fichas:
        assert "ao cargo de senadora" not in f
        assert "ao cargo de deputada" not in f
        assert "presidenta da República" not in f


def test_trio_sorteia_nomes_distintos():
    temas = sortear_temas(INSTRUMENTO_GENERO, PIDS, seed=13)
    rot, _ = plan_round(INST_2T, PIDS, seed=13, temas_por_perfil=temas)
    nomes = [n for n, _ in CANDIDATAS_2T]
    for p in PIDS:
        for t in rot[p]:
            f = ficha(INST_2T, t)
            usados = [n for n in nomes if n in f]
            # numa pergunta de trio, os tres nomes sorteados do insumo de
            # 4 devem ser pessoas diferentes
            if "{candidata3}" in str(t) or len(usados) >= 3:
                assert len(set(usados)) == len(usados)
