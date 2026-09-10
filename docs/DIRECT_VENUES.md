# Direct venue fabric

Sportage treats **aggregated discovery** and **direct personal-account execution** as separate capabilities.

Aggregators can expose many bookmaker prices and are useful for discovery/cross-checking. They do not count as a connected personal bookmaker account and Sportage never routes an order through an aggregator unless that product explicitly provides an execution API and has separately passed the venue policy/certification layer.

## Italy policy

The ACEPC production host is configured for an Italian user. A venue is eligible for unattended order execution only when all of the following are true:

1. an official operator API is documented;
2. the API can authenticate the user's own account;
3. the API supports programmatic order placement/cancellation or the equivalent;
4. the venue is usable for an Italian resident/location under the operator's own terms; and
5. the operator is authorised for Italian remote gaming where required.

A technically functional foreign API is therefore not enough. Sportage records researched venues that fail the policy but deliberately does not call them from the Italian ACEPC.

## Audited venues — 2026-09-10

| Venue | Personal account API | Market data | Orders | Italy ACEPC policy | Production role |
|---|---:|---:|---:|---|---|
| Betfair Italia Exchange | yes | yes | yes | ADM eligible | direct monitoring + execution-ready |
| BetFlag Exchange | yes | yes | yes | ADM eligible | direct monitoring + execution-ready |
| Matchbook | yes | yes | yes | not ADM-authorised for Italy | recorded, disabled |
| BETDAQ | approval | yes | yes | operator accepted-country list excludes Italy | recorded, disabled |
| Smarkets | approval | yes | yes | operator API/location terms prohibit Italy | recorded, disabled |
| Pinnacle | application-only | yes | yes | not ADM; public API access closed | recorded, disabled |
| PS3838 | documented | yes | yes | not ADM-authorised for Italy | recorded, disabled |
| Cloudbet | self-service key | yes | yes | not ADM-authorised for Italy | recorded, disabled |
| BetConnect | documented | yes | yes | not ADM-authorised for Italy | recorded, disabled |
| bwin Sports API | partner API | yes | no personal execution | business partner only | discovery candidate only |
| MollyBet | broker API | yes | yes | broker, not a direct personal bookmaker account | not counted as direct venue |
| Sportmarket | broker API | yes | yes | broker, not a direct personal bookmaker account | not counted as direct venue |

Current direct Italian production target is therefore **Betfair Italia + BetFlag**, not an invented target of ten. `src/arbengine/direct_venues.py` is the machine-readable source of truth used by the runtime monitor.

## Official references

- Betfair Exchange API: https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687808
- BetFlag Exchange API: https://api-doc-exchange.mediasystemtechnologies.it/
- ADM remote-gaming concessionaires: https://www.adm.gov.it/portale/monopoli/giochi/giochi-a-distanza/concessionari-autorizzati-al-gioco-a-distanza
- Matchbook API: https://developers.matchbook.com/
- BETDAQ API: https://api.betdaq.com/v2.0/Docs/Intro.aspx
- Smarkets API terms: https://help.smarkets.com/hc/en-gb/articles/34697834941085-Smarkets-API-Access-Integration-T-Cs
- Pinnacle API documentation: https://github.com/pinnacleapi/pinnacleapi-documentation
- PS3838 API: https://ps3838api.github.io/docs/
- Cloudbet API: https://www.cloudbet.com/api/
- BetConnect API: https://developer.betconnect.com/
- bwin Sports API: https://beta.m.bwin.it/index.html
- MollyBet API: https://api.mollybet.com/docs/
- Sportmarket API: https://api.sportmarket.com/docs/

Operator terms, regulatory status and API access rules can change. A venue must be re-audited before its policy is promoted to `adm_eligible`.

## Runtime behavior

`sportage-venues.timer` runs the read-only venue monitor every 15 minutes. The monitor persists current state in `direct_venue_status` and an append-only check history in `direct_venue_checks`. For supported Italian direct venues it checks credentials, authenticates, performs the existing read-only venue certification, refreshes account funds and records whether the venue is execution-ready. It never submits an order and systemd forces `SPORTAGE_LIVE_EXECUTION=false` for the monitor.

The existing execution connector still requires all production gates before an actual order can be transmitted: fresh venue certification, sufficient account funds, canary allowance, commissioning validity, venue circuit-breaker health and the explicit master live switch. Betfair supports certificate login so the ACEPC can refresh its session without persisting a session token.

## Credentials

Secrets belong only in `/home/stoicpawn/projects/Sportage/.env` or protected certificate files on the ACEPC. Do not commit them.

### Betfair Italia

Set `BETFAIR_JURISDICTION=IT`, App Key, username/password and certificate/private-key paths. The certificate-login wrapper creates the short-lived session in process memory. Use a Betfair application key approved for the intended personal betting/API use before commissioning live orders.

### BetFlag

Obtain production API access from BetFlag/API support, then configure the API-key name/value and account login/session fields. Keep `BETFLAG_ENVIRONMENT=staging` until the staging connector is healthy; production execution remains blocked while the environment is staging.

## Safety boundary

`SPORTAGE_LIVE_EXECUTION=false` is the default and is forced on monitoring services. Having a venue marked `execution_ready=1` means the account/API is authenticated and Sportage's read-only certification/funds checks passed; it does **not** mean Sportage is currently allowed to transmit real wagers.
