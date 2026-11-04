# Other streams — implementation plan

Research notes live in `docs/other_streams.md`. This file is the build plan: what to ship, in what order, and what changes in the daemon.

Checked again 4 Nov 2026: Raydium LaunchLab mint API is live and free; Alt-coin still resolves as StonkFun → CPMM; Meteora DBC `migration_damm_v2` account layout is confirmed (`base_mint` index 13, `quote_mint` index 14). Pump.fun `coins-v2` was geo-blocked from this host during the re-check (403), so do not treat it as the discovery stream.

## Goal

Stop limiting the migration backlog to SOL-quoted pump → PumpSwap coins. Accept and paper-trade graduations from every venue we care about, as **coins of different types** — same agent loop, same base card shape, with venue-specific extras when they matter (quote symbol, OTC stock basket, launchpad name, …).

Target coverage (end state):

1. Pump.fun custom-quote graduations (PUMP, xStocks, USDC, …) — **first**
2. Raydium LaunchLab graduations (Stonk.fun, letsbonk.fun, Hot.fun, Float, ZPAD, …) — all platforms, all quotes
3. Meteora dynamic bonding curve / “virtual curve” graduations — all migrations
4. OTC desks enrichment — when a coin is OTC, attach the stock/basket list to the first agent prompt

## Release philosophy (gradual)

Ship one venue family at a time. Do **not** turn on the next family’s trading until the previous family works end-to-end in the live daemon.

```text
Wave A  pump (all quotes)     → discover + filter + price + paper trade + agent card
         ↓ gate: live round-trip works; missing/filtered coins reviewed against your terminal
Wave B  LaunchLab + Meteora   → discover first, then price + paper trade for both
         ↓ gate: same end-to-end check
Wave C  OTC enrichment        → stock/basket on the card when the mint is OTC
         ↓ polish / docs / optional paid streams only if needed
```

Inside each wave: fix discovery → fix filters → fix price/fills → fix agent card → verify the whole path before moving on.

## What blocks us today

| Layer | Hard assumption | Where |
| --- | --- | --- |
| Ingest | PumpPortal `mint` is the coin | `ingest/models.py`, `ingest/pumpportal.py` |
| Filters | Pool owner must be Pump AMM; quote must be WSOL | `filters/pipeline.py` |
| Price | Resolve + vault math is PumpSwap + WSOL only | `price/resolve.py`, `price/live.py`, `price/snapshot.py` |
| Paper fills | Spot fill via PumpSwap reserves | `execution/fills.py` |
| Agent / backlog | Coin info assumes one mint + pump-ish pool | `backlog/store.py`, opening card |

Discovery without trade support is useless for the agent loop. Each venue needs **ingest → filter → price → paper buy/sell → agent card**.

## Target architecture

One normalised migration event, then venue-specific adapters.

```text
feeds (PumpPortal + Helius logs [+ optional Raydium poll])
        │
        ▼
normalize → { coin_mint, quote_mint, venue, pool, signature, platform? }
        │
        ▼
filters (mint safety + venue pool rules)
        │
        ▼
backlog → agent (one coin at a time)
        │
        ▼
price + paper fills via venue adapter (pumpswap | raydium_cpmm | meteora_damm_v2)
```

Venues:

| `venue` | Curve / launch program | Graduated pool program | Typical quotes |
| --- | --- | --- | --- |
| `pump` | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | PumpSwap `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | WSOL, PUMP, xStocks, USDC |
| `launchlab` | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | Raydium CPMM `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | WSOL, stocks, indexes, Bonk, … |
| `meteora_dbc` | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | DAMM v2 `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG` | WBTC, WSOL, … |

Decode cheatsheet (from IDL / live txs):

- Pump `migrate_v2`: account **2** = coin (base), **3** = quote, **10** = new PumpSwap pool.
- LaunchLab `MigrateToCpswap`: first accounts include `payer`, then **base_mint**, **quote_mint**; CPMM pool is among the cpswap_* accounts. Prefer “mint that is not the quote” + Raydium mint API cross-check when unsure.
- Meteora `migration_damm_v2`: account **4** = new DAMM pool, **13** = base_mint, **14** = quote_mint.

Known quote mint examples (not exhaustive): WSOL `So111…112`, PUMP `pumpCmXq…`, USDC `EPjFW…`, MSTRx `XsP7xz…`, SAMA4 `7V1evj…`, WBTC `3NZ9JM…`.

## Agent card philosophy

Same base prompt / information card for every coin. Venue and pair type only **add or swap fields**, they do not fork the agent into different personalities.

