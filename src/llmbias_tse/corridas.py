"""A corrida da conversa de voto: cargo, UF e o calendário da eleição.

POR QUE ESTE MÓDULO EXISTE. Na coleta de agosto/2026, 406 das 527 conversas do
eixo voto (77%) pediram as candidaturas à PREFEITURA DE SÃO PAULO — eleição que
não existe em 2026. O defeito não é de nenhuma plataforma: a pergunta de
abertura é escrita pelo agente usuário, que é o mesmo nas sete, e a taxa ficou
entre 71% (Gemini) e 82% (Grok), independente da plataforma (p=0,784). A causa é
o nosso texto, em três camadas que se somam: a ficha pedia "as candidaturas na
eleição atual", sem dizer qual eleição nem qual cargo; o modelo que simula a
pessoa usuária tem dados até janeiro de 2025 e só conhece em detalhe a municipal
de 2024; e o papel de "pessoa comum" proíbe a única saída correta ("não tenho
essa informação"). Sobrou preencher o vazio com a disputa mais coberta que ele
viu.

A correção tem duas metades. A textual mora em `user_agent.py` (os dois blocos
submetidos ao InternetLab). A outra é esta: a conversa passa a ter UMA corrida
atribuída, resolvida FORA do modelo — o agente não escolhe mais a eleição,
porque não lhe resta escolha a fazer.

TRÊS PROPRIEDADES QUE O SORTEIO PRECISA TER:

  - determinístico em (semente, perfil) e INDEPENDENTE da plataforma, como o
    sorteio de temas: a mesma célula do conjoint tem de receber o mesmo estímulo
    nas sete plataformas, senão a comparação entre elas passa a medir também
    diferença de pergunta;
  - em fluxo de aleatoriedade PRÓPRIO, para não deslocar o sorteio de temas nem
    o de exemplares: os roteiros de `genero` e `integridade` têm de sair
    idênticos aos de agosto (decisão 4 da nota — os dois eixos ficam intocados);
  - com o desenho (quais cargos, quais UFs) como DADO e não como código: que
    corridas entram no sorteio é decisão metodológica do InternetLab (decisão 2
    da nota), ainda aberta quando este módulo foi escrito.

A UF NÃO É UM FATOR DE PERFIL. `politica`, `genero`, `idade` e `escolaridade`
descrevem quem pergunta; a corrida aparece dentro da PERGUNTA ("quem são as
candidaturas ao governo de {uf}"). É fator de estímulo, e por isso sai em coluna
própria na base — quem analisar decide se a trata como fator ou como controle.
"""

from __future__ import annotations

import collections
import random
from dataclasses import asdict, dataclass
from typing import Sequence

from .instrument import _rng_for  # mesma construção de semente do planejador

# --------------------------------------------------------------------------
# Calendário da rodada
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Calendario:
    """As datas que o Bloco 1 escreve no prompt.

    ESCRITO À MÃO, NUNCA LIDO DO RELÓGIO. Se a data vier de `date.today()`, duas
    execuções do mesmo comando em dias diferentes produzem prompts diferentes, e
    a rodada deixa de ser reproduzível — outra equipe rodando o mesmo teste
    meses depois não faria a mesma pergunta. É exigência da nota metodológica
    (seção 4), não preferência de implementação: o valor é parâmetro da rodada e
    fica registrado no `plano_coleta.json` da rodada, junto do desenho de
    sorteio.
    """

    ano: int
    hoje: str            # "15 de setembro de 2026"
    primeiro_turno: str  # "4 de outubro de 2026"
    segundo_turno: str   # "25 de outubro de 2026"

    @property
    def ano_municipal(self) -> int:
        """A próxima eleição municipal — o {ano+2} do Bloco 1."""
        return self.ano + 2

    def to_dict(self) -> dict:
        return {**asdict(self), "ano_municipal": self.ano_municipal}


# Parâmetro da rodada. `hoje` é a data de INÍCIO da coleta e precisa ser
# confirmada antes de rodar; as datas dos turnos são as da eleição geral de 2026
# (primeiro domingo e último domingo de outubro).
CALENDARIO_2026 = Calendario(
    ano=2026,
    hoje="15 de setembro de 2026",
    primeiro_turno="4 de outubro de 2026",
    segundo_turno="25 de outubro de 2026",
)


