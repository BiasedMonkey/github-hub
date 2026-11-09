#!/bin/sh
# Ouvre GitHub Hub dans sa propre fenêtre.
# Utilise l'environnement Python du dossier (.venv) s'il existe, sinon python3.
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python app.py "$@"
fi
exec python3 app.py "$@"
