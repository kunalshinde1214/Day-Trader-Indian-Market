# Automatic exchange observations

Included in 0.10.0. No brokerage credentials are needed.

```text
python scripts/exchange_coverage.py --symbols RELIANCE.NS SBIN.NS 500325.BO --out exchange-new.json
python scripts/india_collect.py --symbols RELIANCE.NS --out india-new.json
```

The combined collector and its bounded repeated-collection workflow now include the same exchange observations automatically. Index proxies do not map to individual securities. The standalone command returns exit code 2 if both sources fail, otherwise 0 with an explicitly partial report. Inspect the component coverage in either case.

## Sources and dates

The collector discovers the latest advertised surveillance report through the [NSE cash-market report catalogue](https://www.nseindia.in/api/daily-reports?key=CM). The report's date, exact file name and allowed download directory must agree. For equal dates, REG1_IND takes precedence over REG_IND. No older substitute is tried after a selected file fails. Reports older than seven calendar days are rejected; this bound is a research freshness policy, not proof that a report is effective for a trading session.

It decodes GSM, long/short-term ASM, ESM, trading status, T2T series and the F&O removal indicator. GSM stage zero remains a flagged stage. Other source columns are retained as raw fields without assigning them invented meanings. A removal indicator does not establish complete F&O membership. Unknown stages, conflicting rows, changed required headers or malformed data fail the component. Multiple series remain separate.

The [NSE ban file](https://nsearchives.nseindia.com/content/fo/fo_secban.csv) is collected independently. Its own heading supplies the trade date. An explicit NIL is accepted only without contradictory rows. An empty response is never NIL. Non-NIL files require a supported symbol table; unfamiliar layouts fail. A next-session report remains visible but does not establish today's ban status. The collector never guesses the next trading day from weekdays.

NSE's `.com` pages redirected to `.in` during public verification on 15 November 2026. The new catalogue and calendar/status endpoints use the exact verified `.in` URLs. Report downloads are restricted to the two explicit NSE archive hosts and `/content/cm/REG[_or_1_IND]DDMMYY.csv` file patterns implemented in the collector. Arbitrary redirects, extra query parameters and URLs from other hosts remain refused.

Source definitions: [NSE surveillance overview](https://www.nseindia.com/regulations/exchange-market-surveillance-actions), [REG_IND file structure](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/REG_INDDDMMYY_File%20Structure%201.pdf), and [NSE surveillance master circular](https://nsearchives.nseindia.com/content/circulars/SURV74008.pdf). The public runtime does not download these PDFs or treat their text as instructions.

## What this establishes

Each observation records the source URL, source date, retrieval time and SHA-256. A live public smoke test on 15 November 2026 parsed the advertised 13 November REG1 report with 3,589 rows and the separate explicit-NIL ban report for 16 November. This demonstrates those observed formats, not continuing availability or validation of every possible non-NIL layout. Downloaded source samples stay in excluded local test output and are not redistributed in the skill archive.

Output always has `execution_eligible: false`, `full_market_coverage: false` and `submitted: false`. A report date is not a verified effective session. Symbols absent from surveillance remain UNKNOWN; BSE symbols remain UNKNOWN because this adapter covers NSE. `NOT_LISTED` in a same-date ban file is limited to that file, never general clearance. These reports do not replace live entry restrictions or instrument-session evidence.

Remaining coverage: BSE surveillance, instrument auctions/special sessions, emergency intraday notices, effective-session reconciliation, complete derivatives eligibility and interpretation of additional indicator columns. See the [ordered completion checklist](completion-tracker.md).
