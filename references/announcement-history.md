# Announcement backfill and filing discovery

Development after the published 0.9.9 ZIP. This workflow uses public NSE data without brokerage credentials.

```text
python scripts/announcement_history.py --from-date 2026-11-12 --through-date 2026-11-13 --out announcements-new.json
python scripts/announcement_history.py --from-date 2026-11-12 --through-date 2026-11-13 --format markdown --limit 30 --out headlines-new.md
python scripts/filing_workflow.py match --input announcements-new.json --universe sourced-universe.json --out issuers-new.json
python scripts/filing_archive.py --input issuers-new.json --database state/filings.sqlite --limit 5 --out filings-new.json
```

Dates are examples: choose the actual requested historical window. Each backfill invocation covers one to seven calendar dates, one bounded request per date. It uses the [NSE announcement endpoint](https://www.nseindia.in/api/corporate-announcements?index=equities&from_date=13-11-2026&to_date=13-11-2026), linked to the [corporate announcements workflow](https://www.nseindia.in/companies-listing/corporate-filings-announcements). Requests use exact allowlisted endpoints and refuse redirects. Dates that fail stay visible; they are not automatically retried. Large or unsupported responses fail rather than being truncated locally and presented as complete.

The JSON output uses the existing `india_news_v1` contract and can feed the personal filing archive. Use the JSON output for downstream commands; Markdown is for reading. Archive batches download at most twenty attachments, persist each result, avoid repeat work and require explicit retry of failed downloads. Historical announcements can be collected in separate non-overlapping windows and queued into the same archive. This is bounded backfill, not a claim that every historical filing was discovered.

## Provenance and issuer matching

Records retain NSE's sequence ID, symbol, ISIN where supplied, source-response hash, announcement time and dissemination time where available. Dissemination cannot precede submission. Timestamps without an offset are explicitly labelled as assuming IST. Conflicting duplicate sequence IDs invalidate that date's response. Malformed individual rows are counted; an empty successful response does not establish no-event clearance.

Structured symbols and ISINs are matched together against the supplied universe. Agreement is labelled `structured_identity_match`; a conflicting ISIN remains a conflict and never falls back to a headline guess. This verifies agreement between supplied fields, not independent authenticity, company ownership or the filing's contents. `issuer_verified` remains false. Headline-only news retains the older candidate matching behavior.

The collector ignores voice summaries and does not treat source text as instructions. It retains short announcement headings and links, not full news articles or source-provided financial summaries.

## Attachment discovery

The workflow now accepts direct official links and official attachment links found in description anchors. RSS collection retains those discovered links too. Discovery is bounded to twenty links per item. JavaScript, embedded scripts, credentials in URLs and foreign hosts do not become downloads. No general website crawler is started.

Multiple attachments from one announcement enter the archive separately. Their original announcement identity is retained; selecting one archive item does not requeue its siblings. `hasXbrl: true` remains a source flag: it does not create an invented XBRL URL. Unknown portal-only attachment layouts remain missing coverage.

## Supported inline XBRL numbers

The financial parser now understands the 2015 registry's `numdotdecimalin` transformation for Indian grouping, including comma, space and nonbreaking-space separators. For example, `1,23,456.50` becomes `123456.50`; `sign` and `scale` are applied separately. Standard three-digit grouping remains a different transformation. Invalid grouping, signs embedded in a transformed number, unknown namespaces and nested prefix rebinding to an unknown registry do not get guessed.

Supported registry/name pairs are `numdotdecimal` in the 2011/2015 registries, `numdotdecimalin` in 2015, and `num-dot-decimal` in 2020/2022. This is a bounded subset, not a general conforming XBRL processor. Continuations/exclusions, unsupported numeric formats and typed dimensions remain review-only or unsupported. See [XBRL International's transformation registry](https://www.xbrl.org/Specification/inlineXBRL-transformationRegistry/REC-2015-02-26%2Berrata-2019-04-17/inlineXBRL-transformationRegistry-REC-2015-02-26%2Bcorrected-errata-2019-04-17.html) for the specification.

## Validation and remaining work

A public read-only smoke test on 15 November 2026 returned 2,099 announcements across 12–13 November. The local source samples are excluded from distribution. No articles, accounts or trades were accessed by that test.

The report remains partial. BSE history, silent provider truncation/pagination, undiscovered attachments, same-URL revisions, complete taxonomy/entity mapping and independent verification of unstructured financial numbers remain unfinished. A matching symbol or parsed number does not establish a verified investment fact. The [ordered checklist](completion-tracker.md) tracks the remaining work.
