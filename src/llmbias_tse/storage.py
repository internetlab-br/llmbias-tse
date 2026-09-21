"""Persistência das trocas — o coração do e2e.

Cada troca (prompt -> resposta) vira um registro estruturado, pronto para
ser consumido por um LLM-juiz na etapa de avaliação de viés. Guardamos:

  - JSONL append-only (`exchanges.jsonl`) — uma linha por troca, com todos
    os metadados (ferramenta, modelo, prompt, resposta, timestamps, status).
  - Artefatos brutos por troca (HTML + screenshot) numa pasta da rodada,
    referenciados no registro — para auditoria e reprodutibilidade.

Tudo fica sob `data/<run_id>/`, fora do versionamento (pode conter dados
de conta pessoal).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DATA_ROOT = Path("data")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    # ex.: 20260604_143015 — ordenável, legível.
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class Exchange:
    """Um par prompt->resposta com uma ferramenta, mais metadados.

    Serve tanto para a POC (1 troca solta) quanto para o experimento ESEB
    (um turno de uma conversa). Os campos do experimento (script_id, gênero,
    ideologia, estilo, dimensão, tipo de conversa, id da conversa, turno)
    são opcionais e ficam None na POC.
    """

    run_id: str
    tool: str                      # "chatgpt", "gemini", ...
    session: str                   # "logged_in" | "anon"
    prompt_id: int                 # índice do prompt na rodada
    prompt: str
    response: str = ""
    model: str | None = None       # se a UI expuser (ex.: "GPT-4o")
    conversation_url: str | None = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    response_chars: int = 0
    ok: bool = False
    error: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    # --- metadados do experimento ESEB (opcionais) ---
    script_id: str | None = None        # "S02", "S18", ...
    gender: str | None = None           # "Homem" | "Mulher"
    ideology: str | None = None         # "Esquerda" | "Centro" | "Direita"
    style: str | None = None            # "Neutro" | "Adversarial" | "Adulante"
    dimension: str | None = None        # "voto", "urnas", ...
    conv_type: str | None = None        # "short" | "long"
    conversation_id: str | None = None  # agrupa os turnos de uma conversa
    turn: int | None = None             # turno 1-based dentro da conversa
    n_turns: int | None = None          # total de turnos da conversa


_REC_CACHE: dict[str, dict] = {}


def _recuperados(run_dir=None) -> dict:
    """Índice `(conversa, turno) -> recuperação` lido de `recuperados.jsonl`.

    Arquivo opcional; sem ele a leitura segue igual. Existe porque em alguns
    turnos o texto gravado é um PREFIXO do que a página tinha, e o fim está no
    HTML do artefato. Recuperar dali foi decisão do time em 21/09/2026 —
    recoletar no WhatsApp custa horas por conversa.
    """
    base = Path(run_dir) if run_dir else DATA_ROOT / "experimento_2026_09"
    chave = str(base)
    if chave in _REC_CACHE:
        return _REC_CACHE[chave]
    idx: dict = {}
    f = base / "recuperados.jsonl"
    if f.exists():
        for linha in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(linha)
            except Exception:
                continue
            idx[(r["conversation_id"], r["turno"])] = r
    _REC_CACHE[chave] = idx
    return idx


def turnos_limpos(conversation: dict) -> list[dict]:
    """Os turnos da conversa com o cromo de interface removido da resposta.

    Aplicada NA LEITURA, não na gravação: o arquivo em `conversations/` é o
    registro BRUTO do que a página devolveu, e reescrever resposta já coletada
    é mexer no conteúdo do experimento. Quem consome — o juiz e a base de
    análise — lê por aqui.

    Existe porque um cromo pode ser descoberto depois de a conversa estar
    gravada, e foi o que aconteceu em 18/09/2026: 124 de 439 turnos do Google
    AI Mode carregavam 132 chars do aria-live do botão de copiar ("Copiado
    para a área de transferência…"), colados no fim da resposta. O driver
    passou a cortá-los na captura, mas os já gravados só se corrigem aqui —
    e aqui a correção vale para todos, velhos e novos, sem perder o bruto.

    Usa o `limpar_resposta` DO DRIVER da plataforma, que é onde mora o
    conhecimento sobre o rodapé de cada UI. Plataforma desconhecida passa
    intacta.
    """
    from .drivers import REGISTRY
    from .conjoint_experiment import PLATFORM_DRIVERS

    recuperados = _recuperados(conversation.get("run_dir"))
    plat = conversation.get("platform")
    chave = (PLATFORM_DRIVERS.get(plat) or (plat,))[0]
    driver = REGISTRY.get(chave)
    turns = conversation.get("turns") or []
    if driver is None:
        return list(turns)
    d = driver()
    cid = conversation.get("conversation_id")
    saida = []
    for t in turns:
        r = t.get("response")
        if not r:
            saida.append(t)
            continue
        # Continuação recuperada do artefato, quando existe (ver
        # `infra/local/recuperar_artefato.py`). Emendada NA LEITURA, como o
        # resto: o bruto continua sendo o que a captura trouxe.
        rec = recuperados.get((cid, t.get("turn")))
        emendado = r
        if rec:
            emendado = r.rstrip() + " " + rec["continuacao"]
        limpo = d.limpar_resposta(emendado)
        if limpo == r:
            saida.append(t)   # nada a fazer neste turno
            continue
        novo = dict(t)
        novo["response"] = limpo
        novo["response_chars"] = len(limpo)
        # Guarda o que entrou e o que saiu: sem isto, "o texto mudou" vira
        # afirmação sem prova, e a diferença entre o bruto e o lido fica
        # indevassável. Comparar com `emendado`, e não com `r`, senão a
        # recuperação aparece como cromo REMOVIDO negativo.
        cromo = len(emendado) - len(limpo)
        if cromo:
            novo["chars_cromo_removido"] = cromo
        if rec:
            novo["chars_recuperados"] = rec["chars_recuperados"]
        saida.append(novo)
    return saida


class RunStore:
    """Escreve registros e artefatos de uma rodada de coleta."""

    def __init__(self, run_id: str | None = None, root: Path = DATA_ROOT):
        self.run_id = run_id or new_run_id()
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "exchanges.jsonl"

    def artifacts_dir(self, session: str, tool: str, prompt_id: int) -> Path:
        return self.dir / "artifacts" / session / tool / f"{prompt_id:02d}"

    def turn_artifacts_dir(self, conversation_id: str, turn: int) -> Path:
        """Pasta de artefatos de um turno do experimento."""
        return self.dir / "artifacts" / conversation_id / f"turn_{turn:02d}"

    def append(self, ex: Exchange) -> None:
        ex.response_chars = len(ex.response or "")
        if ex.finished_at is None:
            ex.finished_at = _now_iso()
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(ex), ensure_ascii=False) + "\n")

    def save_conversation(self, record: dict) -> Path:
        """Salva uma conversa completa (multi-turno) como JSON legível, e
        também a anexa em `conversations.jsonl`. Esse arquivo por-conversa é
        a entrada natural do LLM-juiz: persona + dimensão + lista de turnos.
        """
        conv_dir = self.dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        path = conv_dir / f"{record['conversation_id']}.json"
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (self.dir / "conversations.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def summary(self) -> str:
        return f"{self.run_id}: registros em {self.jsonl}"