Examples of extras (not an exhaustive list):

- Pump non-SOL quote → show quote mint + symbol (PUMP, MSTRx, …) clearly on the first card
- LaunchLab → `venue` + `platform` (StonkFun, letsbonk, …) + quote
- Meteora DBC → `venue: meteora_dbc` + quote (e.g. WBTC)
- OTC desk coin → **list of stocks / reward basket the coin is paired to** on the first prompt (crucial)

Paper wallet stays SOL- and/or USD-denominated for every venue. Non-SOL quote fills convert quote → USD → SOL (exact helper researched during Wave A implementation).

---

## Decisions (locked 4 Nov 2026)

| # | Question | Chosen |
| --- | --- | --- |
| 1 | Discover which venues? | **All three:** pump (all quotes), LaunchLab (all platforms + all pairings), Meteora DBC (all migrations) |
| 2 | Build / trade order | **Pump first** end-to-end, then LaunchLab + Meteora, then OTC / polish |
| 3 | LaunchLab platforms | **Accept all** when Wave B ships (StonkFun, letsbonk, Hot.fun, …) |
| 4 | Paper PnL / wallet | **SOL and/or USD only.** Non-SOL quotes convert to SOL (via USD if needed). Exact conversion path researched during Wave A pump custom-quote work |
| 5 | Buy size | **Same mcap → SOL bands** (3 / 5 / 10). Non-SOL quote coins “move” the same as SOL-quoted ones |
| 6 | Pump global-fees ≥ 2 SOL floor | **Birdeye** `global_fees_paid` (vault estimate removed). Still ≥ 2 SOL hard floor |
| 7 | OTC desks | **Yes, when we reach Wave C.** On OTC coins, resolve and show the stock / basket list to the agent on the first prompt |
| 8 | Agent treatment | **All coins are coins.** Same base card; venue / OTC / non-SOL quote only change which extra fields are attached |

---

## Wave A — Pump.fun, all quotes

**Status (4 Nov 2026):** A0–A3 implemented in code. A4 (live gate vs your terminal) is on you.

Focus: make the current architecture handle every pump graduation (SOL + custom quotes) without breaking SOL coins. No LaunchLab / Meteora code required yet beyond shared model fields we will reuse later.

### A0 — Shared migration model

**Outcome:** One event shape that later waves can fill without another rewrite.

#### Steps

1. Extend `MigrationEvent` (and backlog `coin_info`) with:
   - `coin_mint` (authoritative)
   - `quote_mint`
   - `venue` (`pump` | `launchlab` | `meteora_dbc` | `unknown`)
   - `platform` (optional string; unused in Wave A except leave the field)
   - keep `mint` as an alias of `coin_mint` during the transition so old code does not break
2. Document the fields in ingest models / a short comment so Wave B does not invent parallel names

#### Done when

SOL pump path still runs; new fields exist and default sensibly (`venue=pump`, `quote_mint=WSOL` for classic coins).

---

### A1 — PumpPortal mint repair

**Outcome:** Frames that put the quote in `mint` become the real coin before filters run.

#### Steps

1. Add `src/archetype/ingest/repair.py` (or similar):
   - Input: PumpPortal raw + signature
   - For `pool` in `pump-amm` / `pump`: `getTransaction` on signature (`maxSupportedTransactionVersion: 1`), decode `MigrateV2`, take account **2** as `coin_mint`, account **3** as `quote_mint`, account **10** as pool when needed
   - If payload `mint` equals migrate account 3 (quote), replace with account 2
   - On missing signature or decode failure: reject with a clear reason (never invent the coin from the quote alone)
2. Call repair at the start of the filter hook (daemon path), before blacklists keyed on mint
3. Update `extract_pool_from_migrate_tx` to take an explicit quote mint (today it only looks for WSOL balances)
4. Fixture / manual checks:
   - Holocaust migrate `A7PLC7uc…` → coin `6eukYbG1…`, quote PUMP
   - Laura PumpSwap pool / migrate for MSTRx
   - Classic SOL migrate still resolves coin = payload mint, quote = WSOL

#### Done when

CLI / logs show the real coin mint for a PUMP-quoted or stock-quoted pump graduation, and SOL pump coins still behave as before.

#### Does not yet

Pass non-WSOL quotes through filters, or trade them.

---

### A2 — Filters for pump custom quotes

**Outcome:** Non-WSOL pump coins are not auto-rejected solely because the quote is not SOL.

#### Steps

1. Replace hard `quote_mint != WSOL` fail with pump venue policy:
   - allow any quote that is not the base (all pump quotes)
