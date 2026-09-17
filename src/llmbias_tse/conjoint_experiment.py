"""Runner do experimento de CONJOINT ANALYSIS (pré-teste multi-plataforma).

Pipeline:
  1. Sorteia N perfis únicos (6 fatores, incl. escolaridade) de forma
     determinística.
  2. Carrega a rubrica curada de cada eixo (grade tipo × voz): `voto`
     (ranqueamento), `genero` (violência de gênero) e `integridade`.
  3. Para cada plataforma × perfil × eixo, conduz uma conversa de N turnos
     (default 10) contra a plataforma sob teste no browser: o LLM as a user
     (API gemini-3.5-flash) gera cada turno reagindo à resposta real da
     plataforma, escalando a posição não conforme. O CONTEÚDO de cada turno
     vem do instrumento curado do eixo (`instrumentos.py`): o roteiro da
     conversa inteira — alternativas, exemplares, quais turnos levam duas
     perguntas — é resolvido FORA do modelo por `instrument.plan_round()`,
     antes da coleta, e o agente recebe a ficha de um turno por vez. Toda
     conversa cobre TODOS os temas do eixo, ao menos duas vezes cada, conforme
     as instruções de combinação. O Gemini roda em conversa momentânea (sem
     memória); as demais plataformas abrem um chat novo a cada conversa.
  4. Aplica o LLM as a judge (v4.0) a cada conversa: extrai achados POR
     RESPOSTA (tipo × voz) e agrega contagens descritivas (sem escore somado).
  5. Escreve a base final (uma linha por conversa) em dataset.jsonl/csv/parquet.

Tudo sob data/<run_id>/. Resumível: conversas já concluídas e anotadas são
puladas ao reusar --run-id.
"""

from __future__ import annotations

import collections
import json
import os
import random
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from patchright.sync_api import sync_playwright

from . import browser, capture, corridas, events, llm
from .axes import EIXOS
from .conjoint import Profile, load_seed, persona_presentation, sample_profiles
from .drivers import REGISTRY
from .judge import annotate, annotate_conversa, annotate_panel
from .rubrics import RESISTENCIAS, RUBRICS, RubricGrid, get_rubric
from .storage import RunStore, _now_iso
from . import instrument
from .instrumentos import DUPLAS_RESERVA, get_instrumento
from .user_agent import UserAgent

# Plataforma -> (driver no REGISTRY, rótulo de modo de isolamento).
# Isolamento de memória entre conversas (para não contaminar o experimento),
# todos verificados ao vivo (jul/2026):
#   gemini  -> conversa momentânea
#   chatgpt -> conversa temporária (?temporary-chat=true)
#   grok    -> conversa privada (Switch to Private Chat)
#   claude  -> conversa incognito (Use incognito -> /new?incognito=)
#   deepseek-> NÃO tem modo temporário; usa chat novo. É isolado por não ter
#             memória entre conversas (sem "reference chat history"); a única
#             diferença é que a conversa fica salva no histórico (não afeta a
#             independência entre conversas). Ver DEEPSEEK_SEM_MOMENTANEA.
PLATFORM_DRIVERS: dict[str, tuple[str, str]] = {
    "gemini": ("gemini_momentary", "momentanea"),
    "chatgpt": ("chatgpt_momentary", "temporaria"),
    "grok": ("grok_momentary", "privada"),
    "claude": ("claude_momentary", "incognito"),
    "deepseek": ("deepseek", "chat_novo"),
    # Copilot (Microsoft, conta pessoal): chat temporário (/chats/temporary).
    "copilot": ("copilot_momentary", "temporaria"),
    # Google AI Mode (udm=50): sem incognito; isolamento por navegação fresca
    # a cada conversa (thread nova). Fora do default: `--platforms google_aimode`.
    "google_aimode": ("google_aimode", "thread_nova"),
    # WhatsApp/Meta AI: NÃO tem chat novo; o isolamento é por `/reset-all-ais`
    # (feito no open_new_chat do driver). Requer WhatsApp Web logado (QR) e o
    # chat do Meta AI aberto à mão. Fora do default: rodar com
    # `--platforms whatsapp_metaai`.
    "whatsapp_metaai": ("whatsapp_metaai", "reset_memoria"),
}

# DeepSeek não oferece conversa temporária/privada na UI (jul/2026). Um chat
# novo já é isolado (a plataforma não tem memória entre conversas), mas fica
# salvo no histórico da conta.
DEEPSEEK_SEM_MOMENTANEA = True

# Tema de subordinação do eixo de gênero: o único cuja porta de entrada faz o
# modelo enumerar duplas de pessoas reais, e o único com lista de reserva.
_TEMA_SUBORDINACAO = "T5"

# Temas como variável independente binária: prob. de inclusão de cada tema e
# piso por conversa. O piso 1 é NECESSÁRIO (sem tema não há pergunta a fazer nos
# eixos com instrumento) e é o que menos distorce o desenho — exigir 2 desloca
# bem mais a marginal e correlaciona os temas entre si.
DEFAULT_TEMA_PROB = 0.5
DEFAULT_MIN_TEMAS = 1

DEFAULT_PLATFORMS = ["gemini", "chatgpt", "claude", "deepseek", "grok",
                     "copilot"]
DEFAULT_EIXOS = list(RUBRICS.keys())  # ["voto", "genero", "integridade"]
# Sem `--n-turns`, cada eixo usa o tamanho de conversa que a especificação
# fixa para ele (7 no ranqueamento, 10 nos outros dois).


# --------------------------------------------------------------------------
# Persistência auxiliar
# --------------------------------------------------------------------------

def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _fatiar_perfis(profiles: list[Profile], fatia: str) -> list[Profile]:
    """Divide os PERFIS em fatias disjuntas: `"2/3"` = segunda de três.

    Existe para coletar uma plataforma em várias contas ao mesmo tempo sem
    confundir a conta com o eixo. Se cada conta pegasse um eixo, toda conversa
    de `voto` viria de uma conta e toda de `genero` de outra — e na rodada 1 o
    efeito de conta no Meta AI foi grande (a recusa evasiva apareceu em 69% dos
    turnos de uma conta e em 0% de outra). Fatiando por PERFIL, cada conta roda
    os TRÊS eixos, então a conta varia dentro de cada eixo.

    A fatia é determinística (ordena por id e reparte por resto), então as
    fatias são disjuntas sem combinação entre as máquinas, e o balanceamento
    entre eixos sai por construção: cada fatia faz todos os eixos dos perfis
    dela, e os tamanhos diferem em no máximo um.
    """
    try:
        i, n = (int(x) for x in fatia.split("/"))
    except Exception:
        raise SystemExit(f"--fatia inválida: {fatia!r} (use i/n, ex.: 2/3)")
    if not (1 <= i <= n):
        raise SystemExit(f"--fatia fora da faixa: {fatia!r} (1 <= i <= n)")
    ordenados = sorted(profiles, key=lambda p: p.id)
    escolhidos = [p for k, p in enumerate(ordenados) if k % n == i - 1]
    print(f"[conjoint] fatia {i}/{n}: {len(escolhidos)} de {len(profiles)} "
          f"perfis ({escolhidos[0].id}..{escolhidos[-1].id})"
          if escolhidos else f"[conjoint] fatia {i}/{n}: nenhum perfil")
    return escolhidos


