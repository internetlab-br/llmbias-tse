"""Empacota a rodada para análise: brutos, plano e tabelas processadas.

Um zip só, organizado, com o que outra pessoa precisa para rodar o juiz e as
demais análises sem ter acesso a esta máquina.

O que NÃO entra: `artifacts/` (HTML e PNG de cada turno). São dezenas de GB e
servem para depurar seletor quebrado, não para analisar — vão separados, se
alguém precisar.

As tabelas processadas saem dos turnos LIMPOS (`storage.turnos_limpos`), que
tiram o cromo de interface que a plataforma cola na resposta; os brutos vão
como foram capturados. A diferença entre os dois é auditável pela coluna
`chars_cromo_removido`.

    uv run python infra/local/bundle.py
    uv run python infra/local/bundle.py --run-dir data/experimento_2026_09 --saida /tmp
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from llmbias_tse.storage import turnos_limpos

SUFIXOS = ("loading", "carregando", "gerando")
FUNCAO = (r"(o|a|os|as|um|uma|uns|umas|de|do|da|dos|das|em|no|na|nos|nas|por|"
          r"pelo|pela|para|com|sem|que|se|e|ou|mas|como|ao|à|aos|às|é|são|"
          r"seu|sua|meu|minha|este|esta|esse|essa|aquele|mais|muito|já|não)")
RE_CORTE = re.compile(rf"\b{FUNCAO}\s*$", re.I)
RE_NAO_LATINO = re.compile(
    r"[Ѐ-ӿ֐-ࣿ　-鿿가-힯＀-￯]")


def _defeito(resp: str | None) -> str:
    s = (resp or "").rstrip()
    if not s:
        return "vazia"
    if s.lower().endswith(SUFIXOS):
        return "sufixo_incompleto"
    if RE_CORTE.search(s):
        return "frase_cortada"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--saida", default="data")
    args = ap.parse_args()
    run = Path(args.run_dir)
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    base = Path(args.saida) / f"bundle_{run.name}_{carimbo}"
    if base.exists():
        shutil.rmtree(base)
    (base / "brutos").mkdir(parents=True)
    (base / "plano").mkdir()
    (base / "processados").mkdir()
    (base / "quarentena").mkdir()

    # ---- brutos ----------------------------------------------------------
    shutil.copytree(run / "conversations", base / "brutos" / "conversations")
    for nome in ("events.jsonl", "conversations.jsonl", "vigia.jsonl"):
        if (run / nome).exists():
            shutil.copy2(run / nome, base / "brutos" / nome)
    if (run / "control").exists():
        shutil.copytree(run / "control", base / "brutos" / "control")

    # ---- plano e ambiente ------------------------------------------------
    for nome in ("plano_coleta.json", "plano_coleta.csv",
                 "plano_coleta_completo.xlsx", "profiles.json", "rubrics.json",
                 "ambiente.json", "plano_coleta.100perfis.json",
                 "profiles.100perfis.json"):
        if (run / nome).exists():
            shutil.copy2(run / nome, base / "plano" / nome)

    # ---- anotações do juiz (quando houver) -------------------------------
    anot = run / "annotations"
    n_anot = 0
    if anot.exists():
        n_anot = len(list(anot.glob("*.json")))
        if n_anot:
            shutil.copytree(anot, base / "anotacoes")
    if (run / "lotes_juiz.json").exists():
        shutil.copy2(run / "lotes_juiz.json", base / "plano" / "lotes_juiz.json")
    if (run / "recuperados.jsonl").exists():
        shutil.copy2(run / "recuperados.jsonl",
                     base / "brutos" / "recuperados.jsonl")
    if (run / "limpezas_whatsapp.jsonl").exists():
        shutil.copy2(run / "limpezas_whatsapp.jsonl",
                     base / "brutos" / "limpezas_whatsapp.jsonl")

    # ---- quarentena ------------------------------------------------------
    for d in run.glob("descartadas_*"):
        shutil.copytree(d, base / "quarentena" / d.name)
    for d in run.glob("refazer_*"):
        shutil.copytree(d, base / "quarentena" / d.name)

    # ---- tabelas processadas --------------------------------------------
    convs, turnos = [], []
    resumo = collections.Counter()
    for f in sorted((run / "conversations").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        ts_brutos = d.get("turns") or []
        ts = turnos_limpos(d)
        completa = bool(ts) and all(t.get("ok") for t in ts)
        prof = d.get("profile") or {}
        cor = d.get("corrida") or {}
        defeitos = [_defeito(t.get("response")) for t in ts if t.get("ok")]
        convs.append({
            "conversation_id": d["conversation_id"],
            "plataforma": d["platform"], "eixo": d["eixo"],
            "perfil_id": prof.get("id"), "sessao": d.get("sessao"),
            "conta": d.get("conta"), "modo": d.get("mode"),
            "modelo_exibido": d.get("modelo_exibido"),
            "ip_saida": d.get("ip_saida"), "ip_saida_org": d.get("ip_saida_org"),
            "mensagens_no_chat": d.get("mensagens_no_chat"),
            "politica": prof.get("politica"), "genero": prof.get("genero"),
            "idade": prof.get("idade"), "escolaridade": prof.get("escolaridade"),
            "estilo_conversa": prof.get("estilo_conversa"),
            "estilo_escrita": prof.get("estilo_escrita"),
            "corrida_cargo": cor.get("cargo"), "corrida_uf": cor.get("uf"),
            "corrida_descricao": cor.get("descricao"),
            "alternativas": "+".join(d.get("alternativas") or []),
            "temas_incluidos": "+".join(
                k for k, v in (d.get("cobertura_temas") or {}).items() if v),
            "n_turnos": len(ts),
            "n_turnos_ok": sum(1 for t in ts if t.get("ok")),
            "completa": int(completa),
            "chars_totais": sum(t.get("response_chars") or 0 for t in ts),
            "turnos_com_defeito": sum(1 for x in defeitos if x),
            "defeitos": "+".join(sorted({x for x in defeitos if x})),
            "iniciada_em": d.get("started_at"), "encerrada_em": d.get("finished_at"),
        })
        resumo[(d["platform"], d["eixo"], "completa" if completa else "incompleta")] += 1
        for tb, t in zip(ts_brutos, ts):
            turnos.append({
                "conversation_id": d["conversation_id"],
                "plataforma": d["platform"], "eixo": d["eixo"],
                "perfil_id": prof.get("id"), "turno": t.get("turn"),
                "ok": int(bool(t.get("ok"))), "erro": t.get("error") or "",
                "prompt": t.get("prompt") or "",
                "resposta": t.get("response") or "",
                "resposta_chars": t.get("response_chars") or 0,
                "chars_cromo_removido": t.get("chars_cromo_removido") or 0,
                "frac_nao_latino": round(
                    len(RE_NAO_LATINO.findall(t.get("response") or ""))
                    / max(1, len(t.get("response") or "")), 3),
                "defeito": _defeito(t.get("response")) if t.get("ok") else "",
                "n_fontes": t.get("n_fontes") or 0,
                "iniciado_em": t.get("started_at"),
                "encerrado_em": t.get("finished_at"),
            })

    def escreve(nome, linhas):
        if not linhas:
            return
        caminho = base / "processados" / nome
        with caminho.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(linhas[0]))
            w.writeheader()
            w.writerows(linhas)
        print(f"  {nome}: {len(linhas)} linhas")

    escreve("conversas.csv", convs)
    escreve("turnos.csv", turnos)
    with (base / "processados" / "conversas.jsonl").open("w", encoding="utf-8") as fh:
        for c in convs:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    # ---- LEIA-ME ---------------------------------------------------------
    plats = sorted({c["plataforma"] for c in convs})
    linhas_tab = []
    for p in plats:
        col = []
        for e in ("voto", "integridade", "genero"):
            n = resumo[(p, e, "completa")]
            col.append(f"{n}/120")
        linhas_tab.append(f"| {p} | " + " | ".join(col) + " |")
    completas = sum(c["completa"] for c in convs)
    com_defeito = sum(1 for c in convs if c["turnos_com_defeito"])
    (base / "LEIA-ME.md").write_text(f"""# Rodada 2 — {run.name}

