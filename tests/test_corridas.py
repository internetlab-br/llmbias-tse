"""O sorteio da corrida do eixo voto.

O que estes testes protegem é o que a nota metodológica afirma ao InternetLab:
que a corrida é a mesma em todas as plataformas para uma mesma célula do
conjoint, que ela não se inventa quando falta tabela, e que o texto que entra
no prompt sai em português correto.
"""

from __future__ import annotations

import collections

import pytest

from llmbias_tse import corridas

# Tabela de exercício: NÃO é a tabela da rodada (essa é decisão do InternetLab
# e entra em `corridas.UFS`). Serve para cobrir os três artigos e o caso do DF.
UFS_TESTE = (
    corridas.UF("SP", "São Paulo", ""),
    corridas.UF("PA", "Pará", "o"),
    corridas.UF("BA", "Bahia", "a"),
    corridas.UF("DF", "Distrito Federal", "o"),
)


@pytest.fixture
def com_ufs(monkeypatch):
    monkeypatch.setattr(corridas, "UFS", UFS_TESTE)
    monkeypatch.setattr(corridas, "UFS_POR_SIGLA",
                        {u.sigla: u for u in UFS_TESTE})


# --------------------------------------------------------------------------
# Determinismo e independência de plataforma
# --------------------------------------------------------------------------

def test_mesma_semente_e_perfil_dao_sempre_a_mesma_corrida(com_ufs):
    pids = [f"P{i:03d}" for i in range(1, 30)]
    a = corridas.sortear_corridas(pids, seed=2026, desenho="cinco_cargos")
    b = corridas.sortear_corridas(pids, seed=2026, desenho="cinco_cargos")
    assert a == b


def test_sorteio_nao_depende_da_plataforma():
    """A corrida é função de (semente, perfil) — a plataforma não entra.

    Se entrasse, a comparação entre plataformas passaria a medir também a
    diferença de pergunta, e não só a diferença de comportamento.
    """
    import inspect
    params = inspect.signature(corridas.sortear_corrida).parameters
    assert "platform" not in params and "plataforma" not in params


def test_semente_diferente_muda_o_sorteio(com_ufs):
    pids = [f"P{i:03d}" for i in range(1, 30)]
    a = corridas.sortear_corridas(pids, seed=2026, desenho="cinco_cargos")
    b = corridas.sortear_corridas(pids, seed=99, desenho="cinco_cargos")
    assert a != b


def test_desenho_so_presidente_dispensa_tabela_de_ufs():
    c = corridas.sortear_corrida("P001", desenho="so_presidente")
    assert c.cargo == "presidente" and c.uf is None


def test_cinco_cargos_usa_todos_os_cargos(com_ufs):
    pids = [f"P{i:03d}" for i in range(1, 200)]
    sorteadas = corridas.sortear_corridas(pids, desenho="cinco_cargos")
    assert {c.cargo for c in sorteadas.values()} == set(
        corridas.DESENHOS["cinco_cargos"]
    )


# --------------------------------------------------------------------------
# A falha tem de ser barulhenta
# --------------------------------------------------------------------------

def test_cargo_que_exige_uf_sem_tabela_levanta_erro(monkeypatch):
    """Sem tabela de UFs, ABORTA — nunca inventa um estado.

    Inventar o que falta é exatamente o modo de falha que produziu as 406
    conversas sobre a prefeitura de São Paulo. A tabela hoje está preenchida;
    o que este teste protege é o comportamento se alguém a esvaziar.
    """
    monkeypatch.setattr(corridas, "UFS", ())
    with pytest.raises(ValueError, match="tabela"):
        corridas.sortear_corridas([f"P{i:03d}" for i in range(1, 10)],
                                  desenho="majoritarias")


def test_desenho_desconhecido_levanta_erro():
    with pytest.raises(ValueError, match="desconhecido"):
        corridas.sortear_corrida("P001", desenho="inexistente")


def test_balanceamento_desconhecido_levanta_erro():
    with pytest.raises(ValueError, match="desconhecido"):
        corridas.sortear_corridas(["P001"], balanceamento="inexistente")


def test_cargo_com_uf_sem_uf_levanta_erro():
    with pytest.raises(ValueError, match="exige UF"):
        corridas.CARGOS["governador"].descricao(None)


# --------------------------------------------------------------------------
# Cobertura: a decisão de 15/09/2026 (27 UFs, balanceado por cargo)
# --------------------------------------------------------------------------

def test_a_tabela_tem_as_27_ufs():
    assert len(corridas.UFS) == 27
    assert len({u.sigla for u in corridas.UFS}) == 27
    assert all(u.artigo in ("", "o", "a") for u in corridas.UFS)


def test_balanceado_por_cargo_reparte_igual_entre_os_cargos():
    """Cada cargo com o mesmo N, a menos do resto da divisão."""
    pids = [f"P{i:03d}" for i in range(1, 101)]
    at = corridas.sortear_corridas(pids, desenho="majoritarias",
                                   balanceamento="cargo")
    por_cargo = collections.Counter(c.cargo for c in at.values())
    assert set(por_cargo) == {"presidente", "governador", "senador"}
    assert max(por_cargo.values()) - min(por_cargo.values()) <= 1


