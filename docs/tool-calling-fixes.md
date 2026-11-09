# Tool-calling fixes — `tool:` field, optional with any action, one-think cooldown

Implementation plan. **Built** (code + prompts + `docs/all-tools.md` / overview). Keep this as the behaviour brief.

Triggered by the 2026-11-09 Nothing Coin paper trade
(`6GMjV5PHfeNMaXykCktvbdmXAx54TdKfRxSRaD5Spump`, transcript under `transcripts/`).

Related: `docs/all-tools.md`, `src/archetype/llm/prompts/trader.md`, `choices.py`,
`decision.py`, `graph.py`, `coin_prompt.py`, `runner.py`, `tools/price_change.py`,
`agent/live.py`.

---

## 1. What went wrong (this run)

### Timeline (searching)

| Wall | Event |
|------|--------|
| 18:00:20 | PASS → focus Nothing Coin |
| 18:00:20–40 | Think 1 → bare `wait` (wanted more info, **no tool**) |
| 18:00:40–52 | Think 2 → `action: metrics` |
| 18:01:04–36 | Thinks 3–4 → `metrics` again (same reason) |
| 18:01:56–04 | Think 5 → buy (reason used since-last peek, not metrics) |
| 18:02:05–27 | Buy → SL (~−12%, held ~23s) |

Transcript: `transcripts/6GMjV5PHfeNMaXykCktvbdmXAx54TdKfRxSRaD5Spump.txt`.

### Root causes

1. **Tools are top-level `action` values** (`metrics|safety|dex|wallets`) and are
   **mutually exclusive** with `wait` / `buy` / …. The model cannot say “wait and
   also fetch safety” or “buy and also fetch wallets” in one reply. “I need info”
   became a wasted bare-wait think, then a separate tool think.
2. **No cooldown** — same tool callable every think; catalogue says “call whenever”.
3. **Price already fed** — `mcap_usd` + `since last peek: +X% in Ys` every card;
   metrics spam did not drive the buy.

---

## 2. Goals (decided)

1. **New tool-calling shape only.** Research tools are requested with a **`tool`**
   field. They are **never** valid values of `action`. No legacy alias that maps
   `{"action":"metrics"}` → tool. Prompt + grammar + parse enforce the new shape;
   if the model puts a tool name in `action`, that reply is simply **invalid**
   (normal `DecisionInvalid` / retry coach — **do not** add special “catch and
   translate tool-in-action” logic).
2. **`tool` is optional and combinable with any trade `action`.**  
   - Tool only: `{"tool":"safety","reasoning":"..."}`  
   - Action only: `{"action":"wait",...}` / `buy` / …  
   - Both: `{"action":"buy","tool":"wallets", ...}` → do the action **and** run
     the tool so the **next** think on this focus sees the result (saves a round trip).
3. **Bare wait / bare hold remain legal** (watch price). Pure wait/hold → **10s**
   before the next think.
4. **One-think catalogue suppress** after a tool runs (omit that tool from the
   next think’s catalogue + grammar; restore after that one think).
5. **Tool calls do not count** toward `_MAX_SEARCH_LOOKS` (the searching 3-look
   force-reject budget). Unchanged.
6. **Stray `tool` is not “stray” anymore on buy** — buy+tool is a first-class
   feature. (Ignore only makes sense for actions that **end** the focus with no
   next think — see §4.3.)
7. Prompt/catalogue rewritten so the agent learns `tool:` clearly.
8. Keep since-last peek **`%` + duration** (`in Ys`) on every non-first card;
   teach the model that window in the prompt.

---

## 3. Reply contract (what the agent may output)

One JSON object per think. Always include `reasoning` (short sentence).

### Fields

| Field | Role |
|-------|------|
| `action` | Optional. Trade / phase verb only. |
| `tool` | Optional. Exactly one research tool name, or omitted. |
| `reasoning` | Required. |
| `narrative` | Optional (searching); blank = keep prior. |
| `profit_pct` / `loss_pct` | As today for buy / revise_tpsl. |

**At least one of `action` or `tool` must be present.**

### Allowed `action` values (phase-gated)

| Phase | `action` enum |
|-------|----------------|
| Searching | `buy`, `reject`, `wait` |
| Locked | `hold`, `sell`, `revise_tpsl` |

**Not** in `action`: `metrics`, `safety`, `dex`, `wallets`, `vamp`, or any tool name.

### Allowed `tool` values

Callable research tools only: `metrics`, `safety`, `dex`, `wallets` (and future
agent-callable tools). Filtered by one-think suppress list.

**Not** in `tool`: `vamp` (fed/interrupt alert, not callable).

### Valid combinations (examples)

