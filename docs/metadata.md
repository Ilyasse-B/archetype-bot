# Pump.fun coin metadata and modes

Research only. Nothing here is wired into the agent yet.

The agent’s first card is still name, symbol, description, which social channels exist, and the buy size. Market cap arrives on later looks. The modes below (charity, cashback, holder rewards, fee redirect, tokenized agent, mayhem) are not in that card, and most of them are not in the off-chain metadata JSON either.

## What we already have

The migration feed is PumpPortal `subscribeMigration` on `wss://pumpportal.fun/api/data`. That stream is free. We keep mint, name, symbol, pool, and signature from the message. Mode flags are not part of that parse.

Off-chain metadata is Helius DAS `getAsset`, then an HTTP GET of the JSON URI (IPFS or a host like j7tracker). That JSON is the usual pump shape: `name`, `symbol`, `description`, `image`, `twitter`, `telegram`, `website`, sometimes `video`, `createdOn`, `showName`. Those fields describe the coin. They do not say where fees go.

The backlog stores more than the prompt shows: the twitter / telegram / website URLs, image, deployer, and the fee estimate. The prompt only gets the list of social names (`twitter`, `website`) and a description cut at 400 characters.

Our bonding-curve decoder stops at the creator pubkey (bytes 49–81). It does not read `is_mayhem_mode`, `is_cashback_coin`, or `is_holder_reward`. Those were appended later. Official docs say the curve grew from 81 bytes to at least 82 for mayhem, and later flags sit after that. Use the Pump SDK decoder (`PumpSdk.decodeBondingCurve` / `PumpAmmSdk.decodePool`) rather than a new hardcoded offset. Older accounts are shorter; a missing byte means false.

## Sources checked

