"""Análisis de una carpeta: duplicados, grandes, basura, 0 bytes, vacías y enlaces duros.

Solo lee. Nunca modifica nada en disco.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from organizador import NOMBRE_CUARENTENA
from organizador.report import Archivo, GrupoDuplicados, GrupoEnlaces, Incidencia, Informe
from organizador.rutas import es_enlace, esta_dentro, normalizar, para_os, validar_raiz

BLOQUE_PARCIAL = 64 * 1024
BLOQUE_LECTURA = 1024 * 1024

EXCLUSIONES_POR_DEFECTO: tuple[str, ...] = (
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "$RECYCLE.BIN", "System Volume Information", NOMBRE_CUARENTENA,
)
EXTENSIONES_BASURA_POR_DEFECTO: frozenset[str] = frozenset({
    ".tmp", ".temp", ".bak", ".old", ".swp", ".swo", ".dmp", ".chk",
    ".crdownload", ".part", ".partial",
})
NOMBRES_BASURA: frozenset[str] = frozenset({"thumbs.db", ".ds_store"})
UMBRAL_GRANDE_POR_DEFECTO = 100 * 1024 * 1024

Progreso = Callable[[str, int, int], None]


class Cancelado(Exception):
    """El trabajo se ha cancelado de forma cooperativa."""


@dataclass
class Opciones:
    umbral_grande: int = UMBRAL_GRANDE_POR_DEFECTO
    extensiones_basura: frozenset[str] = EXTENSIONES_BASURA_POR_DEFECTO
    excluir: tuple[str, ...] = ()
    usar_exclusiones_por_defecto: bool = True
    permitir_sistema: bool = False
    excluir_rutas: tuple[str, ...] = field(default_factory=tuple)  # rutas absolutas (p. ej. cuarentena)

    def patrones(self) -> tuple[str, ...]:
        base = EXCLUSIONES_POR_DEFECTO if self.usar_exclusiones_por_defecto else (NOMBRE_CUARENTENA,)
        return tuple(p for p in (*base, *self.excluir) if p)

    def a_dict(self) -> dict[str, object]:
        return {
            "umbral_grande": self.umbral_grande,
            "extensiones_basura": sorted(self.extensiones_basura),
            "excluir": list(self.patrones()),
            "permitir_sistema": self.permitir_sistema,
        }


def normalizar_extensiones(extensiones: Iterable[str]) -> frozenset[str]:
    resultado = set()
    for ext in extensiones:
        ext = ext.strip().lower()
        if ext:
            resultado.add(ext if ext.startswith(".") else "." + ext)
    return frozenset(resultado)


def elegir_conservada(archivos: Iterable[Archivo]) -> Archivo:
    """Criterio determinista: ruta más corta → mtime más antiguo → orden alfabético."""
    return min(archivos, key=lambda a: (len(a.ruta), a.mtime_ns, a.ruta.casefold(), a.ruta))


def es_basura(nombre: str, extensiones: frozenset[str]) -> bool:
    bajo = nombre.lower()
    if bajo in NOMBRES_BASURA or (bajo.endswith("~") and len(bajo) > 1):
        return True
    return os.path.splitext(bajo)[1] in extensiones


def hash_archivo(ruta: str, cancelar: threading.Event | None = None, parcial: bool = False,
                 tamano: int | None = None) -> str:
    """SHA-256 por bloques; comprueba la cancelación entre bloques.

    Con ``parcial=True`` solo lee los primeros y últimos 64 KB (si el archivo es
    pequeño, lee el archivo completo y el resultado coincide con el hash total).
    """
    h = hashlib.sha256()
    with open(para_os(ruta), "rb") as f:
        if parcial and tamano is not None and tamano > 2 * BLOQUE_PARCIAL:
            h.update(f.read(BLOQUE_PARCIAL))
            f.seek(-BLOQUE_PARCIAL, os.SEEK_END)
            h.update(f.read(BLOQUE_PARCIAL))
            return h.hexdigest()
        while True:
            if cancelar is not None and cancelar.is_set():
                raise Cancelado()
            bloque = f.read(BLOQUE_LECTURA)
            if not bloque:
                break
            h.update(bloque)
    return h.hexdigest()


class _Analisis:
    def __init__(self, raiz: str, opciones: Opciones, cancelar: threading.Event | None,
                 progreso: Progreso | None) -> None:
        self.raiz = raiz
        self.opciones = opciones
        self.cancelar = cancelar
        self.progreso = progreso or (lambda fase, hechos, total: None)
        self.patrones = [p.casefold() for p in opciones.patrones()]
        self.excluir_rutas = [normalizar(r) for r in opciones.excluir_rutas]
        self.informe = Informe(raiz=raiz, opciones=opciones.a_dict())

    def comprobar(self) -> None:
        if self.cancelar is not None and self.cancelar.is_set():
            raise Cancelado()

    def excluido(self, ruta: str, nombre: str) -> bool:
        nombre_cf = nombre.casefold()
        relativa = ruta[len(self.raiz):].lstrip("\\/").replace(os.sep, "/").casefold()
        for patron in self.patrones:
            if fnmatch.fnmatchcase(nombre_cf, patron) or fnmatch.fnmatchcase(relativa, patron):
                return True
        return any(esta_dentro(ruta, r) for r in self.excluir_rutas)

    # ------------------------------------------------------------ recorrido
    def recorrer(self) -> list[tuple[str, os.stat_result]]:
        archivos: list[tuple[str, os.stat_result]] = []
        hijos: dict[str, list[str]] = defaultdict(list)
        con_contenido: set[str] = set()
        orden: list[str] = []
        pendientes = [self.raiz]
        inf = self.informe
        while pendientes:
            self.comprobar()
            carpeta = pendientes.pop()
            orden.append(carpeta)
            try:
                entradas = list(os.scandir(para_os(carpeta)))
            except OSError as e:
                inf.errores.append(Incidencia(carpeta, _describir(e)))
                con_contenido.add(carpeta)
                continue
            for entrada in entradas:
                ruta = os.path.join(carpeta, entrada.name)
                if self.excluido(ruta, entrada.name):
                    inf.omitidos.append(Incidencia(ruta, "excluido"))
                    con_contenido.add(carpeta)
                    continue
                try:
                    st = os.lstat(para_os(ruta))
                except OSError as e:
                    inf.errores.append(Incidencia(ruta, _describir(e)))
                    con_contenido.add(carpeta)
                    continue
                if es_enlace(st):
                    inf.omitidos.append(Incidencia(ruta, "enlace simbólico o reparse point (no se sigue)"))
                    con_contenido.add(carpeta)
                elif stat.S_ISDIR(st.st_mode):
                    hijos[carpeta].append(ruta)
                    pendientes.append(ruta)
                elif stat.S_ISREG(st.st_mode):
                    archivos.append((ruta, st))
                    con_contenido.add(carpeta)
                    if len(archivos) % 500 == 0:
                        self.progreso("recorrido", len(archivos), 0)
                else:
                    inf.omitidos.append(Incidencia(ruta, "no es un archivo regular"))
                    con_contenido.add(carpeta)
        self.progreso("recorrido", len(archivos), len(archivos))

        # Una carpeta está vacía si no tiene contenido propio y todas sus subcarpetas lo están.
        vacia: dict[str, bool] = {}
        for carpeta in reversed(orden):  # los hijos se visitan después que el padre
            vacia[carpeta] = carpeta not in con_contenido and all(vacia[h] for h in hijos[carpeta])
        inf.vacias = sorted((c for c in orden if vacia[c] and c != self.raiz), key=str.casefold)
        return archivos

    # ----------------------------------------------------------- clasificar
    def clasificar(self, archivos: list[tuple[str, os.stat_result]]) -> list[Archivo]:
        inf, op = self.informe, self.opciones
        enlaces: dict[tuple[int, int], list[tuple[Archivo, int]]] = defaultdict(list)
        candidatos: list[Archivo] = []
        for ruta, st in archivos:
            a = Archivo(ruta=ruta, tamano=st.st_size, mtime_ns=st.st_mtime_ns)
            inf.total_archivos += 1
            inf.total_bytes += a.tamano
            if st.st_nlink > 1:
                # Mover un enlace duro no libera espacio: solo se informa.
                enlaces[(st.st_dev, st.st_ino)].append((a, st.st_nlink))
                continue
            if a.tamano == 0:
                inf.cero_bytes.append(a)
            else:
                candidatos.append(a)
            if a.tamano >= op.umbral_grande:
                inf.grandes.append(a)
            if es_basura(os.path.basename(ruta), op.extensiones_basura):
                inf.basura.append(a)
        inf.enlaces_duros = [
            GrupoEnlaces(archivos=sorted((a for a, _ in g), key=lambda a: a.ruta), enlaces_totales=g[0][1])
            for g in enlaces.values()
        ]
        inf.grandes.sort(key=lambda a: -a.tamano)
        inf.basura.sort(key=lambda a: a.ruta.casefold())
        inf.cero_bytes.sort(key=lambda a: a.ruta.casefold())
        return candidatos

    # ------------------------------------------------------------ duplicados
    def duplicados(self, candidatos: list[Archivo]) -> None:
        por_tamano: dict[int, list[Archivo]] = defaultdict(list)
        for a in candidatos:
            por_tamano[a.tamano].append(a)
        grupos = [g for g in por_tamano.values() if len(g) > 1]

        parciales = self._agrupar_por_hash(grupos, parcial=True, fase="hash parcial")
        # Si el archivo cabe entero en el hash parcial, ese hash ya es el SHA-256 total.
        completos: list[list[Archivo]] = []
        ya_completos: list[tuple[str, list[Archivo]]] = []
        for huella, grupo in parciales:
            if grupo[0].tamano <= 2 * BLOQUE_PARCIAL:
                ya_completos.append((huella, grupo))
            else:
                completos.append(grupo)
        finales = ya_completos + self._agrupar_por_hash(completos, parcial=False, fase="hash completo")

        resultado = []
        for huella, grupo in finales:
            grupo.sort(key=lambda a: a.ruta.casefold())
            conservada = elegir_conservada(grupo)
            resultado.append(GrupoDuplicados(sha256=huella, tamano=grupo[0].tamano, archivos=grupo,
                                             conservar=conservada.ruta))
        resultado.sort(key=lambda g: (-g.recuperable, g.conservar.casefold()))
        self.informe.duplicados = resultado

    def _agrupar_por_hash(self, grupos: list[list[Archivo]], parcial: bool,
                          fase: str) -> list[tuple[str, list[Archivo]]]:
        total = sum(len(g) for g in grupos)
        hechos = 0
        salida: list[tuple[str, list[Archivo]]] = []
        for grupo in grupos:
            por_hash: dict[str, list[Archivo]] = defaultdict(list)
            for a in grupo:
                self.comprobar()
                try:
                    huella = hash_archivo(a.ruta, self.cancelar, parcial=parcial, tamano=a.tamano)
                except OSError as e:
                    self.informe.errores.append(Incidencia(a.ruta, _describir(e)))
                else:
                    por_hash[huella].append(a)
                hechos += 1
                self.progreso(fase, hechos, total)
            salida.extend((h, g) for h, g in por_hash.items() if len(g) > 1)
        return salida


def analizar(ruta: str | os.PathLike[str], opciones: Opciones | None = None,
             cancelar: threading.Event | None = None, progreso: Progreso | None = None) -> Informe:
    """Analiza ``ruta`` y devuelve el informe. Lanza ``Cancelado`` o ``RutaNoPermitida``."""
    opciones = opciones or Opciones()
    raiz = validar_raiz(ruta, permitir_sistema=opciones.permitir_sistema)
    analisis = _Analisis(raiz, opciones, cancelar, progreso)
    archivos = analisis.recorrer()
    candidatos = analisis.clasificar(archivos)
    analisis.duplicados(candidatos)
    return analisis.informe


def _describir(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "permiso denegado o archivo bloqueado"
    return e.strerror or e.__class__.__name__