```text
{"tool":"metrics","reasoning":"want holders/flow refresh"}
{"action":"wait","reasoning":"watching mcap before deciding","narrative":"..."}
{"action":"wait","tool":"safety","reasoning":"rug check while I wait","narrative":"..."}
{"action":"buy","tool":"wallets","reasoning":"...","narrative":"...","profit_pct":30,"loss_pct":10}
{"action":"hold","tool":"dex","reasoning":"check boosts while holding"}
{"action":"sell","reasoning":"thesis broken"}
{"action":"reject","reasoning":"no narrative"}
```

### Invalid (fail parse / grammar)

```text
{"action":"metrics",...}          # tool name must not be action
{"reasoning":"..." }              # neither action nor tool
{"action":"buy"}                  # missing reasoning / levels as today
{"tool":"vamp",...}               # not callable
{"tool":"metrics","tool":"safety"}# one tool max (single field)
```

---

## 4. Semantics — what the loop does

### 4.1 Tool-only (`tool` set, `action` omitted)

Meaning: stay on the current coin and fetch that tool now.

| Phase | Behaviour |
|-------|-----------|
| Searching | Same as today’s tool path: fetch → append event → `route: stop`. **Does not** increment search-look `steps` (no burn). Next think sees the tool block. |
| Locked | Same: fetch → event → stop; bag unchanged. |

Pacing after tool-only: use normal focused gap (`think_sec`, default 12s) or a
short gap — **not** the bare-wait 10s (tool latency already used wall time).
Recommendation: keep `think_sec` after tool-only / action+tool.

### 4.2 Action-only

| `action` | Behaviour |
|----------|-----------|
| `wait` | Searching continue; **10s** delay before next think. Counts as a search look. |
| `hold` | Locked continue; **10s** delay. |
| `buy` / `reject` / `sell` / `revise_tpsl` | Existing apply paths. |

### 4.3 Action + tool (same reply)

**Order:** apply the **action first**, then run the **tool** (when the focus still
exists for a next think).

| Combo | Behaviour |
|-------|-----------|
| `wait` + `tool` | Fetch tool → event on card for next searching think. No extra bare-wait 10s required (tool path). Search look: count the wait as a look **or** treat wait+tool like tool-only for the look budget? **Decision: wait+tool counts as one search look** (it is still a “wait” decision), but the tool fetch itself does not add a second look. Implement by incrementing steps on the wait side once, not again in refresh. |
| `hold` + `tool` | Revise nothing; fetch tool; next locked think sees it. |
| `buy` + `tool` | Execute buy → phase locked → **then** fetch tool into focus memory / live feed → first locked think already has the tool event (saves “buy then think then tool”). Set suppress for that tool. |
| `revise_tpsl` + `tool` | Apply TP/SL change → fetch tool → next think sees it. |
| `reject` + `tool` | **Ignore `tool`.** Focus ends; no next think on this mint. Log debug once. |
| `sell` + `tool` | **Ignore `tool`.** Trade closes / thesis path; no next hold think. Log debug once. |

Rationale for ignore on reject/sell: user wants tool results on the **next** think;
those actions have no next think on this focus. Do not delay close to run a
useless HTTP call.

### 4.4 One-think suppress

After any successful tool fetch (tool-only or action+tool):

1. `suppress_tool = <name>`
2. Next `_think`: omit `<name>` from catalogue lines, examples, and grammar `tool` enum.
3. End of that think: clear suppress, **unless** this think ran a tool again →
   set suppress to the newly run tool.
4. Clear on focus end (pop / reject / close / release).

Exactly **one** think of omission, then restore.

### 4.5 Search-look budget (clarified)

- `_MAX_SEARCH_LOOKS = 3` still applies to **searching decision looks**.
- **Tool-only replies do not increment** that counter (today’s
  `_refresh_metrics` behaviour — keep it).
- Bare `wait` / `buy` / `reject` increment as today.
- `wait` + `tool`: one look for the wait, tool fetch does not add another.

### 4.6 Price peek duration

`since_last_line` already emits:

```text
since last peek: +31.59% in 33s ($34,569 -> $45,491)
```

Keep this. Prompt must say the `%` is over that **Ys** window — do not invent
longer horizons. Acceptance: every non-first user card includes `%` and `in Ns`.

---

## 5. Prompt / catalogue (`trader.md` + assembly)

### 5.1 Natural-language contract (searching sketch)

```text
Reply with one JSON object.

You can set "action", or "tool", or both.

Actions (searching): buy, reject, wait.
- wait — look again later. You may wait with no tool just to watch market cap.
- buy / reject — decide now.

Tools are separate. To call a tool, set "tool" to one name from the list below
(not "action"). You may call a tool alone, or together with an action.
If you set both (for example buy and tool), we run the action and fetch the tool;
the tool result appears on your next think so you do not need an extra round trip.

Live mcap_usd and "since last peek: +X% in Ys (...)" are already on every card.
The Ys is how long that move took. Do not call metrics to learn price.

After you use a tool, it is omitted from the catalogue for the next think.
```