| Source | What it is | Useful for modes? |
| --- | --- | --- |
| [PumpPortal real-time data](https://pumpportal.fun/data-api/real-time) | Websocket. `subscribeNewToken` and `subscribeMigration` are free. `subscribeTokenTrade` and `subscribeAccountTrade` cost 0.01 SOL per 10,000 messages and need a key with a wallet holding at least 0.02 SOL. | No. Migrations do not carry coin type. Trade messages are trades, not a mode catalog. |
| [BankkRoll/pumpfun-apis](https://github.com/BankkRoll/pumpfun-apis) | Unofficial spec of pump.fun HTTP APIs, including a 2026-06-17 capture. Base URL `https://frontend-api-v3.pump.fun/`. | Yes for discovery. Lists `GET /coins-v2/{mint}`, `GET /coins-v2/{mint}/mayhem-state`, `GET /coins/mayhem-mode`, `GET /v1/fee-sharing/account/{id}/shares`, `GET /docs/charitycoins`, `GET /docs/mayhem-mode`. Many captured routes claim a JWT. `coins-v2` answered without one from a server fetch. |
| [pumpdev.io](https://pumpdev.io/welcome) | Third-party trading and websocket API, same subscribe methods as PumpPortal, plus lightning create/trade. | Create body can set mayhem, cashback, and tokenized agent. It is a launcher, not a reader of an existing coin. Not needed for the prompt. |
| [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) | Official instruction docs. | Yes. This is the source of truth for on-chain flags. |
| [pump-fun/pump-fun-skills](https://github.com/pump-fun/pump-fun-skills) | Official agent skills. Coin-fees and create-coin. | Yes for how to tell fee destination apart, and for the `coins-v2` URL. |
| [pump.fun/docs](https://pump.fun/docs/fees) | Product docs: fees, charity, mayhem, tokenized agent. | Yes for what the mode means. Thin on account layouts. |
| Helius | `getAccountInfo` / `getMultipleAccounts` (1 credit). DAS `getAsset` is 10 credits. | Yes. One batched account read can cover the flags that `coins-v2` leaves out. |

`coins-v2` is not a documented public API. Pump’s own skill says it is CORS-blocked in the browser and that `token_program` on it can be stale, so the mint owner should still be read on-chain. A bare `curl` from this machine got HTTP 403. A server-side fetch with a normal fetch client got JSON. Treat it as unofficial and check it before relying on it.

## Coin modes

These are separate switches. A coin can be more than one (for example mayhem plus a fee split). “Cashback” in casual speech covers two different on-chain things.

### 1. Creator keeps the fee (default)

Creator fee on each trade goes to the coin creator’s vault.

- Bonding curve: `creator` is a normal wallet. No `sharing-config` account for the mint.
- HTTP: `coins-v2` `creator` is that wallet. `is_cashback_enabled` and `is_holder_reward` are false.
- Source: [COLLECT_CREATOR_FEE.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/COLLECT_CREATOR_FEE.md), [coin-fees skill](https://github.com/pump-fun/pump-fun-skills/blob/main/coin-fees/SKILL.md).

### 2. Cashback (old). Fees back to the trader who just traded

The creator fee on that user’s own volume is paid back to them as cashback, not to the creator. They claim it. This is not “fees to holders.”

- On-chain: `BondingCurve.is_cashback_coin`. PumpSwap pool has the same idea once graduated.
- HTTP: `coins-v2` field `is_cashback_enabled` (bool). Seen live on a graduated coin.
- Status: **deprecated.** `create_v2` now rejects `is_cashback_enabled = true`. Old cashback coins still trade and still pay cashback. New coins that want to pay the community are holder-reward coins instead.
- Source: [PUMP_CASHBACK_README.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_CASHBACK_README.md), [HOLDER_REWARDS_README.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/HOLDER_REWARDS_README.md).

### 3. Holder rewards (current “fees to holders”)

The creator fee is set aside for people who hold the coin. Pump pays it out. Holders do not call a claim instruction. The creator has nothing to collect. The choice is made at creation and does not reverse. A normal coin can later be converted by pump’s CTO process; a holder-reward coin cannot go back.

- On-chain: `BondingCurve.is_holder_reward` and, after graduation, `Pool.is_holder_reward`. The curve’s `creator` field is a pump-controlled address, not the human who launched it. `CreateEvent` / `CreatePoolEvent` also carry the bool.
- HTTP: `coins-v2` field `is_holder_reward` (bool). Seen live.
- Source: [HOLDER_REWARDS_README.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/HOLDER_REWARDS_README.md), [COIN_CREATION.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/COIN_CREATION.md).

### 4. Fee redirect / fee sharing

The creator fee is split across up to 10 addresses. Shares are in basis points and must add up to 10,000. This is how a developer sends fees to another wallet, a github user, an X account, or a mix. It can be changed until the admin is revoked, then it is locked.

- On-chain: one PDA per mint, seeds `["sharing-config", mint]`, program **Pump Fees** `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`. Account holds `admin`, `admin_revoked`, `status` (active or paused), and `shareholders[]` of `{address, share_bps}`.
- When sharing is on, the bonding curve’s `creator` (and the pool’s `coin_creator` if it has graduated) is rewritten to that sharing-config address. The creator vault seed becomes `["creator-vault", sharing_config]`, not the human wallet. That is also why a per-creator vault balance is a bad “fees paid by this coin” number.
- If the account is missing, fees are not shared.
- HTTP: BankkRoll lists `GET /v1/fee-sharing/account/{accountId}/shares` on `frontend-api-v3`. Not verified live here. The deterministic read is `getAccountInfo` on the PDA.
- Source: [CREATOR_FEE_SHARING.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/CREATOR_FEE_SHARING.md), [Sharing Configuration](https://deepwiki.com/pump-fun/pump-public-docs/5.2-sharing-configuration).

### 5. Charity

Not a bool on the bonding curve. It is a field on `coins-v2` when the coin is a charity coin.

Live check, 13 Oct 2026, mint `3sKaoPryg4KrXubLWtoqF7MznLT7QNqKRxMMkkxTpump` (Save The Seals): `GET https://frontend-api-v3.pump.fun/coins-v2/{mint}` returned `is_charity: true` and `verified: true`. The earlier kimi sample (`3vknzB6x…pump`) did not include `is_charity` at all. Treat a missing field as “not marked,” and only `true` as charity.

Pump’s product docs say creator fees for a charity coin are routed through [Donate.gg](https://pump.fun/docs/charitycoins). Only SOL-paired and USDC-paired coins can add a charity. Donate.gg converts and pays the nonprofit, and takes its own fee (the live config below stored `feeBps: "2000"`, which is the 20% API fee). Pump says it does not custody those funds after they are routed.

The name of the charity is not on `coins-v2`. Donate.gg has it, and the lookup by mint is public. See the Donate.gg section below.

- Source: [Charity coins disclaimer](https://pump.fun/docs/charitycoins), [Donate.gg token by address](https://docs.donate.gg/api/charity-tokens/token-address-route), [Donate.gg donation configs](https://docs.donate.gg/api/donation-configs).

### 6. Tokenized agent (“agent mode”)

A creator setting that automates buybacks. It is not an AI trader, and it is not mayhem mode.

A percentage of assets sent to a per-coin deposit address is used to buy the coin and burn it, on an hourly schedule. The creator sets that percentage at launch and can change it later. The rest can be claimed by the creator. Deposit assets start as SOL, USDC, USDT, and USD1. The buyback authority is controlled by pump. An agent coin must be created with an initial buy.

- Set at creation by appending an `agent_initialize` instruction (`buyback_bps`, 5000 = 50%) from `@pump-fun/agent-payments-sdk`, after `create_v2`.
- On-chain check: the token-agent-payments account for that mint exists (`getTokenAgentPaymentsPDA(mint)` in that SDK). That account holds the buyback rate and the payment authority. This repo does not yet have that program id pinned; take it from the SDK IDL when we build this, do not guess.
- HTTP: not on the live `coins-v2` sample. Pump’s create API (`POST https://fun-block.pump.fun/agents/create-coin`) accepts `tokenizedAgent` and `buybackBps`, but that builds a launch transaction. It does not describe an existing coin.
- Source: [Tokenized agent disclaimer](https://pump.fun/docs/tokenized-agent-disclaimer), [create-coin skill](https://github.com/pump-fun/pump-fun-skills/blob/main/create-coin/SKILL.md), [COIN_CREATION.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/COIN_CREATION.md) (`tokenized_agent_buyback_bps`).

### 7. Mayhem mode

Optional at creation only. Cannot be turned on later. For the first 24 hours an agent wallet trades the coin in a random walk. The launch mints an extra 1,000,000,000 tokens (2,000,000,000 total) for that. Unsold tokens are burned after 24 hours. The agent is not guaranteed to trade every mayhem coin. Protocol fees from these coins go to a separate recipient and are not on fees.pump.fun.

- On-chain: `BondingCurve.is_mayhem_mode` and the same flag on the PumpSwap pool. Mayhem program `MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e`. State PDA seeds `["mayhem-state", mint]`.
- HTTP: BankkRoll lists `GET /coins-v2/{mint}/mayhem-state` and `GET /coins/mayhem-mode`. The flag was **not** on the `coins-v2` body sampled below, so do not assume the main coin object includes it.
- Public agent wallet (docs, 12 Nov 2025): `BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s`. Fee recipient: `GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS`.
- Source: [Mayhem mode](https://pump.fun/docs/mayhem-mode), [pump-public-docs README on the byte-size bump](https://github.com/pump-fun/pump-public-docs/blob/1059c0d9/README.md), [COIN_CREATION.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/COIN_CREATION.md).

## Other metadata worth attaching

`GET https://frontend-api-v3.pump.fun/coins-v2/{mint}` on `3vknzB6xHZ3ZGpGFpG4CqSoJhhAmbgvFEgUZ5oS6pump` (kimi, already graduated) returned these fields. The official skill’s example is shorter. This live body is the fuller one.

Identity and text, which we mostly already have from DAS + URI:

- `name`, `symbol`, `description`, `image_uri`, `metadata_uri`
- `twitter`, `website` (no telegram on this coin)
- `creator`, `created_timestamp`, `show_name`, `nsfw`, `reply_count`
- `is_currently_live`, `livestream_ban_expiry` (a livestream flag, not a fee mode)

Market, which we already compute ourselves from the pool:

- `usd_market_cap` / `market_cap_usd`, `ath_market_cap`, `volume_1h_usd`
- `complete`, `pump_swap_pool` / `pool_address`, reserves

Mode and pair, which we do **not** give the agent:

- `is_cashback_enabled`, `is_holder_reward`
- `quote_mint` (`11111111111111111111111111111111` means native SOL; USDC is `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
- `boost_mode` (this coin was `"COMPLETED"`)
- `transfer_fee_bps`, `transfer_hook_program` (Token-2022 extras; both empty here)
- `program` (`pump`), `token_program` (do not trust this field; read the mint owner)
- `inverted`, `is_banned`, `verified`, `security_verdict` (pump’s own moderation blob: `verdict`, `reasons`)

`is_charity` is on this object when the coin is a charity coin (seen `true` on Save The Seals). It was absent on the kimi sample. Still not on `coins-v2`: the charity’s name, the fee-sharing shareholder list, tokenized-agent buyback, and mayhem.

Off-chain JSON can still mention a fee deal in the description (“Fees to @deanmlittle via UsePaid” on this coin) and put a fee site in `website`. That is a hint, not a mode. The sharing-config account is the check.

Fee rates themselves are not per-coin for SOL and USDC pairs. Bonding-curve total is 1.25% (creator 0.30%, protocol 0.95%). After graduation, PumpSwap canonical fees depend on market cap. A custom quote (not SOL or USDC) can have its own `creator_fee_bps`. Source: [pump.fun fees](https://pump.fun/docs/fees), [COIN_CREATION.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/COIN_CREATION.md).

## How to read it, and what it costs

One coin, at migrate time:

1. `GET coins-v2/{mint}` from our backend. No Helius credits. Gives description, social URLs, quote mint, cashback, holder-reward, livestream, ATH, 1h volume. Can 403 or change shape without notice.
2. One Helius `getMultipleAccounts` (1 credit) on:
   - bonding curve `["bonding-curve", mint]` under `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` — mayhem, cashback, holder-reward, and who `creator` points at
   - PumpSwap pool, if graduated — same flags, plus `coin_creator`
   - sharing config `["sharing-config", mint]` under `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` — missing means no split; present means the shareholder list and bps
   - mayhem state `["mayhem-state", mint]` under `MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e` — present confirms mayhem even if the curve decoder is behind
   - token-agent-payments PDA, once the program id is taken from the SDK — present means agent mode, and the account has `buyback_bps`

That is one extra Helius credit on top of the filter reads we already do. DAS `getAsset` is a separate 10 credits and we already pay it for name and URI.

Charity name is the extra HTTP call: `GET https://www.donate.gg/api/v1/101/tokens/{mint}` when `is_charity` is true. No API key. A 404 means Donate.gg does not have that mint.

PumpPortal’s paid trade socket does not help here. PumpDev’s websocket is the same kind of stream.

## What a prompt line could look like

Not built. The useful sentence is short:

- pair: SOL or USDC (or a custom quote)
- fees: creator wallet, or cashback (legacy), or holder rewards, or a split (`address` + percent, and “charity” only when Donate.gg says so)
- agent: off, or on with buyback percent
- mayhem: on or off
- social URLs and description, which we already fetched

Do not paste `security_verdict`, raw reserves, or the whole `coins-v2` body. Those are large and the agent already has market cap from the live price.

## More than the modes

Checked live on 13 Oct 2026. These are extra facts about a coin, not fee modes. None of them need a new subscription. The ones that failed are listed at the bottom so we do not build on them.

### Already fetched, not shown to the agent

The URI JSON and the backlog already have the actual twitter, telegram, and website URLs. The prompt only gets the names of which ones exist. The same fetch also keeps `video`, `createdOn`, and `coin_community` / `community` when the JSON has them (`src/archetype/metadata/fetch.py`). DAS `getAsset` also returns mint authorities, supply, and decimals. We only copy name, symbol, description, image, and links out of it. The filter already reads the mint owner and freeze / mint authority on-chain.

### `coins-v2` extras beyond the first write-up

Same unofficial `GET https://frontend-api-v3.pump.fun/coins-v2/{mint}`, no key, no Helius credits. On Save The Seals, on top of the kimi fields:

- `is_charity`
- `verified`
- `canonical_pool_liquidity_usd` (pool liquidity in USD)
- `circulating_supply_str`
- `ath_market_cap` and `ath_market_cap_timestamp`
- `volume_1h_usd`
- `reply_count` (count only, not the comment text)
- `is_currently_live`, `livestream_ban_expiry`
- `nsfw`, `is_banned`, `hide_banner`
- `boost_mode` (`"COMPLETED"` on both samples)
- `created_timestamp`, `last_trade_timestamp`
- `creator` (wallet)

`security_verdict` was `allow` on both. On the charity coin the reason was `operator_bypass` and `decided_by` was `bypass`. That is pump’s moderation blob, not a trading signal we should paste in full.

### Top holders, no login

`GET https://frontend-api-v3.pump.fun/coins/top-holders/{mint}` answered with no `Authorization` header.

For Save The Seals it returned `totalHolders` (2166) and a `topHolders` list. Each holder has `address`, `amount`, `isDev`, `isSniper`, `isBundler`, `enteredAt`, `fundingSource`. This is the useful “who holds it” snapshot. It is unofficial and can change. A short prompt line is enough: holder count, and whether the top of the list is flagged dev / sniper / bundler. Do not paste all 50 wallets.

Helius `getTokenLargestAccounts` is 1 credit and returns balances only. It does not have the sniper / bundler flags. Prefer the HTTP list while it keeps working. Source for the path: [BankkRoll capture index, 2026-06-17](https://github.com/BankkRoll/pumpfun-apis/blob/main/captures/2026-06-17/INDEX.md).

### Creator profile, no login

`GET https://frontend-api-v3.pump.fun/users/{wallet}` for the Save The Seals creator returned, with no key:

- `username`, `bio`, `x_username`
- `followers`, `following`
- `is_pump_user`, `is_banned`, `kind`
- `profile_image`

Useful as one line: pump username, follower count, banned or not, X handle if set. Not a substitute for the on-chain creator address.

### SOL price

`GET https://frontend-api-v3.pump.fun/sol-price` returned `solPrice`, `asOfTimestamp`, `stale`. We already price SOL ourselves. No need to add this call.

### Mayhem state route

`GET https://frontend-api-v3.pump.fun/coins-v2/{mint}/mayhem-state` returned `null` for Save The Seals (not a mayhem coin). The route exists and did not ask for a key. A non-null body is still unverified here. The on-chain `is_mayhem_mode` byte remains the check.

### Fee-sharing HTTP

`GET https://swap-api.pump.fun/v1/fee-sharing/account/{id}/shares?limit=5` answered with no key. For both the Save The Seals creator wallet and the mint, `items` was empty. So this URL is alive, but it did not return the charity split. The on-chain sharing-config PDA is still the shareholder list. The June 2026 capture of the same path on `advanced-api-v2.pump.fun` is not usable: that host returned Cloudflare error 1016 (origin DNS) on 13 Oct 2026.

`GET .../totals` on the same swap host 404’d.

### What did not answer

| Call | Result |
| --- | --- |
| `advanced-api-v2.pump.fun` (candles, trades, market-activity, callouts, bounties, narratives) | Cloudflare 1016. Host does not resolve. The [2026-06-17 capture](https://github.com/BankkRoll/pumpfun-apis/blob/main/captures/2026-06-17/INDEX.md) is a snapshot, not a live API. |
| `GET /replies/{mint}` on frontend-api-v3 | 404 with no key. The unofficial spec says replies need a pump.fun JWT. `reply_count` on `coins-v2` is the free substitute. |
| `GET /voice-chats/coin/{mint}/count` | 404 |
| Communities, callouts, bookmarks, king of the hill, KOL lists | Captured in BankkRoll. Not verified live, and several of those captures sat on the dead advanced host or are documented as JWT. Do not plan on them. |
| PumpPortal `subscribeTokenTrade` / `subscribeAccountTrade` | Metered. 0.01 SOL per 10,000 messages, API key, wallet with at least 0.02 SOL. Trade prints, not a metadata catalog. [Docs](https://pumpportal.fun/data-api/real-time). |
| [pumpdev.io](https://pumpdev.io/welcome) | Trading and create API. Needs their key for lightning trades. Does not add coin metadata we cannot already read. |

PumpPortal `subscribeNewToken` and `subscribeMigration` stay free and do not need a key. A create message, from third-party samples, carries mint, name, symbol, uri, creator, initial buy, and bonding-curve reserves. It is not documented as carrying mayhem, charity, or holder-reward. We already subscribe to migrations only.

## Donate.gg

Base URL `https://www.donate.gg/api/v1`. OpenAPI: [https://www.donate.gg/api/v1/openapi](https://www.donate.gg/api/v1/openapi). Overview: [developer overview](https://docs.donate.gg/developer/overview).

A donation config is a split: 1 to 5 charities, each with a weight. The API returns the config id as `{ hex, base58 }`. Those are the same 32 bytes. On Solana, `base58` is the pubkey form. Donate.gg’s relay program is `RLAYHr9TRFcKB2ubYQhspcnXiaGpaVzNQvHytt47RZu` ([Solana docs](https://docs.donate.gg/blockchain/solana-svm)). Solana’s chain id in their API is `101`, not mainnet-beta’s genesis.

Pump’s charity coins show up with donation source **Pump Fees Program** `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`. The subject address is the coin mint. Confirmed on `GET /charity-coins?limit=2&sortBy=recent-donation` with no API key.

### The call we want

`GET https://www.donate.gg/api/v1/101/tokens/{mint}`

No `donate-api-key` header. Live result for Save The Seals:

- token name and symbol
- `creatorAddress`
- `activeConfig.id.base58` = `BTRQNNY3mTo9pyRJPQFhzpRZ6Hc94AoQU1Q417gp6aWV`
- `activeConfig.feeBps` = `2000` (Donate.gg keeps 20%)
- `activeConfig.charities[]`: name **Stichting Zeehondencentrum Pieterburen**, slug, website, mission, country, weight `"100"`, socials

So we do not have to guess which fee-split wallet is the charity. The mint is enough. If we do have the config pubkey from the sharing-config account, the same object comes back from `GET /configs/{configId}` with the base58 id. That also worked with no key. Docs: [token by address](https://docs.donate.gg/api/charity-tokens/token-address-route) (public), [get config by id](https://docs.donate.gg/api-reference/get-a-config-by-id) (public, key optional). [Authentication](https://docs.donate.gg/developer/authentication) says catalog GETs, including config-by-id, may omit the key.

`GET /configs/{id}/stats` and the config leaderboard are different. Those are only for configs your own project created, and they need a key. We do not need them.

`GET /101/tokens/{mint}/stats` is documented as public and returns USD donated, donation count, and rank ([token stats](https://docs.donate.gg/api/charity-tokens/token-stats-route)). A bare GET in this session returned HTTP 400. The token lookup above is the one that worked. The charity-coins list also returns donated USD without a key (`displayedUsdE6` is micro-dollars).

### Do you need a Donate.gg key?

Not for naming the charity.

| Call | Key |
| --- | --- |
| `GET /101/tokens/{mint}` | No |
| `GET /configs/{base58-or-hex}` | No |
| `GET /charity-coins` | No |
| `GET /charities`, `GET /chains` | No |
| `POST /configs`, list your configs, delete, config stats, config leaderboard | Yes. Header `donate-api-key` |

New Developer Portal accounts are invite-only ([getting access](https://docs.donate.gg/developer/getting-access)). Anonymous reads are about 20 requests per minute per IP. A key, if you later get one, is the Free tier at 50 per minute, then paid Pro at 200 and Enterprise at 1,000 ([rate limits](https://docs.donate.gg/developer/rate-limits)). One lookup per charity coin is inside the anonymous budget. Cache the result. There is nothing to put in `.env` unless we start creating configs, which we should not.

## Keys and money

Nothing in the useful path needs a new API key or a new paid plan.

| Source | New key? | Money |
| --- | --- | --- |
| `frontend-api-v3.pump.fun` (`coins-v2`, top holders, users, sol price, mayhem-state) | No | No. Unofficial, no documented quota. A scraper report says the frontend API has answered 429 around 60 requests a minute from one IP. One coin at focus time is fine. |
| `swap-api.pump.fun` fee-sharing shares | No | No. Empty on the samples above. |
| Donate.gg public GETs | No | No. About 20/min anonymous. A key is optional and invite-only, and the free keyed tier is 50/min. Paid tiers exist if we ever needed bulk. |
| Helius, already in `.env` | Already have it | Free plan is $0 and 1,000,000 credits/month, 10 RPC req/s, 2 DAS req/s ([plans](https://www.helius.dev/docs/billing/plans), [credits](https://www.helius.dev/docs/billing/credits)). `getMultipleAccounts` and `getAccountInfo` are 1 credit. DAS `getAsset` is 10 credits and we already call it. One extra batched account read per focused coin does not require Developer ($49), Business, or Professional. |
| PumpPortal migration socket | No | Free. Trade subscriptions are the paid ones (SOL from a linked wallet, not a SaaS bill). |
| PumpDev | Their key, only if we traded through them | Not used for this. |
| pump.fun JWT (replies, bookmarks, some social routes) | Would be a logged-in user token, not an API product | Not worth it. `reply_count` is already on `coins-v2`. |

No new variable belongs in `.env` for this metadata. Helius stays the only key.
