# Manual India research stages

Interpret research with these agent workflows. The [operations tools](operations.md) collect named public feeds and process explicit structured analyst/disclosure/event inputs; attachment extraction and complete exchange coverage are not automatic. Use connected market-data tools or official public sources when available. If a feed is unavailable, report the gap; do not synthesize an announcement, earnings date or “no events” result.

## News

For the named instrument or current watchlist, collect exchange announcements and corporate results dates first; then attributable headlines, public bulk/block deals, promoter-pledge changes and sector context. Each record should contain `symbol`, `exchange`, `category`, `headline`, `url`, `published_at`, `retrieved_at` and a concise factual summary. Preserve corrections, delays and uncertainty. `news.json` should disclose covered and unavailable names.

Label FII/DII values by their actual trading date and provisional status. During a session, the latest released daily flow normally describes a previous session; never relabel it as today's flow before publication. A risk-on/risk-off/mixed label is an explicitly defined heuristic, not a fact or instruction to trade. GIFT Nifty is context with a source timestamp, not a deterministic cash-market opening forecast.

## Analyst

Use current and comparison-period quarterly results, auditor notes, shareholding patterns and exchange filings. Preserve consolidated versus standalone basis, currency/units, quarter versus year-to-date, restatements and reporting period. Show source-backed changes in revenue, earnings, debt/cash and ownership where available. Do not manufacture missing fields or issue personalized security ratings. Save facts in `analyst.json` with links and period labels.

## Whales

Replace US congressional disclosures with public Indian PIT/SAST filings, promoter pledge/release notices, and bulk/block records. Distinguish transaction date from filing/publication date; a delayed filing cannot be presented as real-time informed activity. Preserve holder identity, disclosed instrument, direction, quantity/value, threshold and amendment status. Output `whales.json` with public source links. Public filing facts alone do not establish motive or insider advantage.

## Events and expiry

Maintain an evidence-backed calendar from official Budget, RBI, Federal Reserve, statistics agencies, election authority and exchange notices. Keep dates, exact release time/timezone when known, source, last check and scope (market/sector/symbol). `context.event` is a current risk decision derived from that calendar, not a fabricated static list. Major unresolved events default to blocked; a half-risk policy needs an explicit user choice.

For expiry, resolve the specific contract in the daily master. Note stock physical settlement, actual expiry shifts, liquidity and broker-specific restrictions. OI concentrations may be described as observations with timestamps; pinning and theta claims are not predictions. Recheck actual broker margin rather than assuming a fixed expiry-day benefit or penalty.

## Tax/journal

Use the local journal to retain factual fills, audited contract-note fee corrections and separate cost components for CSV export. Do not create a fill from an order acknowledgment or a paper preview. Refer tax classification, turnover computation, audit applicability, return form and elections to a qualified CA with current-year sources. The research report's tax table is not an implemented tax engine.