Locked sketch: same `tool` rules; actions are hold / revise_tpsl / sell; bare hold
allowed (10s); hold+tool / revise+tool / tool-only allowed; sell+tool ignored.

### 5.2 Catalogue block

Dynamically built from allowed tools (minus suppress):

```text
Tools. Set "tool" to one of these names. One tool at a time. Tools do not buy,
sell, wait, reject, hold, or change levels by themselves — they only fetch info.
metrics - 0.5 - …
safety - 0.8 - …
…
vamp - 0.9 - fed alert (not callable) — …
{"tool":"metrics","reasoning":"why you want this read"}
{"action":"wait","tool":"safety","reasoning":"why wait and why this tool","narrative":"…"}
{"action":"buy","tool":"wallets","reasoning":"…","narrative":"…","profit_pct":30,"loss_pct":10}
```

Remove all examples of `{"action":"metrics",...}`.

### 5.3 Metrics blurb

Call for holders / flow / deployer-style data — **not** for market cap (already
on the card). Drop “call whenever you want updated metrics”.

---

## 6. Code implementation guide

### 6.1 `ThinkDecision` (`decision.py`)

- `action: ThinkAction | None` — trade verbs only (`reject|hold|buy|revise_tpsl|sell|wait`
  if wait stays distinct; today wait aliases to hold — keep label wait in JSON,
  store as today).
- `tool: Literal["metrics","safety","dex","wallets"] | None`
- Remove tool names from `ThinkAction` and from `_ACTION_ALIASES` as destinations
  that mean “run tool”. **Delete** aliases like `"onchain" → "metrics"` as
  action aliases (optional: still reject clearly).
- Validators:
  - `reasoning` required (existing).
  - Require `action or tool`.
  - If `action` is a known tool string → `DecisionInvalid` (“use tool field”).
  - buy still needs levels; etc.
- Coach text: if error mentions tool/action confusion, short line:
  “use tool:\"metrics\" not action:\"metrics\".”

### 6.2 Grammar (`choices.py` → `decision_format`)

```text
properties:
  action: enum [phase trade verbs]          # optional (not in required)
  tool: enum [allowed_tools…]               # optional
  reasoning: string (required)
  narrative, profit_pct, loss_pct: as today
required: ["reasoning"]
# post-parse: at least one of action|tool
```

Pass `allowed_tools: list[str]` so suppress can shrink the `tool` enum.
Searching still requires profit/loss keys in grammar if that remains the 3b
workaround (strip on non-buy as today).

**Do not** put tool names in the `action` enum.

### 6.3 Parse path (`decision_from_message`)

- Read `action` and `tool` from JSON.
- If `action` in callable tool names → `DecisionInvalid` (no auto-translate).
- Build `ThinkDecision(action=…, tool=…)`.

### 6.4 Prompt assembly (`coin_prompt.py` + `trader.md`)

- Replace static tools section with `build_tools_section(allowed_tools)`.
- `assemble_trader_prompt(..., allowed_tools=..., phase=...)`.
- Phase blocks updated for the contract in §5.
- Vamp remains a fed info line inside/beside the tools section.

### 6.5 Graph think / apply (`graph.py`)

**`_think` after successful parse:**

```text
if decision.tool:
    # may also have action
    if decision.action in ("reject", "sell"):
        # ignore tool; fall through to normal action-only apply
        …
    elif decision.action in (None, "wait", "hold") or only-tool:
        # tool path first (or only)
        return await _run_tool(...); set suppress; 
        # if action was wait/hold, count look once for wait; tool-only: no look bump
    elif decision.action in ("buy", "revise_tpsl"):
        # go to apply with pending_decision including tool;
        # apply runs action then tool
        route apply
elif decision.action:
    # action-only — existing path; bare wait/hold → flag next_delay_sec=10
else:
    unreachable if validators ok
```

**Cleaner unified approach (recommended):**

1. Always `route: apply` with full decision (action and/or tool).
2. `_apply`:
   - If action in buy/reject/sell/revise/wait/hold → existing side effects.
   - If tool set and action not in {reject, sell}: call shared `_fetch_tool(kind)`
     (extract body of today’s `_refresh_metrics`), append event, emit live, set
     suppress.
   - For **buy + tool**: after successful buy + `_flush_vamp_events`, run tool
     fetch before return.
   - For **tool-only**: no trade side effects; just fetch + stop; **do not**
     increment search looks (pass a flag or skip steps bump — today steps bump
     in `_think` before apply; keep tool-only returning from `_think` via
     refresh helper **without** going through “decision look” accounting, same
     as current `_refresh_metrics`).

