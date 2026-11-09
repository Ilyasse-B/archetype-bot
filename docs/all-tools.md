# Phase 5 — tools

The old “early tools” phase and the old “expand the catalog” phase are one phase. Build **live metrics and price-change first**. X and the other lookups come after those.

More tools will show up later. Add them here when we decide to, then build them. Do not invent extras ahead of that.

## How a call works

- The agent chooses which tool to call. It calls **one tool at a time**, never a batch.
- It can call a tool on **wait** (still looking, not bought) or on **hold** (already in the trade). Same rule for both.
- The standing prompt stays a short catalogue: tool name, one line on what it is, and when to call it. Field meanings are not in that list.
- When it picks one, the next think gets that tool’s result. The first time that tool returns, each field has a short note beside the number. Later calls for the same tool are the numbers only. Then it thinks again.
- **Cheap** facts can run on their own every think and sit in the packet, as long as they stay small. Extra on-chain numbers for the coin in focus are the example. If a call is slow, paid, or fat, the agent asks for it.
- Do not push a mid-think price alert. The since-last-peek line already covers a move, and an alert would interrupt the think.

Labels on each tool:

| Label | Meaning |
|-------|---------|
| **fed** | Deterministic. We run it and put the result in the think packet. The agent does not ask. |
| **agent** | Only runs when the agent calls it. |
| **both** | Runs in the background (or once when we focus the coin) and also when the agent asks. |

## Tool catalogue

The catalogue lives in `src/archetype/llm/prompts/trader.md`, under the buy / wait / reject examples. It is part of the system prompt, so it stays on every think. There is no separate tools file.

While the agent is still deciding whether to buy, that prompt includes buy, reject, and wait. After a buy, those examples and the before-buy instructions are removed. The hold paragraph then says the agent can call tools, and the catalogue below is the detail.

One line per tool: the action name, a rank from 0 to 1 (higher matters more), what it returns, and when to call it. No field definitions. `metrics` is 0.5, `safety` is 0.8, `dex` is 0.25, `wallets` is 0.4. New tools get a line when they are built.

Field notes are not on the opening card. They sit inline on the first result for that tool, beside each number. `metrics` and `safety` also have one short closing line on how to use the block. `dex` and `wallets` are the values and their notes, with no extra closing line. A later call for the same tool is the numbers only.

A missing key or a failed request is `unavailable` on that line. No second source.

## Call control (later)

Not part of building tool 3. Note it so we do not forget.

Once a few tools exist, stop the model from wasting looks:

- Do not call the same tool again while its last result is still the latest read.
- Do not call the same tool again inside a short cooldown.
- Early in a search, steer it at `metrics` and `safety` first. Narrative tools (website, X, Gemini) come after it has a safety read and still wants the coin. Those tools build the thesis. They do not decide if the coin is safe.

This is a nudge on top of the catalogue. It is not a second trader, and it does not pick the trade.

## Already in the loop (do not rebuild)

These are not Phase 5 work. They already feed the agent.

| What | Label | What it does |
|------|-------|----------------|
| Filter snapshot | **fed** | Name, symbol, description, mcap, socials. Taken once when the migration passes. Global fees stay a deterministic filter and are not shown to the agent. |
| Coin snapshot | **fed** | Once, on the first think, then replayed. ATH (Solana Tracker), 1h volume / liquidity / created (Jupiter), coin type (Tracker cashback/holder-reward flags; Donate.gg charity; Helius fee redirect / tokenized agent). No pump.fun frontend. Livestream omitted. Mayhem is not included. Non-SOL pairs also get a `pairing` line on the card. |
| Price peek | **fed** | Latest market cap off the focused coin's live vault websocket, plus PnL if we already hold it. Not a separate Helius poll. |
| TP/SL engine | — | Daemon sells when the levels hit. Not a research tool. The agent sets the levels; it does not watch every tick. |

## Build order

### 1. Price change — **fed** every think (built)

No agent call. Asking would cost a round trip, and the peek already happened.

- **Every think:** the packet includes how **market cap** moved since the last peek (percent, seconds, from → to in dollars). Any size. The first look says none yet. The agent is not shown token price.
- The vault websocket starts when the agent is on the coin (the first think), not while it is still sitting in the backlog. Peek and TP/SL read that socket.
- There is **no fast-move alert**. A big move shows up on the next think as the since-last line. It does not get its own event, and it does not cut a think short.
- Do not paste every tick.

