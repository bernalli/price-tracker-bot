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
# Exit 1 if any matches are found in covered scope, 2 if the search could not run.
set -euo pipefail

PATTERN='[àèéìòù]|\b(prezzo|errore|comando|impostazion|aggiungere|elenca|notifica|riprova|sono)\b'

# ripgrep exits 0 on a match, 1 on no match and 2 on an error. A missing tool
# exits 127. Only "no match" is a pass: a search that did not run found nothing
# because it searched nothing.
status=0
matches=$(rg --pcre2 "$PATTERN" \
            src/price_tracker \
            --type py \
            --glob '!src/price_tracker/locale/**' \
            --glob '!src/price_tracker/bot/messages.py' \
            --glob '!src/price_tracker/bot/handlers/product_list.py' \
            --glob '!src/price_tracker/scrapers/**') || status=$?
case "$status" in
  0)
    echo "ERROR: Italian residual strings found in covered source:"
    echo "$matches"
    exit 1
    ;;
  1)
    echo "OK: English-only audit passed (covered scope)"
    ;;
  *)
    echo "UNABLE TO MEASURE: the search did not run (exit ${status})." >&2
    echo "A gate that cannot measure is red, not green." >&2
    exit 2
    ;;
esac