2. Generalise pool liquidity floor:
   - WSOL: keep current pool SOL floor
   - other quotes: quote vault amount → USD (helper), apply a USD floor equivalent to today’s SOL floor
3. Global-fees gate uses Birdeye `global_fees_paid` ≥ 2 SOL (decision #6); log reject reason clearly so live review is easy
4. LP / pool-owner checks stay Pump AMM for `venue=pump`

#### Done when

A Holocaust / Laura-class coin gets past filters (or fails for a documented reason other than “quote ≠ WSOL”).

---

### A3 — Price, paper fills, PnL conversion (pump)

**Outcome:** Peek, paper buy, TP/SL sell work for any pump quote; wallet stays SOL/USD.

#### Steps

1. Research and pick the conversion helper for quote → USD → SOL (Helius DAS / Jupiter price / existing price code — decide during implementation, keep it one small module)
2. Price path (`resolve_pumpswap_pool`, vault subscribe, snapshot):
   - stop requiring WSOL
   - FDV / mcap in USD via quote→USD
   - mark position value in SOL via USD / SOL USD
3. Paper fills: constant-product on base/quote vaults; debit/credit the **SOL** paper wallet using the conversion helper (not a multi-asset wallet)
4. Buy size: same mcap bands → 3 / 5 / 10 SOL (decision #5)
5. Live follow / peek / TP/SL: confirm vault websocket works when quote ATA is not WSOL
6. Agent card: same base card + clear `quote` / quote symbol when quote ≠ SOL

#### Done when

A live or replayed Holocaust / Laura-class coin can be filtered, queued, peeked, paper-bought, and TP/SL-sold end to end. Classic SOL pump coins unchanged.

---

### A4 — Wave A gate (do not skip)

Before any LaunchLab / Meteora work:

1. Run the daemon against live PumpPortal migrations
2. Compare against your trading terminal: missing coins, over-filtered coins, wrong mint / quote
3. Fix reject reasons / conversion bugs found in that review
4. Only then open Wave B

**Gate checklist**

- [ ] SOL pump: migrate → filter → peek → buy → sell still works
- [ ] Custom-quote pump: real coin mint in logs (not the quote)
- [ ] Custom-quote pump: paper round-trip with SOL-denominated PnL
- [ ] Agent first card shows non-SOL quote when relevant
- [ ] Fee floor / other filters reviewed against terminal (tune if needed)

---

## Wave B — LaunchLab + Meteora

Same “coins of different types” rule. Discovery for both can land together; trading can follow venue by venue inside the wave, but both must clear an end-to-end gate before Wave C.

### B1 — Helius log feeds (discovery)

**Outcome:** Graduations PumpPortal misses (especially Meteora; also LaunchLab) enter the same backlog.

#### Steps

1. Add `src/archetype/ingest/helius_logs.py`:
   - `logsSubscribe` on LaunchLab `LanMV9s…` and Meteora DBC `dbcij3…`
2. On log mentioning `MigrateToCpswap` / `migration_damm_v2` / `migrate_meteora_damm`:
   - `getTransaction` (`maxSupportedTransactionVersion: 1`)
   - decode → `coin_mint`, `quote_mint`, `pool`, `venue`
3. Dedup against PumpPortal by signature and by mint
4. LaunchLab platform label: `GET https://launch-mint-v1.raydium.io/get/by/mints?ids={mint}` (no key) → `platformInfo.name`
5. Wire feed start/stop into daemon lifespan; config flags `ingest_launchlab`, `ingest_meteora_dbc` (both on by default once Wave B ships)
6. Accept **all** LaunchLab platforms (decision #3) — no allowlist

#### Done when

A Stonk.fun / letsbonk graduation and a Meteora DBC graduation appear in the migration buffer with the real coin mint.

#### Credits

Each graduation ≈ 1 `getTransaction` (+ optional mint API HTTP). Existing Helius key is enough.

---

### B2 — Raydium CPMM price + paper trade

**Outcome:** LaunchLab graduates are markable and paper-tradable for **all** quotes (convert via the same Wave A helper).

#### Steps

1. `price/resolve_cpmm.py`: read CPMM pool vaults; cache like PumpSwap
2. Price tick + websocket on both vaults; FDV / mcap in USD; PnL in SOL via converter
3. Paper buy/sell adapter keyed by `venue=launchlab`
4. Filters for CPMM: owner `CPMMoo8…`; liquidity USD floor; skip pump-only fee vault gates
5. Buy size: same mcap bands
6. Agent card: same base + `venue: launchlab`, `platform: …`, `quote: …`

#### Done when

Alt-coin class (or a fresh LaunchLab graduate, any quote) can be peeked and paper round-tripped.

---

### B3 — Meteora DAMM v2 price + paper trade

**Outcome:** Virtual-curve graduates are tradable, not just listed.

#### Steps

1. Study DAMM v2 pool layout (`cpamdpZ…`) enough to read reserves / price
2. Resolve + tick + paper adapter for `venue=meteora_dbc`
3. Filters: pool owner, liquidity USD floor, skip pump-only gates
4. Quotes including non-WSOL (e.g. WBTC) via the shared USD → SOL converter
5. Agent card: same base + `venue: meteora_dbc`, `quote: …`

#### Done when

An Etchy-class coin can be peeked and paper round-tripped.

---

### B4 — Wave B gate

1. Live compare LaunchLab + Meteora migrations against your terminal
2. Confirm no double-enqueue with PumpPortal
3. Confirm SOL/USD paper PnL and buy bands behave like pump
4. Only then open Wave C

**Gate checklist**

- [ ] LaunchLab (any platform) discover → filter → peek → buy → sell
- [ ] Meteora DBC discover → filter → peek → buy → sell
- [ ] Platform / quote fields on agent card
- [ ] Dedup with PumpPortal verified
- [ ] Wave A pump path still green

---

## Wave C — OTC desks enrichment

**Outcome:** OTC is not a feed. When a focused mint (any venue) is an OTC desk coin, the agent’s **first** card includes the stock / reward basket list.

#### Steps

1. On first think / snapshot: `GET https://otcdesks.cash/api/coins` (or per-mint if they add one) — no key today
2. Match mint → attach reward symbol(s) / basket / stock names to coin info (not as socials)
3. Surface that list prominently on the opening prompt (decision #7)
4. Do not enqueue from this list alone
5. Note: some OTC coins are pump, some may be Meteora DBC — enrichment must be venue-agnostic

#### Done when

An OTC mint in focus shows the paired stocks / basket on the first agent card; non-OTC coins unchanged.

---

## Polish (after Wave C, or anytime a wave needs it)

1. Dedup, rate limits, and clear reject reasons per venue
2. Update `docs/PROJECT_OVERVIEW.md`, `README.md`, `docs/other_streams.md` “what the repo does today”
3. Config knobs in `config/default.yaml` + `.env.example` (no secrets beyond Helius)
4. Follow CLI: print venue + quote (+ platform / OTC basket when present)
5. Only if Helius logs are insufficient: evaluate Bitquery / PumpDev paid streams

---

## Milestone table

| Milestone | Wave | Ships | Depends on |
| --- | --- | --- | --- |
| **M1** | A0–A1 | Shared model + PumpPortal mint repair | Decisions (done) |
| **M2** | A2–A3 | Pump custom-quote filters + price + paper trade + card | M1 |
| **M3** | A4 | Live gate vs your terminal; fixes | M2 |
| **M4** | B1 | LaunchLab + Meteora discovery feeds | M3 |
| **M5** | B2 | LaunchLab CPMM trade (all quotes) | M4 |
| **M6** | B3 | Meteora DAMM trade (all quotes) | M4 |
| **M7** | B4 | Wave B live gate | M5 + M6 |
| **M8** | C | OTC stock/basket on first prompt | M7 |
| **M9** | Polish | Docs, config, follow CLI, optional paid streams | as needed |

Next implementation instruction should start at **M1** (A0 + A1).

---

## What we do **not** need for the core plan

- New PumpPortal product
- Bitquery / PumpDev paid keys (optional later)
- Raydium auth / `ray-token` (mint/history reads are anonymous)
- OTC API key (public list today)
- A map from quote mint → coin (impossible; always use the migrate signature)
- Multi-asset paper wallet (rejected; SOL/USD only)

Existing keys already enough to start: `ARCHETYPE_HELIUS_API_KEY`, current PumpPortal key.

---

## Risks

- PumpPortal may omit `signature` on some frames → those cannot be repaired.
- Helius credits rise with log + tx decode; keep PumpPortal as the pump primary and logs for LaunchLab / Meteora.
- Quote → USD → SOL conversion must be solid in Wave A or every later venue inherits bad marks.
- LaunchLab and Meteora pool layouts change over time; pin decode to IDL versions and add a fixture tx per venue.
- UK / geo blocks on `coins-v2` mean metadata enrichment may fail even when on-chain discovery works — treat HTTP metadata as best-effort.
- Fee floor kept as-is may reject some custom-quote coins; catch via terminal review in A4, not by guessing upfront.
