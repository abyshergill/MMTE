#!/usr/bin/env bash
# Build (first run only) and start Modern Multi Terminal (Rust edition).
# Installs Rust with rustup if cargo is not found. Works on macOS and Linux.
set -e
cd "$(dirname "$0")"
if ! command -v cargo >/dev/null 2>&1; then
  if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi
fi
if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust was not found. Installing it with rustup (no sudo needed)..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  . "$HOME/.cargo/env"
fi
exec cargo run --release -- "$@"
