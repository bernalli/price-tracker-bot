# Reliability review — 1.11.0

Base commit: `5c33ae5896d1398f64c000ad55c360518ec87e4b`.
Runtime inventory: 159 files; SHA-256 `728e261d9c2325c5b5e2ff0942114890a18644a26fd0af21edd17ea4e4f1975d` (path, NUL, bytes).

Independent reviews accepted:
- Product and offer identity; positive, finite quotes in known currencies; explicit stock proof.
- Per-product serialization, including failures; persistent latest-status delivery with compare-and-swap.
- Preferences and digest; Italian UI and accurate copied count.

Local validation: 7,644 passed, 1 skipped; coverage 95.32%.
Ruff 0.17.3 and MyPy 2.4.1, including tests: passed. Gitleaks and changelog/release synchronization: passed.
CI includes metrics endpoint tests; container acceptance includes three metrics checks.

Independent review record and CI and image acceptance are required before default-branch integration. CI and image acceptance remain pending.
