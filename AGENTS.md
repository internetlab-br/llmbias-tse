# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this project is

`llmbias-tse` is a research project by **LabDados** (FGV Direito SP) in partnership with **InternetLab** to build an **open, reproducible methodology for automated auditing of generative-AI models in the Brazilian electoral context** (TSE = Tribunal Superior Eleitoral), targeting the 2026 elections.

LabDados owns the **technical automation side**: a harness that programmatically queries widely-used AI tools in Brazil (chat assistants, AI search/browser modes, MetaAI on WhatsApp/Instagram, etc.) with a protocol of test prompts, then collects and processes the responses across four planned data-collection rounds. The analysis looks for: disinformation about candidacies and electoral integrity; reproduction of gender/race-based political violence and deepfakes; conformity with TSE rules (notably the ban on vote recommendation and candidate ranking); and alignment with democratic-integrity principles.

Key constraints that shape the design: the code and methodology must be **publicly released under a free license** for reproducibility and public scrutiny, and the system is meant for **longitudinal, at-scale monitoring** (test automation, rigorous handling of large data volumes, and extraction from proprietary AI models). Methodology decisions are made jointly with InternetLab and are due by **end of July 2026** — much is not yet fixed.

Full proposal: `docs/Projeto InternetLab-LabDados.docx` (internal partnership doc — gitignored, not in the public repo). Tracking issue: https://github.com/lab-dados/adm/issues/61

## Current state

A first **POC of the data-collection harness** exists: browser automation that drives logged-in AI web UIs with personal accounts, captures the responses, and stores them for downstream bias evaluation. The real prompt protocol and evaluation criteria are still TBD (jointly with InternetLab, by end of July 2026); the POC is the e2e skeleton.

## Architecture (POC harness)

Browser automation mirrors the sibling project `monitor-italia`: a **real Google Chrome** launched on a CDP debug port (`--remote-debugging-port`) with a **persistent profile** (`--user-data-dir`), so the user logs in **once** and the script connects to the already-authenticated session via **Patchright** (stealth Playwright fork). Two phases, decoupled on purpose so you can iterate on the collection script without relaunching/re-logging the browser:

1. `launch` — opens Chrome on the port; user logs into each tool manually (incl. WhatsApp QR). Profile persists cookies under `tmp/profile`.
2. `run` — connects over CDP, sends prompts, captures responses, writes `data/<run_id>/`.

