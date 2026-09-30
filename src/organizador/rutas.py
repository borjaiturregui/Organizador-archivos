"""Utilidades de rutas: Windows (rutas largas, reparse points), raíz y rutas de sistema."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ES_WINDOWS = sys.platform == "win32"

_PREFIJO_LARGO = "\\\\?\\"
_PREFIJO_UNC_LARGO = "\\\\?\\UNC\\"
_LIMITE_RUTA = 240  # margen bajo MAX_PATH (260) y el límite de directorios (248)

# Nombre de atributo presente en os.stat_result en Windows (3.11 compatible).
_ATRIBUTO_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class RutaNoPermitida(ValueError):
    """La ruta indicada no se puede analizar ni modificar."""


def quitar_prefijo(ruta: str) -> str:
    """Quita el prefijo de ruta extendida de Windows (``\\\\?\\``) si lo tiene."""
    if ruta.startswith(_PREFIJO_UNC_LARGO):
        return "\\\\" + ruta[len(_PREFIJO_UNC_LARGO):]
    if ruta.startswith(_PREFIJO_LARGO):
        return ruta[len(_PREFIJO_LARGO):]
    return ruta


def para_os(ruta: str | os.PathLike[str]) -> str:
    """Devuelve la ruta lista para llamadas al sistema.

    En Windows añade el prefijo ``\\\\?\\`` a rutas absolutas largas para
    superar el límite de 260 caracteres. En otros sistemas no cambia nada.
    """
    texto = os.fspath(ruta)
    if not ES_WINDOWS or texto.startswith(_PREFIJO_LARGO) or len(texto) < _LIMITE_RUTA:
        return texto
    texto = _absoluta(texto)
    if texto.startswith("\\\\"):
        return _PREFIJO_UNC_LARGO + texto[2:]
    return _PREFIJO_LARGO + texto


def _absoluta(texto: str) -> str:
    # normpath es puro procesamiento de texto: no sufre el límite de 260 caracteres.
    return os.path.normpath(texto) if os.path.isabs(texto) else os.path.abspath(texto)


def normalizar(ruta: str | os.PathLike[str]) -> str:
    """Ruta absoluta, real (enlaces resueltos) y sin prefijo extendido."""
    return quitar_prefijo(os.path.realpath(para_os(_absoluta(os.fspath(ruta)))))


def relativa_a(ruta: str, raiz: str) -> str:
    """Parte relativa de ``ruta`` bajo ``raiz`` (ambas normalizadas, ruta dentro de raíz).

    Se hace por texto para no depender de ``relpath``/``abspath`` con rutas largas.
    """
    if not esta_dentro(ruta, raiz):
        raise RutaNoPermitida(f"'{ruta}' no está dentro de '{raiz}'")
    return ruta[len(os.path.normpath(raiz)):].lstrip("\\/")


def clave(ruta: str) -> str:
    """Clave de comparación: en Windows no distingue mayúsculas ni separadores."""
    return os.path.normcase(os.path.normpath(ruta))


def esta_dentro(ruta: str, raiz: str) -> bool:
    """True si ``ruta`` es ``raiz`` o está por debajo (ambas ya normalizadas)."""
    r, b = clave(ruta), clave(raiz)
    try:
        return os.path.commonpath([r, b]) == b
    except ValueError:  # distintas unidades en Windows
        return False


def es_enlace(st: os.stat_result) -> bool:
    """True para symlinks y, en Windows, cualquier reparse point (junctions incluidas).

    No depende de ``Path.is_junction`` (3.12+): usa ``st_file_attributes`` de lstat.
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    atributos = getattr(st, "st_file_attributes", 0)
    return bool(atributos & _ATRIBUTO_REPARSE)


def _rutas_sistema() -> list[str]:
    if ES_WINDOWS:
        candidatas = [
            os.environ.get("SystemRoot", r"C:\Windows"),
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramData", r"C:\ProgramData"),
            os.environ.get("ProgramW6432", r"C:\Program Files"),
        ]
    else:
        candidatas = [
            "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc",
            "/sbin", "/sys", "/usr", "/System", "/Library", "/Applications",
        ]
    return [os.path.normpath(c) for c in candidatas if c]


def es_raiz_de_unidad(ruta: str) -> bool:
    """True para ``C:\\``, ``\\\\servidor\\recurso\\`` o ``/``."""
    ruta = os.path.normpath(ruta)
    return os.path.dirname(ruta) == ruta


def motivo_bloqueo_sistema(ruta: str) -> str | None:
    """Devuelve por qué ``ruta`` es de sistema, o None si se puede usar."""
    if es_raiz_de_unidad(ruta):
        return f"'{ruta}' es la raíz de una unidad"
    for sistema in _rutas_sistema():
        if esta_dentro(ruta, sistema):
            return f"'{ruta}' está dentro de la ruta de sistema '{sistema}'"
    return None


def validar_raiz(ruta: str | os.PathLike[str], permitir_sistema: bool = False) -> str:
    """Normaliza y valida una raíz de análisis. Lanza RutaNoPermitida si no vale."""
    raiz = normalizar(ruta)
    if not os.path.isdir(para_os(raiz)):
        raise RutaNoPermitida(f"'{raiz}' no existe o no es una carpeta")
    if not permitir_sistema:
        motivo = motivo_bloqueo_sistema(raiz)
        if motivo:
            raise RutaNoPermitida(
                f"{motivo}. Se bloquea por seguridad; usa --permitir-sistema solo si sabes lo que haces."
            )
    return raiz
