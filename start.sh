#!/bin/sh

nb orm upgrade

uv run playwright install chromium --with-deps

nb run
