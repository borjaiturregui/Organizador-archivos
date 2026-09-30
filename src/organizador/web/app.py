"""Servidor web local (FastAPI): análisis y apply como trabajos en segundo plano.

* Un solo trabajo en marcha (análisis **o** apply).
* Cancelación cooperativa de ambos.
* Apply solo acepta identificadores de resultados de un análisis de este
  servidor; nunca rutas enviadas por el cliente. La cuarentena es fija.
"""

from __future__ import annotations

import html
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from organizador import NOMBRE_CUARENTENA, __version__
from organizador.analyzer import (
    EXTENSIONES_BASURA_POR_DEFECTO,
    UMBRAL_GRANDE_POR_DEFECTO,
    Cancelado,
    Opciones,
    analizar,
    normalizar_extensiones,
)
from organizador.quarantine import OperacionRechazada, Resultado, aplicar, planificar
from organizador.report import Informe, iso, relativa
from organizador.rutas import RutaNoPermitida, validar_raiz
from organizador.web.security import HOST, MiddlewareSeguridad, Seguridad

CARPETA = Path(__file__).parent
MAX_INCIDENCIAS = 1000
MAX_TRABAJOS = 20


# ---------------------------------------------------------------------------
# Trabajos
# ---------------------------------------------------------------------------
@dataclass
class Trabajo:
    id: str
    tipo: str  # "analisis" | "aplicar"
    estado: str = "en_curso"  # en_curso | completado | cancelado | error
    fase: str = ""
    hechos: int = 0
    total: int = 0
    error: str | None = None
    informe: Informe | None = None
    resultado: Resultado | None = None
    elementos: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (categoría, ruta)
    cancelar: threading.Event = field(default_factory=threading.Event)

    def progreso(self, fase: str, hechos: int, total: int) -> None:
        self.fase, self.hechos, self.total = fase, hechos, total


class Ocupado(Exception):
    pass


class GestorTrabajos:
    def __init__(self) -> None:
        self._cerrojo = threading.Lock()
        self.trabajos: dict[str, Trabajo] = {}
        self.actual: Trabajo | None = None

    def lanzar(self, tipo: str, funcion: Callable[[Trabajo], None]) -> Trabajo:
        with self._cerrojo:
            if self.actual is not None and self.actual.estado == "en_curso":
                raise Ocupado()
            trabajo = Trabajo(id=uuid.uuid4().hex, tipo=tipo)
            self.actual = trabajo
            self.trabajos[trabajo.id] = trabajo
            while len(self.trabajos) > MAX_TRABAJOS:
                self.trabajos.pop(next(iter(self.trabajos)))
        threading.Thread(target=self._ejecutar, args=(trabajo, funcion), daemon=True).start()
        return trabajo

    @staticmethod
    def _ejecutar(trabajo: Trabajo, funcion: Callable[[Trabajo], None]) -> None:
        try:
            funcion(trabajo)
        except Cancelado:
            trabajo.estado = "cancelado"
        except (RutaNoPermitida, OperacionRechazada, OSError, ValueError) as e:
            trabajo.estado, trabajo.error = "error", str(e)
        except Exception as e:  # pragma: no cover - red de seguridad
            trabajo.estado, trabajo.error = "error", f"error inesperado: {e}"
        else:
            trabajo.estado = "cancelado" if trabajo.cancelar.is_set() else "completado"

    def obtener(self, id_: str) -> Trabajo:
        trabajo = self.trabajos.get(id_)
        if trabajo is None:
            raise HTTPException(404, "trabajo no encontrado")
        return trabajo


# ---------------------------------------------------------------------------
# Modelos de petición
# ---------------------------------------------------------------------------
class PeticionAnalisis(BaseModel):
    ruta: str = Field(min_length=1, max_length=4096)
    umbral_grande_mb: float = Field(default=UMBRAL_GRANDE_POR_DEFECTO / 1024**2, gt=0, le=10**7)
    excluir: list[str] = Field(default_factory=list, max_length=200)
    ext_basura: list[str] = Field(default_factory=list, max_length=200)


class PeticionAplicar(BaseModel):
    analisis: str = Field(min_length=1, max_length=64)
    ids: list[str] = Field(min_length=1, max_length=1_000_000)