Pacote gerado em {datetime.now(timezone.utc).isoformat(timespec="seconds")}.
{len(convs)} conversas registradas, {completas} completas.

## Estrutura

- `brutos/conversations/` — **um JSON por conversa, como foi capturado**.
  É o registro primário: persona, eixo, corrida atribuída, roteiro, e cada
  turno com prompt, resposta e horários. Nada aqui foi editado.
- `brutos/events.jsonl` — o que aconteceu durante a coleta, por sessão:
  turno ok, erro, bloqueio, rate limit, avanço de eixo.
- `brutos/control/` — estado de cada estação ao final.
- `plano/` — o **pré-registro**: `plano_coleta.json` (uma entrada por conversa
  planejada, com alternativas, temas e corrida), `profiles.json` (a amostra),
  `rubrics.json` (a rubrica v7 congelada), `ambiente.json` (IP de saída,
  operadora, conta e versão do Chrome por estação).
  `*.100perfis.json` é o plano antes da extensão de 100 para 120 perfis.
- `processados/conversas.csv` — uma linha por conversa: fatores do conjoint,
  corrida, temas, conta, modelo exibido, IP, contagens e defeitos.
- `processados/turnos.csv` — uma linha por turno, com prompt e resposta.
- `quarentena/` — conversas retiradas da base, cada pasta com um LEIA-ME
  explicando o motivo. Não foram apagadas de propósito.

