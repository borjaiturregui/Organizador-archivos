"""Crea una carpeta de demostración con duplicados, basura, grandes y vacías.

Uso:
    python examples/crear_demo.py            # crea examples/demo
    python examples/crear_demo.py C:\\ruta    # crea la demo en otra carpeta

Después:
    organizador analyze examples/demo --umbral-grande 1MB --output informe.json
    organizador apply informe.json --vacias
    organizador restore examples/demo/_cuarentena_organizador/cuarentena_log_*.jsonl
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ARCHIVOS: dict[str, bytes] = {
    "Documentos/presupuesto_2026.xlsx": b"presupuesto v3" * 200,
    "Documentos/copia de presupuesto_2026.xlsx": b"presupuesto v3" * 200,
    "Descargas/presupuesto_2026 (1).xlsx": b"presupuesto v3" * 200,
    "Fotos/vacaciones/playa.jpg": b"\xff\xd8JPEG playa" * 5000,
    "Fotos/importadas/IMG_0001.jpg": b"\xff\xd8JPEG playa" * 5000,
    "Proyectos/web/index.html": b"<!doctype html><title>demo</title>",
    "Proyectos/web/index.html.bak": b"<!doctype html><title>demo antigua</title>",
    "Proyectos/web/~$borrador.tmp": b"temporal de Office",
    "Descargas/instalador.part": b"descarga a medias",
    "Descargas/Thumbs.db": b"miniaturas",
    "Notas/lista_vacia.txt": b"",
    "Notas/otra_vacia.txt": b"",
}
CARPETAS_VACIAS = ["Escritorio/nueva carpeta", "Proyectos/viejo/src/tmp"]
GRANDE = ("Videos/grabacion.mp4", 3 * 1024 * 1024)


def crear(destino: Path) -> None:
    for relativa, contenido in ARCHIVOS.items():
        ruta = destino / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
    # IMG_0001 es más antigua, pero gana la ruta más corta (primer criterio).
    antigua = 1_600_000_000
    os.utime(destino / "Fotos/importadas/IMG_0001.jpg", (antigua, antigua))
    for relativa in CARPETAS_VACIAS:
        (destino / relativa).mkdir(parents=True, exist_ok=True)
    ruta, tamano = GRANDE
    (destino / ruta).parent.mkdir(parents=True, exist_ok=True)
    (destino / ruta).write_bytes(os.urandom(tamano))


if __name__ == "__main__":
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "demo"
    if destino.exists() and any(destino.iterdir()):
        sys.exit(f"'{destino}' ya existe y no está vacía; elige otra ruta.")
    crear(destino)
    print(f"Demo creada en {destino.resolve()}")
