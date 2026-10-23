#!/bin/bash
# v6 -> v7: app-written configs become one-time copies the app owns and may change.
# Replaces only links into the installed copy; real files and foreign links stay.
set -euo pipefail
CURRENT_FILE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../scripts" && pwd)"
# shellcheck source=/dev/null
source "$CURRENT_FILE_DIR/dotheader.sh"
installed="$USER_HOME_DIR/.local/share/workstation/"
# Keep in sync with SEEDED_CONFIGS in scripts/deployment.py.
for rel in zed/settings.json zed/keymap.json Code/User/settings.json; do
  target="$USER_HOME_DIR/.config/$rel"
  [[ -L "$target" ]] || continue
  resolved="$(readlink -f "$target")" || continue
  [[ "$resolved" == "$installed"* && -f "$resolved" ]] || continue
  cp -- "$resolved" "$target.dfa-seed"
  mv -fT -- "$target.dfa-seed" "$target"
  print_action_message "Copied $target; the app owns it from now on"
done
