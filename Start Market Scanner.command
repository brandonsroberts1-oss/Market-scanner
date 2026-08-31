#!/usr/bin/env bash
# ===================================================================
#  Market Scanner - double-click this file to start the app.
#  Works on macOS (Finder) and Linux desktops.
# ===================================================================

# Double-clicking runs the script from the user's home directory, so
# move to wherever this file actually lives before doing anything.
cd "$(dirname "$0")" || exit 1

find_python() {
  for candidate in python3 python python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYEXE="$(find_python)" || {
  cat <<'MSG'

  ------------------------------------------------------------
   PYTHON IS NOT INSTALLED (or is too old)
  ------------------------------------------------------------

   Market Scanner needs Python 3.10 or newer.

   macOS:  install it from https://www.python.org/downloads/
           (or run:  brew install python )

   Linux:  sudo apt install python3 python3-venv python3-pip

   Then double-click this file again.

MSG
  read -r -p "  Press Enter to close this window. " _
  exit 1
}

"$PYEXE" "$(pwd)/launcher.py"
STATUS=$?

# Keep the window open on failure so the error stays readable.
if [ "$STATUS" -ne 0 ]; then
  echo
  echo "  The app stopped with an error (code $STATUS)."
  read -r -p "  Press Enter to close this window. " _
fi
exit $STATUS