This comes before X. A coin dying or running shows up on the next peek, not as an interrupt.

### 2. Live on-chain metrics — **both** (built, rank 0.5)

Fed once, on the first think, in that think’s changing message. That first block has a short note beside each field, then a line saying the block is a gauge and does not decide the trade. The numbers are not written into the opening card, so a later think does not replay a stale read. A later `metrics` reply is the numbers only.

The agent can reply `metrics` while searching or locked. That call does not count as a look. The next think gets a new HTTP read.

Uses Jupiter (no key) and Solana Tracker (`ARCHETYPE_SOLANA_TRACKER_API_KEY`). No pump.fun HTTP. No Helius. If one call fails, that line says `unavailable`.

Fields the agent sees: `holders`, `top10_pct`, `dev_pct`, `snipers`, `bundlers`, `trades_5m`, `deployer`.

- Holders / top-10 % / deployer launched+graduated: Jupiter `tokens/v2/search` (`holderCount`, `audit.topHoldersPercentage`, `audit.devMints`, `audit.devMigrations`).
- Dev % / snipers still holding / bundlers: Solana Tracker `GET /tokens/{mint}` `risk.*`.
- 5m trades: Tracker `GET /stats/{mint}` `5m` when available; else Jupiter `stats5m` (traders instead of buyers/sellers).

Global fees already paid are not in this tool. Total fees are tool 3 (safety), from Solana Tracker.

### 3. Safety — **agent** (built, rank 0.8)

One action, `safety`. The agent asks. Not on the first prompt. One result, a few lines. The old one-call legitimacy tool is this tool. Rugcheck’s score and risks are the score.

Three HTTP calls, no Helius. Anything not listed here is discarded, including wallet lists, charts, ATH, pool rows, LP lock, and the insider graph.

**Rugcheck, no key.** `GET https://api.rugcheck.xyz/v1/tokens/{mint}/report`

- `score`: `score_normalised` (0–100, lower is safer).
- `risks`: each risk’s name and level. Empty means none.
- `insider_networks`: how many networks, how many are still active (`activeAccounts`), and the % of supply they still hold. Shape: `3 networks with 15 active wallets holding 5.6% supply`. Total wallet count (`size`) is not shown. That percent is `currentHolding` divided by `token.supply` on this same report. No extra call.

**Streamflow, no key.** `GET https://api-public.streamflow.finance/v2/api/locks/solana/{mint}/active/` and the vesting twin `.../vesting/solana/{mint}/active/`.

- `locked`: total % still locked, across how many locks. Percent is `amount - withdrawnAmount`, summed, divided by the supply from the Rugcheck report. A lock whose `endDt` or `expiryDt` is already past is not counted. The API does not give a recipient wallet on a plain lock, so the count is lock escrows, not holder addresses. `0% across 0` when both lists are empty.

**Solana Tracker.** `GET https://data.solanatracker.io/tokens/{mint}` with `x-api-key` (`ARCHETYPE_SOLANA_TRACKER_API_KEY`). One call covers all four lines. If `risk.bundlers` is missing, `GET /tokens/{mint}/bundlers` supplies `total` and `percentage` only.

- `snipers`: only if they still hold supply (`totalPercentage` > 0). Then the count and that percent. If they have sold out, omit the line.
- `insiders`: count and % of supply they still hold.
- `bundlers`: count, and % of supply they still hold. Example shape: `592 bundlers holding 10.5%`.
- `fees_sol`: `risk.fees.total`.

If the Tracker key is missing, those four lines say `unavailable`. Do not fill them from the top-50 holder list.

The first `safety` result has a short note beside each field, then a line saying to weigh it with everything else and to reject or sell on it alone only when the coin clearly looks dangerous. A later `safety` result is the numbers only. The recent-events line is the block itself, starting at `safety:`.

### 4. Dexscreener paid check — **agent** (built, rank 0.25)

Action `dex`. Rank 0.25. Not on the first prompt. Two calls, no key: `GET https://api.dexscreener.com/orders/v1/solana/{mint}` and `GET https://api.dexscreener.com/token-pairs/v1/solana/{mint}`.

The agent only sees:

- `dex paid` — true when the orders list has an approved `tokenProfile`. Empty orders is false. A failed orders call is `unavailable`, not false.
- `active boosts` — `boosts.active` on the pair (the live count). Not the sum of boost purchases. A failed pairs call is `unavailable`.

The first result has a short note beside each field. A later result is the two values only. The recent-events line is the block itself, starting at `dex:`. The call does not count as a search look.