# ---------------------------------------------------------------------------
# Serialización para el frontend
# ---------------------------------------------------------------------------
def _asignar_ids(informe: Informe) -> dict[str, tuple[str, str]]:
    elementos: dict[str, tuple[str, str]] = {}
    for i, g in enumerate(informe.duplicados):
        for j, a in enumerate(g.archivos):
            elementos[f"d{i}-{j}"] = ("duplicados", a.ruta)
    for prefijo, categoria, lista in (("b", "basura", informe.basura), ("g", "grandes", informe.grandes)):
        for i, a in enumerate(lista):
            elementos[f"{prefijo}{i}"] = (categoria, a.ruta)
    for i, v in enumerate(informe.vacias):
        elementos[f"v{i}"] = ("vacias", v)
    return elementos


def _resultados_analisis(informe: Informe) -> dict[str, Any]:
    rel = lambda r: relativa(r, informe.raiz)  # noqa: E731
    conservadas = {g.conservar for g in informe.duplicados}

    def archivo(a, id_=None):
        datos = {"ruta": rel(a.ruta), "tamano": a.tamano, "modificado": iso(a.mtime_ns),
                 "conservada": a.ruta in conservadas}
        if id_:
            datos["id"] = id_
        return datos

    return {
        "raiz": informe.raiz,
        "cuarentena": NOMBRE_CUARENTENA,
        "resumen": informe.resumen(),
        "duplicados": [
            {"tamano": g.tamano, "sha256": g.sha256,
             "archivos": [archivo(a, f"d{i}-{j}") for j, a in enumerate(g.archivos)]}
            for i, g in enumerate(informe.duplicados)
        ],
        "basura": [archivo(a, f"b{i}") for i, a in enumerate(informe.basura)],
        "grandes": [archivo(a, f"g{i}") for i, a in enumerate(informe.grandes)],
        "vacias": [{"id": f"v{i}", "ruta": rel(v)} for i, v in enumerate(informe.vacias)],
        "cero_bytes": [archivo(a) for a in informe.cero_bytes],
        "enlaces_duros": [{"archivos": [rel(a.ruta) for a in g.archivos], "enlaces_totales": g.enlaces_totales}
                          for g in informe.enlaces_duros],
        "omitidos": [{"ruta": rel(i.ruta), "motivo": i.motivo} for i in informe.omitidos[:MAX_INCIDENCIAS]],
        "errores": [{"ruta": rel(i.ruta), "motivo": i.motivo} for i in informe.errores[:MAX_INCIDENCIAS]],
    }


def _resultado_aplicar(resultado: Resultado, raiz: str) -> dict[str, Any]:
    return {
        "movidos": len(resultado.movidos),
        "carpetas": len(resultado.carpetas),
        "omitidos": [{"ruta": relativa(i.ruta, raiz), "motivo": i.motivo}
                     for i in resultado.omitidos[:MAX_INCIDENCIAS]],
        "log": resultado.log,
        "cancelado": resultado.cancelado,
    }


def _estado(trabajo: Trabajo) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "id": trabajo.id, "tipo": trabajo.tipo, "estado": trabajo.estado, "fase": trabajo.fase,
        "hechos": trabajo.hechos, "total": trabajo.total, "error": trabajo.error,
    }
    if trabajo.estado == "completado" and trabajo.informe is not None and trabajo.tipo == "analisis":
        datos["resultados"] = _resultados_analisis(trabajo.informe)
    if trabajo.resultado is not None:
        raiz = trabajo.informe.raiz if trabajo.informe else ""
        datos["aplicado"] = _resultado_aplicar(trabajo.resultado, raiz)
    return datos


# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------
def crear_app(puerto: int = 8765, testigo: str | None = None) -> FastAPI:
    seguridad = Seguridad(puerto, testigo)
    gestor = GestorTrabajos()
    app = FastAPI(title="organizador-archivos", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.seguridad = seguridad
    app.state.gestor = gestor
    app.add_middleware(MiddlewareSeguridad, seguridad=seguridad)
    app.mount("/static", StaticFiles(directory=CARPETA / "static"), name="static")
    plantilla = (CARPETA / "templates" / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def inicio() -> HTMLResponse:
        pagina = plantilla.replace("{{TESTIGO}}", html.escape(seguridad.testigo, quote=True))
        return HTMLResponse(pagina.replace("{{VERSION}}", __version__))

    @app.post("/api/analisis", status_code=202)
    def lanzar_analisis(peticion: PeticionAnalisis) -> dict[str, str]:
        try:
            validar_raiz(peticion.ruta)  # en la web no se pueden forzar rutas de sistema
        except RutaNoPermitida as e:
            raise HTTPException(400, str(e)) from None
        opciones = Opciones(
            umbral_grande=int(peticion.umbral_grande_mb * 1024**2),
            extensiones_basura=EXTENSIONES_BASURA_POR_DEFECTO | normalizar_extensiones(peticion.ext_basura),
            excluir=tuple(p.strip() for p in peticion.excluir if p.strip()),
        )

        def trabajo_analisis(t: Trabajo) -> None:
            t.informe = analizar(peticion.ruta, opciones, cancelar=t.cancelar, progreso=t.progreso)
            t.elementos = _asignar_ids(t.informe)

        return {"id": _lanzar(gestor, "analisis", trabajo_analisis).id}

    @app.post("/api/aplicar", status_code=202)
    def lanzar_aplicar(peticion: PeticionAplicar) -> dict[str, str]:
        origen = gestor.obtener(peticion.analisis)
        if origen.tipo != "analisis" or origen.estado != "completado" or origen.informe is None:
            raise HTTPException(400, "el análisis indicado no está completado")
        desconocidos = [i for i in peticion.ids if i not in origen.elementos]
        if desconocidos:
            raise HTTPException(400, f"identificadores desconocidos: {', '.join(desconocidos[:5])}")
        elegidos = [origen.elementos[i] for i in dict.fromkeys(peticion.ids)]
        informe = origen.informe

        def trabajo_aplicar(t: Trabajo) -> None:
            t.informe = informe
            t.fase = "planificando"
            plan = planificar(informe, elegidos)  # cuarentena fija: <raíz>/_cuarentena_organizador
            t.resultado = aplicar(plan, cancelar=t.cancelar, progreso=t.progreso)

        return {"id": _lanzar(gestor, "aplicar", trabajo_aplicar).id}

    @app.get("/api/trabajos/{id_}")
    def estado(id_: str) -> dict[str, Any]:
        return _estado(gestor.obtener(id_))

    @app.post("/api/trabajos/{id_}/cancelar")
    def cancelar(id_: str) -> dict[str, str]:
        trabajo = gestor.obtener(id_)
        if trabajo.estado == "en_curso":
            trabajo.cancelar.set()
        return {"estado": trabajo.estado}

    @app.get("/api/trabajos/{id_}/informe.{formato}")
    def descargar(id_: str, formato: str) -> Response:
        trabajo = gestor.obtener(id_)
        if trabajo.tipo != "analisis" or trabajo.informe is None:
            raise HTTPException(404, "no hay informe para este trabajo")
        if formato == "json":
            return JSONResponse(trabajo.informe.a_dict(),
                                headers={"Content-Disposition": 'attachment; filename="informe.json"'})
        if formato == "csv":
            return Response("﻿" + trabajo.informe.a_csv(), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="informe.csv"'})
        raise HTTPException(404, "formato no soportado")

    return app


def _lanzar(gestor: GestorTrabajos, tipo: str, funcion: Callable[[Trabajo], None]) -> Trabajo:
    try:
        return gestor.lanzar(tipo, funcion)
    except Ocupado:
        raise HTTPException(409, "ya hay un trabajo en marcha; espera a que termine o cancélalo") from None


def ejecutar_servidor(puerto: int = 8765, abrir_navegador: bool = True) -> None:  # pragma: no cover
    import uvicorn

    url = f"http://{HOST}:{puerto}/"
    print(f"organizador-archivos web en {url}  (Ctrl+C para salir)")
    if abrir_navegador:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(crear_app(puerto), host=HOST, port=puerto, log_level="warning")
