# Changelog

## 0.8.1-public-alpha — 8 November 2026

- Prepare the public source-available repository with installation instructions, a feature table, daily command examples and explicit alpha limits.
- Update the personal-use license to 1.2, retaining third-party MIT rights and the applicable GitHub platform permissions.
- Add trading-risk disclosure, security reporting and contribution guidance; broaden ignored local-output directories.
- Fix the standalone public-feed command to parse Nifty 50/Bank Nifty constituent CSVs correctly and record the retrieval time after downloading. Source availability and membership effective dates remain separate acceptance requirements.
- Include 463 offline test methods. Live remains disabled by default; no authenticated broker acceptance or real-trade validation is claimed.

## 0.8.0-portfolio-alpha — 8 November 2026

- Add limited read-only Kite carry-account collection against reviewed opening inventory.
- Reconcile holdings/position overlap, matched trades and fresh quotes; reject unsupported inventory or ambiguous evidence.
- Validate all 460 offline tests and the unpacked collector-to-journal workflow.

## 0.7.0-streaming-alpha — 7 November 2026

- Add Kite quote streaming, monitoring deployment-file rendering and optional daily constituent/news collection.
- Validate 425 offline tests, loopback WebSocket transport and native Windows task XML parsing.

Earlier local milestones introduced six scanners, five-broker read-only adapters and human-operated execution, the four daily commands, journal/service operations and canonical carry accounting. Historical checkpoint packages do not establish production acceptance.
