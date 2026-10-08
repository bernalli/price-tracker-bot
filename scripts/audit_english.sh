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
# Coverage scope:
#   - bot/commands.py + bot/decorators.py + bot/handlers/{__init__,auth,debug,
#     history,monitoring,product,product_io,settings,text_input,_helpers}.py
#     and all descendants under callbacks/.
#
# Out of scope (carry-over IT strings, scheduled for a later sweep):
#   - bot/handlers/product_list.py — legacy list handler pending its own sweep.
#   - scrapers/** — scraper messages stay out of scope because they are stored
#     in the DB and shown later; selectors also contain legitimate Italian.
#   - locale/** + bot/messages.py — translation catalogs and i18n module.
#
# Exit 1 if any matches are found in covered scope.
set -euo pipefail

PATTERN='[àèéìòù]|\b(prezzo|errore|comando|impostazion|aggiungere|elenca|notifica|riprova|sono)\b'

if matches=$(rg --pcre2 "$PATTERN" \
              src/price_tracker \
              --type py \
              --glob '!src/price_tracker/locale/**' \
              --glob '!src/price_tracker/bot/messages.py' \
              --glob '!src/price_tracker/bot/handlers/product_list.py' \
              --glob '!src/price_tracker/scrapers/**'); then
  echo "ERROR: Italian residual strings found in covered source:"
  echo "$matches"
  exit 1
fi
echo "OK: English-only audit passed (covered scope)"
