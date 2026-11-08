# Third-party notices

India Day Trader contains adaptations of **day-trader-skill** by **Jake Schincariol**, copyright 2026, distributed under the MIT License.

- Upstream project: https://github.com/Jakeschincariol/day-trader-skill
- Full unchanged upstream license: [third-party/day-trader-skill.LICENSE](third-party/day-trader-skill.LICENSE)
- Adaptation areas: the stage/file pipeline, opening-range and daily-pattern analysis, four-cap position sizing, sequential risk reservations, ticket fingerprints, and time-slot bootstrap with conservative same-bar handling. Relevant implementation includes `scripts/core.py`, `scripts/patterns.py`, `scripts/replay.py`, and the corresponding skill guidance.
- Kunal Shinde is credited as the skill's creator and maintainer. The restrictive [project license](LICENSE) applies only to contributions he is authorized to license under its terms; attribution alone does not establish that authority. It does not revoke MIT permissions in inherited material or claim ownership of the upstream project.

Broker APIs and market data remain subject to their providers' terms, permissions and data licenses. A software license is not permission to redistribute exchange data or another provider's credentials.

The local `research/` archive contains user-supplied source documents and extracted text. Their redistribution rights have not been established. They are excluded from the intended GitHub package via `.gitignore`; keep them outside any release ZIP as well. They are not automatically relicensed under the project license.
