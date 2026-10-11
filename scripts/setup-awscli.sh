#!/bin/bash
# AWS CLI v2 for the work profile on rolling Arch / Ubuntu 26.04. Safe to rerun.
# Native package; pacman/APT updates keep it current via update-system.sh.

CURRENT_FILE_DIR="$(cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd)"

if [ -r "$CURRENT_FILE_DIR/dotheader.sh" ]; then
  # shellcheck source=/dev/null
  source "$CURRENT_FILE_DIR/dotheader.sh"
else
  echo "Missing header file: $CURRENT_FILE_DIR/dotheader.sh"
  exit 1
fi

print_tool_setup_start "AWS CLI"

ensure_core_cli awscli || exit 1
aws --version

print_info_message "Next: aws configure sso (credentials stay in ~/.aws, never in the dotfiles)"
print_tool_setup_complete "AWS CLI"
