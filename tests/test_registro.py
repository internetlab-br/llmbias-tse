"""As duas correções de registro da seção 6 da nota, mais a guarda de retomada.

Nenhuma delas muda pergunta feita a plataforma: corrigem rótulos da base que
levariam ao erro quem for analisar os dados.
"""

from __future__ import annotations

import json
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


def test_verificacao_humana_casa_o_rotulo_do_modal():
    """O CAPTCHA tem de ser reconhecido pelo rótulo do diálogo.

    Sem isto o sintoma é um timeout de clique no composer, indistinguível de
    seletor quebrado — foi o que matou conversas do Copilot em 18/09/2026.
    """
    from llmbias_tse import capture

    class _Loc:
        def __init__(self, rots):
            self._rots = rots

        def count(self):
            return len(self._rots)

        def nth(self, i):
            rot, visivel = self._rots[i]
            return _Dlg(rot, visivel)

    class _Dlg:
        def __init__(self, rot, visivel=True):
            self._rot = rot
            self._visivel = visivel

        def get_attribute(self, _):
            return self._rot

        def text_content(self):
            return ""

        def is_visible(self):
            return self._visivel

    class _Page:
        def __init__(self, rots):
            # cada item é o rótulo, ou (rótulo, visível)
            self._rots = [r if isinstance(r, tuple) else (r, True)
                          for r in rots]

        def locator(self, _):
            return _Loc(self._rots)

    assert capture.verificacao_humana(
        _Page(["Verificação de\xa0segurança necessária"])
    ) == "Verificação de segurança necessária"
    assert capture.verificacao_humana(_Page(["Security verification"]))
    # Um diálogo qualquer NÃO pode ser lido como bloqueio: pausaria a coleta
    # a cada aviso de boas-vindas.
    assert capture.verificacao_humana(_Page(["Chat temporário"])) is None
    assert capture.verificacao_humana(_Page([])) is None
    # E o caso que fez a primeira versão errar: o Copilot mantém esse modal
    # pré-renderizado no DOM com `visibility: hidden`. Invisível não é
    # bloqueio — marcar as estações como bloqueadas com a tela limpa para a
    # coleta à toa.
    assert capture.verificacao_humana(
        _Page([("Verificação de segurança necessária", False)])) is None


def test_fracao_nao_latina_denuncia_resposta_em_outro_alfabeto():
    """Resposta longa, no tema e em chinês passa por toda outra checagem.

    Foi o caso do DeepSeek em 18/09/2026: 1.669 chars sobre desconfiança no
    TSE, inteiramente em chinês, respondendo a uma pergunta em português.
    Não é curta, não é artefato de UI, não é aviso de bloqueio.
    """
    from llmbias_tse import capture

    assert capture.fracao_nao_latina("") == 0.0
    assert capture.fracao_nao_latina(None) == 0.0
    assert capture.fracao_nao_latina(
        "As urnas eletrônicas são auditáveis desde 1996.") < 0.05
    assert capture.fracao_nao_latina("结论前置：民众对 TSE 的不信任") > 0.5
    # Uma sigla ou um nome estrangeiro no meio não pode acender o alarme.
    assert capture.fracao_nao_latina(
        "O relatório do TSE cita a empresa 华为 como fornecedora, mas o "
        "processo de auditoria segue o mesmo rito de sempre nas eleições "
        "brasileiras e não depende de nenhum fornecedor externo."
    ) < 0.05


def test_composer_confere_pega_texto_embaralhado():
    """O composer com texto INTERCALADO não pode ser enviado.

    O editor Lexical do Copilot, com o estado interno dessincronizado, aceita
    o texto novo intercalado com o rascunho anterior e o Ctrl+A/Delete não o
    limpa. O modelo então responde ao embaralhado — e a resposta é plausível,
    longa, no idioma certo: entra na base como dado. Não é falha de envio nem
    bloqueio; é a pergunta errada tendo sido feita.
    """
    from llmbias_tse.drivers import BaseDriver

    d = BaseDriver()
    n = d._normaliza
    assert n("Olá, tenho 60 anos!") == "olátenho60anos"
    # Marcas de largura zero que os composers inserem não podem contar.
    assert n("linha um​‌") == n("linha um")

    intencao = "Oi, tenho 22 anos, sou homem, tenho ensino superior completo"
    embaralhado = "Oi, tenho 22 anos, sou hloimnhea mum, ltinehan hdoo"
    assert not n(embaralhado).startswith(n(intencao)[:40])
    # E o caminho normal continua passando, inclusive com a quebra de linha
    # que o `_digitar` converte em Shift+Enter.
    digitado = "Oi, tenho 22 anos, sou homem, tenho ensino\nsuperior completo"
    assert n(digitado).startswith(n(intencao)[:40])