def test_balanceado_por_cargo_cobre_todas_as_corridas():
    """Com perfis suficientes, nenhuma disputa fica sem conversa.

    É a razão de existir do balanceamento: no sorteio independente, ~9 das 55
    corridas ficariam de fora de uma rodada de 100 perfis, por azar.
    """
    pids = [f"P{i:04d}" for i in range(1, 201)]
    at = corridas.sortear_corridas(pids, desenho="majoritarias",
                                   balanceamento="cargo")
    cob = corridas.resumo_cobertura(at, "majoritarias")
    assert cob["corridas_possiveis"] == 1 + 2 * 27
    assert cob["faltando"] == []


def test_sorteio_independente_deixa_buracos():
    """O contraste que justifica a decisão — mesmo N, mesma semente."""
    pids = [f"P{i:03d}" for i in range(1, 101)]
    iid = corridas.sortear_corridas(pids, desenho="majoritarias",
                                    balanceamento="iid")
    assert corridas.resumo_cobertura(iid, "majoritarias")["faltando"]


def test_a_sobra_nao_cai_sempre_na_mesma_uf():
    """A sobra da divisão vai para UFs sorteadas, não para as primeiras.

    Sem isso o Acre — primeira sigla da tabela — levaria a conversa extra em
    toda rodada, e um detalhe de implementação viraria viés do desenho.
    """
    extras = set()
    for semente in range(2020, 2040):
        pids = [f"P{i:03d}" for i in range(1, 101)]
        at = corridas.sortear_corridas(pids, seed=semente,
                                       desenho="majoritarias",
                                       balanceamento="cargo")
        cont = collections.Counter(
            c.uf for c in at.values() if c.cargo == "governador")
        teto = max(cont.values())
        extras |= {uf for uf, n in cont.items() if n == teto}
    assert len(extras) > 5


def test_ordem_dos_perfis_nao_muda_a_atribuicao():
    pids = [f"P{i:03d}" for i in range(1, 61)]
    a = corridas.sortear_corridas(pids)
    b = corridas.sortear_corridas(list(reversed(pids)))
    assert a == b


def test_balanceamento_por_celula_da_o_mesmo_peso_a_presidencial():
    """O contraste com `cargo`: por célula, a presidencial é uma entre 55."""
    pids = [f"P{i:04d}" for i in range(1, 221)]
    at = corridas.sortear_corridas(pids, desenho="majoritarias",
                                   balanceamento="celula")
    cont = collections.Counter(c.colunas()["corrida"] for c in at.values())
    assert max(cont.values()) - min(cont.values()) <= 1


# --------------------------------------------------------------------------
# A redação que entra no prompt
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cargo,sigla,esperado", [
    ("presidente", None, "a eleição para presidente da República"),
    ("governador", "SP", "a eleição para governador de São Paulo"),
    ("governador", "PA", "a eleição para governador do Pará"),
    ("governador", "BA", "a eleição para governador da Bahia"),
    ("senador", "SP", "a eleição para o Senado por São Paulo"),
    ("senador", "PA", "a eleição para o Senado pelo Pará"),
    ("senador", "BA", "a eleição para o Senado pela Bahia"),
    ("deputado_federal", "PA", "a eleição para deputado federal pelo Pará"),
    ("deputado_estadual", "BA", "a eleição para deputado estadual pela Bahia"),
    # o DF elege deputado DISTRITAL — redação própria, não "estadual"
    ("deputado_estadual", "DF",
     "a eleição para deputado distrital no Distrito Federal"),
])
def test_descricao_em_portugues_correto(com_ufs, cargo, sigla, esperado):
    assert corridas.Corrida(cargo, sigla).descricao == esperado


def test_colunas_da_base(com_ufs):
    c = corridas.Corrida("governador", "PA")
    assert c.colunas() == {
        "corrida_cargo": "governador",
        "corrida_uf": "PA",
        "corrida": "governador_PA",
        "corrida_descricao": "a eleição para governador do Pará",
    }


# --------------------------------------------------------------------------
# Calendário
# --------------------------------------------------------------------------

def test_proxima_municipal_e_dois_anos_depois():
    assert corridas.CALENDARIO_2026.ano_municipal == 2028


def test_calendario_nao_vem_do_relogio():
    """As datas são parâmetro da rodada, escrito à mão.

    Se viessem do relógio, duas execuções do mesmo comando em dias diferentes
    fariam perguntas diferentes e a rodada deixaria de ser reproduzível. O jeito
    de garantir isso é o módulo não ter relógio nenhum ao alcance: a asserção é
    sobre os IMPORTS, não sobre o texto do arquivo.
    """
    import ast
    import inspect

    arvore = ast.parse(inspect.getsource(corridas))
    importados: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            importados |= {a.name.split(".")[0] for a in no.names}
        elif isinstance(no, ast.ImportFrom) and no.module:
            importados.add(no.module.split(".")[0])
    assert not importados & {"datetime", "time", "calendar"}


def test_calendario_registra_tudo_o_que_o_bloco_1_escreve():
    d = corridas.CALENDARIO_2026.to_dict()
    assert set(d) == {"ano", "hoje", "primeiro_turno", "segundo_turno",
                      "ano_municipal"}
