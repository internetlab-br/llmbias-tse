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


# A TABELA DE UFs — PENDENTE.
#
# Quais UFs entram no sorteio é a decisão 2 da nota, do InternetLab, e a tabela
# vem da equipe. Enquanto ela não chega, a lista fica VAZIA de propósito:
# `sortear_corrida` levanta erro ao sortear um cargo que exige UF sem tabela,
# em vez de inventar um estado — que é exatamente o modo de falha que esta
# correção existe para eliminar.
UFS: tuple[UF, ...] = ()

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

# Padrão até a decisão: o único desenho que roda sem a tabela de UFs. Não é
# recomendação — é o que não trava a coleta enquanto a decisão não chega.
DESENHO_PADRAO = "so_presidente"

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


def sortear_corrida(profile_id: str, seed: int = 2026,
                    desenho: str = DESENHO_PADRAO) -> Corrida:
    """Sorteia a corrida de UM perfil.

    Determinístico em (semente, perfil) e cego à plataforma — a mesma célula do
    conjoint recebe a mesma corrida nas sete. O rótulo "corrida" na semente do
    RNG isola este sorteio do de temas e do de exemplares: acrescentá-lo não
    desloca nenhum roteiro já planejado.
    """
    cargos = DESENHOS.get(desenho)
    if cargos is None:
        raise ValueError(
            f"desenho de corrida {desenho!r} desconhecido; "
            f"disponíveis: {sorted(DESENHOS)}"
        )
    rng = _rng_for(seed, "corrida", desenho, profile_id)
    cargo = CARGOS[rng.choice(list(cargos))]
    if not cargo.exige_uf:
        return Corrida(cargo=cargo.key)
    if not UFS:
        raise ValueError(
            f"o desenho {desenho!r} sorteia {cargo.key!r}, que exige UF, mas a "
            f"tabela `corridas.UFS` está vazia. Preencha-a com a tabela da "
            f"equipe antes de rodar (decisão 2 da nota metodológica)."
        )
    return Corrida(cargo=cargo.key, uf=rng.choice(list(UFS)).sigla)


def sortear_corridas(profile_ids: Sequence[str], seed: int = 2026,
                     desenho: str = DESENHO_PADRAO) -> dict[str, Corrida]:
    """A corrida de cada perfil da rodada, na mesma chamada do plano."""
    return {pid: sortear_corrida(pid, seed=seed, desenho=desenho)
            for pid in profile_ids}
