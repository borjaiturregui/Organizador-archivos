"""Modelo del informe y su exportación (JSON, CSV y texto para consola).

El JSON es un formato de intercambio **no fiable**: ``Informe.desde_dict``
valida tipos y estructura, pero la seguridad real (rutas dentro de la raíz,
copia conservada, revalidación) la aplica ``quarantine``.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from organizador import __version__

VERSION_INFORME = 1


class InformeInvalido(ValueError):
    """El informe JSON no tiene la estructura esperada."""


@dataclass
class Archivo:
    ruta: str
    tamano: int
    mtime_ns: int


@dataclass
class GrupoDuplicados:
    sha256: str
    tamano: int
    archivos: list[Archivo]
    conservar: str  # Informativo: apply lo recalcula y no se fía de este campo.

    @property
    def recuperable(self) -> int:
        return self.tamano * (len(self.archivos) - 1)


@dataclass
class GrupoEnlaces:
    archivos: list[Archivo]
    enlaces_totales: int


@dataclass
class Incidencia:
    ruta: str
    motivo: str


@dataclass
class Informe:
    raiz: str
    opciones: dict[str, Any] = field(default_factory=dict)
    generado: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    duplicados: list[GrupoDuplicados] = field(default_factory=list)
    grandes: list[Archivo] = field(default_factory=list)
    basura: list[Archivo] = field(default_factory=list)
    cero_bytes: list[Archivo] = field(default_factory=list)
    vacias: list[str] = field(default_factory=list)
    enlaces_duros: list[GrupoEnlaces] = field(default_factory=list)
    omitidos: list[Incidencia] = field(default_factory=list)
    errores: list[Incidencia] = field(default_factory=list)
    total_archivos: int = 0
    total_bytes: int = 0

    # ------------------------------------------------------------------ resumen
    def resumen(self) -> dict[str, int]:
        return {
            "archivos": self.total_archivos,
            "bytes": self.total_bytes,
            "grupos_duplicados": len(self.duplicados),
            "copias_sobrantes": sum(len(g.archivos) - 1 for g in self.duplicados),
            "bytes_recuperables": sum(g.recuperable for g in self.duplicados),
            "grandes": len(self.grandes),
            "basura": len(self.basura),
            "cero_bytes": len(self.cero_bytes),
            "carpetas_vacias": len(self.vacias),
            "grupos_enlaces_duros": len(self.enlaces_duros),
            "omitidos": len(self.omitidos),
            "errores": len(self.errores),
        }

    # -------------------------------------------------------------- serializar
    def a_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION_INFORME,
            "herramienta": f"organizador-archivos {__version__}",
            "generado": self.generado,
            "raiz": self.raiz,
            "opciones": self.opciones,
            "resumen": self.resumen(),
            "duplicados": [asdict(g) for g in self.duplicados],
            "grandes": [asdict(a) for a in self.grandes],
            "basura": [asdict(a) for a in self.basura],
            "cero_bytes": [asdict(a) for a in self.cero_bytes],
            "vacias": list(self.vacias),
            "enlaces_duros": [asdict(g) for g in self.enlaces_duros],
            "omitidos": [asdict(i) for i in self.omitidos],
            "errores": [asdict(i) for i in self.errores],
        }

    @classmethod
    def desde_dict(cls, datos: Any) -> "Informe":
        if not isinstance(datos, dict):
            raise InformeInvalido("el informe debe ser un objeto JSON")
        if datos.get("version") != VERSION_INFORME:
            raise InformeInvalido(f"versión de informe no soportada: {datos.get('version')!r}")
        raiz = _texto(datos.get("raiz"), "raiz")
        resumen = datos["resumen"] if isinstance(datos.get("resumen"), dict) else {}
        opciones = datos["opciones"] if isinstance(datos.get("opciones"), dict) else {}
        return cls(
            raiz=raiz,
            opciones=opciones,
            generado=str(datos.get("generado", "")),
            duplicados=[_grupo(g) for g in _lista(datos, "duplicados")],
            grandes=[_archivo(a) for a in _lista(datos, "grandes")],
            basura=[_archivo(a) for a in _lista(datos, "basura")],
            cero_bytes=[_archivo(a) for a in _lista(datos, "cero_bytes")],
            vacias=[_texto(v, "vacias[]") for v in _lista(datos, "vacias")],
            enlaces_duros=[
                GrupoEnlaces(
                    archivos=[_archivo(a) for a in _lista(g, "archivos")],
                    enlaces_totales=_entero(g.get("enlaces_totales", 0), "enlaces_totales"),
                )
                for g in _lista(datos, "enlaces_duros")
            ],
            omitidos=[_incidencia(i) for i in _lista(datos, "omitidos")],
            errores=[_incidencia(i) for i in _lista(datos, "errores")],
            total_archivos=_entero(resumen.get("archivos", 0), "resumen.archivos"),
            total_bytes=_entero(resumen.get("bytes", 0), "resumen.bytes"),
        )

    def a_json(self) -> str:
        return json.dumps(self.a_dict(), ensure_ascii=False, indent=2)

    def guardar_json(self, destino: str | os.PathLike[str]) -> None:
        with open(destino, "w", encoding="utf-8") as f:
            f.write(self.a_json())

    @classmethod
    def cargar_json(cls, origen: str | os.PathLike[str]) -> "Informe":
        try:
            with open(origen, encoding="utf-8") as f:
                datos = json.load(f)
        except json.JSONDecodeError as e:
            raise InformeInvalido(f"JSON mal formado: {e}") from e
        return cls.desde_dict(datos)

    # -------------------------------------------------------------------- CSV
    def a_csv(self) -> str:
        salida = io.StringIO()
        w = csv.writer(salida)
        w.writerow(["categoria", "grupo", "ruta", "tamano", "modificado", "conservar"])
        for n, g in enumerate(self.duplicados, 1):
            for a in g.archivos:
                w.writerow(["duplicado", n, a.ruta, a.tamano, iso(a.mtime_ns), "si" if a.ruta == g.conservar else ""])
        for categoria, lista in (("grande", self.grandes), ("basura", self.basura), ("cero_bytes", self.cero_bytes)):
            for a in lista:
                w.writerow([categoria, "", a.ruta, a.tamano, iso(a.mtime_ns), ""])
        for v in self.vacias:
            w.writerow(["carpeta_vacia", "", v, "", "", ""])
        for n, e in enumerate(self.enlaces_duros, 1):
            for a in e.archivos:
                w.writerow(["enlace_duro", n, a.ruta, a.tamano, iso(a.mtime_ns), ""])
        for i in self.omitidos:
            w.writerow(["omitido", "", i.ruta, "", "", i.motivo])
        for i in self.errores:
            w.writerow(["error", "", i.ruta, "", "", i.motivo])
        return salida.getvalue()

    def guardar_csv(self, destino: str | os.PathLike[str]) -> None:
        # utf-8-sig para que Excel en Windows detecte la codificación.
        with open(destino, "w", encoding="utf-8-sig", newline="") as f:
            f.write(self.a_csv())

    # ------------------------------------------------------------------ texto
    def a_texto(self, limite: int = 10) -> str:
        r = self.resumen()
        rel = lambda ruta: relativa(ruta, self.raiz)  # noqa: E731
        lineas = [
            f"Raíz analizada: {self.raiz}",
            f"Archivos: {r['archivos']} ({tamano_legible(r['bytes'])})",
            "",
            f"Duplicados: {r['grupos_duplicados']} grupos, {r['copias_sobrantes']} copias sobrantes, "
            f"{tamano_legible(r['bytes_recuperables'])} recuperables",
        ]
        for g in sorted(self.duplicados, key=lambda g: -g.recuperable)[:limite]:
            lineas.append(f"  [{len(g.archivos)} x {tamano_legible(g.tamano)}] se conserva: {rel(g.conservar)}")
            lineas.extend(f"      - {rel(a.ruta)}" for a in g.archivos if a.ruta != g.conservar)
        _bloque(lineas, "Archivos grandes",
                [f"{tamano_legible(a.tamano):>10}  {rel(a.ruta)}" for a in self.grandes], limite)
        _bloque(lineas, "Basura / temporales", [rel(a.ruta) for a in self.basura], limite)
        _bloque(lineas, "Archivos de 0 bytes", [rel(a.ruta) for a in self.cero_bytes], limite)
        _bloque(lineas, "Carpetas vacías", [rel(v) for v in self.vacias], limite)
        _bloque(
            lineas,
            "Enlaces duros (mover no libera espacio; solo se informan)",
            [" = ".join(rel(a.ruta) for a in g.archivos) for g in self.enlaces_duros],
            limite,
        )
        _bloque(lineas, "Omitidos", [f"{rel(i.ruta)}  ({i.motivo})" for i in self.omitidos], limite)
        _bloque(lineas, "Errores", [f"{rel(i.ruta)}  ({i.motivo})" for i in self.errores], limite)
        return "\n".join(lineas)


# ---------------------------------------------------------------------------
def tamano_legible(n: int) -> str:
    valor = float(n)
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if valor < 1024 or unidad == "TB":
            return f"{valor:.0f} {unidad}" if unidad == "B" else f"{valor:.1f} {unidad}"
        valor /= 1024
    return f"{n} B"  # pragma: no cover


def relativa(ruta: str, raiz: str) -> str:
    """Ruta relativa a la raíz para mostrar; si no es posible, la ruta tal cual."""
    try:
        rel = os.path.relpath(ruta, raiz)
    except ValueError:
        return ruta
    return ruta if rel.startswith("..") else rel


def iso(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9).isoformat(timespec="seconds")


def _bloque(lineas: list[str], titulo: str, elementos: list[str], limite: int) -> None:
    lineas.append("")
    lineas.append(f"{titulo}: {len(elementos)}")
    lineas.extend(f"  {e}" for e in elementos[:limite])
    if len(elementos) > limite:
        lineas.append(f"  ... y {len(elementos) - limite} más (ver JSON/CSV)")


def _lista(datos: dict[str, Any], nombre: str) -> list[Any]:
    valor = datos.get(nombre, [])
    if not isinstance(valor, list):
        raise InformeInvalido(f"'{nombre}' debe ser una lista")
    return valor


def _texto(valor: Any, nombre: str) -> str:
    if not isinstance(valor, str) or not valor or "\x00" in valor:
        raise InformeInvalido(f"'{nombre}' debe ser un texto no vacío")
    return valor


def _entero(valor: Any, nombre: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise InformeInvalido(f"'{nombre}' debe ser un entero no negativo")
    return valor


def _archivo(datos: Any) -> Archivo:
    if not isinstance(datos, dict):
        raise InformeInvalido("cada archivo debe ser un objeto")
    return Archivo(
        ruta=_texto(datos.get("ruta"), "ruta"),
        tamano=_entero(datos.get("tamano"), "tamano"),
        mtime_ns=_entero(datos.get("mtime_ns"), "mtime_ns"),
    )


def _grupo(datos: Any) -> GrupoDuplicados:
    if not isinstance(datos, dict):
        raise InformeInvalido("cada grupo de duplicados debe ser un objeto")
    return GrupoDuplicados(
        sha256=str(datos.get("sha256", "")),
        tamano=_entero(datos.get("tamano"), "tamano"),
        archivos=[_archivo(a) for a in _lista(datos, "archivos")],
        conservar=str(datos.get("conservar", "")),
    )


def _incidencia(datos: Any) -> Incidencia:
    if not isinstance(datos, dict):
        raise InformeInvalido("cada incidencia debe ser un objeto")
    return Incidencia(ruta=str(datos.get("ruta", "")), motivo=str(datos.get("motivo", "")))
