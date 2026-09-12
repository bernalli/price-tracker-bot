#!/usr/bin/env bash
# Audit Italian residual strings in source files.
#
# Pattern matches:
#   - tokens with accented vowels (à è é ì ò ù) — strong Italian signal
#   - distinct Italian words that have no English homograph: prezzo, errore,
#     comando, impostazion, aggiungere, elenca, notifica, riprova, sono
#     (intentionally excludes ambiguous bigrams like "non" / "più" which
#     appear in legitimate English compounds e.g. "non-EUR", "non-None")
#
# Coverage scope: all of src/price_tracker. The legacy handler bodies and
# callback modules that F5 left in Italian were swept in the post-1.0.0 pass
# (msgids are English throughout; it_IT/es_ES live in the catalogs).
#
# Out of scope:
#   - scrapers/** — domain-specific IT/EN dual-language parsing logic
#     (CSS selectors like .prezzo-attuale, regex alternations); user-facing
#     error strings there are English and translated in the bot layer.
#   - locale/** + bot/messages.py — translation catalogs and i18n module.
#   - the Italian command aliases (/aggiungi, /soglia, …) registered next to
#     their English names, and the legacy Italian CSV headers accepted by
#     the importer — both are compatibility identifiers, not UI copy.
#
# Exit 1 if any matches are found in covered scope.
set -euo pipefail

PATTERN='[àèéìòù]|\b(prezzo|errore|comando|impostazion|aggiungere|elenca|notifica|riprova|sono)\b'

if matches=$(rg --pcre2 "$PATTERN" \
              src/price_tracker \
              --type py \
              --glob '!src/price_tracker/locale/**' \
              --glob '!src/price_tracker/bot/messages.py' \
              --glob '!src/price_tracker/scrapers/**'); then
  echo "ERROR: Italian residual strings found in covered source:"
  echo "$matches"
  exit 1
fi
echo "OK: English-only audit passed (covered scope)"
