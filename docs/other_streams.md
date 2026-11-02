# Other migration streams

PumpPortal `subscribeMigration` is a free notice that something graduated. On a normal SOL pair the `mint` field is the coin. On the coins below it is often the **quote** (PUMP, an xStock, an index) and there is no second field for the coin. That quote address is shared by every coin paired to it, so it cannot be turned back into one coin. The migration **signature** can. One `getTransaction` on that signature has both mints.

Checked 20 Oct 2026 against the five examples below. Pump.fun `coins-v2` answered. A 22 second listen on `wss://pumpportal.fun/api/data` subscribed cleanly and then sent no migration, so the live frame shape is from earlier runs plus these transactions, not from a fresh frame in this session.

## The five examples

| Coin | Mint | What it actually is | Trading quote | Where it graduated |
| --- | --- | --- | --- | --- |
| Holocaust | `6eukYbG1BLHsHEMseyoTZPJeC9EZQuH3wyUBMFDvpump` | pump.fun coin, holder rewards | PUMP `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` | PumpSwap `4Ma4KzASHnKusxnJxpuJgxWo5aP4ktvMWZQvt8fx7rnG` |
| Forever Laura | `Cg9zDsrNUzkm7cUgrXyv5TaKpbH5dpnr4E7pFAZYpump` | pump.fun coin, holder rewards | MSTRx `XsP7xzNPvEHS1m6qfanPUGjNmdnmsLKEoNAnHjdxxyZ` | PumpSwap `Fv44ZP4nZa45cT7Vv2UaQ8o5jNdrZzjuE3jvT1jPYVDK` |
| Alt-coin | `DER1KeNskuJuBh8roxvSD6iuJhSFFAYKqsa5WZbfWTsZ` | Stonk.fun, which is Raydium LaunchLab | SAMA index `7V1evjzUMFi4QDK7kAntcfdr9MyYC6S5bvsUCtxX1dSJ` | Raydium CPMM `2cvpEZEXt4UWLoZMVSPBmD8nG6V4Rs8ZQ1VZAiEvEPYZ` |
| Universal High Income | `FGkCj28xhabWTYg9QyzACvs4NsN6LUyesP6EwiHepump` | normal pump.fun coin. [OTC desks page](https://otcdesks.cash/coin/FGkCj28xhabWTYg9QyzACvs4NsN6LUyesP6EwiHepump) | SOL. `coins-v2` `quote_mint` is the zero pubkey `11111111111111111111111111111111`, which means SOL, not a stock | PumpSwap `3ENsvQnHuHtQzMBxpDG4pkxwbBnzJvFcz2yyCfxC9QNH` |
| Etchy | `GM8EysYT8NRSsoULdeYShbRsUdhoUoiB9EN4eW3YETCH` | Meteora dynamic bonding curve ("virtual curve") | WBTC `3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh` | Meteora DAMM v2 `A17Ssu23iAdcwLx5YkZ7QxpgdLptpcsegfL9dBZJPhx2` |

`coins-v2` already knows the real mint, the quote, and `program` (`pump`, `raydium_launchpad`, or `meteora_dbc`). That endpoint is a lookup once you have the mint. It is not a stream.

Pump.fun documents custom quotes in [CREATE_WITH_PUMP_COIN_QUOTE](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/CREATE_WITH_PUMP_COIN_QUOTE.md). A new coin can be quoted in PUMP, USDC, or another eligible coin, including an xStock. SOL pairs leave `quote_mint` on the curve as the default pubkey and the buy instruction passes wrapped SOL.

## Why the quote address is not enough

PUMP, MSTRx, SAMA4, and WBTC are each the quote for many coins. A frame that only contains `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` matches every PUMP-quoted graduation at once. There is no directory of "the coin this PUMP migration was about" without the transaction.

What the frame does have, when PumpPortal sends it, is `signature`. That is the migrate transaction. Decode that. Do not try to map the quote mint on its own.

## Pump.fun custom quotes (Holocaust, Laura)

These still graduate with pump `migrate_v2` into PumpSwap. The pool owners above are `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`.

Holocaust migrate transaction `A7PLC7ucStHMSNvNLk3Z65C5gcpM7naDvtAga6ggHSDaySanjnwjr3xFMJv1GLzYZbwYYdc6etvYsHm21a2ERG9` logs `Instruction: MigrateV2`. The pump instruction accounts, in order, start:

| Index | Account | This coin |
| --- | --- | --- |
| 0 | global | `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` |
| 1 | withdraw authority | `39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg` |
| 2 | **base mint, the coin** | `6eukYbG1BLHsHEMseyoTZPJeC9EZQuH3wyUBMFDvpump` |
| 3 | **quote mint** | `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` |
| 4 | bonding curve | `J2yGXDukL3bL5jZsoz8LCeNRRjdN4DqMz66vxSFnFStE` |
| 10 | new PumpSwap pool | `4Ma4KzASHnKusxnJxpuJgxWo5aP4ktvMWZQvt8fx7rnG` |

Same order is in the [pump IDL `migrate_v2`](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json): account 2 is `base_mint`, account 3 is `quote_mint`. A buy on the same curve lists them the same way: base, then quote.

So for a PumpPortal frame whose `pool` is `pump-amm` and whose `mint` is a known quote (PUMP, an `Xs…` stock, USDC), call `getTransaction` on `signature` and read `migrate_v2` account 2. That is the coin. Account 3 is the quote, and it should match the `mint` PumpPortal sent. One Helius credit per migration. No second websocket.

If the signature is missing, the frame cannot be repaired.

## Stonk.fun / Raydium LaunchLab (Alt-coin)

Stonk.fun is not a separate chain program. Alt-coin's LaunchLab row says platform `StonkFun`, web `https://www.stonkfun.xyz`, platform config `6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt`. The curve is Raydium LaunchLab `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`. Graduation is `migrate_to_cpswap` into Raydium CPMM `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C`. PumpPortal sometimes emits these with `pool: "raydium-cpmm"` and `mint` set to the stock or index. Same rule: the signature's migrate instruction has the coin and the quote as separate accounts. The coin is the mint that is not the quote.

Free HTTP, no key, from [LaunchLab Mint API](https://docs.raydium.io/api-reference/launch-mint-v1/overview):

- One coin: `GET https://launch-mint-v1.raydium.io/get/by/mints?ids=DER1KeNskuJuBh8roxvSD6iuJhSFFAYKqsa5WZbfWTsZ`
- Newest launches: `GET https://launch-mint-v1.raydium.io/get/list?sort=new&size=20&includeNsfw=true`

Each row has `mint` (the coin), `mintB` (the quote, with symbol and address), `platformInfo.name` (`StonkFun`, `letsbonk.fun`, `Raydium`, …), `finishingRate` (100 means the curve filled), `poolId` (the curve), and `migrateAmmId` (the CPMM pool after graduation). Alt-coin's `mintB` is SAMA Index `7V1evjzUMFi4QDK7kAntcfdr9MyYC6S5bvsUCtxX1dSJ`. A sample of 20 newest rows was 10 StonkFun, 5 letsbonk, and a few others. None of those 20 were finished yet, so `sort=new` is launches, not graduations. To catch graduations, poll and notice `finishingRate` reaching 100, or subscribe to LaunchLab logs (below). There is no Raydium graduation websocket on the free tier. [LaunchLab docs](https://docs.raydium.io/products/launchlab/code-demos) say graduation is `MigrateToCpswap`, sent by Raydium's crank, and live updates are gRPC or a poll of pool status.

A paid indexer (Bitquery's [Stonk.fun stream](https://docs.bitquery.io/docs/blockchain/Solana/stonkfun-api/)) can filter LaunchLab `initialize_v2` / `initialize_with_token_2022` and `migrate_to_cpswap` by those platform configs. We do not need it if Helius `logsSubscribe` plus `getTransaction` is enough.

## Virtual curve (Etchy)

This is Meteora's dynamic bonding curve, not pump and not LaunchLab. The curve account `DTtcqDAvPe8qJsbpYr59FNUCn1GfzaxfjhnTtWVCqbQi` is owned by `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`. The graduated pool is Meteora DAMM v2, owner `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG`. "Virtual" here means the curve holds virtual reserves until a keeper migrates them. Meteora's [migration note](https://docs.meteora.ag/core-products/dbc/migration-and-liquidity) describes that handoff. The current instruction is `migration_damm_v2`. An older path, `migrate_meteora_damm`, still shows up in history.

PumpPortal is not a Meteora feed. If a frame arrives whose `mint` is WBTC, it is the same class of bug as the PUMP frame, and the signature is still the way back to Etchy. Coins this feed never emits will not be repaired that way. A Helius `logsSubscribe` on `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`, then `getTransaction` on `migration_damm_v2`, is the stream. The coin and the quote (WBTC here) are both accounts on that instruction.

`coins-v2` already labels this `program: meteora_dbc` once you know the mint. Dexscreener also shows the DAMM pair against WBTC and later SOL pairs. Neither is a live graduation socket.

## OTC desks (Universal High Income)

[otcdesks.cash](https://otcdesks.cash/) is a site on top of coins. It is not an AMM. The featured list `GET https://otcdesks.cash/api/coins` returned 12 coins, no key. Each row has the real `mint`, plus `rewardMint`, `rewardSymbol`, and `rewardBasket`. Those reward fields are what holders are paid (an xStock or a basket). They are not the pool quote.

The Builders coin on that list is mint `aFjbuV3ndEPd7YNonNe2pTB6HDTmPPZUdC1LAX9pump`, reward `SPCXx`. Universal High Income was not in those 12. On `coins-v2` it is an ordinary SOL-quoted pump coin that graduated to PumpSwap. A healthy PumpPortal migration for it should already carry `FGkCj28xhabWTYg9QyzACvs4NsN6LUyesP6EwiHepump`. If a frame for this mint showed a stock address instead, that stock is the reward, and the signature is still required to find the coin. The OTC list is a catalog, not a migration firehose. [Meteora lists OTC desks as a DBC venue](https://www.meteora.fyi/projects/otcdesks) as well, so some OTC coins will be virtual-curve launches rather than pump coins. UHI is the pump kind.

## What this repo does today

`parse_pumpportal_message` stores `mint`, and also `baseMint` if that key exists. The frames we have seen do not include `baseMint`. The parser keeps the raw message, including `signature`.

The filter then requires a PumpSwap pool whose quote is wrapped SOL `So11111111111111111111111111111111111111112` (`src/archetype/filters/pipeline.py`). A correctly recovered Holocaust or Laura coin would still be rejected: quote is PUMP or MSTRx. A Stonk.fun or Etchy coin would be rejected because the pool owner is Raydium CPMM or Meteora DAMM, not PumpSwap. Paper buys also assume PumpSwap and WSOL. Fixing discovery does not make those coins tradable.

## Ways to get the coin

1. **Keep PumpPortal, decode the signature.** Cheapest for pump custom quotes and for the `raydium-cpmm` frames PumpPortal already sends. `getTransaction` once. `migrate_v2` account 2 is the pump coin. For `MigrateToCpswap`, take the mint that is not the quote. Drop the frame if there is no signature. Do not build a map from PUMP or MSTRx to "the" coin.

2. **Helius `logsSubscribe` on the three programs.** This catches graduations PumpPortal misses or mislabels, including Meteora. Programs: pump `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` (`MigrateV2`), LaunchLab `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` (`MigrateToCpswap`), Meteora DBC `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` (`migration_damm_v2`). Each log still needs one `getTransaction` to read the accounts. Standard plan, no LaserStream. This is the stream. PumpPortal becomes optional.

3. **Poll Raydium for LaunchLab only.** `launch-mint-v1.raydium.io/get/list?sort=new` is free and already splits `mint` from `mintB`, with the platform name. Use it for Stonk.fun launches. It is not a graduation socket, and it does not see pump custom quotes or Meteora curves.

Leave Bitquery, PumpDev, and other paid websockets until the Helius logs are not enough. PumpDev's feed does carry `quoteMint` separately ([their data API](https://pumpdev.io/data-api)), which is the same fact we can read from the transaction ourselves.