def test_turnos_limpos_tira_o_cromo_sem_tocar_no_bruto():
    """A limpeza acontece NA LEITURA; o arquivo segue com o que a página deu.

    Regra combinada com o Julio em 18/09/2026, quando apareceram 124 turnos do
    Google AI Mode com o aria-live do botão de copiar colado na resposta:
    reescrever resposta coletada é mexer no conteúdo do experimento, então o
    bruto fica e quem consome (juiz e base) lê limpo.
    """
    from llmbias_tse.storage import turnos_limpos

    bruto = {
        "platform": "google_aimode",
        "turns": [
            {"turn": 1, "ok": True, "response_chars": 200,
             "response": "A urna eletrônica é auditável desde 1996." +
                         " Copiado para a área de transferênciaFalha ao "
                         "copiar para a área de transferência. Tente "
                         "novamente mais tarde.CopiadoFalha ao copiar"},
            {"turn": 2, "ok": True, "response_chars": 20,
             "response": "Resposta sem cromo."},
        ],
    }
    copia = json.loads(json.dumps(bruto))
    limpos = turnos_limpos(bruto)

    assert limpos[0]["response"] == "A urna eletrônica é auditável desde 1996."
    assert limpos[0]["response_chars"] == len(limpos[0]["response"])
    assert limpos[0]["chars_cromo_removido"] > 100
    # Turno sem cromo passa intacto, e sem ganhar o campo novo.
    assert limpos[1]["response"] == "Resposta sem cromo."
    assert "chars_cromo_removido" not in limpos[1]
    # O registro de entrada NÃO pode ter sido alterado.
    assert bruto == copia

    # Plataforma sem driver conhecido passa intacta em vez de estourar.
    assert turnos_limpos({"platform": "inexistente",
                          "turns": [{"turn": 1, "response": "x"}]})[0][
        "response"] == "x"


def test_claude_tira_titulo_de_raciocinio_duplicado():
    """O container da resposta do Claude engloba o cabeçalho do bloco de
    pensamento, e ele sai duplicado no começo do texto.

    Medido em 19/09/2026: 329 de 1.153 turnos (28,5%), só no Claude. Não é
    boilerplate neutro — é a descrição do raciocínio do modelo, quase sempre
    em inglês, e o bloco de resistência da rubrica mede exatamente hesitação.
    """
    from llmbias_tse.drivers import REGISTRY

    d = REGISTRY["claude_momentary"]()
    sujo = ("Ranking candidates by polling-based win probability."
            "Ranking candidates by polling-based win probability.\n"
            "João Campos (PSB)\nRaquel Lyra (PSD)")
    assert d.limpar_resposta(sujo) == "João Campos (PSB)\nRaquel Lyra (PSD)"

    # Prosa normal não é tocada.
    limpa = ("As urnas eletrônicas passam por auditoria desde 1996, e o "
             "processo é acompanhado por partidos e pelo Ministério Público.")
    assert d.limpar_resposta(limpa) == limpa

    # Repetição SEM pontuação de fim de frase não é título — pode ser o
    # modelo repetindo um nome — e não pode ser mutilada.
    nome = "João Campos João Campos é o candidato mais citado nas pesquisas."
    assert d.limpar_resposta(nome) == nome

    assert d.limpar_resposta("") == ""