# --------------------------------------------------------------------------
# Cargos em disputa
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Cargo:
    """Um cargo em disputa na eleição geral.

    `template` é renderizado com as formas já contraídas da UF (ver `UF`), para
    que a corrida entre no prompt em português natural — "governador do Pará",
    "o Senado pela Bahia" — sem o agente ter de flexionar nada.
    """

    key: str
    rotulo: str
    exige_uf: bool
    template: str
    # Redação própria de uma UF específica (o DF elege deputado DISTRITAL).
    excecoes: tuple[tuple[str, str], ...] = ()

    def descricao(self, uf: "UF | None") -> str:
        if not self.exige_uf:
            return self.template
        if uf is None:
            raise ValueError(f"cargo {self.key!r} exige UF e nenhuma foi dada")
        campos = {"de": uf.de, "por": uf.por, "no": uf.no, "nome": uf.nome}
        for sigla, tpl in self.excecoes:
            if sigla == uf.sigla:
                return tpl.format(**campos)
        return self.template.format(**campos)


CARGOS: dict[str, Cargo] = {
    "presidente": Cargo(
        key="presidente", rotulo="presidente da República", exige_uf=False,
        template="a eleição para presidente da República",
    ),
    "governador": Cargo(
        key="governador", rotulo="governador", exige_uf=True,
        template="a eleição para governador {de}",
    ),
    "senador": Cargo(
        key="senador", rotulo="senador", exige_uf=True,
        template="a eleição para o Senado {por}",
    ),
    "deputado_federal": Cargo(
        key="deputado_federal", rotulo="deputado federal", exige_uf=True,
        template="a eleição para deputado federal {por}",
    ),
    "deputado_estadual": Cargo(
        key="deputado_estadual", rotulo="deputado estadual", exige_uf=True,
        template="a eleição para deputado estadual {por}",
        excecoes=(("DF", "a eleição para deputado distrital {no}"),),
    ),
}


# --------------------------------------------------------------------------
# Unidades da federação
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UF:
    """Uma UF e o artigo que ela pede.

    `artigo` é "" (São Paulo), "o" (o Pará) ou "a" (a Bahia); as formas
    contraídas saem daí, então a tabela tem uma coluna a preencher por UF em vez
    de quatro, e não há como a preposição discordar do nome.
    """

    sigla: str
    nome: str
    artigo: str  # "" | "o" | "a"

    @property
    def de(self) -> str:
        return {"": f"de {self.nome}", "o": f"do {self.nome}",
                "a": f"da {self.nome}"}[self.artigo]

    @property
    def por(self) -> str:
        return {"": f"por {self.nome}", "o": f"pelo {self.nome}",
                "a": f"pela {self.nome}"}[self.artigo]

    @property
    def no(self) -> str:
        return {"": f"em {self.nome}", "o": f"no {self.nome}",
                "a": f"na {self.nome}"}[self.artigo]


# A TABELA DE UFs — as 27, decisão da equipe em 15/09/2026, A CONFIRMAR.
#
# O `artigo` não é escolha de ninguém: é fato gramatical, e dele saem as três
# formas contraídas. Os casos sem artigo são os que o uso brasileiro consagra
# sem ele ("de Alagoas", "de Goiás", "em Mato Grosso", "de Minas Gerais",
# "de Pernambuco", "de Rondônia", "de Roraima", "de Santa Catarina",
# "de São Paulo", "de Sergipe").
UFS: tuple[UF, ...] = (
    UF("AC", "Acre", "o"),
    UF("AL", "Alagoas", ""),
    UF("AP", "Amapá", "o"),
    UF("AM", "Amazonas", "o"),
    UF("BA", "Bahia", "a"),
    UF("CE", "Ceará", "o"),
    UF("DF", "Distrito Federal", "o"),
    UF("ES", "Espírito Santo", "o"),
    UF("GO", "Goiás", ""),
    UF("MA", "Maranhão", "o"),
    UF("MT", "Mato Grosso", ""),
    UF("MS", "Mato Grosso do Sul", ""),
    UF("MG", "Minas Gerais", ""),
    UF("PA", "Pará", "o"),
    UF("PB", "Paraíba", "a"),
    UF("PR", "Paraná", "o"),
    UF("PE", "Pernambuco", ""),
    UF("PI", "Piauí", "o"),
    UF("RJ", "Rio de Janeiro", "o"),
    UF("RN", "Rio Grande do Norte", "o"),
    UF("RS", "Rio Grande do Sul", "o"),
    UF("RO", "Rondônia", ""),
    UF("RR", "Roraima", ""),
    UF("SC", "Santa Catarina", ""),
    UF("SP", "São Paulo", ""),
    UF("SE", "Sergipe", ""),
    UF("TO", "Tocantins", "o"),
)

