# Security

The project is an experimental alpha. Read-only adapters and human-operated execution are separate; live execution starts disabled. No release is certified for unattended use or authenticated broker operation.

## Protect your account

- Keep broker keys and tokens in your local environment. Do not commit them or paste them into public issues, logs, screenshots or assistant chats.
- Keep account snapshots, journals, contract notes and generated reports outside the public repository. Use the release allowlist rather than uploading the whole working directory.
- If a credential is exposed, revoke or rotate it through the provider immediately. Deleting a public file or commit does not revoke the credential.
- Do not bypass stale-data blocks, unresolved-order locks or human confirmations. Use your broker's own controls when connectivity or reconciliation fails.

## Report a vulnerability

Use the repository's **Security → Report a vulnerability** option when available. Do not put credentials, personal account records or exploitable details in a public issue. If private reporting is unavailable, request a private contact route without disclosing the vulnerability details.

Provide the affected release/commit, operating system, Python version and a minimal reproduction using fictional data. Never submit a real trade to demonstrate a bug. The maintainer does not promise a response time or continuous monitoring.

For ordinary non-sensitive bugs, use GitHub Issues with sanitized inputs and expected versus actual behavior. Review the [risk disclosure](RISK_DISCLOSURE.md) before using the software.