def _load_or_sample_profiles(store: RunStore, n: int, seed: int) -> list[Profile]:
    path = store.dir / "profiles.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        print(f"[conjoint] reusando {len(raw)} perfis de {path}")
        return [Profile(**p) for p in raw]
    profiles = sample_profiles(n, seed=seed)
    _save_json(path, [asdict(p) for p in profiles])
    print(f"[conjoint] sorteados {len(profiles)} perfis (seed={seed}) -> {path}")
    return profiles


def _snapshot_rubrics(store: RunStore, eixos: list[str]) -> dict[str, RubricGrid]:
    rubrics = {e: get_rubric(e) for e in eixos}
    path = store.dir / "rubrics.json"
    if not path.exists():
        _save_json(path, {k: r.to_dict() for k, r in rubrics.items()})
        print(f"[conjoint] rubricas 4.0 (curadas) registradas em {path}")
    return rubrics


def _conferir_plano_compativel(plano: dict, eixos, desenho_corrida: str,
                               calendario: corridas.Calendario,
                               balanceamento: str = corridas.BALANCEAMENTO_PADRAO,
                               ) -> None:
    """Retomar uma rodada antiga NÃO pode trocar o estímulo no meio.

    O plano é recalculado a cada execução e o arquivo serve de registro, então
    nada impede — em silêncio — que uma rodada começada antes da correção de
    setembro/2026 tenha as primeiras conversas de voto perguntando sobre "a
    eleição atual" e as seguintes sobre uma corrida atribuída. Metade da perna
    com um estímulo e metade com outro é pior do que qualquer uma das duas
    sozinhas: nem compara com agosto, nem responde pela rodada nova.

    Por isso a incompatibilidade ABORTA em vez de avisar.
    """
    com_corrida = [e for e in eixos if e in corridas.EIXOS_COM_CORRIDA]
    if not com_corrida:
        return
    anterior = plano.get("desenho_corrida")
    if anterior is None:
        raise SystemExit(
            f"[conjoint] ABORTADO: esta rodada foi planejada ANTES da correção "
            f"do eixo voto (setembro/2026) e o plano não registra corrida "
            f"atribuída. Retomá-la com o código atual daria estímulo diferente "
            f"às conversas que faltam.\n"
            f"  - para continuar a rodada antiga: rode sem os eixos "
            f"{com_corrida} (--eixos ...);\n"
            f"  - para coletar com a correção: use um --run-id novo."
        )
    cal_anterior = plano.get("calendario") or {}
    bal_anterior = plano.get("balanceamento_corrida",
                             corridas.BALANCEAMENTO_PADRAO)
    if (anterior != desenho_corrida or cal_anterior != calendario.to_dict()
            or bal_anterior != balanceamento):
        raise SystemExit(
            f"[conjoint] ABORTADO: o plano desta rodada foi montado com "
            f"desenho={anterior!r}, balanceamento={bal_anterior!r} e "
            f"calendário={cal_anterior}, e agora a execução pede "
            f"desenho={desenho_corrida!r}, balanceamento={balanceamento!r} e "
            f"calendário={calendario.to_dict()}. Mudar isso no meio troca a "
            f"pergunta. Use um --run-id novo."
        )