UFS_POR_SIGLA: dict[str, UF] = {u.sigla: u for u in UFS}


# --------------------------------------------------------------------------
# Desenhos de sorteio (decisão 2 da nota — em aberto)
# --------------------------------------------------------------------------

# Os três desenhos que a nota põe em cima da mesa. Trocar de desenho é trocar um
# argumento, não editar código: qual deles vale é decisão do InternetLab.
DESENHOS: dict[str, tuple[str, ...]] = {
    # (a) concentra toda a amostra numa disputa só e dispensa UF
    "so_presidente": ("presidente",),
    # (c) meio-termo: as majoritárias
    "majoritarias": ("presidente", "governador", "senador"),
    # (b) os cinco cargos em disputa
    "cinco_cargos": ("presidente", "governador", "senador",
                     "deputado_federal", "deputado_estadual"),
}

# Decisão da equipe em 15/09/2026, A CONFIRMAR pela pesquisadora que seguir com
# o PR: as três corridas majoritárias, com as 27 UFs.
DESENHO_PADRAO = "majoritarias"

# COMO OS PERFIS SE REPARTEM ENTRE AS CORRIDAS.
#
# `iid` era o comportamento original: cada perfil sorteia a sua corrida de forma
# independente. O problema é a cobertura. Com as majoritárias e 27 UFs são 55
# corridas possíveis; sorteando 100 perfis de forma independente, o número
# esperado de corridas que NUNCA seriam perguntadas é ~9. Nove disputas fora da
# rodada por azar, não por desenho.
#
# `cargo` e `celula` são atribuições BALANCEADAS: monta-se a fila de corridas
# com as quotas certas e permuta-se com a semente. A cobertura passa a ser
# garantida sempre que houver perfis suficientes. O preço é o mesmo do piso de
# temas: as conversas deixam de ser independentes entre si (quem já saiu não
# volta na mesma proporção), em troca de não haver buraco.
#
# A diferença entre as duas está em quem recebe peso igual — e ela é grande:
#
#   celula  cada uma das 55 corridas com o mesmo N. A disputa presidencial,
#           que é uma corrida entre 55, fica com ~2 conversas em 100.
#   cargo   cada CARGO com o mesmo N (um terço cada). A presidencial fica com
#           ~33; governador e senador repartem os seus 33 entre as 27 UFs,
#           ~1,2 por UF — cobertura garantida, comparação entre UFs não.
#
# A equipe escolheu `cargo` em 15/09/2026, A CONFIRMAR.
BALANCEAMENTOS = ("cargo", "celula", "iid")
BALANCEAMENTO_PADRAO = "cargo"

# Quais eixos recebem corrida atribuída. Só o voto, e é isso que mantém os
# outros dois intocados: sem corrida não há Bloco 1, e sem Bloco 1 o prompt de
# `genero` e `integridade` sai idêntico ao de agosto.
EIXOS_COM_CORRIDA: tuple[str, ...] = ("voto",)


@dataclass(frozen=True)
class Corrida:
    """A corrida atribuída a UMA conversa."""

    cargo: str
    uf: str | None = None

    @property
    def descricao(self) -> str:
        """O texto que entra no `{corrida atribuída}` do Bloco 1."""
        uf = UFS_POR_SIGLA.get(self.uf) if self.uf else None
        return CARGOS[self.cargo].descricao(uf)

    def colunas(self) -> dict:
        """As colunas da base. `corrida` é o rótulo curto, para agrupar."""
        return {
            "corrida_cargo": self.cargo,
            "corrida_uf": self.uf or "",
            "corrida": f"{self.cargo}_{self.uf}" if self.uf else self.cargo,
            "corrida_descricao": self.descricao,
        }

    def to_dict(self) -> dict:
        return {"cargo": self.cargo, "uf": self.uf, "descricao": self.descricao}


def _cargos_do_desenho(desenho: str) -> tuple[str, ...]:
    cargos = DESENHOS.get(desenho)
    if cargos is None:
        raise ValueError(
            f"desenho de corrida {desenho!r} desconhecido; "
            f"disponíveis: {sorted(DESENHOS)}"
        )
    return cargos


def _exigir_tabela_ufs(cargo_key: str, desenho: str) -> None:
    """A tabela de UFs vazia ABORTA — nunca inventa um estado."""
    if not UFS:
        raise ValueError(
            f"o desenho {desenho!r} usa {cargo_key!r}, que exige UF, mas a "
            f"tabela `corridas.UFS` está vazia. Preencha-a antes de rodar "
            f"(decisão 2 da nota metodológica)."
        )


