"""Interfaz de línea de comandos: ``organizador analyze | apply | restore | web``."""

from __future__ import annotations

import re
import signal
import sys
import threading
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from organizador import __version__
from organizador.analyzer import (
    EXTENSIONES_BASURA_POR_DEFECTO,
    UMBRAL_GRANDE_POR_DEFECTO,
    Cancelado,
    Opciones,
    analizar,
    normalizar_extensiones,
)
from organizador.quarantine import (
    OperacionRechazada,
    Resultado,
    aplicar,
    leer_log,
    planificar,
    restaurar,
    seleccion_por_categorias,
)
from organizador.report import Informe, InformeInvalido, relativa, tamano_legible
from organizador.rutas import RutaNoPermitida

app = typer.Typer(
    help="Analiza carpetas (duplicados, grandes, basura, vacías) y organiza de forma segura con cuarentena.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)

_UNIDADES = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2,
             "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4}


def parsear_tamano(texto: str) -> int:
    """'100MB' → 104857600. Acepta B, KB, MB, GB, TB (base 1024) y decimales."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([KMGT]?B?)\s*", texto.upper())
    if not m:
        raise typer.BadParameter(f"tamaño no válido: {texto!r} (ejemplos: 500KB, 100MB, 2GB)")
    return int(float(m.group(1).replace(",", ".")) * _UNIDADES[m.group(2)])


def _separar(valores: list[str] | None) -> list[str]:
    return [v.strip() for bruto in (valores or []) for v in bruto.split(",") if v.strip()]


def _error(mensaje: str, codigo: int = 1) -> typer.Exit:
    typer.secho(f"Error: {mensaje}", fg=typer.colors.RED, err=True)
    return typer.Exit(codigo)


class _Ctrl_C:
    """Convierte Ctrl+C en cancelación cooperativa: el elemento en curso termina limpio."""

    def __init__(self) -> None:
        self.evento = threading.Event()
        self._anterior: Any = None

    def __enter__(self) -> threading.Event:
        if threading.current_thread() is threading.main_thread():
            self._anterior = signal.signal(signal.SIGINT, self._manejar)
        return self.evento

    def _manejar(self, *_: object) -> None:
        if self.evento.is_set():
            raise KeyboardInterrupt
        typer.secho("\nCancelando... (Ctrl+C otra vez para forzar)", fg=typer.colors.YELLOW, err=True)
        self.evento.set()

    def __exit__(self, *_: object) -> None:
        if self._anterior is not None:
            signal.signal(signal.SIGINT, self._anterior)


def _confirmar(pregunta: str) -> bool:
    """Confirmación en español: acepta s/sí/si (y también y/yes)."""
    while True:
        respuesta = typer.prompt(f"{pregunta} [s/N]", default="n", show_default=False).strip().lower()
        if respuesta in ("s", "si", "sí", "y", "yes"):
            return True
        if respuesta in ("n", "no", ""):
            return False


def _progreso():
    ultima = {"fase": None}

    def informar(fase: str, hechos: int, total: int) -> None:
        if fase != ultima["fase"]:
            ultima["fase"] = fase
            typer.echo(f"… {fase}", err=True)

    return informar


def _version(valor: bool) -> None:
    if valor:
        typer.echo(f"organizador-archivos {__version__}")
        raise typer.Exit()


@app.callback()
def principal(
    version: Annotated[Optional[bool], typer.Option("--version", callback=_version, is_eager=True,
                                                    help="Muestra la versión.")] = None,
) -> None:
    """Organizador de archivos seguro: nunca borra archivos, los mueve a cuarentena."""


# ---------------------------------------------------------------------------
@app.command()
def analyze(
    ruta: Annotated[Path, typer.Argument(help="Carpeta a analizar.")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Guardar informe JSON.")] = None,
    csv: Annotated[Optional[Path], typer.Option("--csv", help="Guardar informe CSV.")] = None,
    umbral_grande: Annotated[str, typer.Option(help="Tamaño a partir del cual un archivo es 'grande'.")]
    = f"{UMBRAL_GRANDE_POR_DEFECTO // 1024**2}MB",
    excluir: Annotated[Optional[list[str]], typer.Option(
        "--exclude", "--excluir", "-e", help="Patrón a excluir (nombre o ruta relativa; admite * y ?). Repetible.")] = None,
    sin_exclusiones_por_defecto: Annotated[bool, typer.Option(
        "--sin-exclusiones-por-defecto", help="No excluir .git, node_modules, .venv, etc. (la cuarentena se excluye siempre).")] = False,
    ext_basura: Annotated[Optional[list[str]], typer.Option(
        help="Extensiones basura adicionales (p. ej. .log,.cache). Repetible.")] = None,
    solo_ext_basura: Annotated[Optional[list[str]], typer.Option(
        help="Sustituye la lista de extensiones basura por esta.")] = None,
    permitir_sistema: Annotated[bool, typer.Option(
        "--permitir-sistema", help="Permite analizar rutas de sistema o raíces de unidad (bloqueadas por defecto).")] = False,
    limite: Annotated[int, typer.Option(help="Elementos por sección en la consola.")] = 10,
) -> None:
    """Analiza una carpeta y muestra el informe (opcionalmente lo guarda en JSON/CSV)."""
    extensiones = (normalizar_extensiones(_separar(solo_ext_basura)) if solo_ext_basura
                   else EXTENSIONES_BASURA_POR_DEFECTO | normalizar_extensiones(_separar(ext_basura)))
    opciones = Opciones(
        umbral_grande=parsear_tamano(umbral_grande),
        extensiones_basura=extensiones,
        excluir=tuple(excluir or ()),
        usar_exclusiones_por_defecto=not sin_exclusiones_por_defecto,
        permitir_sistema=permitir_sistema,
    )
    for destino in (output, csv):
        if destino is not None and not destino.resolve().parent.is_dir():
            raise _error(f"la carpeta de destino de '{destino}' no existe")
    with _Ctrl_C() as cancelar:
        try:
            informe = analizar(ruta, opciones, cancelar=cancelar, progreso=_progreso())
        except RutaNoPermitida as e:
            raise _error(str(e)) from None
        except Cancelado:
            raise _error("análisis cancelado", 130) from None

    typer.echo(informe.a_texto(limite=limite))
    try:
        if output:
            informe.guardar_json(output)
            typer.secho(f"\nInforme JSON guardado en {output}", fg=typer.colors.GREEN)
        if csv:
            informe.guardar_csv(csv)
            typer.secho(f"Informe CSV guardado en {csv}", fg=typer.colors.GREEN)
    except OSError as e:
        raise _error(f"no se pudo guardar el informe: {e.strerror or e}") from None


# ---------------------------------------------------------------------------
@app.command()
def apply(
    informe_json: Annotated[Path, typer.Argument(metavar="INFORME", help="Informe JSON generado por analyze.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No pedir confirmación.")] = False,
    duplicados: Annotated[bool, typer.Option("--duplicados/--sin-duplicados", help="Mover las copias duplicadas sobrantes.")] = True,
    basura: Annotated[bool, typer.Option("--basura/--sin-basura", help="Mover los archivos basura/temporales.")] = True,
    grandes: Annotated[bool, typer.Option("--grandes", help="Mover también los archivos grandes.")] = False,
    vacias: Annotated[bool, typer.Option("--vacias", help="Eliminar carpetas vacías (rmdir, restaurable).")] = False,
    cuarentena: Annotated[Optional[Path], typer.Option(
        help="Carpeta de cuarentena (misma unidad). Por defecto <raíz>/_cuarentena_organizador.")] = None,
    simular: Annotated[bool, typer.Option("--simular", help="Solo mostrar el plan, sin mover nada.")] = False,
    permitir_sistema: Annotated[bool, typer.Option("--permitir-sistema", help="Permite raíces de sistema.")] = False,
) -> None:
    """Mueve a cuarentena lo indicado en el informe, revalidando cada elemento."""
    try:
        informe = Informe.cargar_json(informe_json)
    except (OSError, InformeInvalido) as e:
        raise _error(f"no se puede leer el informe: {e}") from None

    categorias = [c for c, activa in (("duplicados", duplicados), ("basura", basura),
                                      ("grandes", grandes), ("vacias", vacias)) if activa]
    if not categorias:
        raise _error("no hay ninguna categoría seleccionada", 2)
    try:
        plan = planificar(informe, seleccion_por_categorias(informe, categorias), cuarentena, permitir_sistema)
    except OperacionRechazada as e:
        raise _error(str(e)) from None

    typer.echo(f"Raíz: {plan.raiz}\nCuarentena: {plan.cuarentena}\nCategorías: {', '.join(categorias)}\n")
    for accion in plan.a_mover:
        typer.echo(f"  mover   {relativa(accion.ruta, plan.raiz)}  [{', '.join(accion.categorias)}]")
    for accion in plan.a_eliminar:
        typer.echo(f"  rmdir   {relativa(accion.ruta, plan.raiz)}")
    if plan.omitidos:
        typer.echo(f"\nSe omitirán {len(plan.omitidos)} elementos:")
        for i in plan.omitidos:
            typer.echo(f"  omitir  {relativa(i.ruta, plan.raiz)}  ({i.motivo})")
    typer.echo(f"\nTotal: {len(plan.a_mover)} archivos ({tamano_legible(plan.bytes)}) a cuarentena, "
               f"{len(plan.a_eliminar)} carpetas vacías a eliminar.")

    if simular:
        typer.echo("Simulación: no se ha modificado nada.")
        return
    if not plan.acciones:
        typer.echo("Nada que hacer.")
        return
    if not yes and not _confirmar("¿Aplicar estos cambios?"):
        raise typer.Exit(1)

    with _Ctrl_C() as cancelar:
        try:
            resultado = aplicar(plan, cancelar=cancelar, progreso=_progreso())
        except OperacionRechazada as e:
            raise _error(str(e)) from None
    _mostrar_resultado(resultado, "movidos a cuarentena")
    typer.echo(f"Para deshacer: organizador restore \"{resultado.log}\"")
    if resultado.cancelado:
        raise typer.Exit(130)


# ---------------------------------------------------------------------------
@app.command()
def restore(
    log: Annotated[Path, typer.Argument(help="Log .jsonl generado por apply.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No pedir confirmación.")] = False,
    permitir_sistema: Annotated[bool, typer.Option("--permitir-sistema", help="Permite raíces de sistema.")] = False,
) -> None:
    """Devuelve los archivos de la cuarentena a su sitio. Nunca sobrescribe."""
    try:
        datos = leer_log(log)
    except (OSError, ValueError, OperacionRechazada) as e:
        raise _error(f"no se puede leer el log: {e}") from None
    typer.echo(f"Raíz: {datos.raiz}\nSe restaurarán hasta {len(datos.movidos)} archivos "
               f"y {len(datos.carpetas)} carpetas.")
    if datos.lineas_ignoradas:
        typer.secho(f"Aviso: {datos.lineas_ignoradas} líneas incompletas o corruptas del log se ignorarán "
                    "(p. ej. por un corte durante apply).", fg=typer.colors.YELLOW)
    if not (datos.movidos or datos.carpetas):
        typer.echo("Nada que restaurar.")
        return
    if not yes and not _confirmar("¿Restaurar?"):
        raise typer.Exit(1)
    with _Ctrl_C() as cancelar:
        try:
            resultado = restaurar(log, permitir_sistema=permitir_sistema, cancelar=cancelar)
        except OperacionRechazada as e:
            raise _error(str(e)) from None
    _mostrar_resultado(resultado, "restaurados")
    if resultado.cancelado:
        raise typer.Exit(130)


def _mostrar_resultado(resultado: Resultado, verbo: str) -> None:
    typer.secho(f"\n{len(resultado.movidos)} archivos {verbo}; {len(resultado.carpetas)} carpetas.",
                fg=typer.colors.GREEN)
    if resultado.omitidos:
        typer.secho(f"{len(resultado.omitidos)} omitidos:", fg=typer.colors.YELLOW)
        for i in resultado.omitidos:
            typer.echo(f"  {i.ruta}  ({i.motivo})")
    for aviso in resultado.avisos:
        typer.secho(f"Aviso: {aviso}", fg=typer.colors.YELLOW)
    if resultado.cancelado:
        typer.secho("Operación cancelada: lo ya hecho queda registrado en el log.", fg=typer.colors.YELLOW)
    typer.echo(f"Log: {resultado.log}")


# ---------------------------------------------------------------------------
@app.command()
def web(
    puerto: Annotated[int, typer.Option(help="Puerto local.")] = 8765,
    abrir: Annotated[bool, typer.Option(help="Abrir el navegador automáticamente.")] = True,
) -> None:
    """Arranca la interfaz web local (solo en 127.0.0.1). Requiere pip install -e \".[web]\"."""
    try:
        from organizador.web.app import ejecutar_servidor
    except ImportError:
        raise _error('faltan dependencias web. Instala con: pip install -e ".[web]"') from None
    ejecutar_servidor(puerto=puerto, abrir_navegador=abrir)


def main() -> None:
    # Evita errores de codificación con nombres raros en consolas de Windows.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            try:
                flujo.reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
