# AI Agent Framework & Architecture Blueprint

## 1. Overview
This document compiles the complete architectural blueprint for designing, deploying, and training a continuous, looping AI agent on a resource-constrained local system (16GB RAM laptop) with cloud-assisted optimization.

---

## 2. Core Framework & OS Selection
*   **Orchestration Framework:** **LangGraph**
    *   *Why:* Natively supports cyclic graphs (infinite loops) and offers precise state management via custom reducers, preventing memory leaks during continuous execution.
*   **Operating System:** **Linux Ubuntu (Server or Lightweight Desktop)**
    *   *Why:* Offers superior RAM optimization and system process management compared to Windows, freeing maximum memory for local inference.

---

## 3. Hardware & Local Inference Specifications
*   **Host Specs:** 16GB System RAM, ~100GB SSD available storage, NVIDIA MX550 (2GB VRAM), Intel Iris Xe Integrated Graphics (Shared RAM).
*   **Inference Engine:** **Ollama** (100% Free, local open-source weights execution).
*   **Target Model:** **Llama 3.2 (3B Parameters)** or **Gemma 2 (2B Parameters)**.
    *   *Sizing Note:* Sub-4B models fit safely within system memory allocations without overwhelming the 2GB dedicated VRAM or causing extreme swapping bottlenecks.

---

## 4. Blockchain & API Integrations (Python-Centric)
*   **EVM (Ethereum Virtual Machine) Stacks:** `web3.py` combined with **Coinbase AgentKit** for automated wallet actions and on-chain log reading.
*   **Solana Stacks:** `solana-py` / `anchorpy` combined with **Solana Agent Kit** (Sendai) for fast transaction and token stream parsing.
*   **API Streaming:** Standard Python bindings for real-time extraction (e.g., X/Twitter stream lookups) piped directly into the LangGraph workflow.

---

## 5. Dual-Database Memory Strategy (SSD Partitioning)
To allow the agent to process data quickly without context fatigue or runaway storage consumption, memory is partitioned into two distinct physical SQLite databases:

### File 1: `trading_ledger.db` (The Training Vault)
*   **Retention:** Permanent (Never Purge).
*   **Purpose:** Captures the golden training data used for weekly cloud optimization.
*   **Schema Fields:** Timestamp, Input State (Tweets/Blocks), Actions Selected (Trades), Resulting 24h ROI (Reward Metric).

### File 2: `active_context.db` (The Sliding Window)
*   **Retention:** Transient (Sliding 5-Day Purge Window).
*   **Purpose:** Provides immediate, high-signal lookup contexts for the local LLM without bloating the inference token window.
*   **Purge Logic:** Rows where `timestamp < (current_time - 5 days)` are programmatically removed on every processing block loop using standard SQL eviction queries.

---

## 6. Hybrid Training Pipeline (Decoupled Cloud Cycle)
Direct on-device training will overwhelm a 2GB VRAM laptop. Instead, the process leverages a decoupled execution and learning rhythm:

1.  **Weekly Operational Cycle (Mon–Sat):** The agent loops for 6–12 hours a day locally, executing paper trades and logging telemetry safely into `trading_ledger.db`.
2.  **Weekend Training Window (Sunday):** 
    *   The `trading_ledger.db` is uploaded to **Google Colab** (utilizing their free tier access to an NVIDIA T4 GPU with 16GB VRAM).
    *   A **LoRA (Low-Rank Adaptation)** fine-tuning session is run via **Unsloth** to adapt the base 3B model parameters using only high-ROI historical records.
    *   The updated adapter layers are compiled into a fresh `.gguf` file.
3.  **Hot-Swapping the Brain:** The fresh model file is pulled back to the laptop and loaded into Ollama (`ollama create`), allowing the agent to launch on Monday with a natively optimized understanding of past successful trade patterns.