def _load_or_build_plan(store: RunStore, profiles, platforms, eixos,
                        seed: int, n_turns: int | None,
                        tema_prob: float = DEFAULT_TEMA_PROB,
                        min_temas: int = DEFAULT_MIN_TEMAS,
                        desenho_corrida: str = corridas.DESENHO_PADRAO,
                        calendario: corridas.Calendario | None = None,
                        balanceamento: str = corridas.BALANCEAMENTO_PADRAO,
                        ) -> tuple[dict[str, dict[str, tuple]],
                                   dict[str, dict[str, dict[str, bool]]],
                                   dict[str, dict[str, corridas.Corrida]]]:
    """Monta (ou retoma) o PLANO DE COLETA da rodada ANTES de rodar.

    Para cada eixo com instrumento, `instrument.plan_round()` resolve o roteiro
    de todos os perfis de uma vez — precisa ser de uma vez porque a não
    repetição de exemplares vale ENTRE conversas. O roteiro é função de (seed,
    eixo, perfil) e não da plataforma, então a mesma célula do conjoint recebe
    o mesmo estímulo em todas as plataformas.

    O eixo voto recebe ainda a CORRIDA atribuída (cargo e, quando o cargo
    exige, UF), sorteada com a mesma propriedade: função de (semente, perfil) e
    cega à plataforma. É ela que substitui "a eleição atual" da ficha de agosto.

    Persistido em `plano_coleta.json` (fonte da verdade, retomável) e
    `plano_coleta.csv` (uma linha por conversa planejada, para inspeção e
    pré-registro). Devolve {eixo: {profile_id: roteiro}}.
    """
    path = store.dir / "plano_coleta.json"
    pids = [p.id for p in profiles]
    calendario = calendario or corridas.CALENDARIO_2026
    if path.exists():
        try:
            plano_anterior = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — json ou leitura, dá no mesmo
            raise SystemExit(
                f"[conjoint] ABORTADO: {path} existe mas não é legível ({e!r}). "
                f"É esse arquivo que registra o estímulo da rodada; sem "
                f"conferi-lo não há como garantir que a retomada faz as mesmas "
                f"perguntas que as conversas já coletadas."
            )
        _conferir_plano_compativel(plano_anterior, eixos, desenho_corrida,
                                   calendario, balanceamento)

    roteiros: dict[str, dict[str, tuple]] = {}
    temas_plan: dict[str, dict[str, dict[str, bool]]] = {}
    corridas_plan: dict[str, dict[str, corridas.Corrida]] = {}
    avisos: dict[str, list[str]] = {}
    for e in eixos:
        # A corrida é do eixo voto e independe de haver instrumento.
        corridas_plan[e] = (
            corridas.sortear_corridas(pids, seed=seed, desenho=desenho_corrida,
                                      balanceamento=balanceamento)
            if e in corridas.EIXOS_COM_CORRIDA else {}
        )
        inst = get_instrumento(e)
        if inst is None:
            print(f"[conjoint]   eixo {e}: sem instrumento (arco de referência)")
            roteiros[e] = {}
            temas_plan[e] = {pid: {} for pid in pids}
            continue
        # Temas como variável independente binária (sorteio por perfil,
        # independente da plataforma). Eixo sem temas (voto) passa direto.
        tp = ({} if inst.sem_temas
              else instrument.sortear_temas(inst, pids, seed=seed,
                                            prob=tema_prob,
                                            min_temas=min_temas))
        temas_plan[e] = tp or {pid: {} for pid in pids}
        rot, av = instrument.plan_round(
            inst, pids, seed=seed, n_turns=n_turns or inst.n_turns,
            temas_por_perfil=(tp or None),
        )
        roteiros[e] = rot
        avisos[e] = av
        if tp:
            cont = {t.key: sum(1 for pid in pids if tp[pid].get(t.key))
                    for t in inst.temas}
            media = sum(sum(1 for v in tp[pid].values() if v)
                        for pid in pids) / max(1, len(pids))
            print(f"[conjoint]   eixo {e}: temas sorteados (p={tema_prob}, "
                  f"piso={min_temas}) — perfis por tema: {cont} | "
                  f"média de temas/conversa: {media:.2f}")
        if corridas_plan.get(e):
            cob = corridas.resumo_cobertura(corridas_plan[e], desenho_corrida)
            print(f"[conjoint]   eixo {e}: corridas atribuídas "
                  f"(desenho={desenho_corrida}, balanceamento={balanceamento}) "
                  f"— por cargo: {cob['por_cargo']} | cobertura: "
                  f"{cob['corridas_cobertas']}/{cob['corridas_possiveis']}")
            if cob["faltando"]:
                # Cobertura incompleta não é erro (com poucos perfis é
                # inevitável), mas não pode ficar invisível: sem esta linha a
                # rodada sai parecendo que cobriu o país.
                print(f"[conjoint]     sem nenhuma conversa: "
                      f"{len(cob['faltando'])} corrida(s) — "
                      f"{', '.join(cob['faltando'][:8])}"
                      + (" ..." if len(cob["faltando"]) > 8 else ""))

    if path.exists():
        print(f"[conjoint] reusando plano de coleta de {path}")
    else:
        conversas = []
        for pl in platforms:
            for prof in profiles:
                for e in eixos:
                    inst = get_instrumento(e)
                    rot = roteiros.get(e, {}).get(prof.id, ())
                    cob = instrument.resumo_cobertura(inst, rot) if inst else {}
                    fmt = instrument.resumo_formato(rot)
                    flags = temas_plan.get(e, {}).get(prof.id, {}) or {}
                    cor = corridas_plan.get(e, {}).get(prof.id)
                    conversas.append({
                        "conversation_id": f"{pl}_{prof.id}_{e}",
                        "platform": pl, "perfil_id": prof.id, "eixo": e,
                        "alternativas": [
                            a.key for t in rot for a in t.alternativas
                        ],
                        "temas_flags": flags,
                        "temas_incluidos": [c for c, v in flags.items() if v],
                        "cobertura_temas": cob,
                        "corrida": cor.to_dict() if cor else None,
                        **fmt,
                    })
        _save_json(path, {
            "seed": seed, "n_turns": n_turns,
            "tema_prob": tema_prob, "min_temas": min_temas,
            # Parâmetros do estímulo do eixo voto. Ficam no plano porque é ele
            # que outra equipe lê para reproduzir a rodada — e porque é contra
            # eles que `_conferir_plano_compativel` barra uma retomada que
            # trocaria a pergunta no meio da coleta.
            "desenho_corrida": desenho_corrida,
            "balanceamento_corrida": balanceamento,
            "calendario": calendario.to_dict(),
            "platforms": list(platforms), "eixos": list(eixos),
            "conversas": conversas, "avisos": avisos,
        })
        csv_rows = [
            {
                "conversation_id": c["conversation_id"],
                "platform": c["platform"], "perfil_id": c["perfil_id"],
                "eixo": c["eixo"], "turnos": c["turnos"],
                "turnos_uma_pergunta": c["turnos_uma_pergunta"],
                "turnos_duas_perguntas": c["turnos_duas_perguntas"],
                "perguntas_substantivas": c["perguntas_substantivas"],
                "alternativas": "+".join(c["alternativas"]),
                # corrida atribuída (eixo voto): fator de ESTÍMULO, não de
                # perfil — aparece dentro da pergunta feita à plataforma.
                "corrida_cargo": (c["corrida"] or {}).get("cargo", ""),
                "corrida_uf": (c["corrida"] or {}).get("uf") or "",
                "corrida_descricao": (c["corrida"] or {}).get("descricao", ""),
                "n_temas": sum(1 for v in c["temas_flags"].values() if v),
                "temas_incluidos": "+".join(c["temas_incluidos"]),
                # tema_Tx = variável independente (sorteada); aparicoes_Tx =
                # quantas perguntas o roteiro dedicou ao tema (dose).
                **{f"tema_{cod}": int(bool(v))
                   for cod, v in c["temas_flags"].items()},
                **{f"aparicoes_{cod}": n
                   for cod, n in c["cobertura_temas"].items()},
            }
            for c in conversas
        ]
        pd.DataFrame(csv_rows).to_csv(
            store.dir / "plano_coleta.csv", index=False,
            encoding="utf-8-sig", sep=";",
        )
        print(f"[conjoint] plano de coleta montado: {len(conversas)} conversas "
              f"planejadas -> {path}")

    for e in eixos:
        av = avisos.get(e) or []
        if av:
            print(f"[conjoint]   eixo {e}: {len(av)} aviso(s) de planejamento")
            for a in av[:5]:
                print(f"[conjoint]     - {a}")
            if len(av) > 5:
                print(f"[conjoint]     ... (todos em {path.name})")
    return roteiros, temas_plan, corridas_plan


def _conv_path(store: RunStore, conv_id: str) -> Path:
    return store.dir / "conversations" / f"{conv_id}.json"


def _conv_done(store: RunStore, conv_id: str, n_turns: int) -> bool:
    p = _conv_path(store, conv_id)
    if not p.exists():
        return False
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    turns = rec.get("turns", [])
    return len(turns) == n_turns and all(t.get("ok") for t in turns)


# --------------------------------------------------------------------------
# Fase 1 — geração das conversas (browser + LLM as a user)
# --------------------------------------------------------------------------

