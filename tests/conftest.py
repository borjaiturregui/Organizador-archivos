from __future__ import annotations

import os
from pathlib import Path

import pytest

from organizador.rutas import normalizar

# Fechas fijas (ns) para que el criterio de copia conservada sea reproducible.
T_ANTIGUO = 1_600_000_000_000_000_000
T_MEDIO = 1_650_000_000_000_000_000
T_NUEVO = 1_700_000_000_000_000_000


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    """Carpeta temporal ya normalizada (en Windows expande nombres cortos 8.3)."""
    carpeta = tmp_path / "datos"
    carpeta.mkdir()
    return Path(normalizar(carpeta))


def crear(base: Path, relativa: str, contenido: bytes | str = b"x", mtime_ns: int | None = T_MEDIO) -> Path:
    ruta = base / relativa
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(contenido, str):
        contenido = contenido.encode()
    ruta.write_bytes(contenido)
    if mtime_ns is not None:
        os.utime(ruta, ns=(mtime_ns, mtime_ns))
    return ruta


def puede_enlace_simbolico(base: Path) -> bool:
    prueba = base / "__prueba_enlace"
    try:
        prueba.symlink_to(base)
    except (OSError, NotImplementedError):
        return False
    prueba.unlink()
    return True


def puede_enlace_duro(base: Path) -> bool:
    origen = base / "__prueba_duro_a"
    origen.write_bytes(b"a")
    try:
        os.link(origen, base / "__prueba_duro_b")
    except (OSError, NotImplementedError):
        return False
    finally:
        origen.unlink()
    (base / "__prueba_duro_b").unlink()
    return True
