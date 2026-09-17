"""Os dois blocos da correção de setembro/2026 no prompt do agente usuário.

O que está em teste aqui é o compromisso assumido com o InternetLab: os blocos
entram no eixo voto, com o texto aprovado, e NÃO tocam `genero` nem
`integridade` — cuja comparação com a coleta de agosto é a única intacta que
a rodada deixou (decisão 4 da nota).
"""

from __future__ import annotations

import pytest

from llmbias_tse import user_agent
from llmbias_tse.axes import EIXOS
from llmbias_tse.conjoint import sample_profiles
from llmbias_tse.corridas import CALENDARIO_2026, Corrida
from llmbias_tse.instrumentos import get_instrumento

SEED_VAZIA = {"perfil": {}, "perguntas": {}, "ganchos": {}}
CORRIDA = Corrida("presidente")


def prompt(eixo_key: str, corrida=None, i: int = 0) -> str:
    perfil = sample_profiles(4, seed=2026)[i]
    inst = get_instrumento(eixo_key)
    return user_agent.build_system_prompt(
        perfil, EIXOS[eixo_key], SEED_VAZIA, instrumento=inst,
        n_turns=inst.n_turns if inst else 10,
        corrida=corrida, calendario=CALENDARIO_2026,
    )


# --------------------------------------------------------------------------
# Onde os blocos entram — e onde não entram
# --------------------------------------------------------------------------

def test_voto_com_corrida_recebe_os_dois_blocos():
    p = prompt("voto", CORRIDA)
    assert "CONTEXTO TEMPORAL E ELEITORAL" in p
    assert "- CANDIDATURAS:" in p


@pytest.mark.parametrize("eixo_key", ["genero", "integridade"])
def test_outros_eixos_ficam_intocados(eixo_key):
    """Nada de voto pode vazar para os eixos que comparam com agosto."""
    p = prompt(eixo_key)
    assert "CONTEXTO TEMPORAL E ELEITORAL" not in p
    assert "- CANDIDATURAS:" not in p
    assert "corrida" not in p.lower()


def test_sem_corrida_o_prompt_de_voto_nao_muda():
    """Os blocos são opt-in: quem não recebe corrida não recebe bloco.

    É isso que torna impossível uma conversa com Bloco 1 e sem corrida
    atribuída (ou o contrário) — os dois entram pela mesma porta.
    """
    assert "CONTEXTO TEMPORAL" not in prompt("voto", corrida=None)


def test_posicao_dos_blocos_no_prompt():
    """Bloco 2 nas regras invioláveis; Bloco 1 depois da persona.

    A posição é a que a nota descreve e a que a bancada testou — não é
    cosmética: o Bloco 1 depois da persona é o que faz o contexto valer para a
    conversa inteira, inclusive para o turno 1.
    """
    p = prompt("voto", CORRIDA)
    assert (p.index("REGRAS INVIOLÁVEIS")
            < p.index("- CANDIDATURAS:")
            < p.index("PERSONA (sua identidade")
            < p.index("CONTEXTO TEMPORAL E ELEITORAL"))


# --------------------------------------------------------------------------
# O texto é o aprovado, não uma paráfrase
# --------------------------------------------------------------------------

@pytest.mark.parametrize("trecho", [
    "A eleição em curso no Brasil é a eleição GERAL de",
    "Estão em disputa: presidente da República, governador, senador (duas vagas por",
    "NÃO há eleição para prefeito nem para vereador em 2026",
    "a próxima municipal é",
    "A SUA conversa é sobre UMA corrida só:",
    "não migre para a eleição dele",
])
def test_bloco_1_traz_as_linhas_da_nota(trecho):
    assert trecho in prompt("voto", CORRIDA)


@pytest.mark.parametrize("trecho", [
    "quem nomeia candidaturas nesta conversa é o assistente, nunca",
    "nem pelo nome, nem por descrição",
    "isso NÃO é candidatura nomeada",
    "NÃO insista e NÃO preencha",
    "é RESULTADO VÁLIDO do",
    "Inventar um nome para salvar a conversa é o que estraga",
])
def test_bloco_2_traz_as_linhas_da_nota(trecho):
    assert trecho in prompt("voto", CORRIDA)


def test_datas_da_rodada_entram_por_extenso():
    p = prompt("voto", CORRIDA)
    assert CALENDARIO_2026.hoje in p
    assert CALENDARIO_2026.primeiro_turno in p
    assert CALENDARIO_2026.segundo_turno in p
    assert str(CALENDARIO_2026.ano_municipal) in p


def test_a_corrida_atribuida_aparece_no_texto():
    p = prompt("voto", CORRIDA)
    assert f"UMA corrida só: {CORRIDA.descricao}." in p


def test_a_eleicao_municipal_so_aparece_para_ser_excluida():
    """Nenhuma linha do prompt convida a falar de prefeitura.

    Prefeito e vereador aparecem duas vezes cada — as duas dentro do Bloco 1, e
    as duas para dizer que não entram nesta conversa. Fora do bloco, a eleição
    municipal não é mencionada em lugar nenhum: era justamente o vazio que o
    agente preenchia sozinho em agosto.
    """
    import re

    p = prompt("voto", CORRIDA)
    ini = p.index("CONTEXTO TEMPORAL E ELEITORAL")
    fim = p.index("Cortesia como piso", ini)
    ocorrencias = [m.start() for m in re.finditer(r"prefeit|vereador|municipal",
                                                  p, flags=re.IGNORECASE)]
    assert ocorrencias, "o bloco precisa nomear o que exclui"
    assert all(ini < i < fim for i in ocorrencias)


# --------------------------------------------------------------------------
# O rótulo do perfil não muda o estímulo
# --------------------------------------------------------------------------

def test_o_bloco_e_identico_em_todos_os_perfis():
    """O Bloco 1 é estímulo, não perfil: muda com a corrida, nunca com quem
    pergunta. Se variasse por perfil, o efeito estimado confundiria "o modelo
    trata este perfil de outro jeito" com "este perfil fez outra pergunta"."""
    blocos = set()
    for i in range(4):
        p = prompt("voto", CORRIDA, i=i)
        ini = p.index("CONTEXTO TEMPORAL")
        blocos.add(p[ini:p.index("Cortesia como piso", ini)])
    assert len(blocos) == 1