def _origem_das_duplas(record: dict) -> dict:
    """QUEM NOMEOU PRIMEIRO cada dupla candidata/parente do tema de subordinação.

    A porta de entrada faz o MODELO enumerar; quando ele recusa, o agente cai
    na lista de reserva da equipe (decisão de 26/08). A análise precisa separar
    os dois casos.

    A verificação anterior não separava: marcava "reserva" sempre que um nome
    da lista aparecia numa mensagem do usuário — inclusive quando quem o tinha
    trazido era o assistente e o agente apenas o repetia, que é exatamente o
    que o instrumento manda fazer ("tome uma das duplas que o assistente
    nomeou"). Em agosto isso errou em 27 das 54 conversas marcadas como
    reserva, metade delas (correção 2 da seção 6 da nota).

    O critério certo é a ORDEM: dentro de um turno a mensagem do usuário vem
    antes da resposta do assistente, então basta varrer os turnos em sequência
    e ver de que lado o nome aparece primeiro.

    Valores de `duplas_origem`:
      reserva ...... o agente introduziu ao menos uma dupla da lista (foi a
                     contingência de recusa);
      assistente ... duplas da lista apareceram, todas trazidas pelo modelo;
      sem_reserva .. nenhuma dupla da lista apareceu (o modelo enumerou nomes
                     próprios, ou não enumerou ninguém — os dois casos que o
                     rótulo "assistente" antes confundia).

    Só se aplica quando o tema de subordinação entrou na conversa.
    """
    if (record.get("cobertura_temas") or {}).get(_TEMA_SUBORDINACAO, 0) <= 0:
        return {}
    turnos = record.get("turns") or []
    pelo_agente: list[str] = []
    pelo_assistente: list[str] = []
    for dupla in DUPLAS_RESERVA:
        nome = dupla.split(",")[0].strip().casefold()
        for t in turnos:
            if nome in (t.get("prompt") or "").casefold():
                pelo_agente.append(dupla)
                break
            if nome in (t.get("response") or "").casefold():
                pelo_assistente.append(dupla)
                break
    if pelo_agente:
        origem = "reserva"
    elif pelo_assistente:
        origem = "assistente"
    else:
        origem = "sem_reserva"
    return {
        "duplas_origem": origem,
        "duplas_reserva_usadas": pelo_agente,
        "duplas_reserva_citadas_pelo_assistente": pelo_assistente,
    }


def _run_one_conversation(page, store, driver, platform, mode, profile: Profile,
                          eixo_key: str, seed_data, model, n_turns: int,
                          turn_delay: float, plan_roteiros,
                          plan_corridas=None,
                          calendario: corridas.Calendario | None = None) -> dict:
    eixo = EIXOS[eixo_key]
    conv_id = f"{platform}_{profile.id}_{eixo_key}"
    # Roteiro pré-resolvido lido do PLANO DE COLETA — o mesmo para todas as
    # plataformas num dado perfil × eixo (mesmo estímulo → comparabilidade).
    inst = get_instrumento(eixo_key)
    roteiro = plan_roteiros.get(eixo_key, {}).get(profile.id, ())
    # Corrida atribuída (só o eixo voto): idem, vem do plano.
    corrida = (plan_corridas or {}).get(eixo_key, {}).get(profile.id)
    calendario = calendario or corridas.CALENDARIO_2026
    if roteiro:
        n_turns = len(roteiro)
    cobertura = instrument.resumo_cobertura(inst, roteiro) if inst else {}
    formato = instrument.resumo_formato(roteiro)
    alt_keys = [a.key for t in roteiro for a in t.alternativas]
    print(f"\n[conjoint] === {conv_id} | {profile.fatores()} | "
          f"{n_turns} turnos | cobertura={cobertura} ===")
    record = {
        "run_id": store.run_id,
        "conversation_id": conv_id,
        "platform": platform,
        "mode": mode,
        "model_user_agent": model,
        "profile": asdict(profile),
        "eixo": eixo_key,
        "tema": eixo.tema,
        "instrumento": inst.key if inst else None,
        # A corrida e o calendário são gravados COMO FORAM USADOS, não
        # recalculados na análise: é exatamente a distância entre o rótulo do
        # desenho e a pergunta efetiva que produziu o defeito de agosto
        # (a coluna dizia "candidatos para Presidente" e a conversa falava da
        # prefeitura de São Paulo).
        "corrida": corrida.to_dict() if corrida else None,
        "calendario": calendario.to_dict() if corrida else None,
        "cobertura_temas": cobertura,
        "formato_turnos": formato,
        "alternativas": alt_keys,
        "roteiro": [
            {
                "ordem": t.ordem,
                "n_perguntas": t.n_perguntas,
                "perguntas": [
                    {
                        "relato": q.relato.key if q.relato else None,
                        "pedido": q.pedido.key if q.pedido else None,
                        "fundida": q.fundida,
                        "exemplares": [
                            {"alternativa": k, "lista": r, "item": v}
                            for k, r, v in q.exemplares
                        ],
                    }
                    for q in t.perguntas
                ],
            }
            for t in roteiro
        ],
        "persona": persona_presentation(profile),
        "n_turns": n_turns,
        "started_at": _now_iso(),
        "finished_at": None,
        "conversation_url": None,
        "turns": [],
    }
    events.contexto(conversa=conv_id)
    events.emit(events.CONVERSA_INICIADA, perfil=profile.id, eixo=eixo_key,
                turnos_previstos=n_turns)
    try:
        driver.open_new_chat(page)
    except Exception as e:
        # NÃO roda os turnos se o chat isolado não abriu: em modo normal isso
        # vazaria a conversa ao histórico (memória entre conversas). Salva sem
        # turnos -> não conta como concluída -> retomada refaz.
        # Na prática este é o sintoma nº 1 de sessão deslogada ou de mudança na
        # UI: é alerta, não aviso — a plataforma não anda até alguém olhar.
        print(f"[conjoint] {conv_id}: ABORTADA — chat isolado não abriu: {e!r}")
        events.alerta(events.CHAT_ISOLADO_FALHOU, repr(e),
                      perfil=profile.id, eixo=eixo_key, modo=mode)
        record["error"] = repr(e)
        record["finished_at"] = _now_iso()
        store.save_conversation(record)
        return record

    ua = UserAgent(profile, eixo, seed_data, instrumento=inst,
                   roteiro=roteiro, n_turns=n_turns, model=model,
                   corrida=corrida, calendario=calendario)
    prev_response: str | None = None
    # Fontes citadas: a página acumula os turnos, então guardamos os links já
    # vistos e cada turno registra só os que apareceram nele. Ver
    # `capture.fontes_novas`. O conjunto tem de viver a conversa inteira.
    links_vistos: set[str] = set()
    capture.fontes_novas(page, links_vistos)  # zera o cromo da página vazia
    for ti in range(1, n_turns + 1):
        t0 = _now_iso()
        try:
            user_msg = ua.next_turn(prev_response)
        except Exception as e:
            print(f"[conjoint] [{conv_id}] ERRO ao gerar turno {ti}: {e!r}")
            events.aviso(events.TURNO_ERRO, f"user_agent: {e!r}",
                         turno=ti, de=n_turns, origem="user_agent")
            record["turns"].append({
                "turn": ti, "prompt": "", "response": "", "ok": False,
                "error": f"user_agent: {e!r}", "started_at": t0,
                "finished_at": _now_iso(), "response_chars": 0, "artifacts": {},
            })
            break
        ok, err, resp = True, None, ""
        try:
            resp = driver.submit(page, user_msg)
        except Exception as e:
            ok, err = False, repr(e)
            print(f"[conjoint] [{conv_id}] ERRO no turno {ti} (web): {e!r}")
        fontes = capture.fontes_novas(page, links_vistos)
        art = capture.snapshot(
            page, store.turn_artifacts_dir(conv_id, ti),
            "ok" if ok else "error",
        )
        print(f"[conjoint] [{conv_id}] turno {ti}/{n_turns}: "
              f"user={user_msg[:60]!r} -> resp={len(resp)} chars ok={ok} "
              f"fontes={len(fontes)}")
        if ok:
            events.emit(events.TURNO_OK, turno=ti, de=n_turns,
                        chars=len(resp), fontes=len(fontes))
        else:
            events.aviso(events.TURNO_ERRO, err or "", turno=ti, de=n_turns)
        record["turns"].append({
            "turn": ti,
            "prompt": user_msg,
            "response": resp,
            "ok": ok,
            "error": err,
            "started_at": t0,
            "finished_at": _now_iso(),
            "response_chars": len(resp),
            "fontes": fontes,
            "n_fontes": len(fontes),
            "artifacts": art,
        })
        record["conversation_url"] = driver.conversation_url(page)
        record["finished_at"] = _now_iso()
        if not ok:
            break
        prev_response = resp
        # Pausa entre turnos: base + tempo de LEITURA proporcional ao tamanho
        # da resposta + jitter. Antes era a constante `turn_delay`, que somada
        # à digitação de cadência fixa formava a assinatura de automação que o
        # ChatGPT sinalizou em 28/08/2026 (respondíamos em 5 s fixos uma
        # resposta de 2 mil caracteres, que um humano levaria ~100 s só para
        # ler). Teto por env `LLMBIAS_LEITURA_MAX_S`.
        leitura_max = float(os.environ.get("LLMBIAS_LEITURA_MAX_S", "30"))
        leitura = min(len(resp) / 140.0, leitura_max) if resp else 0.0
        time.sleep(turn_delay + leitura + random.uniform(0, turn_delay))

    record.update(_origem_das_duplas(record))
    store.save_conversation(record)
    turnos = record.get("turns", [])
    completa = len(turnos) == n_turns and all(t.get("ok") for t in turnos)
    events.emit(
        events.CONVERSA_CONCLUIDA if completa else events.CONVERSA_ABORTADA,
        nivel=events.INFO if completa else events.AVISO,
        perfil=profile.id, eixo=eixo_key,
        turnos_ok=sum(1 for t in turnos if t.get("ok")), de=n_turns,
    )
    events.contexto(conversa=None)
    return record


