#!/bin/bash
# One-time setup. No credentials or administrator access are requested here.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
trap 'printf "\nLa instalación no ha terminado. Revisa el mensaje anterior.\n"; read -r -p "Pulsa Intro para cerrar… "' ERR

if [[ "$(uname -s)" != Darwin ]]; then
    echo "La interfaz de escritorio requiere macOS. Consulta README.md para el modo de terminal."
    exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "Instala las herramientas de desarrollo de Apple con: xcode-select --install"
    echo "Después vuelve a abrir este archivo."
    exit 1
fi
if ! command -v python3.12 >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Necesitas Python 3.12 y FFmpeg. Si tienes Homebrew, ejecuta:"
    echo "brew install python@3.12 ffmpeg"
    echo "Después vuelve a abrir este archivo."
    exit 1
fi

echo "Preparando reZme…"
if [[ ! -x .venv/bin/python ]]; then
    python3.12 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python desktop/build.py

if [[ ! -e "$HOME/Desktop/reZme.app" && ! -L "$HOME/Desktop/reZme.app" ]]; then
    ln -s "$PWD/reZme.app" "$HOME/Desktop/reZme.app"
    echo "Acceso directo creado en el escritorio."
else
    echo "Ya existe un icono reZme en el escritorio; se ha conservado."
fi
echo "Listo. Abre reZme.app con doble clic. Conserva esta carpeta en su ubicación."
open "$PWD/reZme.app"
