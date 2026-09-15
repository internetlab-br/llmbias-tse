"""llmbias-tse — POC de coleta automatizada de respostas de ferramentas de
IA generativa via automação de navegador (Patchright + Chrome real/CDP).

Subcomandos:
    launch      abre o Chrome na porta de debug com perfil persistente (login manual)
    run         conecta no Chrome logado, manda prompts e salva as trocas
    experiment  roda o experimento ESEB (conversas curtas vs longas por persona)
    tools       lista as ferramentas disponíveis
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="llmbias-tse", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("launch", help="abre o Chrome com perfil persistente (login)")

    p_run = sub.add_parser("run", help="coleta: manda prompts e salva as trocas")
    p_run.add_argument(
        "--tools", nargs="+", default=None,
        help="ferramentas (default: chatgpt gemini claude metaai)",
    )
    p_run.add_argument("--n", type=int, default=2, help="prompts por ferramenta")
    p_run.add_argument("--seed", type=int, default=None, help="semente do sorteio")
    p_run.add_argument(
        "--session", choices=["logged_in", "anon", "both"], default="logged_in",
        help="sessão: logado, deslogado (anônimo) ou ambos",
    )

    p_exp = sub.add_parser(
        "experiment", help="experimento ESEB: conversas curtas vs longas",
    )
    p_exp.add_argument("--tool", default="chatgpt", help="ferramenta (default: chatgpt)")
    p_exp.add_argument(
        "--personas", nargs="+", default=None,
        help="ids de persona (default: selected_personas do long_conversations.json)",
    )
    p_exp.add_argument(
        "--dimensions", nargs="+", default=None,
        help="dimensões (default: todas as 6 do arquivo)",
    )
    p_exp.add_argument(
        "--kinds", nargs="+", default=None, choices=["short", "long"],
        help="tipos de conversa (default: short long)",
    )
    p_exp.add_argument(
        "--session", choices=["logged_in", "anon"], default="logged_in",
        help="sessão (default: logged_in)",
    )
    p_exp.add_argument(
        "--run-id", default=None,
        help="reutiliza/retoma um run existente (pula conversas já concluídas)",
    )
    p_exp.add_argument(
        "--turn-delay", type=float, default=3.0,
        help="pausa entre turnos, em s (evita rate limit; default 3)",
    )
    p_exp.add_argument(
        "--conv-delay", type=float, default=8.0,
        help="pausa entre conversas, em s (evita rate limit; default 8)",
    )

    p_cj = sub.add_parser(
        "conjoint",
        help="conjoint analysis multi-plataforma (LLM-as-user + LLM-as-judge 4.0)",
    )
    p_cj.add_argument("--n-profiles", type=int, default=3,
                      help="nº de perfis sorteados (default 3, pré-teste)")
    p_cj.add_argument("--seed", type=int, default=2026,
                      help="semente do sorteio de perfis (default 2026)")
    p_cj.add_argument("--platforms", nargs="+", default=None,
                      help="plataformas (default: gemini chatgpt claude "
                           "deepseek grok)")
    p_cj.add_argument("--eixos", nargs="+", default=None,
                      help="eixos com rubrica v5 (default: voto genero "
                           "integridade)")
    p_cj.add_argument("--n-turns", type=int, default=None,
                      help="turnos por conversa; sem isto cada eixo usa o "
                           "tamanho que a especificação fixa (7 no voto, 10 "
                           "nos demais)")
    p_cj.add_argument("--run-id", default=None,
                      help="reutiliza/retoma um run existente")
    p_cj.add_argument("--model", default=None,
                      help="modelo Gemini da API (default: gemini-3.5-flash)")
    p_cj.add_argument("--turn-delay", type=float, default=3.0,
                      help="pausa entre turnos, em s (default 3)")
    p_cj.add_argument("--conv-delay", type=float, default=8.0,
                      help="pausa entre conversas, em s (default 8)")
    p_cj.add_argument("--limit", type=int, default=None,
                      help="limita o nº TOTAL de conversas geradas (smoke test)")
    p_cj.add_argument("--per-platform-limit", type=int, default=None,
                      help="limita o nº de conversas POR PLATAFORMA (ex.: 2 no "
                           "pré-teste; útil também na coleta distribuída)")
    p_cj.add_argument("--tema-prob", type=float, default=0.5,
                      help="prob. de incluir cada tema (variável independente "
                           "binária; default 0.5)")
    p_cj.add_argument("--min-temas", type=int, default=1,
                      help="piso de temas por conversa (default 1: sem tema não "
                           "há pergunta a fazer nos eixos com instrumento)")
    p_cj.add_argument("--desenho-corrida", default=None,
                      help="qual corrida o eixo voto sorteia por conversa: "
                           "so_presidente (default), majoritarias "
                           "(presidente+governador+senador) ou cinco_cargos. "
                           "Os que exigem UF precisam da tabela de UFs "
                           "preenchida em corridas.py (decisão do InternetLab)")
    p_cj.add_argument("--juizes", nargs="+", default=None,
                      help="painel de juízes: 'todos' (gemini opus gpt) ou uma "
                           "lista (ex.: gemini opus). Sem a flag, roda só o "
                           "juiz Gemini de sempre.")
    p_cj.add_argument("--judge-mode", choices=["turno", "conversa"],
                      default="turno",
                      help="juiz por turno (default, reenvia a conversa a cada "
                           "turno) ou 'conversa' (1 chamada por conversa, "
                           "achados marcados por turno; ~6x mais barato)")
    p_cj.add_argument("--sem-primeira-mensagem", action="store_true",
                      help="na fase 'plan', não gera o exemplo de 1ª mensagem "
                           "(evita uma chamada de API por linha da planilha)")
    p_cj.add_argument("--phase", choices=["all", "generate", "judge", "plan"],
                      default="all",
                      help="fase: tudo; só gerar; só julgar+base; ou 'plan' "
                           "(exporta a planilha completa p/ validação, sem "
                           "browser)")

    sub.add_parser("tools", help="lista as ferramentas disponíveis")

    args = parser.parse_args()

    if args.cmd == "launch":
        from .browser import main_launch
        sys.exit(main_launch())
    elif args.cmd == "run":
        from .poc import run
        sys.exit(run(args.tools, args.n, args.seed, args.session))
    elif args.cmd == "experiment":
        from .experiment import run as run_experiment
        sys.exit(run_experiment(
            tool=args.tool, personas=args.personas,
            dimensions=args.dimensions, kinds=args.kinds, session=args.session,
            run_id=args.run_id, turn_delay=args.turn_delay,
            conv_delay=args.conv_delay,
        ))
    elif args.cmd == "conjoint":
        from .conjoint_experiment import run as run_conjoint
        sys.exit(run_conjoint(
            n_profiles=args.n_profiles, seed=args.seed,
            platforms=args.platforms, eixos=args.eixos, n_turns=args.n_turns,
            run_id=args.run_id, model=args.model, turn_delay=args.turn_delay,
            conv_delay=args.conv_delay, limit=args.limit,
            per_platform_limit=args.per_platform_limit,
            tema_prob=args.tema_prob, min_temas=args.min_temas,
            juizes_keys=args.juizes, judge_mode=args.judge_mode,
            com_primeira_mensagem=not args.sem_primeira_mensagem,
            **({"desenho_corrida": args.desenho_corrida}
               if args.desenho_corrida else {}),
            phase=args.phase,
        ))
    elif args.cmd == "tools":
        from .drivers import REGISTRY
        for name in REGISTRY:
            print(name)
        sys.exit(0)