def generate_conversations(store: RunStore, profiles, platforms, eixos,
                           seed_data, model, n_turns, turn_delay, conv_delay,
                           plan_roteiros, limit=None,
                           per_platform_limit=None, plan_corridas=None,
                           calendario: corridas.Calendario | None = None) -> None:
    # O tamanho da conversa é por EIXO: a especificação fixa sete turnos no
    # ranqueamento e dez nos outros dois. `--n-turns`, quando passado, sobrepõe
    # todos e serve para smoke test.
    def turnos_de(e: str, pid: str) -> int:
        rot = plan_roteiros.get(e, {}).get(pid, ())
        if rot:
            return len(rot)
        inst = get_instrumento(e)
        return n_turns or (inst.n_turns if inst else 10)

    planned = [(pl, p, e)
               for pl in platforms for p in profiles for e in eixos]
    todo = [(pl, p, e) for (pl, p, e) in planned
            if not _conv_done(store, f"{pl}_{p.id}_{e}", turnos_de(e, p.id))]
    if per_platform_limit:
        cnt: dict[str, int] = {}
        capped = []
        for (pl, p, e) in todo:
            if cnt.get(pl, 0) < per_platform_limit:
                capped.append((pl, p, e))
                cnt[pl] = cnt.get(pl, 0) + 1
        todo = capped
    if limit:
        todo = todo[:limit]
    print(f"[conjoint] conversas planejadas: {len(planned)} | "
          f"a fazer agora: {len(todo)} | plataformas: {platforms} | "
          f"turnos/conversa: "
          f"{ {e: turnos_de(e, profiles[0].id) for e in eixos} }")
    if not todo:
        print("[conjoint] nada a gerar (todas já concluídas).")
        return

    # O painel lê `data/<run>/events.jsonl`. Um container por plataforma
    # significa um processo por plataforma, então o campo fixo `plataforma` já
    # identifica a origem de cada linha.
    events.configurar(store.dir,
                      plataforma=platforms[0] if len(platforms) == 1 else None)
    events.emit(events.COLETA_INICIADA, plataformas=list(platforms),
                eixos=list(eixos), a_fazer=len(todo), planejadas=len(planned))

    drivers = {pl: REGISTRY[PLATFORM_DRIVERS[pl][0]]() for pl in platforms}
    modes = {pl: PLATFORM_DRIVERS[pl][1] for pl in platforms}

    with sync_playwright() as pw:
        b, ctx = browser.connect(pw)
        # WhatsApp/Meta AI precisa da aba EXISTENTE (logada, com o Meta AI
        # aberto): não dá para criar aba nova nem fechá-la (perderia a sessão/o
        # chat ativo). Se whatsapp está nas plataformas e há uma aba dele, REUSA.
        wa_page = next((p for p in ctx.pages
                        if "web.whatsapp.com" in (p.url or "")), None)
        if "whatsapp_metaai" in platforms and wa_page is not None:
            page = wa_page
        else:
            # Cria a PRÓPRIA aba PRIMEIRO (o contexto nunca fica sem página);
            # fechar a última página do contexto CDP o mata (TargetClosedError).
            page = ctx.new_page()
        # Fecha abas remanescentes (de execuções que caíram) para não acumular
        # alvos e TRAVAR o connect_over_cdp seguinte — mas PRESERVA a aba do
        # WhatsApp e a página de trabalho. Seguro porque a coleta roda SERIAL.
        for _pg in list(ctx.pages):
            if _pg is page:
                continue
            if "web.whatsapp.com" in (_pg.url or ""):
                continue  # nunca fecha a sessão do WhatsApp
            try:
                _pg.close()
            except Exception:
                pass
        page.set_default_timeout(60000)
        try:
            for i, (pl, p, e) in enumerate(todo):
                if i:
                    time.sleep(conv_delay)
                _run_one_conversation(page, store, drivers[pl], pl, modes[pl],
                                      p, e, seed_data, model,
                                      turnos_de(e, p.id), turn_delay,
                                      plan_roteiros, plan_corridas,
                                      calendario)
        finally:
            # Não fecha a aba do WhatsApp (preserva a sessão para retomadas).
            if page is not wa_page:
                try:
                    page.close()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Fase "plan" — planilha completa da conjoint p/ validação (sem browser)
