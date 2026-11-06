#!/usr/bin/env bash
# Double-click this file in Finder to set up (first time) and start the app.
# The first run installs everything into a local .venv folder; later runs start straight away.
cd "$(dirname "$0")" || exit 1
chmod +x ./run.sh 2>/dev/null
./run.sh "$@"
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "The app exited with an error (code $status). Press Return to close this window."
  read -r _
fi
exit $status
