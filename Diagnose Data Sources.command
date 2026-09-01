#!/usr/bin/env bash
# ===================================================================
#  Market Scanner - check why market data is not loading.
#  Double-click this file. It prints what every data source returned.
# ===================================================================
cd "$(dirname "$0")" || exit 1

for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
    PYEXE="$candidate"; break
  fi
done

if [ -z "${PYEXE:-}" ]; then
  echo
  echo "  Python 3.10 or newer was not found."
  echo "  Install it from https://www.python.org/downloads/"
  read -r -p "  Press Enter to close. " _
  exit 1
fi

"$PYEXE" "$(pwd)/launcher.py" --diagnose

echo
echo "  Copy the text above if you need to report the problem."
read -r -p "  Press Enter to close this window. " _