Module map under `src/llmbias_tse/`:
- `__init__.py` — `main()` argparse CLI dispatcher (`launch` / `run` / `tools`); console script `llmbias-tse`.
- `browser.py` — Chrome launch on CDP port + `connect()` (profile/port via `LLMBIAS_PROFILE`/`LLMBIAS_CDP_PORT`/`CHROME_PATH` env).
- `drivers.py` — one driver class per tool in `REGISTRY` (`chatgpt`, `gemini`, `Codex`, `grok`, `deepseek`, `metaai`, `whatsapp_metaai`). Each holds the **per-UI selectors** (composer + assistant-message containers). **These selectors drift and are the thing to fix when capture breaks.** `BaseDriver.send()` = open new chat → type → Enter → wait for response; WhatsApp overrides it. NB (jun/2026): Codex's response wrapper is now `div[data-is-streaming]` (`true`→`false` = busy signal) with text in `div.font-Codex-response`; DeepSeek's composer is a plain `textarea` (the `#chat-input` id is gone) with response in `.ds-markdown`.
- `capture.py` — UI-agnostic helpers. End-of-response detection (`wait_until_idle`) combines two signals (first wins): the "generating" indicator (stop-button) disappearing, **or** the response text going stable. Text is read by `last_text()` via **`text_content`, not `inner_text`** (see Operational gotchas). `wait_response_started()` detects a new response by count-OR-busy-OR-text-changed. Plus `snapshot()` (HTML+PNG).
- `storage.py` — `RunStore` + `Exchange` dataclass. Append-only `exchanges.jsonl` (one record per prompt→response/turn, UTF-8) + raw artifacts. For the experiment, also writes per-conversation JSON under `conversations/` (the **LLM-as-a-judge input** — persona + dimension + ordered turns). Keep records self-describing.
- `experiment.py` — ESEB guardrails experiment runner. For each persona (`Sxx`) × dimension × {short, long}, drives ChatGPT and saves the conversation. **short** = 1 turn (the dimension prompt from the file); **long** = 5 turns (that prompt + 4 authored follow-ups that *induce* toward the persona's side, to test guardrails/sycophancy). Resumable (`--run-id` skips already-completed conversations) and paced (`--turn-delay`/`--conv-delay`).
- `prompts.py` — placeholder "light electoral" prompt list + `pick()`. Will be replaced by the real protocol.

Experiment input data (under `data/`, versioned): `scripts_conversa_eseb_guardrails.json` (18 personas × 6 dimensions) and `long_conversations.json` (authored turns 2–5 of the long conversations).

When a driver stops capturing, read the saved snapshot HTML in `data/<run>/artifacts/<tool>/<NN>/` to find the new selector and patch `drivers.py`.

## Tooling and commands

Managed with **`uv`** (build backend `uv_build`), Python **>=3.12**. Deps: `patchright`, `python-dotenv`.

```bash
uv sync                                      # create/update .venv
uv run llmbias-tse launch                    # phase 1: open Chrome, log in manually
uv run llmbias-tse run                        # phase 2: collect (default tools, 2 prompts)
uv run llmbias-tse run --tools chatgpt --n 3  # specific tool / count
uv run llmbias-tse experiment --personas S02 S18            # ESEB experiment (short+long)
uv run llmbias-tse experiment --run-id <id> --turn-delay 5  # resume a run, gentler pacing
uv run llmbias-tse tools                       # list available tool keys
uv add <package>                               # add a dependency
```

Tests run with **`pytest`** (dev dependency; `uv run pytest`, single test `uv run pytest path::test_name`). No linter configured. Note: most harness logic needs a live logged-in browser, so favor unit tests around pure helpers (`storage.py`, `capture.py`, `corridas.py`, prompt building) over end-to-end.

## Operational gotchas (live collection)

Hard-won lessons from running collection at scale against logged-in web UIs:

- **Rate limit / temporary block.** A burst of requests trips ChatGPT's *"Too many requests — You're making requests too quickly. We've temporarily limited access to your conversations to protect your data."* modal. Observed after ~50 turns in ~15 min — it's **cumulative**, not just instantaneous frequency, so slowing down only delays it. Mitigations, all built in:
  - **Pace** with `--turn-delay` / `--conv-delay` (defaults 3s / 8s) to be gentler.
  - **Resume** with `--run-id <existing>`: the runner skips conversations already completed successfully and continues into the **same** run dir, so a block mid-batch costs nothing — wait for the cooldown and re-run with the same `--run-id`.
  - **Auto-wait** (built into `BaseDriver.submit`): `capture.is_rate_limited()` detects the modal (by `text_content`, occlusion-safe), then it **waits passively** — dismiss the modal and *sleep* with exponential backoff — before re-attempting the send. **Do NOT poll by reloading:** a reload re-fetches the conversation list / user data, which counts as requests and **keeps the limit alive** (observed: reloading every 30s left the modal up indefinitely). Stay idle to let it reset.
  - The modal is an overlay, so responses already rendered are still captured (we read the DOM, not the screen). A re-send after waiting is guarded by `user_selector` so the prompt is never double-posted.
  - For throughput, **don't** parallelize within one account (shared limit, flag risk); parallelize across **different tools** (separate services) instead.
- **Read with `text_content`, never `inner_text`.** When the Chrome window is **occluded** (behind the terminal during a run), Windows skips layout for that page, so Playwright's `inner_text()` returns `""` **and** `locator.last.count()` returns `0` — even though the element is present. End-of-response detection then never sees text and hangs to the full timeout (~150s/turn). `capture.last_text()` reads via `text_content` (layout-independent) guarded by the **base** locator's `count()`. `browser.py` also sets anti-throttle Chrome flags (`--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`, `--disable-features=CalculateNativeWinOcclusion`, …); these need a **relaunch** to take effect (the profile persists logins, so relaunching is cheap).
