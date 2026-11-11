#!/bin/bash
# Double-click to run on macOS (also works from a terminal on Linux).
# First run: sets up a private Python environment and installs pyserial.
cd "$(dirname "$0")/python" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is needed. Install it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Enter to close."
  exit 1
fi

if ! .venv/bin/python -c "import serial" >/dev/null 2>&1; then
  echo "First-time setup (needs internet)..."
  { [ -x .venv/bin/python ] || python3 -m venv .venv; } &&
    .venv/bin/python -m pip install --quiet -r requirements.txt || {
    echo "Setup failed."; read -r -p "Press Enter to close."; exit 1; }
fi
PY=.venv/bin/python

stamp=$(date +%Y%m%d-%H%M%S)
echo
echo "Fiero ALDL Reader"
echo "  1) Demo with no car (simulator)"
echo "  2) Check the signal (key on)"
echo "  3) Raw bytes (work out which byte is which)"
echo "  4) Live dashboard + record a log"
echo "  5) Same as 4, but with --invert"
read -r -p "Choose 1-5: " choice

case "$choice" in
  1) $PY aldl_logger.py --simulate ;;
  2) $PY aldl_logger.py --port auto --mode pulses ;;
  3) $PY aldl_logger.py --port auto --mode raw --capture "../logs/raw-$stamp.txt" ;;
  4) $PY aldl_logger.py --port auto --log "../logs/log-$stamp.csv" --capture "../logs/cap-$stamp.txt" ;;
  5) $PY aldl_logger.py --port auto --invert --log "../logs/log-$stamp.csv" --capture "../logs/cap-$stamp.txt" ;;
  *) echo "Not a choice." ;;
esac
echo
echo "Logs are saved in the fiero-aldl/logs folder."
read -r -p "Press Enter to close."
