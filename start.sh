#!/bin/sh
echo "Running nb orm upgrade..."
nb orm upgrade
echo "Installing playwright..."
uv run playwright install chromium --with-deps
echo "Starting nb run..."
nb run