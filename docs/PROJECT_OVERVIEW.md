# Archetype Bot — Project Overview & Suggested Architecture

> Status: **Phase 4 loop is running** — LangGraph lock loop on local Ollama `llama3.2:3b` (one coin, buy/reject/wait, then hold/revise/sell, thesis on close). Live-checked on migrations: it waits and rejects. A full buy → sell round has not been watched through yet. **Phase 5 in progress** — every think includes the market-cap move since the last peek. There is no fast-move alert. Live holder metrics are on the first think and callable again as `metrics` (rank 0.5). The agent can call `safety` (rank 0.8: Rugcheck, Streamflow locks, Solana Tracker), `dex` (rank 0.25: Dexscreener paid profile and active boosts), and `wallets` (rank 0.4: tracked KOL holders ∩ mint, count + avg rating only). **Vamp / copycat** is built as a fed/interrupt scanner on filter-PASS migrations while focused (`docs/vamp-tool.md`). Still ahead: backlog peek, website, X, Gemini. Tool calling uses a `tool` field (alone or with any action); one-think suppress is built (`docs/tool-calling-fixes.md`). Ollama `0.34.4` under `~/.local`; user unit is **disabled** — start it only when the agent runs. Global fees gate is a **hard 2 SOL** floor.
> Host: Linux Mint 23 (server-style background service + CLI).  
> Hardware: i7-12th gen, 16GB RAM, ~2GB NVIDIA MX550 (mostly unused).

---

## 1. What This Project Is

**Archetype** is a local, autonomous **Solana memecoin trading agent** that:

1. Discovers **Pump.fun migrated / graduated** pairs (not raw new launches) as the primary scan universe.
2. Runs **hard, deterministic safety filters** before wasting any LLM cycles.
3. Uses a **small local LLM** (Ollama, e.g. `llama3.2:3b`) in a **per-event, one-coin-at-a-time** loop with **sub-5s / sub-10s** think windows (hard ceiling: never wait longer than **~30s** to react to new info).
4. Executes **paper trades only** (mock wallet + simulated fills), logging everything to SQLite for training data.
5. Runs as a **Linux background daemon** (systemd); a separate **CLI** talks to it so the agent loop never blocks on UI.
6. **Improves weekly** via offline LoRA fine-tuning on Google Colab (Unsloth), then hot-swaps the GGUF into Ollama — training is a **core** feature.

### Primary discovery: migrated pairs (Pump.fun → PumpSwap)

Scan **migrated / graduated** tokens only as the default universe. New pairs are noise for discovery.

**What “migrated” means:** token finishes the Pump.fun bonding curve and graduates into **PumpSwap** (Pump AMM). Since ~March 2025 this is the normal path (not legacy Raydium for new graduates).

### Secondary path: vamp / copycat scan (fed / interrupt)

While **focused on one coin**, filter-PASS migrations are compared for same-narrative copies (“vamps”). Searching buffers hits; buy flushes them; locked hits interrupt the think (not TP/SL close). The agent does not buy the vamp from the alert. Spec: `docs/vamp-tool.md`.

### Single-coin trading lock (critical)

| State | Behaviour |
|-------|-----------|
| **Searching** | Take next candidate from the **migration backlog** (FIFO). Reject → log → never revisit unless explicitly called. |
| **Researching deeper** | If first-pass looks interesting, agent may call more tools **about that coin** (growing toolset over time). |
| **In a trade** | After buy, agent is **locked** to that coin until the position is fully closed. Agent **keeps thinking continuously** about it (no wake/sleep). May look at other coins **only via tools** (e.g. vamps, or the live migration backlog as context) — never opens a second trade. |
| **After sell** | Unlock → pull next item off the backlog top → search/eval again. |

### Tooling vision (important)

Tools named so far (PumpPortal, Helius price, metadata/URI, linked X, vamp scan, rug/holder APIs, later website legitimacy) are only a **small starter set**.

The product goal is an agent that **simulates a real human memecoin trader**: over a coin’s life it selectively pulls many different resources depending on catalysts, narrative shifts, risk flags, etc. New tools will be added continuously as we deepen the system; the architecture must treat tools as a **growing, pluggable catalog**, not a fixed short list.

### Core philosophy

| Principle | Meaning |
|-----------|---------|
| **Migrated-first, Pump.fun-only (v1)** | Discovery = Pump graduations; other launchpads later. |
| **One trade at a time** | Never hold/trade multiple unrelated positions in parallel. |
| **Continuous agent loop** | While focused/locked, the agent keeps thinking — no wake/sleep scheduler. Price ticks stay on the deterministic engine. |
| **Human-trader tool breadth** | Expandable tool catalog; starter tools ≠ final toolset. |
| **Agent updates TP/SL; program executes** | LLM revises take-profit / stop-loss predictions; execution engine buys/sells from those levels + live price. |
| **Paper-only until profitable week** | Net positive SOL after fees/slippage over a week → live gate. |
| **Training is core** | Closed-trade thesis + PnL → weekly Colab LoRA → better local brain. |

---

## 2. Settled Design Decisions