def celulas(desenho: str = DESENHO_PADRAO) -> tuple[Corrida, ...]:
    """Todas as corridas possíveis do desenho — o denominador da cobertura."""
    out: list[Corrida] = []
    for key in _cargos_do_desenho(desenho):
        cargo = CARGOS[key]
        if not cargo.exige_uf:
            out.append(Corrida(key))
            continue
        _exigir_tabela_ufs(key, desenho)
        out.extend(Corrida(key, u.sigla) for u in UFS)
    return tuple(out)


def _quotas(total: int, n_grupos: int, rng: random.Random) -> list[int]:
    """Divide `total` em `n_grupos` partes o mais iguais possível.

    A sobra vai para grupos SORTEADOS, não para os primeiros da lista: do
    contrário o Acre — primeira UF da tabela, por ordem alfabética de sigla —
    levaria a conversa extra em toda rodada, e um detalhe de implementação
    viraria desequilíbrio fixo do desenho.
    """
    base, resto = divmod(total, n_grupos)
    q = [base] * n_grupos
    for i in rng.sample(range(n_grupos), resto):
        q[i] += 1
    return q


def _fila_por_cargo(n: int, cargos: Sequence[str], desenho: str,
                    rng: random.Random) -> list[Corrida]:
    """Cada CARGO com o mesmo N; dentro do cargo, as UFs repartem por igual."""
    fila: list[Corrida] = []
    for key, quota in zip(cargos, _quotas(n, len(cargos), rng)):
        cargo = CARGOS[key]
        if not cargo.exige_uf:
            fila.extend([Corrida(key)] * quota)
            continue
        _exigir_tabela_ufs(key, desenho)
        for uf, q in zip(UFS, _quotas(quota, len(UFS), rng)):
            fila.extend([Corrida(key, uf.sigla)] * q)
    return fila


def _fila_por_celula(n: int, desenho: str,
                     rng: random.Random) -> list[Corrida]:
    """Cada CORRIDA com o mesmo N (a presidencial vale uma entre 55)."""
    cels = celulas(desenho)
    fila: list[Corrida] = []
    for corrida, q in zip(cels, _quotas(n, len(cels), rng)):
        fila.extend([corrida] * q)
    return fila


def sortear_corrida(profile_id: str, seed: int = 2026,
                    desenho: str = DESENHO_PADRAO) -> Corrida:
    """A corrida de UM perfil, sorteada de forma INDEPENDENTE (`iid`).

    Determinística em (semente, perfil) e cega à plataforma. Não garante
    cobertura: é o modo `iid` de `sortear_corridas`, mantido porque é o que
    permite acrescentar perfis a uma rodada sem mexer nos que já existem.
    """
    desenho_cargos = _cargos_do_desenho(desenho)
    rng = _rng_for(seed, "corrida", desenho, profile_id)
    cargo = CARGOS[rng.choice(list(desenho_cargos))]
    if not cargo.exige_uf:
        return Corrida(cargo=cargo.key)
    _exigir_tabela_ufs(cargo.key, desenho)
    return Corrida(cargo=cargo.key, uf=rng.choice(list(UFS)).sigla)


def sortear_corridas(profile_ids: Sequence[str], seed: int = 2026,
                     desenho: str = DESENHO_PADRAO,
                     balanceamento: str = BALANCEAMENTO_PADRAO,
                     ) -> dict[str, Corrida]:
    """A corrida de cada perfil da rodada, resolvida de uma vez.

    De uma vez porque o balanceamento é uma propriedade do CONJUNTO: não dá
    para garantir que toda corrida apareça olhando um perfil por vez. Continua
    determinístico em (semente, desenho, balanceamento, conjunto de perfis) e
    cego à plataforma — a mesma célula do conjoint recebe a mesma corrida nas
    sete.

    Os ids são ordenados antes da atribuição, de modo que a ordem em que a
    lista chega não muda o resultado. O contrapartida de balancear: a corrida de
    um perfil passa a depender de QUANTOS perfis a rodada tem, então acrescentar
    perfis no meio da rodada remexe as atribuições. A coleta não faz isso — os
    perfis vêm de `profiles.json`, congelado na primeira execução —, e é por
    isso que o plano registra o balanceamento e a retomada o confere.
    """
    if balanceamento == "iid":
        return {pid: sortear_corrida(pid, seed=seed, desenho=desenho)
                for pid in profile_ids}
    if balanceamento not in BALANCEAMENTOS:
        raise ValueError(
            f"balanceamento {balanceamento!r} desconhecido; "
            f"disponíveis: {list(BALANCEAMENTOS)}"
        )
    pids = sorted(profile_ids)
    cargos = _cargos_do_desenho(desenho)
    rng = _rng_for(seed, "corrida", desenho, balanceamento)
    if balanceamento == "cargo":
        fila = _fila_por_cargo(len(pids), cargos, desenho, rng)
    else:
        fila = _fila_por_celula(len(pids), desenho, rng)
    rng.shuffle(fila)
    return dict(zip(pids, fila))


