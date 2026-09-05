# Sportage operator connectors

Sportage v0.4 separates **market data** from **bet execution**. Every operator has a canonical
`operator_id`, an ADM concession code, aliases used by external feeds, a market-data capability,
and an execution capability in `arbengine.operators`.

## Tier 1

| operator_id | Operator | ADM | Market data | Execution |
|---|---|---:|---|---|
| bet365 | Bet365 | 16030 | Aggregator fallback | Manual only |
| betfair | Betfair Exchange | 16028 | Official Exchange API-NG | Official Exchange API-NG |
| snai | SNAI | 16032 | Aggregator fallback | Manual only |
| sisal | Sisal | 16020 | Aggregator fallback | Manual only |
| eurobet | Eurobet | 16012 | Aggregator fallback | Manual only |
| goldbet | Goldbet | 16009 | Aggregator fallback | Manual only |
| lottomatica | Lottomatica | 16010 | Aggregator fallback | Manual only |

## Tier 2

| operator_id | Operator | ADM | Market data | Execution |
|---|---|---:|---|---|
| planetwin365 | Planetwin365 | 16007 | Aggregator fallback | Manual only |
| betsson | Betsson | 16027 | Aggregator fallback | Manual only |
| codere | Codere | 16018 | Aggregator fallback | Manual only |
| betflag | BetFlag | 16008 | Aggregator fallback | Manual only |
| bwin | bwin | 16013 | Official partner API exists; credentials/partner approval required | Manual only |
| william_hill | William Hill | 16044 | Aggregator fallback | Manual only |
| winamax | Winamax | 16042 | Aggregator fallback | Manual only |

`manual_only` is deliberate: Sportage does not reverse-engineer private endpoints or bypass anti-bot
controls. The connector still exists and can receive a normalized `BetOrder`, validate routing and
prepare a manual handoff. It can later be replaced by an official connector without changing the engine.

## Unified quote language

The `UnifiedOperatorProvider` consumes one or more upstream sources and writes only canonical Sportage
quotes:

- canonical `operator_id`;
- original provider event id retained as `source_event_id`;
- deterministic cross-source `event_id`;
- normalized sport family;
- H2H / 1X2 normalization based on outcome cardinality;
- canonical outcome labels;
- canonical operator display name.

The SQL schema stores both canonical and source identities for auditability.

## Betfair

`BetfairExchangeMarketDataConnector` uses official API-NG `listMarketCatalogue` + `listMarketBook` and
currently maps `MATCH_ODDS` into Sportage H2H/1X2 quotes.

`BetfairExchangeExecutionConnector` uses official API-NG `placeOrders`. Real calls require all of:

1. `BETFAIR_APP_KEY`;
2. `BETFAIR_SESSION_TOKEN`;
3. a call with `live=True`;
4. `SPORTAGE_LIVE_EXECUTION=true`.

The default path is dry-run and cannot place a real bet.

## Data-source priority

For duplicate quotes from the same operator/event/market/outcome the unified provider prefers:

1. direct Betfair API-NG;
2. The Odds API;
3. Odds-API.io.

This can be extended operator-by-operator as official APIs or partner contracts become available.