| # | Topic | Decision |
|---|--------|----------|
| 1 | **Chain / capital** | Solana memecoins only. Paper trading only. |
| 2 | **Discovery** | Migrated Pump.fun pairs. New pairs = vamp tool when relevant to focused/held coin. |
| 3 | **LLM style** | Continuous thinking while a coin is active. Target **&lt;5–10s** per think step; **&gt;30s is too late**. No wake/sleep. |
| 4 | **Process split** | **One daemon process**: filters + backlog enqueue + paper fills + FastAPI + the lock loop. **CLI is a separate process** (HTTP client). Live Helius **price WS stays in the CLI** (no tick streaming over localhost). |
| 5 | **IPC** | **FastAPI on localhost** (`127.0.0.1`). |
| 6 | **X / social (v1)** | Only tweets/accounts linked in coin metadata. Broader X later. |
| 7 | **Tools** | One tool phase (Phase 5). The short catalogue lives in `trader.md` (name, a rank from 0 to 1, one line, when to call). No separate tools file. Callable so far: `metrics` 0.5, `safety` 0.8, `dex` 0.25. Field notes sit beside the numbers on the first result only. Full list and call rules: `docs/all-tools.md`. Tool field + one-think suppress: `docs/tool-calling-fixes.md` / `docs/all-tools.md`. |
| 8 | **Data sources (hybrid)** | **PumpPortal** = pump migrations (incl. custom quotes). **Helius parsed streams** = LaunchLab + Meteora DBC graduations. **Helius RPC** = live price, filter RPC. **Birdeye** = **global fees** for every venue (pump / LaunchLab / Meteora). Metadata URI / DAS = socials and description. First-think **coin snapshot** = Jupiter + Solana Tracker + Donate.gg — **no pump.fun frontend HTTP**. Live **metrics** = Jupiter + Solana Tracker. Safety = Rugcheck + Streamflow + Solana Tracker. `dex` = Dexscreener. A failed line says `unavailable`. |
| 9 | **Pre-agent enrich + socials gate** | Always fetch metadata before the agent. **No socials at all → reject.** **Any one** of twitter / telegram / website is enough to pass. |
| 10 | **Migration backlog** | While busy, queue passed migrations locally (timestamp + coin info). When free, **FIFO from the top**. Same backlog is a **tool** the locked agent can peek for other-coin context (still one trade only). Human trade stream already consumes FIFO via SQLite. |
| 11 | **Risk (paper)** | Buy size is program-owned from migration mcap: **&lt; $50k → 3 SOL**, **&lt; $125k → 5 SOL**, **≥ $125k → 10 SOL** (missing mcap uses 3). Exact $50k is the 5 SOL band; exact $125k is 10. The model does not pick size. No paper kill-switch. |
| 12 | **Fine-tuning** | Weekly Unsloth/Colab. Maximize **PnL**. Closed trade: final thesis + PnL + time held + timestamps (+ meta). |
| 13 | **Live gate** | Net profitable paper week (even 0.1 SOL). |
| 14 | **Launchpads v1** | **Pump / LaunchLab / Meteora DBC** discovery + paper trade. PumpPortal for pump; Helius `parsedTransactionSubscribe` for LaunchLab→CPMM and Meteora DBC→DAMM v2. Agent card shows `amm` + `launchpad`. **OTC desks:** on filter PASS, `GET otcdesks.cash/api/coins?mint=…` — hit attaches `otc_basket` / `otc_reward` (not an AMM). Paper wallet stays SOL/USD via quote→USD→SOL. |
| 15 | **Buy requires predictions** | Buy only if the agent outputs **TP + SL**. Any profit **&gt; 0**. Any stop-loss **&gt; 0 and &lt; 100**. No fixed menus. Size is injected from mcap (row 11). A `wait` or `reject` that also sends percents has those percents dropped. Human CLI can still set TP/SL via `set-profit` / `set-loss`. |
| 16 | **Exits** | Agent continuously revises TP/SL. Deterministic price/exec hits levels every tick. **Daemon TP/SL auto-exits implemented** (human CLI `set-profit` / `set-loss` + in-watch commands). |
| 17 | **Price shown to agent** | Latest engine price + % since last check + current unrealized PnL. |
| 18 | **Prompt memory while active** | **Last summary + last N events**; default **N = 5**. |
| 19 | **Post-trade training log** | On close: **full history** once → final thesis → store with PnL / hold time / timestamps. |
| 20 | **Rejects** | Log timestamp + short reasoning + decision → discard unless recalled. Human trade stream already writes `agent_rejects`. |
| 21 | **Paper fill / slippage** | **v1 paper fills:** spot fill at watch/RPC price, **no fees/slippage yet**. Slippage model deferred. |
| 22 | **Bankroll / vamp match specifics** | Deferred until those modules. |
| 23 | **Watch PnL** | **Aggregated round PnL** = `(sold proceeds + remaining value) − bought`, not leftover-only. |
| 24 | **Trade-stream fills** | When buying/selling from live watch, CLI sends the **last watch tick** (price + FDV + name/symbol/pool) so ledger matches what the user saw. |
---

## 3. Suggested End-to-End Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     HOST: Linux Mint 23 (systemd)                       │
│  ┌──────────────┐ FastAPI  ┌────────────────┐   ┌────────────────────┐ │
│  │ CLI / TUI    │─────────▶│ Agent Daemon   │──▶│ Ollama (3B local)  │ │
│  │ (typer+rich) │ localhost│ (LangGraph)    │   │ localhost:11434    │ │
│  └──────────────┘          └───────┬────────┘   └────────────────────┘ │
│                                    │                                     │
│         ┌──────────────────────────┼──────────────────────────┐         │
│         ▼                          ▼                          ▼         │
│  ┌─────────────────┐   ┌────────────────┐   ┌─────────────────┐        │
│  │ PumpPortal      │   │ Paper Exec     │   │ Persistence     │        │
│  │ (migrations)    │   │ tick price +   │   │ active_context  │        │
│  │ → backlog queue │   │ TP/SL fills    │   │ trading_ledger  │        │
│  │ Helius (price)  │   │                │   │ migration queue │        │
│  │ metadata / DAS  │   │                │   │                 │        │
│  │ growing tools…  │   │                │   │                 │        │
│  └─────────────────┘   └────────────────┘   └─────────────────┘        │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
                         weekend      │ export trajectories
                                      ▼
                    ┌──────────────────────────────────┐
                    │ Google Colab (Unsloth LoRA)       │
                    └──────────────────────────────────┘
```

---

## 4. Basic Flow (v1 Core Loop) — Settled Shape

This is the first basic part of the project — the spine everything else hangs on:

```
1. PumpPortal: migrated coin appears
2. Deterministic ON-CHAIN filters
      └─ fail → log → (coin never enters backlog for agent)
3. Fetch OFF-CHAIN metadata (URI / DAS)
4. Deterministic OFF-CHAIN filters
      - no socials at all → reject (any one of twitter/telegram/website passes)
      └─ fail → log
5. If agent BUSY (in research/trade): append to LOCAL MIGRATION BACKLOG
   (timestamp + coin info) and continue; do not interrupt the locked coin
6. If agent FREE: take FIFO from backlog (or this coin if backlog empty)
7. Agent FIRST PASS (up to 3 looks; a tool call does not count; then a forced reject)
      ├─ REJECT → log → next backlog item
      ├─ WAIT → look again. May call metrics, safety, or dex (does not burn a look)
      └─ BUY with TP + SL (size set from mcap) → LOCK
8. WHILE LOCKED (agent thinks continuously about this coin):
      - Price/exec engines: every tick; hit TP/SL → paper sell
      - Agent loop: last summary + last 5 events + price/%/PnL helpers
        → revise TP/SL + narrative; may call metrics, safety, dex, or wallets again
        (vamp is a fed/interrupt alert, not callable; linked X, backlog peek, website, Gemini still ahead)
