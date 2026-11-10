#!/bin/bash
# CryoViewer — double-clic pour lancer (macOS)
cd "$(dirname "$0")"

PYTHON=""
for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         "$HOME/miniforge3/bin/python3" "$HOME/miniconda3/bin/python3" \
         "$HOME/anaconda3/bin/python3"; do
    if command -v "$p" >/dev/null 2>&1 || [ -x "$p" ]; then PYTHON="$p"; break; fi
done

if [ -z "$PYTHON" ]; then
    osascript -e 'display alert "Python 3 introuvable" message "Installez Python 3 (brew install python3)." as critical'
    exit 1
fi
echo "Python : $PYTHON ($($PYTHON --version 2>&1))"

# Installe les dépendances manquantes (module:paquet)
for spec in numpy:numpy PIL:pillow tifffile:tifffile; do
    mod="${spec%%:*}"; pkg="${spec##*:}"
    $PYTHON -c "import $mod" 2>/dev/null || \
      $PYTHON -m pip install "$pkg" -q --break-system-packages 2>/dev/null || \
      $PYTHON -m pip install "$pkg" -q
done

echo "CryoViewer → http://127.0.0.1:7473   (Ctrl+C pour quitter)"
exec $PYTHON server.py "$@"
