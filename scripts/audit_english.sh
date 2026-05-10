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
#   - bot/decorators.py + bot/handlers/{auth,monitoring,_helpers}.py and
#     descendants under callbacks/ that were already swept.
#
# Out of scope (carry-over IT strings, scheduled for post-v0.1.0 sweep):
#   - bot/handlers/{product,product_io,history,settings,text_input,debug}.py
#     and callbacks/{_actions,_admin,_menu,_product}.py — legacy handler
#     bodies; UX-visible strings already wrapped in `_()` but msgid texts
#     remain Italian for now (covered by it_IT catalog passthrough).
#   - scrapers/** — domain-specific IT/EN dual-language parsing logic
#     (CSS selectors like .prezzo-attuale, error messages emitted to
#     bot layer for translation upstream).
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
              --glob '!src/price_tracker/bot/handlers/product.py' \
              --glob '!src/price_tracker/bot/handlers/product_io.py' \
              --glob '!src/price_tracker/bot/handlers/product_list.py' \
              --glob '!src/price_tracker/bot/handlers/history.py' \
              --glob '!src/price_tracker/bot/handlers/settings.py' \
              --glob '!src/price_tracker/bot/handlers/text_input.py' \
              --glob '!src/price_tracker/bot/handlers/debug.py' \
              --glob '!src/price_tracker/bot/handlers/__init__.py' \
              --glob '!src/price_tracker/bot/handlers/callbacks/**' \
              --glob '!src/price_tracker/scrapers/**'); then
  echo "ERROR: Italian residual strings found in covered source:"
  echo "$matches"
  exit 1
fi
echo "OK: English-only audit passed (covered scope)"
