# Security Policy

## Reporting a vulnerability

Please use the [GitHub Private Security Advisory](https://github.com/bernalli/price-tracker-bot/security/advisories/new) flow to report vulnerabilities. Do not disclose publicly until a fix is released.

If the GitHub flow is unavailable, email the maintainer at
<254686239+bernalli@users.noreply.github.com> with `[SECURITY]` in the subject.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.2.x   | ✅         |
| < 0.2   | ❌         |

## Response SLA

- **Acknowledgment**: best-effort within 72 hours
- **Patch target**: 30 days for HIGH/CRITICAL, 90 days for MEDIUM, advisory only for LOW

## Threat model

Key concerns considered in design:

| Concern                                | Mitigation                                                        |
| -------------------------------------- | ----------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` exposure          | env-only loading; never logged; gitleaks pre-commit + CI scan     |
| Scraper input poisoning (HTML/regex)   | Decimal-only price parsing; outlier detection via median ratio; lxml safe defaults |
| Database SQL injection                 | parameterized queries via aiosqlite; no string concatenation      |
| Container compromise                   | non-root `botuser` (uid 1000); read-only root fs; cap_drop ALL; no-new-privileges |
| Dependency supply chain                | osv-scanner weekly in CI; Dependabot weekly updates                 |
| User authorization bypass              | every handler decorated `@restricted` or `@admin_only`; default-deny |

## Secure deployment checklist

- [ ] `.env` file permissions `chmod 600 .env`
- [ ] `TELEGRAM_BOT_TOKEN` rotated regularly
- [ ] `ALLOWED_USERS` minimal (≤2 IDs)
- [ ] Container runs with hardened compose (read_only, cap_drop, limits)
- [ ] `data/pricetracker.db` backed up off-host
- [ ] Pre-commit hooks installed locally for contributors
- [ ] No PRs merged without CI green (security.yml + ci.yml)

## Acknowledgments

Security researchers who responsibly disclose vulnerabilities will be credited here (with consent) after the fix ships.
