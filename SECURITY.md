# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly. **Do not open a public issue.**

Report the issue privately using GitHub's [Report a vulnerability](https://github.com/jluqueba/wodbuster-booking-scheduler/security/advisories/new) button under the repository's Security tab (private vulnerability reporting). If that is unavailable to you, contact the maintainer [@jluqueba](https://github.com/jluqueba) directly. Please do not disclose the issue publicly until a fix has been released.

Please include the following information:

- Type of vulnerability (e.g., injection, broken access control, cryptographic failure)
- Full path(s) of affected source file(s)
- Location of the affected code (branch, commit, or URL)
- Steps to reproduce the issue
- Impact assessment and potential exploit scenario (if known)

## Response Process

This is a personal project maintained on a best-effort basis. The maintainer aims to:

- Acknowledge receipt within 5 business days.
- Provide an initial assessment within 10 business days.
- Work on a fix and coordinate disclosure once a remediation is available.

## Supported Versions

The project runs as a single always-on service and is pre-1.0. Only the latest code on the default branch (and the most recent release, if any) receives security updates.

| Version | Supported |
|---------|-----------|
| Latest `main` | Yes |
| Older commits or tags | No |

## Security Best Practices

- Never commit secrets, API keys, tokens, or credentials to the repository.
- Rotate any accidentally exposed credentials immediately.
- Keep dependencies up to date and monitor for known vulnerabilities.
- Follow the principle of least privilege for access control.
- This project never stores WodBuster credentials. Only the `.WBAuth` session cookie is persisted, encrypted at rest with AES-256-GCM, and application secrets are held in Azure Key Vault.
