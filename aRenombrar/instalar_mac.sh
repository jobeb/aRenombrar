#!/bin/bash
echo "============================================"
echo "  aRenombrar - Instalador macOS"
echo "============================================"
echo

# Verificar Python 3
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 no encontrado."
    echo "Instálalo desde https://www.python.org/downloads/"
    exit 1
fi

echo "Instalando dependencias..."
python3 -m pip install --upgrade pip
python3 -m pip install customtkinter Pillow requests

echo
echo "============================================"
echo "  Instalación completada!"
echo "  Ejecuta: python3 main.py"
echo "============================================"