# --------------------------------------------------------------------------

def export_plano_completo(store: RunStore, profiles, platforms, eixos,
                          plan_roteiros, plan_temas, seed_data, model,
                          n_turns: int | None,
                          com_primeira_mensagem: bool = True,
                          plan_corridas=None,
                          calendario: corridas.Calendario | None = None) -> Path:
    """Exporta a AMOSTRA COMPLETA para inspeção antes de rodar: uma linha por
    (perfil × eixo), com os fatores do perfil, os temas sorteados, o roteiro
    resolvido (fichas de todos os turnos), o **system prompt** do LLM-usuário e
    um **exemplo de primeira mensagem** (gerado pela API — ilustrativo; na
    coleta o turno 1 é regerado).

    O roteiro, o system prompt e a 1ª mensagem NÃO dependem da plataforma, então
    a coluna `plataformas` só lista onde aquela conversa será replicada.
    Escreve `plano_coleta_completo.xlsx`.
    """
    rows = []
    plats = "+".join(platforms)
    for eixo_key in eixos:
        eixo = EIXOS[eixo_key]
        inst = get_instrumento(eixo_key)
        for p in profiles:
            flags = plan_temas.get(eixo_key, {}).get(p.id, {}) or {}
            roteiro = plan_roteiros.get(eixo_key, {}).get(p.id, ())
            corrida = (plan_corridas or {}).get(eixo_key, {}).get(p.id)
            n_t = len(roteiro) or (n_turns or (inst.n_turns if inst else 10))
            ua = UserAgent(p, eixo, seed_data, instrumento=inst,
                           roteiro=roteiro, n_turns=n_t, model=model,
                           corrida=corrida,
                           calendario=calendario or corridas.CALENDARIO_2026)
            # A 1ª mensagem custa uma chamada de API por linha; para a planilha
            # de pré-registro ela é ilustrativa, então é opcional.
            if com_primeira_mensagem:
                try:
                    primeira = ua.next_turn(None)
                except Exception as e:  # noqa: BLE001
                    primeira = f"(erro ao gerar exemplo: {e!r})"
            else:
                primeira = ""
            fichas = "\n\n".join(
                instrument.ficha(inst, t) for t in roteiro
            ) if (inst and roteiro) else ""
            cob = instrument.resumo_cobertura(inst, roteiro) if inst else {}
            pf = p.fatores()
            rows.append({
                "perfil_id": p.id,
                "eixo": eixo_key,
                "plataformas": plats,
                **pf,
                # a corrida atribuída entra ao lado dos fatores, mas é de outra
                # natureza: ela varia o ESTÍMULO, não a pessoa que pergunta.
                **({"corrida_cargo": corrida.cargo,
                    "corrida_uf": corrida.uf or "",
                    "corrida_descricao": corrida.descricao} if corrida else {}),
                "n_turnos": n_t,
                "n_temas": sum(1 for v in flags.values() if v),
                "temas_incluidos": "+".join(c for c, v in flags.items() if v),
                **{f"tema_{c}": int(bool(v)) for c, v in flags.items()},
                **{f"aparicoes_{c}": n for c, n in cob.items()},
                "primeira_mensagem_exemplo": primeira,
                "roteiro_fichas": fichas,
                "system_prompt": ua.system,
            })
            print(f"[conjoint] plano: {p.id} × {eixo_key} "
                  f"({sum(1 for v in flags.values() if v)} temas, {n_t} turnos)"
                  + (f" — 1ª msg gerada ({len(primeira)} chars)"
                     if com_primeira_mensagem else ""))
    df = pd.DataFrame(rows)
    out = store.dir / "plano_coleta_completo.xlsx"
    try:
        # UMA ABA POR EIXO. Empilhar os três numa aba só torna as colunas de
        # tema ambíguas: os códigos T1..Tn são LOCAIS ao eixo (T1 é
        # "ordenamento" no voto, "descrédito da atuação política" no gênero e
        # "informação inverídica" na integridade), e o eixo sem temas (voto)
        # deixa a faixa inteira em branco.
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            for eixo_key in eixos:
                sub = df[df["eixo"] == eixo_key].copy()
                if sub.empty:
                    continue
                # fora as colunas que não se aplicam a este eixo — inclusive
                # as que ficam com string vazia (o voto não tem temas; a 1ª
                # mensagem some quando não é gerada), que o dropna não pega.
                sub = sub.dropna(axis=1, how="all")
                vazias = [c for c in sub.columns
                          if sub[c].astype(str).str.strip().eq("").all()]
                sub = sub.drop(columns=["eixo"] + vazias)
                sub.to_excel(xw, sheet_name=eixo_key[:31], index=False)

            # Aba LONGA, para análise: uma linha por perfil × eixo × tema, com
            # o rótulo do tipo junto do código (o código sozinho não é legível
            # e, pior, se repete entre eixos significando outra coisa).
            longo = []
            for r in rows:
                eixo_key = r["eixo"]
                grid = RUBRICS.get(eixo_key)
                if grid is None:
                    continue
                rotulos = {t.codigo: t.tipo for t in grid.tipos}
                for cod, rot in rotulos.items():
                    if f"tema_{cod}" not in r:
                        continue
                    longo.append({
                        "perfil_id": r["perfil_id"], "eixo": eixo_key,
                        "tipo": cod, "rotulo_do_tipo": rot,
                        "incluido": r.get(f"tema_{cod}"),
                        "aparicoes": r.get(f"aparicoes_{cod}"),
                        "n_turnos": r.get("n_turnos"),
                        **{f: r.get(f) for f in (
                            "politica", "genero", "idade", "escolaridade",
                            "estilo_conversa", "estilo_escrita")},
                    })
            if longo:
                pd.DataFrame(longo).to_excel(xw, sheet_name="temas_longo",
                                             index=False)
    except Exception as e:  # fallback CSV se openpyxl faltar
        print(f"[conjoint] (xlsx pulado: {e!r}; salvando CSV)")
        out = store.dir / "plano_coleta_completo.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig", sep=";")
    print(f"[conjoint] planilha completa: {len(rows)} linhas "
          f"(perfil × eixo) -> {out}")
    return out


# --------------------------------------------------------------------------
# Fase 2 — julgamento (API, extração por turno)
# --------------------------------------------------------------------------

