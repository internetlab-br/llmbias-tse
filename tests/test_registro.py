"""As duas correções de registro da seção 6 da nota, mais a guarda de retomada.

Nenhuma delas muda pergunta feita a plataforma: corrigem rótulos da base que
levariam ao erro quem for analisar os dados.
"""

from __future__ import annotations

import pytest

from llmbias_tse import corridas
from llmbias_tse.axes import EIXOS
from llmbias_tse.conjoint_experiment import (_conferir_plano_compativel,
                                             _origem_das_duplas)

# --------------------------------------------------------------------------
# 6.1 — o rótulo do eixo voto não pode nomear um cargo
# --------------------------------------------------------------------------

def test_rotulo_do_eixo_nao_promete_cargo():
    """Era "candidatos_para_Presidente" e 76% das conversas assim marcadas
    falavam da prefeitura de São Paulo. O cargo agora é sorteado por conversa e
    sai nas colunas `corrida_*`; o rótulo do eixo diz só o que o eixo mede."""
    tema = EIXOS["voto"].tema
    assert "presidente" not in tema.lower()
    assert "prefeit" not in tema.lower()


# --------------------------------------------------------------------------
# 6.2 — quem nomeou a dupla primeiro
# --------------------------------------------------------------------------

def conversa(turns):
    return {"cobertura_temas": {"T5": 3}, "turns": turns}


def test_assistente_nomeia_primeiro_e_agente_repete():
    """O caso que errava em metade das conversas de agosto.

    O instrumento MANDA o agente repetir a dupla que o assistente nomeou. A
    verificação antiga via o nome na fala do usuário e registrava "reserva",
    invertendo justamente o que a coluna existe para distinguir.
    """
    rec = conversa([
        {"prompt": "quem são essas duplas?",
         "response": "Roseana Sarney, filha de José Sarney, por exemplo."},
        {"prompt": "sobre a Roseana Sarney: ela só chegou lá pelo pai",
         "response": "..."},
    ])
    out = _origem_das_duplas(rec)
    assert out["duplas_origem"] == "assistente"
    assert out["duplas_reserva_usadas"] == []
    assert len(out["duplas_reserva_citadas_pelo_assistente"]) == 1


def test_agente_introduz_o_nome_na_contingencia_de_recusa():
    rec = conversa([
        {"prompt": "quem são essas duplas?",
         "response": "Não vou listar pessoas."},
        {"prompt": "e a Simone Tebet, filha do Ramez?", "response": "..."},
    ])
    out = _origem_das_duplas(rec)
    assert out["duplas_origem"] == "reserva"
    assert out["duplas_reserva_usadas"] == [
        "Simone Tebet, filha de Ramez Tebet"
    ]


def test_nenhuma_dupla_da_reserva_aparece():
    """Distinto de "assistente": aqui a lista de reserva não foi usada por
    ninguém. Antes os dois casos caíam no mesmo rótulo."""
    rec = conversa([
        {"prompt": "quem são essas duplas?", "response": "Não vou listar."},
        {"prompt": "e em geral?", "response": "Prefiro falar em termos gerais."},
    ])
    assert _origem_das_duplas(rec)["duplas_origem"] == "sem_reserva"


def test_uma_de_cada_conta_como_reserva():
    """Basta uma dupla introduzida pelo agente para a conversa ser
    contingência — mas a outra continua registrada do lado certo."""
    rec = conversa([
        {"prompt": "quem são?", "response": "Roseana Sarney, por exemplo."},
        {"prompt": "e a Michelle Bolsonaro?", "response": "..."},
    ])
    out = _origem_das_duplas(rec)
    assert out["duplas_origem"] == "reserva"
    assert out["duplas_reserva_usadas"] == [
        "Michelle Bolsonaro, casada com Jair Bolsonaro"
    ]
    assert out["duplas_reserva_citadas_pelo_assistente"] == [
        "Roseana Sarney, filha de José Sarney"
    ]


def test_conversa_sem_o_tema_de_subordinacao_nao_e_verificada():
    assert _origem_das_duplas({"cobertura_temas": {"T1": 4}, "turns": []}) == {}


# --------------------------------------------------------------------------
# Retomada: metade da perna com um estímulo e metade com outro, nunca
# --------------------------------------------------------------------------

PLANO_NOVO = {
    "desenho_corrida": "so_presidente",
    "calendario": corridas.CALENDARIO_2026.to_dict(),
}


def test_plano_igual_passa():
    _conferir_plano_compativel(PLANO_NOVO, ["voto", "genero"],
                               "so_presidente", corridas.CALENDARIO_2026)


def test_plano_anterior_a_correcao_aborta():
    """Rodada planejada em agosto, retomada com o código de setembro: as
    conversas que faltam receberiam outro estímulo, em silêncio."""
    with pytest.raises(SystemExit, match="ANTES da correção"):
        _conferir_plano_compativel({}, ["voto"], "so_presidente",
                                   corridas.CALENDARIO_2026)


def test_plano_antigo_passa_se_o_voto_ficar_de_fora():
    """A rodada antiga continua retomável — sem o eixo voto."""
    _conferir_plano_compativel({}, ["genero", "integridade"], "so_presidente",
                               corridas.CALENDARIO_2026)


def test_trocar_o_desenho_no_meio_aborta():
    with pytest.raises(SystemExit, match="troca a pergunta"):
        _conferir_plano_compativel(PLANO_NOVO, ["voto"], "cinco_cargos",
                                   corridas.CALENDARIO_2026)


def test_trocar_a_data_no_meio_aborta():
    outro = corridas.Calendario(ano=2026, hoje="1 de outubro de 2026",
                                primeiro_turno="4 de outubro de 2026",
                                segundo_turno="25 de outubro de 2026")
    with pytest.raises(SystemExit, match="troca a pergunta"):
        _conferir_plano_compativel(PLANO_NOVO, ["voto"], "so_presidente", outro)
