# Architectural Specification & Roadmap: Autonomous Memecoin Trading Agent

## 1. Project Overview & Vision
This project is an autonomous, hybrid-architecture trading agent optimized for real-time memecoin evaluation and execution. Designed to run on a local Linux Mint server-style environment, it leverages a dual-path execution model to balance microsecond-level execution speed with deep local LLM reasoning.

### Key Operational Philosophy
1. **Deterministic Fast Path:** Immediate, code-level execution for market data retrieval, liquidity checks, security guardrails, and order routing (zero LLM latency overhead).
2. **LLM Slow Path:** Asynchronous, local inference (Ollama `llama3.2:3b`) for sentiment analysis, social noise evaluation, and structured trade decisions.
3. **Fail-Fast Safety:** Hard stop rules (rug-check filters, minimum liquidity, max slippage) execute before any LLM inference occurs, preserving computational resources and protecting capital.

---

## 2. Tech Stack & Infrastructure Constraints

* **Host System:** Linux Mint 22 (Cinnamon, Ubuntu 24.04 base)
* **Hardware Constraints:** Intel Core i7 12th Gen, 16GB RAM, NVIDIA GeForce MX550 (2GB VRAM), ~100GB Linux Root
* **Language Runtime:** Python 3.12 managed via `uv`
* **Agent Framework:** LangGraph (State Machine orchestrator) + LangChain
* **Local Inference:** Ollama (`llama3.2:3b` via CPU / light GPU offload)
* **Persistence Layer:** SQLite3 (`active_context.db` and `trade_ledger.db`)
* **Data Sources:** DEXscreener API (Real-time pairs/liquidity) & Twitter/X scrapers (Social signals)
* **Execution Network:** Solana Paper-Trading Core (Mock wallet & transaction simulator)

---

## 3. High-Level System Architecture
┌──────────────────────────────┐
                   │  Incoming Token / Pair Feed  │
                   └──────────────┬───────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │   DETERMINISTIC FILTER   │
                    │ (Liquidity, Mint Auth,   │
                    │   Top Holders, Volume)   │
                    └─────────────┬────────────┘
                                  │
                   ┌──────────────┴──────────────┐
                   │ Passes Safety Constraints?  │
                   └──────┬───────────────┬──────┘
                       NO │               │ YES
                          ▼               ▼
                    ┌───────────┐   ┌───────────────────────────┐
                    │ REJECT &  │   │      LLM SLOW PATH        │
                    │ LOG EVENT │   │  (Ollama Llama 3.2 3B)    │
                    └───────────┘   │ - Social Sentiment Analysis│
                                    │ - Narrative Score         │
                                    │ - Sizing Recommendation   │
                                    └─────────────┬─────────────┘
                                                  │
                                                  ▼
                                    ┌───────────────────────────┐
                                    │  PAPER TRADING EXECUTION  │
                                    │  - SQLite Ledger Log      │
                                    │  - Position Monitoring    │
                                    └───────────────────────────┘

                                    ---

## 4. Phased Development Roadmap

### Phase 1: Environment, CLI, & Core Engine Setup
* Set up Python 3.12 `uv` environment with `langgraph`, `pydantic`, `requests`, and `sqlite3`.
* Build CLI entry point (`main.py`) supporting arguments for scan modes, paper-trading stats, and manual overrides.
* Implement SQLite database initialization scripts for tracking active trades, historical signals, and token blacklists.

### Phase 2: Deterministic Fast-Path Filter
* Implement direct DEXscreener API client with fail-fast validation.
* Build automated rug-check guardrails:
  * Minimum Liquidity Pool ($USD threshold).
  * Lock status & Top 10 Holder Concentration limits.
  * 5m / 1h Volume & Buy/Sell ratio validation.
* Create a lightweight benchmark script to ensure filter checks resolve in `< 200ms`.

### Phase 3: Solana Paper-Trading Engine
* Build a local simulated wallet state (`PaperWallet`) tracking SOL balances, tokens held, and entry prices.
* Implement simulated swap logic accounting for gas/priority fees, slippage, and price impact.
* Create a dedicated position manager with automated Take-Profit (TP) and Stop-Loss (SL) triggers.

### Phase 4: Ollama Local LLM & LangGraph Agent Loop
* Connect Ollama via LangChain integration (`OllamaLLM` / `ChatOllama`).
* Define Pydantic structured output models for LLM decisions (e.g., `ConfidenceScore`, `NarrativeStrength`, `TradeAction`).
* Construct the LangGraph state machine orchestrating transitions between Fast-Path Filter $\rightarrow$ LLM Analysis $\rightarrow$ Execution $\rightarrow$ State Logging.

### Phase 5: Tool Enrichment & Real-Time Monitoring
* Add sentiment analysis nodes using scraped Twitter/X or search data feeds.
* Implement a continuous background monitoring loop in Python for active paper positions.
* Build a Terminal UI (TUI) dashboard using `rich` to visualize current portfolio, live logs, and trade outputs.

### Phase 6: Optimization, Fine-Tuning & Backtesting
* Profile execution latency and CPU/GPU resource utilization on Linux Mint.
* Create a historical paper-testing suite to replay market data and evaluate agent win-rates.
* Document system failure modes and finalize error-handling/re-try mechanisms.