#!/bin/sh
# Ouvre GitHub Hub dans sa propre fenêtre.
cd "$(dirname "$0")"
exec python3 app.py "$@"