def judge_conversations(store: RunStore, rubrics: dict[str, RubricGrid],
                        model, juizes=None, modo: str = "turno") -> None:
    conv_dir = store.dir / "conversations"
    anot_dir = store.dir / "annotations"
    anot_dir.mkdir(parents=True, exist_ok=True)
    convs = sorted(conv_dir.glob("*.json"))
    print(f"[conjoint] julgando {len(convs)} conversas...")
    for cp in convs:
        rec = json.loads(cp.read_text(encoding="utf-8"))
        conv_id = rec["conversation_id"]
        out_path = anot_dir / f"{conv_id}.json"
        if out_path.exists():
            continue
        if rec["eixo"] not in rubrics:
            continue
        if not (rec.get("turns") and any(t.get("ok") for t in rec["turns"])):
            print(f"[conjoint] pulando {conv_id} (sem turnos válidos)")
            continue
        rubric = rubrics[rec["eixo"]]
        try:
            if juizes:
                anot = annotate_panel(rec, rubric, juizes, model=model,
                                      modo=modo)
            elif modo == "conversa":
                anot = annotate_conversa(rec, rubric, model=model)
            else:
                anot = annotate(rec, rubric, model=model)
        except Exception as e:
            print(f"[conjoint] ERRO ao julgar {conv_id}: {e!r}")
            continue
        anot["conversation_id"] = conv_id
        _save_json(out_path, anot)
        if juizes:
            print(f"[conjoint] {conv_id}: {anot['n_juizes']} juízes | "
                  f"votos por tipo {anot['votos_por_tipo']} | "
                  f"unanimidade {anot['concordancia_unanime_tipos']:.0%}")
        else:
            print(f"[conjoint] {conv_id}: {anot['achados_violacao']} achados de "
                  f"violação em {anot['turnos_com_violacao']}/"
                  f"{anot['n_turnos_avaliados']} turnos")


# --------------------------------------------------------------------------
# Fase 3 — base final
# --------------------------------------------------------------------------

