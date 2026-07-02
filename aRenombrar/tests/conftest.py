import os
import sys

# Permite "from core.xxx import ..." al correr pytest desde cualquier carpeta,
# igual que hace main.py al insertar la raíz de aRenombrar/ en sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
