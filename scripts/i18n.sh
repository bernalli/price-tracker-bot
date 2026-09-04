#!/usr/bin/env bash
# i18n workflow helper. See docs/i18n.md.
set -euo pipefail

LOCALE_DIR="src/price_tracker/locale"

case "${1:-help}" in
  extract)
    pybabel extract -F babel.cfg -k _ -k N_ -k "ngettext:1,2" -o messages.pot src/
    ;;
  init)
    [ -n "${2:-}" ] || { echo "usage: $0 init <lang>"; exit 1; }
    pybabel init -i messages.pot -d "$LOCALE_DIR" -l "$2"
    ;;
  update)
    pybabel update -i messages.pot -d "$LOCALE_DIR"
    ;;
  compile)
    # Not `pybabel compile`: its printf validator false-positives on literal
    # percents in prose. See scripts/i18n_compile.py.
    python3 scripts/i18n_compile.py "$LOCALE_DIR"
    ;;
  *)
    echo "usage: $0 {extract|init <lang>|update|compile}"
    exit 1
    ;;
esac