Links, the image, and the banner are stored on the backlog coin and are not shown. Websites and socials are added only when the URL is not already stored (`twitter`, `telegram`, `website`, or `extra_links`). `twitter.com` and `x.com` count as the same link. `imageUrl` and `header` are saved under those names. Pair price, market cap, volume, and liquidity are not used.

### 5. Tracked wallets — **agent** (built, rank 0.4)

Action `wallets`. Rank 0.4. Not on the first prompt. The agent asks before or after a buy. Does not count as a search look.

We keep a scored KOL list in `data/kol_wallets.db` (wallet + rating 0–1). On a call, one Helius `getProgramAccounts` for the mint intersects that list. No Solana Tracker credits on the hot path.

The agent only sees:

- `tracked` — how many wallets from our list hold this coin (balance > 0).
- `avg_rating` — mean rating of those hits (0–1, higher better). `none` when the count is 0.

No wallet addresses or names. The first result has a short note beside each field (including that this is a weak side signal, not a core buy/sell reason). A later result is the two values only. The recent-events line is the block itself, starting at `wallets:`. A missing Helius key or a failed read is `wallets: unavailable`.

### 6. Vamp / copycat scan — **alert (fed / interrupt), not agent-callable**

Built. There is **no** `{"action":"vamp"}`. While focused on one coin, new **filter-PASS** migrations are compared (name / ticker / shared social URLs) against the focus. Pairing is shown for the agent to judge — the detector never marks a copy as “stronger”. Not a second trading universe: the agent does not buy the copy from this alert.

- **Searching:** buffer hits only (no interrupt, not injected into searching thinks).
- **Buy:** flush buffered hits into the first locked think as `vamp` events (header says **selling**).
- **Locked + new hit:** append event, live `vamp` line, cancel in-flight think with a **vamp** reason (not TP/SL close), then rethink. Bag stays open.
- **Reject / drop / idle:** clear the in-memory vamp backlog.
- **Hold / revise after a vamp:** treat as “not better” and drop those vamp mints from the **migration** backlog so FIFO will not feed them next. A sell leaves them queued.

Agent block: header + `name`/`symbol` + `matched` + optional `pairing`, each with bracket field notes like other tools, plus a line that the JSON reply must include `reasoning` for the post-vamp decision. No mint or overlap line on the prompt. When a vamp event is in the think window, the decision grammar requires non-empty `reasoning`.

**Later revisit (with X tools):** extend matching so linked socials can mean reply / quote / reply-chain of the focus tweet, not only exact URL or status-id equality. Track that under the X work and `docs/vamp-tool.md` §13.

Rank ~0.9 informational catalogue line (not callable). Full design: `docs/vamp-tool.md`.

### 7. Backlog peek — **agent**

Read the pending migration queue without consuming it. Other coins that just graduated, as context only. Still one trade.

### 8. Website legitimacy — **agent**

Only when the metadata has a website. The backlog already stores the real website URL (the first card only says that a website exists). Check that link with a free online checker (is it a fresh domain, a template, a dead page). The off-chain JSON can also have `video` and `coin_community`; mention those if they are set. Telegram stays a stored link unless we later add a real Telegram tool.

### 9. X — **agent** (several tools, still one call each)

v1 only looks at accounts and tweets already linked on the coin. The backlog stores the real twitter URL; the first card only says that twitter exists. Broader X search is not this step.

- **Linked account:** core info for the linked account, plus tweets that point at this coin.
- **Crowd:** replies under those tweets, community, follower picture.
- **X metrics API:** if a public API can summarize an account (growth, smart followers, flags), use that instead of scraping. Candidates to check when we build this: alphagate.io, uxento, j7. Use one that has a real API. Skip the ones that do not.

### 10. Gemini question — **agent**

A single “ask anything” call to the Gemini API. The agent uses it like a web search: narrative, news, “what is this meme”, anything the other tools do not cover. The question and a short answer come back. It is not a second trader and it does not place the buy.

## Not in this list yet

Charts, KOL trackers, catalysts, and anything else we have not named stay out until we add them here on purpose.

Mayhem mode is skipped on purpose. Those coins are not in the first card and they are not a tool.

The dev’s holding percent, and how many coins they launched and how many graduated, stay on the metrics tool. There is no separate dev wallet tool. Reply count was left off the metrics block on purpose. Safety, locks, and the Rugcheck score stay tool 3. They are not new tools.
