#!/bin/bash
# Starts Keanu (iMessage AI assistant).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env file not found."
  echo "Copy .env.example to .env and fill in ANTHROPIC_API_KEY."
  exit 1
fi

echo "Starting Keanu..."
python3 server.py