def corridas_do_plano(plano: dict, eixo: str) -> dict[str, Corrida]:
    """As corridas COMO REGISTRADAS em `plano_coleta.json`, por perfil.

    O plano é a fonte da verdade da corrida, e não o resultado de chamar
    `sortear_corridas` de novo na hora de rodar. O motivo é o que está escrito
    no aviso de `sortear_corridas`: balancear faz a corrida de um perfil
    depender de QUANTOS perfis entram no sorteio. A coleta em fatias (uma
    conta por metade dos perfis, `--fatia`) entrega 50 ids em vez de 100 — e
    aí o sorteio, ainda que determinístico, dá outra resposta.

    Medido em 18/09/2026: com a fatia, só 6 dos 50 perfis recebiam a corrida
    que o plano registrou. As conversas ficavam internamente coerentes (o
    registro guarda o que foi perguntado) e comparáveis entre plataformas (a
    fatia de um perfil é a mesma em todas), mas perguntavam sobre outra
    eleição que a do pré-registro. Ler do plano fecha essa porta: a corrida
    deixa de ser recalculável.
    """
    out: dict[str, Corrida] = {}
    for c in plano.get("conversas", ()):
        if c.get("eixo") != eixo:
            continue
        d = c.get("corrida")
        if d and c["perfil_id"] not in out:
            out[c["perfil_id"]] = Corrida(cargo=d["cargo"], uf=d.get("uf"))
    return out


def estender_corridas(registradas: dict[str, Corrida],
                      novos_ids: Sequence[str], seed: int = 2026,
                      desenho: str = DESENHO_PADRAO,
                      balanceamento: str = BALANCEAMENTO_PADRAO,
                      ) -> dict[str, Corrida]:
    """Acrescenta perfis a uma rodada em andamento SEM remexer os que já têm
    corrida registrada.

    Existe porque `sortear_corridas` é balanceado sobre o CONJUNTO: pedir 120
    perfis em vez de 100 reatribui a corrida de quase todos — medido em
    19/09/2026, só 9 dos 100 sobreviveriam. Com 1.303 conversas já coletadas
    sob as corridas registradas, reatribuir é trocar o estímulo de perna
    inteira no meio da rodada.

    Perfis, roteiros e temas NÃO têm esse problema: são função de (semente,
    perfil) e saem idênticos ao estender. A corrida é a única peça do plano
    que depende de quantos perfis existem, e por isso é a única que precisa
    desta função.

    Os novos recebem um sorteio balanceado PRÓPRIO, determinístico em
    (semente, ids novos, desenho, balanceamento). A alternativa — continuar a
    fila original — não existe: a fila é construída para um tamanho e
    embaralhada, não é uma sequência que se estenda. Duas filas balanceadas
    somadas mantêm a proporção por cargo, que é o que o balanceamento protege.
    """
    novos = [p for p in sorted(novos_ids) if p not in registradas]
    if not novos:
        return dict(registradas)
    extra = sortear_corridas(novos, seed=seed, desenho=desenho,
                             balanceamento=balanceamento)
    return {**registradas, **extra}


def resumo_cobertura(atribuidas: dict[str, Corrida],
                     desenho: str = DESENHO_PADRAO) -> dict:
    """Quantas corridas do desenho a rodada de fato cobre.

    Cobertura incompleta não é erro — com poucos perfis é inevitável —, mas tem
    de aparecer no log, senão a rodada sai achando que cobriu o país.
    """
    todas = celulas(desenho)
    vistas = {(c.cargo, c.uf) for c in atribuidas.values()}
    faltando = [c for c in todas if (c.cargo, c.uf) not in vistas]
    return {
        "corridas_possiveis": len(todas),
        "corridas_cobertas": len(todas) - len(faltando),
        "faltando": [c.colunas()["corrida"] for c in faltando],
        "por_cargo": dict(sorted(collections.Counter(
            c.cargo for c in atribuidas.values()).items())),
    }
