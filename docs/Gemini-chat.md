markdown# Architectural Blueprint: Episodic Cognitive Trading Agent

This document outlines the end-to-end system architecture for a high-frequency, multi-source blockchain trading agent designed to run efficiently on a local 16GB RAM laptop, utilizing cloud computing for weekly cognitive reinforcement.

---

## 🏗️ Core System Architecture

The system uses an **Event-Driven, Decentralised Execution Pipeline** to decouple continuous data ingestion from heavy artificial intelligence processing. This ensures the laptop's hardware remains entirely stable while tracking multiple assets.

Use code with caution.[ DATA LAYER: Websockets / RPCs / X Streaming API ]│ (Constantly updates prices, volumes, and streams using pure Python code)│▼[ THE BATCH MONITOR NODE (LangGraph) ] ◄── Loop Interval: Every 30-60 Seconds│├──► Step 1: Packages fresh metrics & APIs for max 3 active tokens.├──► Step 2: Injects raw multi-token data bundle into a single prompt matrix.│▼[ THE CENTRAL AI BRAIN (Ollama 3B) ]Processes the unified matrix in ONE single turn to return structured JSON:{"Token A": {"action": "WAIT", "narrative": "X sentiment remains highly bullish."},"Token B": {"action": "SELL", "narrative": "Dev wallet just moved 10% of supply."}}
---

## 🗄️ Database & Memory Strategy

To keep real-time lookups under **5 milliseconds**, the system maintains two distinct local SQLite databases on your SSD. This isolates short-term application contexts from your permanent training vaults.

### 1. The Active Context Database (`active_context.db`)
This handles the 5-day sliding window. It prevents the local LLM from getting bogged down by obsolete historical noise. 

```sql
-- SLIDING CONTEXT TABLE: Purges records older than 5 days
CREATE TABLE IF NOT EXISTS active_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,      -- 'blockchain_event' or 'x_tweet'
    entity_key TEXT,           -- Contract address or X handle
    raw_payload TEXT NOT NULL, -- Text read by the loop
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_context_timestamp ON active_context(timestamp);
CREATE INDEX IF NOT EXISTS idx_context_entity ON active_context(entity_key);
```

### 2. The Permanent Ledger Database (`trading_ledger.db`)
This database **never purges**. It logs the absolute historical truth of the system to build your fine-tuning training sets.

```sql
-- PERMANENT TABLE: Whitelisted X Accounts
CREATE TABLE IF NOT EXISTS trusted_x_accounts (
    account_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    category TEXT,          -- 'influencer', 'developer', 'project_team'
    influence_score INT     
);

-- PERMANENT TABLE: Monitored Crypto Wallets
CREATE TABLE IF NOT EXISTS monitored_wallets (
    wallet_address TEXT PRIMARY KEY,
    owner_label TEXT NOT NULL, -- 'Whale #1', 'Solana Deployer'
    blockchain TEXT NOT NULL,  
    is_blacklisted INT DEFAULT 0
);
```

---

## 🧠 Weekly Trajectory Fine-Tuning (Cloud Pipeline)

Because the agent mimics a human trader by constantly updating its prospects for a coin over time, it cannot be trained using simple single-turn inputs. Instead, it utilizes **Multi-Turn Trajectory Fine-Tuning** via **Unsloth** on a free **Google Colab T4 GPU**.

### The ShareGPT Trajectory Dataset Format
Every weekend, your local ledger exports your agent's evolving conversations and internal reasoning tags into this multi-turn format:

```json
[
  {
    "conversations": [
      {
        "from": "human",
        "value": "TIME 13:00. Token \$XYZ launched. X sentiment is quiet. Dev wallet has not moved."
      },
      {
        "from": "gpt",
        "value": "<narrative_thesis>This is an unverified, high-risk token. We will watch it, but the social signal is too weak to justify a position.</narrative_thesis> ACTION: WAIT."
      },
      {
        "from": "human",
        "value": "TIME 13:15. UPDATE: Top-tier influencer @AlphaTrader just tweeted '\$XYZ looks interesting'."
      },
      {
        "from": "gpt",
        "value": "<narrative_thesis>The underlying asset risk has not changed, but the social landscape has shifted completely. This influencer has a historical track record of driving 50% volume spikes within 10 minutes. Our thesis updates to high-velocity momentum trading.</narrative_thesis> ACTION: BUY_POSITION."
      },
      {
        "from": "human",
        "value": "TIME 13:30. UPDATE: The token price has doubled, but tracking logs show the developer wallet just transferred 10% of the total supply to an unverified address."
      },
      {
        "from": "gpt",
        "value": "<narrative_thesis>The price increase is organic hype, but the developer's wallet migration is a massive structural red flag. This pattern frequently precedes a liquidity pull. The momentum play is compromised.</narrative_thesis> ACTION: SELL_IMMEDIATELY."
      }
    ]
  }
]
```

### Why This Works For Your Specific Setup
1. **Paces Colab Free Quotas:** Training only once a week prevents Google Colab from throttling your free GPU runtime allocations.
2. **Eliminates Catastrophic Forgetting:** Aggregating data over 7 days forces the model to learn from diverse market cycles rather than overfitting to a single afternoon's market volatility.
3. **Optimizes the Local Brain:** Merging the trained LoRA adapter back into an Ollama GGUF file (`llama3.2:3b`) ensures your agent starts Monday morning with sharper native instincts.