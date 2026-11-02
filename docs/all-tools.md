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

One line per tool: the action name, a rank from 0 to 1 (higher matters more), what it returns, and when to call it. No field definitions. `metrics` is 0.5, `safety` is 0.8, `dex` is 0.25. New tools get a line when they are built.

Field notes are not on the opening card. They sit inline on the first result for that tool, beside each number. `metrics` and `safety` also have one short closing line on how to use the block. `dex` is the two values and their notes, with no extra closing line. A later call for the same tool is the numbers only.

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
| Coin snapshot | **fed** | Once, on the first think, then replayed. ATH, 1h volume, liquidity, livestream, created time, and coin type (creator fees, cashback, holder rewards, charity, fee redirect, tokenized agent). A charity coin also gets the Donate.gg name, mission, website, and country. Mayhem is not included. |
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

No API key. No Helius. If one call fails, that line says `unavailable`. There is no fallback source.

Fields the agent actually sees: `holders`, `top10_pct`, `dev_pct`, `snipers_in_top`, `bundlers_in_top`, `trades_5m`, `deployer`. Reply count and the 1-hour trade window were dropped.

- Holders, top-10 % of supply, creator % in that list, and sniper / bundler counts inside the same list: `GET https://frontend-api-v3.pump.fun/coins/top-holders/{mint}`. `limit=200` still returns about 50 wallets. Those flags are not a count across every holder. That is tool 3.
- Whether price is being pushed, last 5 minutes only: `GET https://swap-api.pump.fun/v1/coins/{mint}/market-activity` (`numBuys`, `numSells`, `numBuyers`, `numSellers`, `priceChangePercent`). The same payload also has 1h, 6h, and 24h. Those stay off so the block stays short.
- Creator wallet and raw supply (for the percents): `GET https://frontend-api-v3.pump.fun/coins-v2/{mint}`. Reply count is on that payload and is not shown.
- Deployer history: `GET https://frontend-api-v3.pump.fun/coins-v2/user-created-coins/{creator}` (`count`, and how many of the returned coins have `complete`). One page. If the creator has more coins than the page, the line says the graduated count is only among the latest page.

Global fees already paid are not in this tool. Total fees are tool 3, from Solana Tracker. The creator-vault read used by the migrate filter is a different number, and it is not used here.

`GET /trades/count/{mint}` and `GET /trades/all/{mint}` on frontend-api-v3 reject the request (they want a `chainId` that did not match the coins-v2 chain id we tried). Buy and sell counts come from market-activity instead.

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

### 5. Tracked wallets — **both**

We keep a list of wallets we care about. When a coin is in focus, check whether any of them hold it.

- A hit is pushed into the next think (wallet label + size, not a full holder dump).
- The agent can ask again later in the trade.

### 6. Vamp / copycat scan — **agent**

While focused on one coin, look at new launches that copy its name or narrative. Those can dump the coin we are in. This is not a second trading universe: the agent does not buy the copy.

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
