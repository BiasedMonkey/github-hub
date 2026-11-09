#!/bin/sh
# Ajoute « GitHub Hub » au menu des applications, pointant vers ce dossier.
# À relancer si tu déplaces le dossier du hub.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/applications/github-hub.desktop"
mkdir -p "$(dirname "$DEST")"
cat >"$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=GitHub Hub
Comment=Installer et mettre à jour mes dépôts GitHub
Exec="$DIR/lancer.sh"
Path=$DIR
Icon=$DIR/logo.svg
Terminal=false
StartupWMClass=github-hub
Categories=Development;
EOF
chmod +x "$DIR/lancer.sh"
update-desktop-database "$(dirname "$DEST")" 2>/dev/null
echo "Raccourci créé : $DEST"
