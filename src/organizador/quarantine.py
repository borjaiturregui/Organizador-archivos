"""Cuarentena segura: planificar, aplicar y restaurar.

Reglas (ver plan, secciones 5.2 a 5.5):

* El informe es un dato no fiable: se valida la raíz, cada ruta debe quedar
  dentro de ella tras resolver enlaces, y la copia conservada se recalcula.
* De cada grupo de duplicados queda siempre al menos una copia: la conservada
  nunca entra en el conjunto final a mover, venga de la categoría que venga.
* Antes de cada movimiento se revalidan origen y copia conservada (existencia,
  tamaño, mtime) y, si el motivo es solo "duplicado", que el contenido coincide.
* La cuarentena debe estar en el mismo volumen: se usa ``rename`` (nunca copiar
  y borrar) y se rechaza si el dispositivo es distinto.
* Nunca se sobrescribe: ni al mover a cuarentena ni al restaurar.
* Única eliminación: ``rmdir`` de carpetas vacías (registrada y restaurable).
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from organizador import MARCA_CUARENTENA, NOMBRE_CUARENTENA, __version__
from organizador.analyzer import Cancelado, elegir_conservada, hash_archivo
from organizador.report import Archivo, Incidencia, Informe
from organizador.rutas import (
    ES_WINDOWS,
    RutaNoPermitida,
    clave,
    es_enlace,
    esta_dentro,
    motivo_bloqueo_sistema,
    normalizar,
    para_os,
    relativa_a,
    validar_raiz,
)

CATEGORIAS = ("duplicados", "basura", "grandes", "vacias")
CATEGORIAS_POR_DEFECTO = ("duplicados", "basura")

Progreso = Callable[[str, int, int], None]


class OperacionRechazada(Exception):
    """La operación completa no se puede realizar de forma segura."""


@dataclass
class Accion:
    tipo: str  # "mover" | "rmdir"
    ruta: str
    categorias: list[str]
    esperado: Archivo | None = None
    conservada: Archivo | None = None  # copia que debe seguir intacta
    verificar_contenido: bool = False


@dataclass
class Plan:
    raiz: str
    cuarentena: str
    acciones: list[Accion] = field(default_factory=list)
    omitidos: list[Incidencia] = field(default_factory=list)

    @property
    def a_mover(self) -> list[Accion]:
        return [a for a in self.acciones if a.tipo == "mover"]

    @property
    def a_eliminar(self) -> list[Accion]:
        return [a for a in self.acciones if a.tipo == "rmdir"]

    @property
    def bytes(self) -> int:
        return sum(a.esperado.tamano for a in self.a_mover if a.esperado)


@dataclass
class Resultado:
    movidos: list[tuple[str, str]] = field(default_factory=list)
    carpetas: list[str] = field(default_factory=list)
    omitidos: list[Incidencia] = field(default_factory=list)
    log: str | None = None
    cancelado: bool = False
    avisos: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validación de rutas
# ---------------------------------------------------------------------------
def _ruta_segura(ruta: str, raiz: str, cuarentena: str) -> str:
    """Devuelve la ruta normalizada o lanza RutaNoPermitida.

    Rechaza rutas relativas, con ``..`` que escapen, que atraviesen enlaces o
    junctions, fuera de la raíz, la propia raíz y todo lo que esté en cuarentena.
    """
    if not os.path.isabs(ruta):
        raise RutaNoPermitida("ruta no absoluta")
    literal = os.path.normpath(ruta)
    real = normalizar(ruta)
    if clave(literal) != clave(real):
        raise RutaNoPermitida("la ruta atraviesa un enlace o contiene '..'")
    if not esta_dentro(real, raiz):
        raise RutaNoPermitida("fuera de la raíz analizada")
    if clave(real) == clave(raiz):
        raise RutaNoPermitida("es la propia raíz")
    if esta_dentro(real, cuarentena):
        raise RutaNoPermitida("está dentro de la cuarentena")
    return real


def _comprobar_archivo(ruta: str, esperado: Archivo) -> os.stat_result:
    """Revalida un archivo contra el informe. Lanza ValueError con el motivo."""
    try:
        st = os.lstat(para_os(ruta))
    except FileNotFoundError:
        raise ValueError("ya no existe") from None
    except OSError as e:
        raise ValueError(f"no se puede leer ({e.strerror or e})") from None
    if es_enlace(st):
        raise ValueError("es un enlace o reparse point")
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("no es un archivo regular")
    if st.st_nlink > 1:
        raise ValueError("tiene enlaces duros (moverlo no libera espacio)")
    if st.st_size != esperado.tamano:
        raise ValueError("el tamaño ha cambiado desde el análisis")
    if st.st_mtime_ns != esperado.mtime_ns:
        raise ValueError("la fecha de modificación ha cambiado desde el análisis")
    return st


def _dispositivo(ruta: str) -> int:
    """st_dev de la ruta o del primer ascendiente existente."""
    actual = ruta
    while True:
        try:
            return os.stat(para_os(actual)).st_dev
        except FileNotFoundError:
            padre = os.path.dirname(actual)
            if padre == actual:
                raise
            actual = padre


def resolver_cuarentena(raiz: str, cuarentena: str | os.PathLike[str] | None) -> str:
    ruta = normalizar(cuarentena) if cuarentena else os.path.join(raiz, NOMBRE_CUARENTENA)
    if clave(ruta) == clave(raiz) or esta_dentro(raiz, ruta):
        raise OperacionRechazada("la cuarentena no puede ser la raíz ni contenerla")
    if motivo_bloqueo_sistema(ruta):
        raise OperacionRechazada(f"la cuarentena no puede estar en una ruta de sistema: {ruta}")
    if _dispositivo(ruta) != _dispositivo(raiz):
        raise OperacionRechazada(
            "la cuarentena está en otro volumen: mover sería copiar y borrar (no atómico). "
            "Elige una cuarentena en la misma unidad."
        )
    return ruta


# ---------------------------------------------------------------------------
# Planificación
# ---------------------------------------------------------------------------
def seleccion_por_categorias(informe: Informe, categorias: Iterable[str]) -> list[tuple[str, str]]:
    """Todos los elementos de las categorías indicadas como pares (categoría, ruta)."""
    categorias = set(categorias)
    desconocidas = categorias - set(CATEGORIAS)
    if desconocidas:
        raise ValueError(f"categorías desconocidas: {', '.join(sorted(desconocidas))}")
    elegidos: list[tuple[str, str]] = []
    if "duplicados" in categorias:
        elegidos += [("duplicados", a.ruta) for g in informe.duplicados for a in g.archivos]
    if "basura" in categorias:
        elegidos += [("basura", a.ruta) for a in informe.basura]
    if "grandes" in categorias:
        elegidos += [("grandes", a.ruta) for a in informe.grandes]
    if "vacias" in categorias:
        elegidos += [("vacias", r) for r in informe.vacias]
    return elegidos


def planificar(informe: Informe, elegidos: Iterable[tuple[str, str]],
               cuarentena: str | os.PathLike[str] | None = None,
               permitir_sistema: bool = False) -> Plan:
    """Construye el conjunto final de acciones aplicando todas las reglas de seguridad.

    ``elegidos`` son pares (categoría, ruta) que deben existir en el informe.
    """
    try:
        raiz = validar_raiz(informe.raiz, permitir_sistema=permitir_sistema)
    except RutaNoPermitida as e:
        raise OperacionRechazada(f"raíz del informe no válida: {e}") from e
    if clave(raiz) != clave(os.path.normpath(informe.raiz)):
        raise OperacionRechazada("la raíz del informe es un enlace o no está normalizada")
    destino = resolver_cuarentena(raiz, cuarentena)
    plan = Plan(raiz=raiz, cuarentena=destino)

    def omitir(ruta: str, motivo: str) -> None:
        plan.omitidos.append(Incidencia(ruta, motivo))

    # Datos del informe indexados por ruta normalizada.
    entradas: dict[str, dict[str, Archivo]] = {c: {} for c in ("basura", "grandes")}
    vacias: dict[str, str] = {}
    grupo_de: dict[str, int] = {}
    miembros: dict[int, dict[str, Archivo]] = {}
    for categoria, lista in (("basura", informe.basura), ("grandes", informe.grandes)):
        for a in lista:
            entradas[categoria].setdefault(clave(a.ruta), a)
    for v in informe.vacias:
        vacias.setdefault(clave(v), v)

    # Recalcular la copia conservada de cada grupo con los miembros que siguen válidos.
    protegidas: dict[str, Archivo] = {}
    conservada_de: dict[int, Archivo] = {}
    for n, grupo in enumerate(informe.duplicados):
        validos: dict[str, Archivo] = {}
        for a in grupo.archivos:
            k = clave(a.ruta)
            grupo_de.setdefault(k, n)
            try:
                real = _ruta_segura(a.ruta, raiz, destino)
                _comprobar_archivo(real, a)
            except (RutaNoPermitida, ValueError):
                continue
            validos.setdefault(clave(real), Archivo(real, a.tamano, a.mtime_ns))
        miembros[n] = validos
        if validos:
            conservada = elegir_conservada(validos.values())
            conservada_de[n] = conservada
            protegidas[clave(conservada.ruta)] = conservada

    # Conjunto final: unión de categorías, sin rutas repetidas.
    finales: dict[str, Accion] = {}
    carpetas: dict[str, Accion] = {}
    for categoria, ruta in elegidos:
        if categoria not in CATEGORIAS:
            omitir(ruta, f"categoría desconocida '{categoria}'")
            continue
        try:
            real = _ruta_segura(ruta, raiz, destino)
        except RutaNoPermitida as e:
            omitir(ruta, str(e))
            continue
        k = clave(real)

        if categoria == "vacias":
            if k not in vacias:
                omitir(ruta, "no figura como carpeta vacía en el informe")
                continue
            carpetas.setdefault(k, Accion("rmdir", real, ["vacias"]))
            continue

        esperado: Archivo | None
        if categoria == "duplicados":
            g = grupo_de.get(clave(ruta))
            esperado = miembros.get(g, {}).get(k) if g is not None else None
            if esperado is None:
                omitir(ruta, "no es un duplicado válido en el informe" if g is None
                       else "no supera la revalidación (cambiado, inexistente o fuera de la raíz)")
                continue
        else:
            listado = entradas[categoria].get(clave(ruta))
            if listado is None:
                omitir(ruta, f"no figura en la categoría '{categoria}' del informe")
                continue
            esperado = Archivo(real, listado.tamano, listado.mtime_ns)

        if k in protegidas:
            omitir(real, "es la copia conservada de un grupo de duplicados (≥1 copia)")
            continue

        accion = finales.get(k)
        if accion is None:
            n_grupo = grupo_de.get(k, grupo_de.get(clave(ruta)))
            accion = Accion("mover", real, [], esperado=esperado,
                            conservada=conservada_de.get(n_grupo) if n_grupo is not None else None)
            finales[k] = accion
        if categoria not in accion.categorias:
            accion.categorias.append(categoria)

    for accion in finales.values():
        # Si solo se mueve por ser duplicado, se comprobará que el contenido es idéntico.
        accion.verificar_contenido = accion.categorias == ["duplicados"]
        try:
            _comprobar_archivo(accion.ruta, accion.esperado)  # type: ignore[arg-type]
        except ValueError as e:
            omitir(accion.ruta, str(e))
            continue
        plan.acciones.append(accion)

    # Carpetas vacías: las más profundas primero, después de mover archivos.
    plan.acciones.extend(sorted(carpetas.values(), key=lambda a: (-a.ruta.count(os.sep), a.ruta)))
    unicos: dict[tuple[str, str], Incidencia] = {}
    for i in plan.omitidos:
        unicos.setdefault((i.ruta, i.motivo), i)
    plan.omitidos = list(unicos.values())
    return plan


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------
class _Log:
    """JSON Lines; cada línea se escribe y se sincroniza en el momento."""

    def __init__(self, ruta: str, cabecera: dict[str, object]) -> None:
        self.ruta = ruta
        self._f = open(para_os(ruta), "x", encoding="utf-8")
        self.escribir({"tipo": "cabecera", **cabecera})

    def escribir(self, entrada: dict[str, object]) -> None:
        entrada = {"fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entrada}
        self._f.write(json.dumps(entrada, ensure_ascii=False) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def cerrar(self) -> None:
        self._f.close()


def _ruta_log(carpeta: str, prefijo: str) -> str:
    sello = datetime.now().strftime("%Y%m%d-%H%M%S")
    ruta = os.path.join(carpeta, f"{prefijo}_{sello}.jsonl")
    n = 1
    while os.path.lexists(para_os(ruta)):
        n += 1
        ruta = os.path.join(carpeta, f"{prefijo}_{sello}_{n}.jsonl")
    return ruta


# ---------------------------------------------------------------------------
# Movimiento sin sobrescritura
# ---------------------------------------------------------------------------
_SIN_ENLACES = {errno.EPERM, errno.EACCES, errno.EMLINK, errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def mover_sin_sobrescribir(origen: str, destino: str) -> None:
    """Mueve un archivo en el mismo volumen sin sobrescribir nunca el destino.

    * Windows: ``os.rename`` falla si el destino existe o si es otro volumen.
    * POSIX: ``rename`` sobrescribe en silencio, así que se usa ``link`` (que
      falla de forma atómica si el destino existe) seguido de ``unlink``.
    Nunca copia: entre volúmenes distintos falla con OSError.
    """
    o, d = para_os(origen), para_os(destino)
    if ES_WINDOWS:
        os.rename(o, d)
        return
    try:
        os.link(o, d, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in _SIN_ENLACES:
            raise
        # Sistema de archivos sin enlaces duros: comprobación + rename.
        if os.path.lexists(d):
            raise FileExistsError(errno.EEXIST, "el destino ya existe", destino) from None
        os.rename(o, d)
        return
    try:
        os.unlink(o)
    except OSError:
        os.unlink(d)
        raise


def _destino_libre(cuarentena: str, raiz: str, origen: str) -> str:
    base = os.path.join(cuarentena, relativa_a(origen, raiz))
    destino, n = base, 1
    raiz_nombre, ext = os.path.splitext(base)
    while os.path.lexists(para_os(destino)):
        n += 1
        destino = f"{raiz_nombre} ({n}){ext}"
    return destino


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------
def aplicar(plan: Plan, cancelar: threading.Event | None = None,
            progreso: Progreso | None = None, ruta_log: str | None = None) -> Resultado:
    """Ejecuta el plan revalidando cada elemento justo antes de actuar."""
    progreso = progreso or (lambda fase, hechos, total: None)
    resultado = Resultado(omitidos=list(plan.omitidos))

    os.makedirs(para_os(plan.cuarentena), exist_ok=True)
    st_c = os.lstat(para_os(plan.cuarentena))
    if es_enlace(st_c) or not stat.S_ISDIR(st_c.st_mode):
        raise OperacionRechazada("la cuarentena no es una carpeta normal")
    if clave(normalizar(plan.cuarentena)) != clave(plan.cuarentena):
        raise OperacionRechazada("la ruta de la cuarentena atraviesa un enlace")
    dispositivo = st_c.st_dev
    if dispositivo != os.stat(para_os(plan.raiz)).st_dev:
        raise OperacionRechazada("la cuarentena está en otro volumen")
    # La marca permite excluir la cuarentena en análisis futuros aunque tenga otro nombre.
    with open(para_os(os.path.join(plan.cuarentena, MARCA_CUARENTENA)), "a", encoding="utf-8"):
        pass

    log = _Log(ruta_log or _ruta_log(plan.cuarentena, "cuarentena_log"), {
        "herramienta": f"organizador-archivos {__version__}",
        "raiz": plan.raiz,
        "cuarentena": plan.cuarentena,
    })
    resultado.log = log.ruta
    for i in plan.omitidos:
        log.escribir({"tipo": "omitido", "ruta": i.ruta, "motivo": i.motivo})

    hashes: dict[str, str] = {}
    total = len(plan.acciones)
    try:
        for hechos, accion in enumerate(plan.acciones):
            progreso("aplicar", hechos, total)
            if cancelar is not None and cancelar.is_set():
                raise Cancelado()
            try:
                # Registro previo (write-ahead): si el proceso muere entre anotar y
                # actuar, restore sabe que el movimiento pudo ocurrir.
                if accion.tipo == "mover":
                    destino = _preparar_movimiento(plan, accion, dispositivo, hashes, cancelar)
                    log.escribir({"tipo": "intento", "origen": accion.ruta, "destino": destino})
                    mover_sin_sobrescribir(accion.ruta, destino)
                    resultado.movidos.append((accion.ruta, destino))
                    log.escribir({"tipo": "movido", "origen": accion.ruta, "destino": destino,
                                  "categorias": accion.categorias,
                                  "tamano": accion.esperado.tamano if accion.esperado else None})
                else:
                    ruta = _preparar_rmdir(plan, accion)
                    log.escribir({"tipo": "intento_rmdir", "ruta": ruta})
                    os.rmdir(para_os(ruta))  # solo borra carpetas vacías; si no lo está, falla
                    resultado.carpetas.append(accion.ruta)
                    log.escribir({"tipo": "rmdir", "ruta": accion.ruta})
            except (ValueError, RutaNoPermitida, OSError) as e:
                motivo = str(e) if not isinstance(e, OSError) else (e.strerror or str(e))
                resultado.omitidos.append(Incidencia(accion.ruta, motivo))
                log.escribir({"tipo": "omitido", "ruta": accion.ruta, "motivo": motivo})
        progreso("aplicar", total, total)
    except Cancelado:
        resultado.cancelado = True
        log.escribir({"tipo": "cancelado"})
    finally:
        log.cerrar()
    return resultado


def _preparar_movimiento(plan: Plan, accion: Accion, dispositivo: int, hashes: dict[str, str],
                         cancelar: threading.Event | None) -> str:
    """Revalida el origen y la copia conservada y devuelve un destino libre en cuarentena."""
    origen = _ruta_segura(accion.ruta, plan.raiz, plan.cuarentena)
    st = _comprobar_archivo(origen, accion.esperado)  # type: ignore[arg-type]
    if st.st_dev != dispositivo:
        raise ValueError("está en otro volumen que la cuarentena")
    c = accion.conservada
    if c is not None:
        try:
            _ruta_segura(c.ruta, plan.raiz, plan.cuarentena)
            _comprobar_archivo(c.ruta, c)
        except (ValueError, RutaNoPermitida) as e:
            raise ValueError(f"la copia conservada ya no es fiable ({c.ruta}: {e})") from None
        if accion.verificar_contenido:
            if c.ruta not in hashes:
                hashes[c.ruta] = hash_archivo(c.ruta, cancelar)
            if hash_archivo(origen, cancelar) != hashes[c.ruta]:
                raise ValueError("el contenido ya no coincide con la copia conservada")
    destino = _destino_libre(plan.cuarentena, plan.raiz, origen)
    os.makedirs(para_os(os.path.dirname(destino)), exist_ok=True)
    return destino


def _preparar_rmdir(plan: Plan, accion: Accion) -> str:
    ruta = _ruta_segura(accion.ruta, plan.raiz, plan.cuarentena)
    st = os.lstat(para_os(ruta))
    if es_enlace(st) or not stat.S_ISDIR(st.st_mode):
        raise ValueError("ya no es una carpeta normal")
    return ruta


# ---------------------------------------------------------------------------
# Restauración
# ---------------------------------------------------------------------------
@dataclass
class LogCuarentena:
    raiz: str
    cuarentena: str
    movidos: list[tuple[str, str]]  # (origen, destino) en orden de ejecución
    carpetas: list[str]
    sin_confirmar: set[tuple[str, str]] = field(default_factory=set)  # intento sin "movido"
    lineas_ignoradas: int = 0


def leer_log(ruta: str | os.PathLike[str]) -> LogCuarentena:
    """Lee y valida un log de cuarentena (también es un dato no fiable).

    Tolera líneas incompletas o corruptas (p. ej. la última tras un apagón): las
    ignora y las cuenta en ``lineas_ignoradas``. La cabecera sí es obligatoria.
    """
    lineas: list[Any] = []
    ignoradas = 0
    with open(ruta, encoding="utf-8", errors="replace") as f:
        for texto in f:
            if not texto.strip():
                continue
            try:
                lineas.append(json.loads(texto))
            except json.JSONDecodeError:
                if not lineas:
                    raise OperacionRechazada("el log no tiene cabecera válida") from None
                ignoradas += 1
    if not lineas or not isinstance(lineas[0], dict) or lineas[0].get("tipo") != "cabecera":
        raise OperacionRechazada("el log no tiene cabecera válida")
    cab = lineas[0]
    raiz, cuarentena = cab.get("raiz"), cab.get("cuarentena")
    if not isinstance(raiz, str) or not isinstance(cuarentena, str):
        raise OperacionRechazada("la cabecera del log no indica raíz y cuarentena")
    log = LogCuarentena(raiz=raiz, cuarentena=cuarentena, movidos=[], carpetas=[],
                        lineas_ignoradas=ignoradas)
    confirmados: set[tuple[str, str]] = set()
    for entrada in lineas[1:]:
        if not isinstance(entrada, dict):
            log.lineas_ignoradas += 1
            continue
        tipo = entrada.get("tipo")
        if tipo in ("intento", "movido") and isinstance(entrada.get("origen"), str) \
                and isinstance(entrada.get("destino"), str):
            par = (entrada["origen"], entrada["destino"])
            if par not in log.movidos:
                log.movidos.append(par)
            if tipo == "movido":
                confirmados.add(par)
        elif tipo in ("intento_rmdir", "rmdir") and isinstance(entrada.get("ruta"), str):
            if entrada["ruta"] not in log.carpetas:
                log.carpetas.append(entrada["ruta"])
    log.sin_confirmar = set(log.movidos) - confirmados
    return log


def restaurar(ruta_log: str | os.PathLike[str], permitir_sistema: bool = False,
              cancelar: threading.Event | None = None) -> Resultado:
    """Deshace un log de cuarentena. Nunca sobrescribe un archivo existente."""
    log = leer_log(ruta_log)
    try:
        raiz = validar_raiz(log.raiz, permitir_sistema=permitir_sistema)
    except RutaNoPermitida as e:
        raise OperacionRechazada(f"raíz del log no válida: {e}") from e
    cuarentena = normalizar(log.cuarentena)
    if esta_dentro(raiz, cuarentena) or motivo_bloqueo_sistema(cuarentena):
        raise OperacionRechazada("cuarentena del log no válida")

    resultado = Resultado()
    if log.lineas_ignoradas:
        resultado.avisos.append(
            f"se han ignorado {log.lineas_ignoradas} líneas incompletas o corruptas del log "
            "(p. ej. por un corte durante apply); revisa la cuarentena por si queda algo")
    registro = _Log(_ruta_log(os.path.dirname(os.path.abspath(ruta_log)), "restauracion_log"),
                    {"herramienta": f"organizador-archivos {__version__}", "raiz": raiz,
                     "cuarentena": cuarentena, "log_original": os.path.abspath(ruta_log)})
    resultado.log = registro.ruta

    def omitir(ruta: str, motivo: str) -> None:
        resultado.omitidos.append(Incidencia(ruta, motivo))
        registro.escribir({"tipo": "omitido", "ruta": ruta, "motivo": motivo})

    try:
        # Primero se recrean las carpetas (en apply se borraron al final).
        for carpeta in reversed(log.carpetas):
            try:
                real = _ruta_segura(carpeta, raiz, cuarentena)
                os.makedirs(para_os(real), exist_ok=True)
                resultado.carpetas.append(real)
                registro.escribir({"tipo": "carpeta_recreada", "ruta": real})
            except (RutaNoPermitida, OSError) as e:
                omitir(carpeta, str(e))

        for origen, destino in reversed(log.movidos):
            if cancelar is not None and cancelar.is_set():
                raise Cancelado()
            try:
                origen_real = _ruta_segura(origen, raiz, cuarentena)
                destino_real = normalizar(destino)
                if clave(destino_real) != clave(os.path.normpath(destino)) \
                        or not esta_dentro(destino_real, cuarentena):
                    raise RutaNoPermitida("el archivo en cuarentena no está dentro de la cuarentena")
            except RutaNoPermitida as e:
                omitir(origen, str(e))
                continue
            if os.path.lexists(para_os(origen_real)):
                omitir(origen_real, "ya existe un archivo en la ruta original; no se sobrescribe")
                continue
            try:
                st = os.lstat(para_os(destino_real))
            except FileNotFoundError:
                if (origen, destino) not in log.sin_confirmar:
                    omitir(destino_real, "no está en la cuarentena")
                # Intento sin confirmar y sin archivo en cuarentena: el movimiento no llegó a ocurrir.
                continue
            if es_enlace(st) or not stat.S_ISREG(st.st_mode):
                omitir(destino_real, "no es un archivo regular")
                continue
            try:
                os.makedirs(para_os(os.path.dirname(origen_real)), exist_ok=True)
                mover_sin_sobrescribir(destino_real, origen_real)
            except OSError as e:
                omitir(origen_real, e.strerror or str(e))
                continue
            resultado.movidos.append((destino_real, origen_real))
            registro.escribir({"tipo": "restaurado", "origen": origen_real, "desde": destino_real})
            _limpiar_vacias(os.path.dirname(destino_real), cuarentena)
    except Cancelado:
        resultado.cancelado = True
        registro.escribir({"tipo": "cancelado"})
    finally:
        registro.cerrar()
    return resultado


def _limpiar_vacias(carpeta: str, cuarentena: str) -> None:
    """Quita las subcarpetas de la cuarentena que han quedado vacías (solo rmdir)."""
    while esta_dentro(carpeta, cuarentena) and clave(carpeta) != clave(cuarentena):
        try:
            os.rmdir(para_os(carpeta))
        except OSError:
            return
        carpeta = os.path.dirname(carpeta)