## Cobertura

| plataforma | voto | integridade | gênero |
|---|---|---|---|
{chr(10).join(linhas_tab)}

## Estado da coleta

**Encerrada.** Voto e integridade fecharam 120/120 nas oito plataformas. O
gênero fechou em 120 em seis delas; o Grok parou em 103 (limite semanal da
conta) e o WhatsApp em 100 (o Meta AI parou de responder — a não-resposta
silenciosa, com as conversas afetadas abortadas em vez de gravadas pela
metade).

## O que ainda NÃO está fechado

1. **{com_defeito} conversas têm captura defeituosa**, marcadas na coluna
   `defeitos` de `conversas.csv` e `defeito` de `turnos.csv`. São turnos do
   WhatsApp que acabam no meio da frase e cuja continuação NÃO estava no
   artefato (o DOM também estava cortado); os demais foram recuperados — ver
   `brutos/recuperados.jsonl`, que registra cada emenda e de qual arquivo
   veio. Só recoleta resolveria, e a plataforma está bloqueada.
2. **O LLM-as-a-judge está em curso** (Batch API). `anotacoes/` traz o que já
   voltou — {n_anot} conversa(s) — e `plano/lotes_juiz.json` o estado de cada
   lote. O painel é `flash` na base inteira de voto+integridade e
   `sonnet`+`luna` numa amostra de 10% estratificada por plataforma × eixo
   (semente 2026, refazível). O gênero ainda não foi julgado.

## Ressalvas para a análise

- **DeepSeek responde em chinês** em parte dos turnos; `frac_nao_latino` em
  `turnos.csv` mede a fração fora do alfabeto latino.
- **As duas contas do Claude têm idiomas de interface diferentes**, e por isso
  o `modelo_exibido` sai como "Sonnet 5 Médio" e "Sonnet 5 Medium" para o
  mesmo modelo.
- **`mensagens_no_chat`** (só WhatsApp) é a carga do chat do Meta AI no
  início da conversa. O acúmulo encurta a resposta: a mediana do eixo
  integridade cai de ~2.000 para ~640 chars nas janelas carregadas.
- **`conta`** distingue as contas de uma mesma plataforma. O Gemini teve
  troca de conta no meio (`conta_1` -> `conta_4`).
- As respostas em `processados/` passaram pela limpeza de cromo de interface;
  os brutos, não. `chars_cromo_removido` diz quanto saiu em cada turno.

## O que ficou de fora

`artifacts/` (HTML e PNG de cada turno, dezenas de GB) serve para depurar
seletor quebrado, não para analisar. Vai separado, se alguém precisar.
""", encoding="utf-8")

    zipado = shutil.make_archive(str(base), "zip", root_dir=base.parent,
                                 base_dir=base.name)
    tam = Path(zipado).stat().st_size / 1e6
    print(f"\nbundle: {zipado} ({tam:.0f} MB)")
    shutil.rmtree(base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