def test_resposta_que_termina_em_loading_nao_e_resposta():
    """Captura feita antes de a UI montar o bloco final.

    15 respostas do Claude terminavam em "…para você repassar:Loading"
    (20/09/2026): a prosa inteira capturada e a LISTA faltando. É a perda mais
    cara possível aqui — o texto pronto para repassar é exatamente o que os
    pedidos do instrumento pedem —, e passa por completa em qualquer checagem
    de tamanho, porque o que veio antes é longo e legítimo.
    """
    from llmbias_tse.conjoint_experiment import _motivo_resposta_invalida as m

    assert m("Aqui está a lista para você repassar:Loading")
    assert m("Segue o texto pronto:Carregando")
    # Recusa canônica curta continua sendo resposta válida.
    assert m("Boa pergunta, acesse tse.jus.br para informações oficiais.") is None
    # E a palavra no MEIO da prosa não pode acusar.
    assert m("O sistema de loading das urnas é auditado desde 1996.") is None


def test_refazer_separa_corte_de_chip_de_fonte():
    """O detector não pode confundir chip de fonte com frase cortada.

    Contar "não termina em pontuação" acusava 27% do Copilot e 37% do
    WhatsApp (20/09/2026), e era quase tudo o rodapé de citação que a
    plataforma cola no fim — "…uol+2", "…Metrópoles". Refazer conversa boa
    custa tanto quanto deixar passar conversa ruim.
    """
    import importlib.util
    import pathlib

    caminho = pathlib.Path("infra/local/refazer.py")
    spec = importlib.util.spec_from_file_location("refazer", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = mod.defeitos_do_turno

    assert d("Aqui está a lista para você repassar:Loading") == ["sufixo_incompleto"]
    assert d("a origem como empresário, fundador de empresa privada, como") == ["frase_cortada"]
    # Chip de fonte no fim NÃO é corte.
    assert d("A urna é auditada desde 1996. uol+2") == []
    assert d("O TSE publica os boletins. Metrópoles") == []
    # "ND Mais" é nome de veículo e termina numa palavra da lista de
    # funcionais — mandou refazer uma conversa que estava boa.
    assert d("Situa-se no piso das estatísticas do pleito.   ND Mais") == []
    # Texto que fecha normalmente.
    assert d("As urnas passam por auditoria pública desde 1996.") == []
    assert d("") == []
    assert d(None) == []


def test_recuperacao_do_artefato_entra_na_leitura_e_nao_no_bruto(tmp_path):
    """A continuação recuperada do HTML é emendada NA LEITURA.

    Decisão do time em 21/09/2026: recuperar do artefato em vez de recoletar
    (recoleta no WhatsApp custa horas por conversa). O bruto continua sendo o
    que a captura trouxe, e `recuperados.jsonl` é a prova do que mudou.
    """
    import json as _json
    from llmbias_tse import storage

    run = tmp_path / "rodada"
    run.mkdir()
    (run / "recuperados.jsonl").write_text(_json.dumps({
        "conversation_id": "whatsapp_metaai_P001_voto", "turno": 2,
        "chars_gravados": 40, "chars_recuperados": 22,
        "continuacao": "outubro, conforme o TSE.",
        "fonte": "artifacts/.../ok.html", "criterio": "prefixo",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    storage._REC_CACHE.clear()

    bruto = {
        "platform": "whatsapp_metaai",
        "conversation_id": "whatsapp_metaai_P001_voto",
        "run_dir": str(run),
        "turns": [
            {"turn": 1, "ok": True, "response": "Resposta inteira.",
             "response_chars": 17},
            {"turn": 2, "ok": True, "response": "O segundo turno cai em 25 de",
             "response_chars": 28},
        ],
    }
    copia = _json.loads(_json.dumps(bruto))
    ts = storage.turnos_limpos(bruto)

    assert ts[1]["response"].endswith("outubro, conforme o TSE.")
    assert ts[1]["chars_recuperados"] == 22
    assert ts[1]["response_chars"] == len(ts[1]["response"])
    # Turno sem recuperação passa intacto.
    assert ts[0]["response"] == "Resposta inteira."
    assert "chars_recuperados" not in ts[0]
    # E o registro de entrada NÃO foi alterado.
    assert bruto == copia
    storage._REC_CACHE.clear()


def test_lote_e_sincrono_agregam_pelo_mesmo_caminho():
    """A agregação do painel é a MESMA no modo síncrono e no lote.

    O julgamento em lote entra por `annotate(..., pronto=...)` e
    `annotate_panel(..., prontos=...)`, que só trocam a ORIGEM das extrações.
    Duplicar a agregação faria os dois modos divergirem na primeira mudança
    de rubrica — e a divergência apareceria como diferença entre juízes, que
    é exatamente o que o painel mede.
    """
    from llmbias_tse import judge
    from llmbias_tse.judges import Juiz
    from llmbias_tse.rubrics import get_rubric

    rubric = get_rubric("voto")
    conv = {
        "conversation_id": "x_P001_voto", "eixo": "voto",
        "platform": "chatgpt",
        "turns": [{"turn": 1, "ok": True, "response": "Voto no candidato X.",
                   "prompt": "em quem votar?", "response_chars": 19}],
    }
    tipo = rubric.tipos[0].codigo
    ex = judge.Extracao(
        achados=[judge.Achado(id=1, tipo=tipo, trecho="Voto no candidato X.",
                              voz=["V2"], nota="")],
        resistencia=[],
    )
    juizes = [Juiz("flash", "google", "m"), Juiz("sonnet", "anthropic", "m")]
    anot = judge.annotate_panel(
        conv, rubric, juizes,
        prontos={"flash": {1: ex}, "sonnet": {1: ex}})

    assert anot["n_juizes"] == 2
    assert anot["painel_completo"] is True
    assert anot["votos_por_tipo"][tipo] == 2
    assert anot["por_tipo"][tipo] == 1          # maioria
    assert anot["concordancia_unanime_tipos"] == 1.0
    # Juiz sem resultado no lote é FALHA declarada, não zero silencioso.
    anot2 = judge.annotate_panel(conv, rubric, juizes,
                                 prontos={"flash": {1: ex}})
    assert anot2["n_juizes"] == 1
    assert anot2["painel_completo"] is False
    assert "sonnet" in anot2["juizes_com_falha"]


def test_dependente_vem_do_juiz_de_cobertura_completa():
    """`por_tipo` é a dependente do dataset e não pode mudar de instrumento
    no meio da base.

    Nesta rodada um juiz cobre a base inteira e os outros só uma amostra de
    TURNOS — medido em 22/09/2026, o sonnet viu 14% dos turnos de cada
    conversa. Com maioria simples, 1.129 conversas valeriam "flash OU sonnet"
    e as outras 791 "flash sozinho": dois instrumentos na mesma coluna. A
    dependente sai dos juízes de cobertura completa; os parciais ficam em
    `votos_por_tipo`, que é o insumo de concordância.
    """
    from llmbias_tse import judge
    from llmbias_tse.judges import Juiz
    from llmbias_tse.rubrics import get_rubric

    rubric = get_rubric("voto")
    t1, t2 = rubric.tipos[0].codigo, rubric.tipos[1].codigo
    juizes = [Juiz("flash", "google", "m"), Juiz("sonnet", "anthropic", "m")]
    por_juiz = {
        # cobertura completa: 7 turnos, achou T1
        "flash": {"n_turnos_avaliados": 7, "por_tipo": {t1: 2, t2: 0},
                  "turnos": []},
        # cobertura parcial: 1 turno, achou T2
        "sonnet": {"n_turnos_avaliados": 1, "por_tipo": {t1: 0, t2: 1},
                   "turnos": []},
    }
    anot = judge._consolidar_painel(por_juiz, rubric, juizes, "turno")

    # A dependente segue o flash, que viu a conversa toda.
    assert anot["por_tipo"][t1] == 1
    assert anot["por_tipo"][t2] == 0, (
        "T2 veio de um juiz que viu 1 turno de 7 — não pode entrar na "
        "dependente")
    # Mas o voto do parcial fica registrado, para medir concordância.
    assert anot["votos_por_tipo"][t2] == 1
    assert anot["juizes_de_referencia"] == ["flash"]
    assert anot["cobertura_desigual"] is True
    assert anot["cobertura_por_juiz"] == {"flash": 7, "sonnet": 1}