Practical split matching current code shape:

| Reply | Path |
|-------|------|
| tool-only | `_think` → `_refresh_metrics` (rename `_run_agent_tool`) → stop; no steps burn |
| wait+tool / hold+tool | `_think` → run tool (same helper) → also record wait/hold decision event → stop; steps += 1 for wait/hold |
| buy+tool / revise+tool | `_think` → apply route → `_apply` does trade then tool helper |
| reject+tool / sell+tool | `_apply` ignores tool |
| action-only | existing apply; bare wait/hold sets `next_delay_sec = 10` |

### 6.6 Suppress state

On `LoopDeps` or loop state:

```text
suppress_tool: str | None
next_delay_sec: float | None
```

- Set suppress in tool helper.
- `_think` entry: `allowed = CALLABLE - {suppress_tool}`.
- `_think` exit: if this think ran a tool, suppress=that tool; else clear suppress
  that was active at entry (one-think window done).
- `AgentLoop` sleep: if `next_delay_sec` set (bare wait/hold), use it (10.0);
  else `think_sec` / `idle_sec` as today; then clear override.

### 6.7 Live feed (`live.py`)

Print tool fetches as today (`metrics` / `safety` / …). When action+tool, show
both (decision line + tool block), e.g. buy then wallets.

### 6.8 Docs to update when implementing

- `docs/all-tools.md` — “How a call works” + replace Call control section; link here.
- `docs/PROJECT_OVERVIEW.md` — short loop note.
- This file — mark **Built** when done.

---

## 7. File checklist

| File | Change |
|------|--------|
| `decision.py` | Optional action; `tool` field; validators; no tool-as-action; coaches |
| `choices.py` | Grammar: trade `action` enum + `tool` enum; `allowed_tools` |
| `coin_prompt.py` | Dynamic catalogue from allowed tools |
| `prompts/trader.md` | Full contract + examples (`tool`, `action`+`tool`, bare wait) |
| `graph.py` | Paths in §6.5; suppress; buy+tool after buy; ignore tool on reject/sell |
| `runner.py` | Honour `next_delay_sec` (10s bare wait/hold) |
| `live.py` | Clear action+tool logging |
| `all-tools.md` / overview | Sync when built |

---

## 8. Implementation order

1. **Schema + grammar + parse** — new fields; reject tool-in-action; no alias.
2. **Prompt + dynamic catalogue** — teach `tool:` / action+tool / bare wait.
3. **Tool helper** — refactor `_refresh_metrics` → `_run_agent_tool`; suppress.
4. **Wire combos** — tool-only, wait/hold+tool, buy/revise+tool; ignore on reject/sell.
5. **Pacing** — 10s bare wait/hold in runner.
6. **Docs + smoke** — `--follow` on a PASS coin.

---

## 9. Acceptance checks

1. `{"tool":"safety",...}` alone works (searching and locked); result on next card.
2. `{"action":"wait","tool":"metrics",...}` one think does both; no empty wait-then-tool pair required.
3. `{"action":"buy","tool":"wallets",...}` buys and first locked think already has wallets (or wallets event before next think).
4. `{"action":"metrics",...}` fails validation / coach says use `tool`.
5. After tool N, next catalogue omits N; following think restores N.
6. Bare wait → ~10s to next think; tool-only does not use that 10s rule.
7. Tool-only does **not** eat a search look; three bare waits still hit the cap.
8. reject/sell + tool does not block close; tool ignored.
9. since-last always shows `%` and `in Ns` after the first peek.
10. No `{"action":"metrics"}` examples left in `trader.md`.

---

## 10. Decisions log

| Topic | Decision |
|-------|----------|
| Tool vs search-look budget | Tools **do not** count toward the 3-look cap |
| How tools are called | **`tool` field only**; never as `action` |
| Legacy `action: metrics` alias | **None** — invalid; prompt+grammar teach the new way |
| Tool without action | **Allowed** (continue focus + fetch) |
| Tool with any action | **Allowed**; result on next think when focus continues |
| buy + tool | **Yes** — buy then fetch for next/first locked think |
| reject/sell + tool | **Ignore tool** (no next think) |
| Bare wait/hold | **Allowed**; **10s** delay |
| Suppress | **One think**, then restore |
| Stray tool on buy | Not stray — supported |

---

## 11. Summary

Stop treating tools as alternate `action` values. The agent sets `tool` to fetch
info (alone or with any trade `action`). That removes the wait→tool double think,
allows buy+tool to prefetch the next card, adds a one-think cooldown, keeps bare
wait for price watching (10s), and leaves the search-look budget counting only
real decisions — not tool fetches. Implement with schema/grammar/prompt/loop
changes above; no legacy action=tool translation path.
