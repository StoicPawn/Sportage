# Connector research — 2026-09-06

## Italy-ready official transactional APIs

### Betfair Italy Exchange
Already implemented in Sportage. Official API-NG supports Italian Exchange accounts, market data, account APIs and order placement/cancellation/reconciliation.

### BetFlag Exchange
Official Exchange API 2.0.7, documented at https://api-doc-exchange.mediasystemtechnologies.it/.

Documented capabilities used by Sportage:
- POST `/security/session`: login and session token
- PUT `/security/keepalive`: session refresh
- GET `/navigation/menu` and `/navigation/menu/dettaglio/{event}`: events/markets
- GET `/offers/market/{market},{level}`: order book
- GET `/offers/all/{market}` and `/offers/matches/{market}`: user orders/matches
- POST `/offers`: place one or more exchange offers
- DELETE `/offers`: cancel selected offers
- DELETE `/offers/unmatched`: cancel all unmatched on a market
- GET `/offers/exp/{market}`: market exposure
- GET `/account/balance`: account balance

The API examples encode odds as decimal odds * 100 and money amounts in euro cents.
For `POST /offers`, documented samples show `tipo=1` with `max_esposizione == importo`, which is BACK/PUNTA semantics, and `tipo=0` with `max_esposizione == importo*(odds-1)`, which is LAY/BANCA semantics.

BetFlag does not document a native FILL_OR_KILL primitive. Sportage therefore uses immediate placement followed by reconciliation and cancellation of any unmatched remainder. Partial fills are never treated as fully hedged and are routed through the existing rescue/emergency state machine.

## Official transactional APIs not enabled for Italy profile

Smarkets, Matchbook and BETDAQ publish official trading APIs, but they are not part of the current ADM-authorized Sportage Italy operator universe. They are intentionally not enabled as live rescue venues in the Italy profile.

## Other Tier 1/2 retail bookmakers

No public retail transactional placement API was verified for Bet365, SNAI, Sisal, Eurobet, Goldbet, Lottomatica, Planetwin365, Betsson, Codere, William Hill or Winamax. bwin publishes partner-facing sports APIs, but Sportage does not classify them as a verified retail execution API without partner credentials and transactional documentation.