def build_dataset(store: RunStore, rubrics: dict[str, RubricGrid]) -> Path:
    conv_dir = store.dir / "conversations"
    anot_dir = store.dir / "annotations"
    rows = []
    for cp in sorted(conv_dir.glob("*.json")):
        rec = json.loads(cp.read_text(encoding="utf-8"))
        conv_id = rec["conversation_id"]
        ap = anot_dir / f"{conv_id}.json"
        anot = json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else None
        prof = rec["profile"]
        # Cada conversa cobre o SUBCONJUNTO de temas sorteado para o seu perfil
        # (tema = variável independente binária). Do lado do tema saem três
        # colunas: `tema_Tx` (0/1, a variável MANIPULADA — entrou no sorteio?),
        # `aparicoes_Tx` (quantas perguntas o roteiro dedicou a ele: a dose) e
        # `violou_Tx` (0/1, a variável dependente). `cobertura_temas` é medida
        # sobre o instrumento COMPLETO, então aparições > 0 identifica
        # exatamente os temas incluídos. Os códigos T1..Tn são LOCAIS ao eixo.
        cobertura = rec.get("cobertura_temas") or {}
        formato = rec.get("formato_turnos") or {}
        por_tipo = (anot or {}).get("por_tipo") or {}
        tema_cols: dict[str, object] = {}
        for cod, n_ap in cobertura.items():
            tema_cols[f"tema_{cod}"] = int(int(n_ap) > 0)
            tema_cols[f"aparicoes_{cod}"] = int(n_ap)
            tema_cols[f"violou_{cod}"] = (
                int(por_tipo.get(cod, 0) > 0) if anot else None
            )
        # Bloco de resistência em coluna (uma por código da rubrica): quantos
        # turnos da conversa trouxeram R1, R2 e R3.
        cor = rec.get("corrida") or {}
        corrida_cols = {
            "corrida_cargo": cor.get("cargo") or "",
            "corrida_uf": cor.get("uf") or "",
            "corrida": (f"{cor['cargo']}_{cor['uf']}" if cor.get("uf")
                        else (cor.get("cargo") or "")),
            "corrida_descricao": cor.get("descricao") or "",
        }
        resist = (anot or {}).get("resistencia") or {}
        resist_cols: dict[str, object] = {
            f"resistencia_{r.codigo}": (int(resist.get(r.codigo, 0))
                                        if anot else None)
            for r in RESISTENCIAS
        }
        row = {
            "id": conv_id,
            "run_id": rec["run_id"],
            "platform": rec["platform"],
            "mode": rec["mode"],
            "eixo": rec["eixo"],
            "tema": rec["tema"],
            "instrumento": rec.get("instrumento"),
            "alternativas": "+".join(rec.get("alternativas") or []),
            # formato dos turnos (§6 da especificação pede o registro)
            "turnos": formato.get("turnos"),
            "turnos_uma_pergunta": formato.get("turnos_uma_pergunta"),
            "turnos_duas_perguntas": formato.get("turnos_duas_perguntas"),
            "perguntas_substantivas": formato.get("perguntas_substantivas"),
            # fatores do perfil (uma coluna cada)
            "politica": prof["politica"],
            "genero": prof["genero"],
            "idade": prof["idade"],
            "escolaridade": prof["escolaridade"],
            "estilo_conversa": prof["estilo_conversa"],
            "estilo_escrita": prof["estilo_escrita"],
            "perfil_id": prof["id"],
            # Corrida atribuída (eixo voto; vazio nos outros). Lida do REGISTRO
            # da conversa, não recalculada: a base tem de dizer o que foi
            # perguntado àquela plataforma naquele dia, mesmo que a tabela de
            # UFs ou o desenho mudem depois.
            **corrida_cols,
            # variável dependente binária no nível da conversa
            "violou": (int(anot["achados_violacao"] > 0) if anot else None),
            # exposição por tema (aparicoes_Tx) e violação por tema (violou_Tx)
            **tema_cols,
            # agregados descritivos da anotação (sem escore somado)
            "n_turnos_avaliados": anot["n_turnos_avaliados"] if anot else None,
            "n_turnos_com_erro": (anot or {}).get("n_turnos_com_erro"),
            "achados_total": anot["achados_total"] if anot else None,
            "achados_violacao": anot["achados_violacao"] if anot else None,
            "turnos_com_violacao": anot["turnos_com_violacao"] if anot else None,
            "n_turns_ok": sum(1 for t in rec["turns"] if t.get("ok")),
            # De onde vieram as duplas do tema de subordinação: do modelo (o
            # mecanismo principal) ou da lista de reserva, na contingência de
            # recusa. A análise precisa separar os dois casos — e as duas
            # colunas de nomes têm de ficar separadas também, senão a correção
            # de quem falou primeiro não chega a quem lê a base.
            "duplas_origem": rec.get("duplas_origem"),
            "duplas_reserva_usadas": "+".join(
                rec.get("duplas_reserva_usadas") or []
            ),
            "duplas_reserva_citadas_pelo_assistente": "+".join(
                rec.get("duplas_reserva_citadas_pelo_assistente") or []
            ),
            # fontes citadas (pedido da equipe, ago/2026): o total da conversa
            # e a lista por turno, que é a unidade em que foram pedidas.
            "n_fontes": sum(len(t.get("fontes") or []) for t in rec["turns"]),
            "fontes_por_turno": json.dumps(
                {t["turn"]: (t.get("fontes") or [])
                 for t in rec["turns"] if t.get("fontes")},
                ensure_ascii=False,
            ),
            # resistência em coluna, não só no JSON: os juízes já produzem
            # R1–R3 por turno, mas sem coluna a concordância em resistência
            # nunca chegou a ser auditada.
            **resist_cols,
            # saúde do painel: sem isto, "2 de 2" e "2 de 3" viram o mesmo
            # número na análise (ver 6.1 em judge.py).
            "n_juizes": (anot or {}).get("n_juizes"),
            "painel_completo": (
                int(bool(anot.get("painel_completo")))
                if anot and "painel_completo" in anot else None
            ),
            "juizes_com_falha": "+".join(
                (anot or {}).get("juizes_com_falha") or []
            ),
            # dicts completos (por tipo / voz / resistência + achados + conversa)
            "por_tipo": json.dumps(anot["por_tipo"] if anot else {},
                                   ensure_ascii=False),
            "por_voz": json.dumps(anot["por_voz"] if anot else {},
                                  ensure_ascii=False),
            "resistencia": json.dumps(anot["resistencia"] if anot else {},
                                      ensure_ascii=False),
            "achados": json.dumps(anot["turnos"] if anot else [],
                                  ensure_ascii=False),
            "roteiro": json.dumps(rec.get("roteiro") or [], ensure_ascii=False),
            "conversa": json.dumps(rec["turns"], ensure_ascii=False),
            "persona": rec["persona"],
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    jsonl = store.dir / "dataset.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    csv = store.dir / "dataset.csv"
    df.to_csv(csv, index=False, encoding="utf-8-sig", sep=";")
    try:
        df.to_parquet(store.dir / "dataset.parquet", index=False)
    except Exception as e:
        print(f"[conjoint] (parquet pulado: {e!r})")
    print(f"[conjoint] base final: {len(rows)} linhas -> {csv}")
    if not df.empty and df["achados_violacao"].notna().any():
        print("[conjoint] achados de violação (média) por eixo × plataforma:")
        piv = (df.dropna(subset=["achados_violacao"])
                 .groupby(["eixo", "platform"])["achados_violacao"]
                 .mean().round(2))
        print(piv.to_string())
    return csv


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def run(n_profiles: int = 3, seed: int = 2026,
        platforms: list[str] | None = None, eixos: list[str] | None = None,
        run_id: str | None = None, model: str | None = None,
        n_turns: int | None = None, turn_delay: float = 3.0,
        conv_delay: float = 8.0, limit: int | None = None,
        per_platform_limit: int | None = None,
        tema_prob: float = DEFAULT_TEMA_PROB, min_temas: int = DEFAULT_MIN_TEMAS,
        juizes_keys: list[str] | None = None, judge_mode: str = "turno",
        com_primeira_mensagem: bool = True,
        desenho_corrida: str = corridas.DESENHO_PADRAO,
        balanceamento_corrida: str = corridas.BALANCEAMENTO_PADRAO,
        calendario: corridas.Calendario | None = None,
        fatia: str | None = None,
        phase: str = "all") -> int:
    platforms = platforms or list(DEFAULT_PLATFORMS)
    eixos = eixos or list(DEFAULT_EIXOS)
    model = model or llm.DEFAULT_MODEL
    # Painel de juízes: vazio = juiz único (Gemini de sempre).
    juizes = []
    if juizes_keys:
        from . import judges as _judges
        keys = None if juizes_keys == ["todos"] else juizes_keys
        juizes = _judges.painel(keys)
        print(f"[conjoint] painel de juízes: "
              f"{[(j.key, j.model) for j in juizes]}")

    unknown_pl = [p for p in platforms if p not in PLATFORM_DRIVERS]
    if unknown_pl:
        print(f"[conjoint] plataformas desconhecidas: {unknown_pl}. "
              f"Disponíveis: {list(PLATFORM_DRIVERS)}")
        return 1
    unknown_ex = [e for e in eixos if e not in RUBRICS]
    if unknown_ex:
        print(f"[conjoint] eixos sem rubrica curada 4.0: {unknown_ex}. "
              f"Disponíveis: {list(RUBRICS)}")
        return 1

    store = RunStore(run_id)
    print(f"[conjoint] run_id={store.run_id} | plataformas={platforms} | "
          f"eixos={eixos} | perfis={n_profiles} | "
          f"turnos={n_turns or 'do eixo'} | modelo juiz/usuário={model}")
    seed_data = load_seed()
    profiles = _load_or_sample_profiles(store, n_profiles, seed)
    profiles_todos = profiles
    if fatia:
        profiles = _fatiar_perfis(profiles, fatia)
    rubrics = _snapshot_rubrics(store, eixos)
    # Plano de coleta (roteiro por eixo × perfil) montado ANTES de rodar,
    # inspecionável e retomável; o mesmo estímulo para todas as plataformas.
    calendario = calendario or corridas.CALENDARIO_2026
    plan_roteiros, plan_temas, plan_corridas = _load_or_build_plan(
        store, profiles, platforms, eixos, seed, n_turns,
        tema_prob=tema_prob, min_temas=min_temas,
        desenho_corrida=desenho_corrida, calendario=calendario,
        balanceamento=balanceamento_corrida,
    )

    if phase == "plan":
        export_plano_completo(store, profiles, platforms, eixos, plan_roteiros,
                              plan_temas, seed_data, model, n_turns,
                              com_primeira_mensagem=com_primeira_mensagem,
                              plan_corridas=plan_corridas,
                              calendario=calendario)
        print(f"\n[conjoint] Plano exportado. Rode o pré-teste com: "
              f"conjoint --run-id {store.run_id} --limit <K> "
              f"[--platforms ...]")
        return 0

    if phase in ("all", "generate"):
        generate_conversations(store, profiles, platforms, eixos, seed_data,
                               model, n_turns, turn_delay, conv_delay,
                               plan_roteiros, limit=limit,
                               per_platform_limit=per_platform_limit,
                               plan_corridas=plan_corridas,
                               calendario=calendario)
    if phase in ("all", "judge"):
        judge_conversations(store, rubrics, model, juizes=juizes,
                            modo=judge_mode)
        build_dataset(store, rubrics)

    print(f"\n[conjoint] Concluído. Dados em: {store.dir}")
    return 0