9. ON CLOSE: full history → final thesis → DB (thesis + PnL + hold time…)
10. UNLOCK → pop next from backlog → step 7
```

### Latency budget

| Stage | Target |
|-------|--------|
| Migration notice (PumpPortal) | sub-second class; LLM is the bottleneck |
| Live price ticks (Helius) | continuous; no LLM |
| Agent continuous think steps | **&lt;5–10s** typical; **&lt;30s** hard ceiling |
| Execution vs TP/SL | every tick, deterministic |

### Execution vs agent (settled)

- **Daemon today:** filters + backlog + paper fills + TP/SL + agent API + the LangGraph lock loop (off until `POST /agent/loop/start`).
- **CLI separate:** localhost FastAPI client; owns Helius live watch / interactive trade stream.
- **Price path:** Helius WS in CLI for display. Daemon keeps **one** vault websocket per focused coin. Peek and TP/SL both read that socket. Peek does not poll RPC while the coin is in focus.
- **Execution:** paper fills via `/paper/*` (human) or `/agent/*` (LLM); caps 2% / 5 SOL still policy (not hard-enforced in CLI yet).
- **Agent (Phase 4, running):** one coin at a time. Searching: `buy` / `reject` / `wait` on `action`, plus optional `tool` (`metrics` / `safety` / `dex` / `wallets`). Locked: `hold` / `revise_tpsl` / `sell` on `action`, same `tool` field. Tool-only replies allowed. Vamp is a fed/interrupt alert (not callable). A tool call does not count as a search look. Buy must include TP+SL. Each think peeks price once and includes the move since the last peek. After a buy, the pinned prompt drops the buy / wait / reject wording. See §10 Phase 4 and Phase 5.
- Human CLI can force exit anytime (`sell` / trade-stream leave).

---

## 5. LangGraph Shape (Draft)

```
SEARCH / FREE:
  backlog_fifo → filter_already_done → llm_first_pass → reject|tools|buy(+TP/SL)

LOCKED (continuous agent loop on one mint):
  loop: summary + last 5 events + price helpers → update TP/SL + narrative
        optional tools (metrics 0.5, safety 0.8, dex 0.25, wallets 0.4); vamp fed/interrupt built; linked X, backlog peek still ahead
  || parallel forever: price_engine ticks → execution hits TP/SL

ON CLOSE:
  full_history → llm_final_thesis → ledger (thesis + PnL + hold time)
  → pop backlog
```

**Rate limiting:** LangChain `InMemoryRateLimiter` / `max_concurrency=1` on Ollama so think steps don’t stack. Continuous thinking still means sequential steps, not parallel Ollama jobs.

---

## 6. Persistence Design

### `trading_ledger.db` — permanent

| Table | Role |
|-------|------|
| `filter_rejects` | Pre-agent deterministic filter fails |
| `agent_rejects` | Agent (or human trade-stream) reject decisions |
| `agent_reviews` | Non-rejects: `outcome=exited` (watched then left) or `outcome=traded` |
| `migration_backlog` | FIFO queue of filter PASSes (`consumed` flag) |
| `paper_wallet` | Singleton mock SOL balance |
| `paper_positions` | Open bags (tokens, cost, bought/sold, `round_id`, pool) |
| `trade_fills` | Every buy/sell leg |
| `trade_rounds` | One lifecycle per bag (open → closed + aggregated PnL when flat) |
| `pool_cache` | Mint → PumpSwap pool + optional name/symbol (Helius credit saver) |
| `blacklist` / `blacklist_deployers` | Empty stubs for now |
| `trades` | Legacy stub — **no longer written** |

**Closed-trade training row (core, Phase 4+):** final thesis summary + **PnL** + time held + timestamps + meta. Mid-trade prompts use last summary + last **5** events; final summary uses full history once.

### `active_context.db` — sliding ~5-day window

Live events for the current search / locked coin (`FocusMemory`).

- `active_context` rows are events (`source` = kind, `entity_key` = mint).
- `focus_summary` is the one rolling summary for that mint.
- A mid-trade prompt reads **summary + last 5 events** only (`prompt_window`).
- `history_for_thesis` is the full log, and only the close step should call it.
- After the thesis is written onto `trade_rounds.thesis`, `drop_focus` deletes the sliding copy. Other mints' leftovers older than ~5 days are pruned. PnL and hold time stay on the closed round (already filled when the bag goes flat).

---

## 7. Packages (Draft)

| Package | Purpose |
|---------|---------|
| Python 3.12 + `uv` | Env |
| `langgraph`, `langchain-core`, `langchain-ollama` | Agent + `InMemoryRateLimiter` |
| `pydantic` / `pydantic-settings` | Schemas / config |
| `httpx`, `websockets` | Metadata HTTP + Helius WS |
| `solders` / `solana` | Decode accounts / logs |
| `aiosqlite` / sqlite3 | DBs |
| `typer`, `rich` | CLI / TUI |
| `tenacity` | Retries |

---

## 8. Service + CLI

Daemon = filters + backlog enqueue + paper wallet + FastAPI. CLI = HTTP client + local Helius watch / trade stream:

```text
archetype status | health | stop
archetype migrations [--filtered] [-f]
archetype migrations --filtered --trade   # interactive human-as-agent loop
archetype backlog
archetype watch <mint> [--once]
archetype buy <mint> <sol> [--no-track]
archetype sell <mint> [pct | init | <n> sol]
archetype balance | balance add <sol> | balance reset
```

(Later: `pause` / `resume` / `force-sell` / `blacklist` / `export-trajectories` / positions TUI.)

---

## 9. Weekly Cognitive Loop (Core)

Paper week → export trajectories → Colab Unsloth LoRA → GGUF → `ollama create` → restart daemon.

---

## 10. Phased Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| **1** | Env, daemon+CLI stub, SQLite, systemd | **Done** |
| **2a** | PumpPortal `subscribeMigration` + CLI | **Done** |
| **2b** | Deterministic on-chain filters (Helius) | **Done** (live-validated) |
| **2c** | Metadata / socials gate + backlog persistence | **Done** |
| **3** | Helius price + PaperWallet + trade stream + TP/SL + agent API | **Done** |
| **4** | Continuous LangGraph lock loop + final thesis logging | **Running** — see Phase 4 below. No completed buy→sell round watched yet. |
| **5** | Tools. Price-change, live metrics, safety, and Dexscreener are in. Rest of the list in `docs/all-tools.md` | **In progress** |
| **6** | Weekly LoRA hardened; profitable-week live gate | Pending |

### Phase 2b — on-chain filters (implemented + live-checked)

PumpPortal only gives `{signature, mint, txType, pool:"pump-amm"}`. Filters use **Helius RPC** right after each migration. Rejects are logged to `filter_rejects` in `trading_ledger.db`.

**Progress so far (as of Phase 2b close-out):**

- Wired a deterministic filter pipeline into the migration feed (`src/archetype/filters/`).
- Gates include mint/freeze authority, decimals/supply, Token-2022 allow/deny, WSOL quote, Pump AMM pool from migrate tx, LP burn heuristic, pool SOL floor, **mcap ≥ $10k**, **global fees hard floor ≥ 2 SOL** via **Birdeye** `global_fees_paid` (**2–2.25 SOL still passes with `fees_check_needed`** for agent / `--trade`), mint/deployer blacklist tables (empty for now).
- CLI A/B: `archetype migrations` (raw) vs `--filtered` (PASS only).
- Live feed checked: low-mcap / low-fee / junk Token-2022 rejects look correct.
- Fixed a false reject race (`gB4NTGkQ…`): PumpPortal can fire before Helius shows pool / curve state — **do not gate on `bonding_curve.complete`**; successful migrate tx is graduation proof. Retries added for mint account, migrate tx, and pool account visibility.
- **Fast-fail:** hard mint safety fails and already-dead migrate snapshots bail before pool/fee polls (saves Helius credits). Double-reject log fixed via shared finalize. (No `pump` suffix gate — some pump.fun deploys omit it.)

**Timing / race notes:**

- Retries on `getTransaction` + mint/pool account reads (~a few seconds).
- `curve.complete` is **informational only** (kept in metrics); not a reject reason.
- **Global fees** = Birdeye `GET /defi/token_overview` → `global_fees_paid` (SOL). Same indexed “Fees” style as trading terminals — **not** a creator-vault estimate (that path was removed). Key: `ARCHETYPE_BIRDEYE_API_KEY`. No Solana Tracker fallback on this gate (keeps Tracker quota for agent tools). Missing key / HTTP failure → reject. **Hard reject under 2 SOL.** **2–2.25 SOL** still passes with `fees_check_needed` (yellow **CHECK** in `--trade`). Free Birdeye is ~1 rps; the client retries briefly on 429.
- Pool account + vault ATAs **polled ~7s / ~4s**; if still missing, soft-continue on migrate-tx snapshot.

**Gates (fail = reject):**

| Check | Rule |
|--------|------|
| Mint / freeze authority | Must be revoked (`null`) |
| Decimals | Must be 6 |
| Supply (UI) | 100M – 10B |
| Token-2022 extensions | Allow metadata*; reject transfer-fee / hook / permanent-delegate / non-transferable / pausable / etc. |
| Bonding curve | Used for deployer only — **not** gated on `complete` |
| Quote mint | Pool quote must be WSOL |
| Pool | Resolved from migrate tx; poll account ~7s; if still missing → continue on tx snapshot (`pool_account_pending`); vault ATAs get a short poll too |
| LP burn heuristic | Largest LP accounts look burned/empty (skipped if pool never appears) |
| Pool SOL at migrate | ≥ 0.5 SOL (sanity) |
| Market cap (USD) | ≥ **$10,000** from migrate-tx reserves × supply × SOL/USD |
| Global fees (SOL) | Birdeye `global_fees_paid`. **Hard reject &lt; 2 SOL.** Fetch fail → reject. **2–2.25 → PASS + `fees_check_needed`** |
| Blacklist | Mint + deployer tables (empty for now; rows can be added later) |

**CLI A/B:**

- `archetype migrations` → raw feed (everything received)
- `archetype migrations --filtered` → only PASS

**Not single-pass gates — Phase 5 live-metrics tool (`docs/all-tools.md`):**

- Top-N holder %
- Creator holding %
- Same-wallet / sybil clusters (explore free APIs later)
- Deployer history (# prior launches)
- Continuous buy/sell counts, price impact over time

Axiom’s visible migrated column filters are mostly **mcap + fees**; the rest of our list is the behind-the-scenes safety net.

### Phase 2c — metadata fetch + socials gate (implemented)

Runs **after** on-chain filters pass (`src/archetype/metadata/` + `run_full_filters`).

**How we fetch:**

1. Helius DAS `getAsset` (`showFungible: true`) → name / symbol / description / `content.json_uri` / image links.
2. Fallback: Token-2022 `tokenMetadata` extension on the mint for `uri` / name / symbol if DAS missed them.
3. HTTP GET the metadata JSON URI (Pump.fun Metaplex doc). `ipfs.io` rate-limits; we try **Pinata / nftstorage / dweb / ipfs.io** mirrors.

**Metadata fields we capture:**

| Field | Source | Notes |
|--------|--------|--------|
| `name`, `symbol` | DAS + URI | Prefer URI when present |
| `description` | DAS + URI | |
| `image` | DAS links/files + URI | |
| `video` | URI | Optional |
| `twitter` | URI (sometimes DAS links) | Normalized; bare `@handle` → `https://x.com/…` |
| `telegram` | URI | |
| `website` | URI | Bare domains get `https://` |
| `created_on` | URI `createdOn` | Usually `https://pump.fun` — **not gated** |
| `show_name` | URI | Informational |
| `coin_community` | URI | If present; informational |
| `json_uri` / `uri_fetched_from` | DAS + gateway that worked | |

**Socials gating rules (deterministic):**

- **Reject** if there are **no** usable socials at all.
- **Pass** if **≥1** of: `twitter` / `telegram` / `website` is a non-empty link-ish string.
- Fail-closed: if URI fetch fails and we have no socials from DAS → reject (`no socials (metadata incomplete: …)`).
- Empty / `"null"` / whitespace-only values do **not** count.
- We do **not** yet validate that the link is live, that X/TG accounts are real, or website legitimacy (later tools).

**Not gated (kept as metadata only):** description content, image presence, `createdOn`, `showName`, `coin_community`, video.

The URI JSON does **not** say where fees go. Coin type, ATH, volume, liquidity, and charity text are fetched later, on the first think (Jupiter / Tracker / Donate.gg / Helius — not pump.fun HTTP). See **Coin snapshot** under Phase 4.

### Phase 2c — migration backlog (implemented)

Persisted FIFO queue in `trading_ledger.db` → table `migration_backlog` (`src/archetype/backlog/`).

**Behaviour:**

- On filter **PASS** (on-chain + socials), daemon **enqueues** mint + coin snapshot (`coin_info` JSON).
- Skips duplicate if the same mint is already pending (`consumed=0`).
- **FIFO:** lowest `id` among unconsumed rows is next for the agent.
- Survives daemon restarts (SQLite).
- **Human trade stream** (`migrations --filtered --trade`) consumes FIFO via `consume_id` (SQLite). While the agent loop is running it is the consumer instead (`POST /agent/backlog/next`). Daemon startup **deletes unconsumed backlog rows** from the previous run.

**CLI / API:**

- `archetype backlog` → list pending (oldest → newest)
- `GET /backlog` · `POST /backlog/pop`
- `/status` includes `backlog_pending`

**coin_info snapshot includes:** name/symbol, signature, mcap/fees/pool SOL, `fees_check_needed` (when Birdeye `global_fees_paid` cleared 2 SOL but is still &lt; 2.25), socials links, deployer, short description.

### Phase 3 step 1 — live price watch (implemented)

`src/archetype/price/`: resolve PumpSwap pool from mint → read vault reserves → price + FDV.

- **Resolve:** top holders → ATA owner that is a Pump AMM pool (base=mint, quote=WSOL); prefer `known_pool` + sqlite/memory `pool_cache`.
- **Price:** `price_sol = pool_SOL / pool_base_ui`.
- **FDV (display):** `price × 1B × SOL/USD` (axiom-style pump display) — CLI shows this only.
- **mcap (internal):** still computed from on-chain supply for filter gates; not shown in watch.
- **Stream:** Helius `accountSubscribe` on base+quote vaults — tick only on reserve change (no heartbeat / poll / `-i`).
- **CLI (local WS):** `archetype watch <mint>` / `--once` — does not require daemon for price-only; PnL overlay needs daemon position.

**How the agent uses price:**

| Mode | What | Why |
|------|------|-----|
| **Peek (pull)** | Latest market cap off the focused coin's vault websocket, plus **% since the last peek** | Streaming every WS tick into the LLM would be noisy. The since-last line is the price move the agent sees. There is no separate fast-move alert. |

CLI `watch` and paper-trade live PnL both use the **same WS ticks** and update **in-place**. The agent reads the focused coin's vault socket for peek. It does not get a pushed price alert.

**Helius request optimisations (applied):** persistent httpx client; `getMultipleAccounts` / batched vault reads; mint→pool memory+sqlite cache; prefer known pool pubkey; skip DAS when name/symbol known; cache never overwrites known labels with null; shared SOL/USD on `balance`; filter poll backoff; LP check uses `pool.lp_supply`; SOL/USD TTL 5m.

### Phase 3 step 2 — paper wallet + buy/sell (implemented)

`src/archetype/execution/` + `src/archetype/api/paper.py`: mock SOL wallet + positions in `trading_ledger.db`.

**Process split:** daemon owns fills + sqlite (`/paper/*`). CLI is the client. Live `watch` keeps Helius WS **in the CLI**; loads open position from `GET /paper/position/{mint}` for PnL.

- **Wallet:** singleton SOL balance (default **100 SOL** on first init / `balance reset`).
- **Buy:** `archetype buy <mint> <sol>` → `POST /paper/buy`; then optional local inline watch.
- **Watch:** `archetype watch <mint>` — local Helius WS; **aggregated round PnL** if daemon has an open position:  
  `PnL = (sold_proceeds + remaining_value) − bought` (not leftover-only). Shows **TP/SL** when set; polls daemon for auto-exit hits.
- **Sell:** full / `%` / `init` / `<n> sol` (same modes in trade stream). Rebuys keep `initial_cost_sol` fixed.
- **TP/SL:** `archetype set-profit <mint> <pct>|clear` / `set-loss <mint> <pct>|clear` → `POST /paper/tpsl`. Daemon opens **one Helius vault WS per mint** with levels; on hit → full paper sell, ledger finalize, log `TPSL HIT`. Levels stored on `trade_rounds.tp` / `sl` (percentages).
- **Balance:** `archetype balance` → cash + marked positions (same aggregated PnL).
- **Ledger:**
  - `trade_fills` — every buy/sell leg (price, FDV, tokens, cost basis / leg PnL on sells; mode `tp`/`sl` on auto-exits)
  - `trade_rounds` — opened on first buy; on flat: `bought_sol`, `sold_sol`, `pnl_sol`/`pnl_pct`, entry/exit price+FDV, buy/sell counts, `time_held_seconds`; **tp/sl %** while open
- **Fund:** `balance add` / `balance reset`.
- **Fill price fidelity:** `/paper/buy` and `/paper/sell` accept optional watch-tick fields (`price_sol`, `fdv_usd`, `sol_usd`, `name`, `symbol`, `pool`). Trade stream always sends the last live tick so rounds match the CLI display.

### Phase 3 step 3 — interactive filtered trade stream (implemented)

`src/archetype/agent/trade_stream.py` — human stands in for the future agent.

```bash
uv run archetype migrations --filtered --trade
```

- **Source:** polls `migration_backlog` over **SQLite only** (daemon already enqueues PASSes; **no extra daemon endpoints or CPU** for the wait loop).
- **First prompt:** `watch` / `reject` / `quit`. First decision **consumes** the backlog row (`consume_id`).
- **reject** → `agent_rejects` (same table the real agent will use).
- **watch** → two-line Rich Live UI: price line + visible `> …█` command line (cbreak stdin so typing isn’t wiped by FDV redraws). Commands:
  - `buy <sol>` → paper buy at **last watch tick**, keep watching with PnL
  - `sell` / `sell <pct>` / `sell init` / `sell <n> sol` when holding
  - `set profit <pct>|clear` / `set loss <pct>|clear` → daemon TP/SL monitor (yellow on watch line)
  - `reject` after watching → `agent_rejects`
  - `leave` / `exit` / `stop` → next coin; `quit` leaves the whole stream
- On **TP/SL hit** while watching: watch shows `TP HIT` / `SL HIT` (daemon sold); position clears.
- **History (`agent_reviews`):**
  - `outcome=exited` — watched then left (aside, **not** a reject)
  - `outcome=traded` — bought at least once during the session
- **Modules:** `src/archetype/agent/store.py` (`record_reject`, `record_review`), backlog `consume_id`.

### Phase 3 step 4 — TP/SL auto-exits (implemented)

`src/archetype/execution/tpsl.py` — `TpslEngine` on daemon lifespan (async background tasks; does not block filters).

- **Semantics:** `profit_pct=50` → sell when aggregated round PnL ≥ **+50%**; `loss_pct=20` → sell when ≤ **−20%**.
- **Helius cost:** WS only while ≥1 level set; uses `known_pool` from position (no holder scan); sell at live tick (no extra price RPC).
- **API:** `POST /paper/tpsl`, `GET /paper/tpsl/{mint}`; position GET includes levels + `last_hit`.

### Phase 3 step 5 — agent API polish (implemented)

Clean internal surface for the Phase 4 LLM — separate from human `/paper/*` + CLI watch.

**Packages:** `src/archetype/tools/` (peek, on-chain metrics, safety, dexscreener, backlog/trade helpers) · `src/archetype/api/agent.py` (`/agent/*`). The model sees a short catalogue inside `trader.md` (`metrics` 0.5, `safety` 0.8, `dex` 0.25). `GET /agent/tools` and `archetype agent-tools` stay removed. `AgentToolkit.call()` still runs buy / reject / sell / set_tpsl / peek for the loop. Metrics, safety, and dex are HTTP reads inside the graph, not toolkit calls.

| Endpoint | Role |
|----------|------|
| `POST /agent/peek` | One-shot FDV/price (+ PnL/TP/SL if holding) |
| `GET /agent/backlog/peek` | List pending candidates **without** consuming |
| `POST /agent/backlog/next` | FIFO pop when searching |
| `GET /agent/wallet` · `GET /agent/position/{mint}` | Cash / open bag |
| `POST /agent/buy` | Paper buy **requires** `profit_pct` + `loss_pct`; size is injected from mcap; arms TP/SL |
| `POST /agent/sell` · `POST /agent/tpsl` | Exit / revise levels |
| `POST /agent/reject` | Log `agent_rejects` |
| `POST /agent/loop/start` · `POST /agent/loop/stop` · `GET /agent/loop` | Start/stop the lock loop; phase, mint, last decision |
| `GET /live` | SSE feed for `migrations --follow` |

**CLI smoke:** `archetype peek <mint>`.

**Price the agent sees:** every think includes how market cap moved since the last peek. The model is not shown token price. There is no fast-move alert and no `/agent/alerts/*` route. The vault socket still opens when the agent focuses a coin, so peek and TP/SL share it.

### Phase 4 — LangGraph lock loop (running)

`src/archetype/llm/` — `graph.py` (`LockLoop`), `runner.py` (`AgentLoop`), `client.py`, `choices.py`, `decision.py`, `memory.py`, prompts `trader.md` + `thesis.md`.

**How to run:** `systemctl --user start ollama`, then the daemon, then `POST /agent/loop/start`, then `uv run archetype migrations --follow` in another terminal. `agent_loop_enabled` stays **false**, so a daemon restart does not start the model. Code changes load only after a daemon restart and a new loop start. Follow must be restarted after SSE changes. Do not also run `migrations --trade` while the loop is up.

**Loop:**

- Phases: `idle` → `searching` → `locked` → close → idle.
- One `tick()` is one graph step, then stop. Next tick is ~2s when idle, ~12s after a think.
- Searching actions the model may return: `buy`, `reject`, `wait`, `metrics`, `safety`, `dex`. `wait` is stored as `hold`. The follow view prints **waiting** before a buy and **holding** after. `metrics`, `safety`, and `dex` do not count toward the 3-look cap and do not trade.
- Locked actions: `hold`, `revise_tpsl`, `sell`, plus `metrics`, `safety`, and `dex`. A locked `hold` that includes percents calls `set_tpsl`.
- **3 looks max** while searching, and the first look counts. If there is still no buy or reject, the program rejects with `looked 3 times without a buy or a reject`. A locked position is not capped.
- One timeout while searching logs and auto-rejects (`timed out before a decision`). A timeout while locked logs and retries (an open bag is not rejected).
- Daemon startup clears pending backlog rows.

**What the model sees each think:** `trader.md` (system, including the short tool catalogue), then a saved opening card, then a short user message of what changed. While searching, that system text includes buy, reject, and wait. Once the coin is bought, those examples and the before-buy instructions are removed, and the hold paragraph says the agent can call tools, which are listed below. The opening card is mint, name, symbol, which socials exist, description, required buy size, optional **`pairing: Name (TICKER)`** when quote ≠ SOL, and the **coin snapshot** below. For paired coins the system prompt also gets a short pairing note (omitted on classic SOL pairs to save tokens). The card is built once and replayed on later thinks. The changing message is phase, live market cap, pnl and hold time if owned, market-cap change since the last peek, and on the **first think only** the live metrics block. Then the rolling summary and the last 5 events. Output is JSON constrained by `decision_format`. `num_predict=-1` so a reply is not cut mid-JSON.

**Coin snapshot (first think):** `src/archetype/metadata/snapshot.py`. One shot when the agent starts on a coin. Not part of the migration filter. **No pump.fun frontend HTTP** (Cloudflare-blocked from this host).

| What | Source | On the card |
|------|--------|-------------|
| ATH mcap | Solana Tracker `GET /tokens/{mint}/ath` | Yes |
| 1h volume, liquidity, created time | Jupiter `lite-api.jup.ag/tokens/v2/search` | Yes |
| Coin type: cashback, holder rewards | Solana Tracker `pools[].pumpfun-amm.isCashbackCoin` / `isHolderReward` | Yes, on `coin_type` |
| Charity | Donate.gg `GET /api/v1/101/tokens/{mint}` (probe every coin; 404 = not charity) | Yes when present |
| Fee redirect | Helius sharing-config PDA | Shareholder address and percent |
| Tokenized agent | Same Helius batch, agent-payments PDA | Buyback percent when present |

Not on the card: livestream (pump-only; dropped), NSFW, banned, boost, raw quote mint, mayhem, Donate.gg’s cut. See `docs/metadata.md` for mode research. Keys: existing Helius + `ARCHETYPE_SOLANA_TRACKER_API_KEY`. Jupiter and Donate.gg need no key.

**Buy / levels:** size comes from mcap (decision 11). Buy must include `profit_pct` &gt; 0 and `loss_pct` &gt; 0 and &lt; 100. `wait`, `reject`, `metrics`, `safety`, and `dex` do not use those percents. Percents on those replies are ignored, including zeros (those used to fail the step). The buy examples in `trader.md` say “a suitable take-profit percentage, e.g. 50” and “a suitable stop-loss percentage, e.g. 25” so the model is not stuck on one pair of numbers.

**Memory:** summary + last 5 events in the think prompt. Blank narrative keeps the stored summary. Reasoning is required on every reply. On reject, the full log JSON goes to `agent_rejects.agent_log`. On a flat close, the same log goes to `trade_rounds.agent_log`, then `thesis.md` writes `trade_rounds.thesis` from the full history.

**Follow:** `migrations --follow` prints focus (mcap, fees, buy size), then each think with **current price and FDV**, then the decision, wait/hold, buy, reject, a metrics, safety, or dex block, or TP/SL hit.

**Ollama on this laptop:** `llama3.2:3b`, `num_ctx` 2048, `keep_alive` 30m, timeout **150s**. `ollama_num_thread: 0` means all logical CPUs except one (11 here). Warmup uses that same context so the real call does not reload the model. Early runs on 2 threads were ~4 tok/s and a think often took 30–70s. The NVIDIA MX550 is unused (driver not loaded); inference is CPU.

**Live so far:** the model mostly **waits**. The first think now includes the coin snapshot and a live metrics block, and the agent can ask for `safety` or `dex`. Forced rejects after 3 looks and the timeout reject have both fired. A buy that then trades out, with thesis and `agent_log`, has not been seen end to end yet. The new prompt and tools load only after a daemon restart and a new loop start.

### Phase 5 — tools (in progress)

Spec and build order: `docs/all-tools.md`. The catalogue the model actually sees is a few lines at the bottom of `src/archetype/llm/prompts/trader.md`. There is no separate tools prompt.

**Built:**

1. **Price change (fed).** Every think says how market cap moved since the last peek. Any size. The first look says none yet. There is no fast-move alert: it was removed so a price jump does not interrupt a think or add another event. `src/archetype/tools/price_change.py`. The vault socket still opens on focus so peek and TP/SL share it.

2. **Live metrics (both).** `src/archetype/tools/onchain.py`. Fed once, on the first think, in the changing message so a replay of the opening card cannot go stale. The agent sets `"tool":"metrics"` (alone or with wait/hold/buy/…). That call does not count as a search look. **Jupiter** (holders, top-10 %, deployer launched/graduated via `audit.devMints` / `devMigrations`, 5m trades fallback) + **Solana Tracker** (dev %, snipers still holding, bundlers, preferred 5m stats). No pump.fun HTTP. No Helius. A failed line says `unavailable`.

3. **Safety (agent).** `src/archetype/tools/safety.py`. One tool, `safety`. Not on the first prompt. The old one-call legitimacy idea is this tool. Four reads in parallel, no Helius:
   - Rugcheck report, no key: score (`score_normalised`, lower is safer), risk names and levels (empty means none), insider networks as active wallets and the supply they still hold. Total wallet count is not shown.
   - Streamflow locks and vesting, no key: one line, percent of supply still locked and how many locks. A lock whose end or expiry is already past is not counted. Both calls missed means `unavailable`, not a fake zero.
   - Solana Tracker `GET /tokens/{mint}` with `ARCHETYPE_SOLANA_TRACKER_API_KEY`: snipers only when they still hold supply (otherwise the line is omitted), insiders, bundlers as a count and a percent, and total fees in SOL (`risk.fees.total`). No key means those lines say `unavailable`. Wallet lists, charts, ATH, pool rows, and LP lock are discarded.

4. **Dexscreener (agent).** `src/archetype/tools/dex.py`. Tool `dex`, rank 0.25. Not on the first prompt. The agent only sees `dex paid` (true when an approved token profile is in the orders) and `active boosts` (the live count on the pair). Extra websites and socials are merged onto the stored coin when the URL is new. `imageUrl` and `header` are stored too. None of those links are shown. No API key.

5. **Tracked wallets (agent).** `src/archetype/tools/wallets.py`. Tool `wallets`, rank 0.4. Count + avg rating only.

6. **Vamp / copycat (alert — fed / interrupt, not callable).** `src/archetype/tools/vamp.py`. On filter-PASS migrations while focused: name/ticker/shared social URL match → buffer (searching), flush on buy, or interrupt + rethink when locked. Agent sees a short alert (name/symbol, matched fields, optional pairing) and judges whether to sell/reject; detector does not score “stronger”. Hold/revise after a vamp drops those mints from the migration backlog. Later, with X tools, revisit reply/quote-chain matching. Design: `docs/vamp-tool.md`.

**Still ahead:** backlog peek, website legitimacy, X, Gemini. Call control (tool field, optional with any action, one-think suppress, bare wait 10s) is built — `docs/tool-calling-fixes.md` / `docs/all-tools.md`. Soft narrative-tool ordering nudges still later.

**Keys:** Solana Tracker (`ARCHETYPE_SOLANA_TRACKER_API_KEY`) is required for ATH, cashback/holder-reward flags, and the richer metrics lines. Rugcheck / Streamflow / Jupiter / Donate.gg need no key. Pump.fun frontend APIs are **not used** (Cloudflare bot wall).

---

## 11. Per-Event Reasoning (Settled)

**Batching multiple unrelated coins into one LLM call is rejected** for this project.

We use **per-event / per-focus-coin** calls:

- Searching: evaluate the current candidate only.  
- Locked: every new packet is about the held coin (or a related vamp).  
- Optional: coalesce 2–3 events for the *same* mint that arrive within ~1s so Ollama isn’t flooded — still one coin.

---

## 12. Hardware Constraints

One 3B model, one locked trade, sequential LLM calls, CPU Ollama, Colab for training.

Ollama `0.34.4` is installed under `~/.local` (no system sudo). User unit `ollama.service` is installed but **disabled** (does not start at login). Start with `systemctl --user start ollama` when the agent should run; `systemctl --user stop ollama` when done. Model `llama3.2:3b` (~2 GB). Settings: `ollama_base_url` / `ollama_model` / `ollama_timeout_sec` (**150s**) / `ollama_num_thread` (**0** = every logical CPU except one). Python deps: `langgraph`, `langchain-core`, `langchain-ollama`. Client is `src/archetype/llm/client.py` (one in-flight call, `num_ctx` 2048, `num_predict` unlimited). On this machine, 100% CPU: cold load **~9–16s**. With the old 2-thread default a full think often took **30–70s**. The 5–10s think target is not met yet.

---

## 13. Success Definition

Unattended daemon; Pump migrations via Helius; rejects logged; single-coin lock; TP/SL-driven paper exits; trajectories stored; **net profitable paper week** before real SOL.

**Progress toward that:** Phases 1–3 done. Phase 4 lock loop runs (search, wait, reject, thesis path on close). Phase 5 has price-change, live metrics, safety, and Dexscreener in the think path. Still need a watched buy→sell round, the rest of `docs/all-tools.md`, and training export before the profitable-week gate.

---

## 14. Research Notes — Helius, Metadata, PumpPortal Speed

### 14.1 Design choice: hybrid data plane (settled)

- **PumpPortal** for migration discovery (initial fetch).  
- **Helius free-tier WebSockets** for live price / pool updates on the active coin.  
- **Metadata URI / Helius DAS** once per candidate for off-chain socials (before agent; socials guardrail).
- **First-think coin snapshot:** Jupiter + Solana Tracker + Donate.gg + Helius fee/agent PDAs. No pump.fun frontend. See Phase 4.
- **Wave A/B (other streams):** PumpPortal custom-quote migrations repair mint via migrate tx; Helius parsed streams catch LaunchLab + Meteora DBC grads; filters/price/fills cover PumpSwap + Raydium CPMM + Meteora DAMM v2; paper PnL in SOL via quote→USD→SOL; agent card shows `amm`, `launchpad`, and `pairing` when relevant. Plan: `docs/other_streams_plan.md`.
- **Safety tool:** Rugcheck and Streamflow (no key) plus Solana Tracker (one key). See Phase 5.
- **Dex tool:** Dexscreener orders and token pairs (no key). The agent sees paid status and active boosts. New links, the image, and the banner are stored on the coin and not shown.

We accept decode work on the Helius price path in exchange for control. PumpPortal removes a lot of migration-parsing glue.

Free tier limits (approx): credit budget, ~10 RPC RPS, a few concurrent WS connections, DAS at lower RPS — enough for **one focused coin**, tight if we spam global firehoses.

### 14.2 Off-chain links (twitter / telegram / website) — your URI idea is correct

Pump.fun tokens use **Metaplex Token Metadata**. On-chain metadata stores a **`uri`** pointing at a JSON document (often IPFS / CDN). That JSON typically includes:

| Field | Example use |
|-------|-------------|
| `name`, `symbol`, `description` | Display / first-pass research |
| `image` | Image URL |
| `twitter` | Linked X URL / account |
| `telegram` | Group link |
| `website` | Site link |
| `createdOn` | Often `https://pump.fun` |

**Pipeline:**

1. From mint → read Metaplex metadata (RPC) **or** call **Helius DAS `getAsset`** → get `content.json_uri` (and often already-parsed name/symbol).  
2. `HTTP GET` that URI → full JSON with social links.  
3. Feed links into the **linked-X tool** (v1: only those links).

**Helius DAS:** yes, useful here. `getAsset` with `showFungible: true` returns indexed metadata / `json_uri` / files. Costs DAS credits (free plan is limited RPS). For **one coin at a time**, DAS once at research time is fine; do **not** poll DAS every price tick. Live price stays on `accountSubscribe`.

**Caveats:** some URIs are slow IPFS gateways; some coins omit socials; metadata is generally **immutable** after Pump create (socials added later on DEXScreener are **not** in on-chain URI).

### 14.3 PumpPortal vs Helius — how fast?

| | **Helius free WS (raw chain)** | **PumpPortal data WS** |
|--|-------------------------------|-------------------------|
| **What you get** | Raw logs / account bytes; you decode | Pre-parsed migration/trade events; often richer fields |
| **Latency (vendor claims / industry)** | Standard RPC WS — good, but not LaserStream/gRPC class. DIY parse adds a little CPU time. | PumpPortal FAQ: data at `processed` commitment; typically **&lt;100ms behind gRPC** if your server is in NY. They say use Helius LaserStream / own gRPC if you need faster. |
| **Metadata/socials** | Separate step: DAS / URI GET | Often already attached / easier — less glue code |
| **Cost** | Free tier credits | Migration/new-token subscribe documented free; per-token trade streams can be metered |
| **Control / lock-in** | Highest | Medium — schema/uptime depend on PumpPortal |
| **Fit for us** | Best for **focused price** + durable architecture | Best for **fast prototype** of migrations + less decode |

**Important for Archetype:** our bottleneck is **LLM think time (seconds)**, not 50–200ms of data lag. For **migrated** trading (not block-0 sniping), PumpPortal being “a bit slower than colocated gRPC” is usually **irrelevant** next to a 5–10s Ollama call.

**Settled hybrid:**

1. **PumpPortal** → “what just migrated?” (initial fetch).  
2. **Helius** → live price / pool state for the active coin.  
3. **Metadata URI / DAS** → socials and description once per candidate (before agent; feeds socials guardrail).
4. **First think** → Jupiter + Solana Tracker + Donate.gg + Helius fee/agent PDAs for the coin snapshot on the opening card. Same think fetches live metrics (Jupiter + Tracker; not saved on the card). Non-SOL quote coins also get a `pairing` line and a short system-prompt note.
5. **When the agent asks** → `safety` hits Rugcheck, Streamflow, and Solana Tracker. A later `metrics` call refreshes the holder block. `dex` hits Dexscreener for paid status and active boosts, and stores any new links plus the image and banner on the coin.

### 14.4 Paper trading (v1 implemented)

Own `PaperWallet` on the same Helius live price stream we would use live — going real later is signer/wallet, not a data rewrite.

**v1 behaviour:** spot fills (no fees/slippage); CLI watch is source of truth for interactive fills; daemon persists wallet / positions / fills / rounds. **PumpSwap / WSOL only.**

### 14.5 StonkFun / Raydium LaunchLab (deferred — research notes)

StonkFun pairs memecoins with tokenized stocks / other quotes via **Raydium LaunchLab** (`LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`) → graduates with `MigrateToCpswap` into **Raydium CPMM** (not PumpSwap).

**What we already see today:** PumpPortal `subscribeMigration` also emits some of these as:

```json
{ "signature": "…", "mint": "<QUOTE_STOCK_CA>", "txType": "migrate", "pool": "raydium-cpmm" }
```

- `mint` is the **quote** (e.g. AMD xStock), **not** the memecoin CA.  
- No `baseMint` field in the payload.  
- Real memecoin CA is recoverable by decoding the migrate tx (`getTransaction`): e.g. `transferChecked` of **206_900_000** tokens, or account index heuristics on `MigrateToCpswap`.

**To support StonkFun (when we choose to):**

1. **Discovery / CA resolve:** on `pool == "raydium-cpmm"` (or non-`pump-amm`), decode PumpPortal `signature` → real base mint + quote mint. Optional later: a **second discovery channel** via Helius `logsSubscribe` on LaunchLab (and/or Raydium LaunchLab HTTP APIs) to catch launches/grads PumpPortal misses.  
2. **Paper trading:** build a **separate Raydium CPMM price + fill path** (vault reserves, non-WSOL quote, mark-to-market via quote asset USD/SOL). Current buy/sell/watch **will not work** after CA resolve alone — they hard-require PumpSwap + WSOL.

**Cost note (feeds):** Raydium’s own LaunchLab Mint/History HTTP APIs advertise **unauthenticated reads** (no API key for search/leaderboards/history). Live graduation streaming is **not** a free Raydium websocket product — use **Helius standard `logsSubscribe`** on the LaunchLab program (available on free Helius plans; uses credits on follow-up `getTransaction`) or a paid indexer (e.g. Codex). PumpPortal path above reuses the existing free migration sub + ~1 Helius tx decode per event.

---

## 15. Deferred / Later Notes (Not Blocking Core Design)

Implementation knobs and later tools — **do not block** the basic flow:

- Exact paper-fill / slippage model (spot fills exist; fees/slippage not yet)
- Exact bankroll starting size
- Fine-tuning export formatting details
- Tool catalog for Phase 5 → **in progress**. Built: price-change, live metrics (0.5), safety (0.8), Dexscreener (0.25), wallets (0.4), vamp alert (fed/interrupt). Still ahead: backlog peek, website, X, Gemini. Spec: **`docs/all-tools.md`**.
- Agent peek → **done** (one price each think, plus % since the last peek). Fast-move alerts were removed.
- LangGraph lock loop → **running** (Phase 4). `metrics`, `safety`, `dex`, and `wallets` are in the action list; vamp is fed. Call-control is not.
- **StonkFun / Raydium LaunchLab:** decode PumpPortal `raydium-cpmm` migrate txs for real CA; separate Raydium paper price/exec; optional second discovery via LaunchLab `logsSubscribe` / Raydium HTTP APIs (§14.5)  

---

## 16. Repo Layout (current)

```text
archetype-bot/
├── docs/
│   ├── PROJECT_PLAN.md
│   ├── PROJECT_OVERVIEW.md
│   ├── all-tools.md            # phase 5 tool list and call rules
│   ├── metadata.md             # coin-type / donate.gg research
│   └── …
├── pyproject.toml
├── config/default.yaml
├── src/archetype/
│   ├── cli.py                 # HTTP client + local watch / trade stream
│   ├── daemon.py              # uvicorn entry
│   ├── api/                   # FastAPI: status, migrations, backlog, /paper/*, /agent/*
│   ├── ingest/                # PumpPortal + Helius launchlab/meteora feeds + buffer
│   ├── filters/               # on-chain deterministic gates + Helius RPC
│   ├── metadata/              # DAS / URI + socials gate + first-think coin snapshot
│   ├── backlog/               # migration_backlog FIFO
│   ├── price/                 # resolve / snapshot / WS stream / pool cache
│   ├── execution/             # paper wallet, fills, trade ledger, inline watch, tpsl
│   ├── tools/                 # peek, onchain metrics, safety, dexscreener, backlog/trades
│   ├── agent/                 # trade stream, live follow, rejects/reviews store
│   ├── llm/                   # lock loop, ollama client, prompts, focus memory
│   ├── db/                    # schema init
│   └── …
├── data/                      # active_context.db, trading_ledger.db (gitignored)
└── tests/
```

Trajectory export for Colab is still Phase 6.
