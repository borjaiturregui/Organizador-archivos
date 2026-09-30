from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from conftest import crear

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from organizador import NOMBRE_CUARENTENA  # noqa: E402
from organizador.web.app import crear_app  # noqa: E402

PUERTO = 8765
BASE = f"http://127.0.0.1:{PUERTO}"
TESTIGO = "testigo-de-prueba"
CAB = {"X-Testigo": TESTIGO}


@pytest.fixture
def app():
    return crear_app(PUERTO, testigo=TESTIGO)


@pytest.fixture
def cliente(app) -> TestClient:
    return TestClient(app, base_url=BASE)


def esperar(cliente: TestClient, id_: str, limite: float = 10) -> dict:
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        datos = cliente.get(f"/api/trabajos/{id_}", headers=CAB).json()
        if datos["estado"] != "en_curso":
            return datos
        time.sleep(0.02)
    raise AssertionError("el trabajo no terminó a tiempo")


def analizar(cliente: TestClient, raiz: Path) -> dict:
    r = cliente.post("/api/analisis", json={"ruta": str(raiz)}, headers=CAB)
    assert r.status_code == 202, r.text
    datos = esperar(cliente, r.json()["id"])
    assert datos["estado"] == "completado", datos
    return datos


def preparar(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "sub/b.txt", "igual")
    crear(raiz, "t.tmp", "t")
    (raiz / "vacia").mkdir()


# ------------------------------------------------------------------ seguridad
def test_pagina_inicial_incluye_testigo_y_cabeceras(cliente: TestClient) -> None:
    r = cliente.get("/")
    assert r.status_code == 200
    assert f'<meta name="testigo" content="{TESTIGO}">' in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers
    assert cliente.get("/static/app.js").status_code == 200


def test_testigo_aleatorio_por_arranque() -> None:
    a, b = crear_app(PUERTO), crear_app(PUERTO)
    assert a.state.seguridad.testigo != b.state.seguridad.testigo
    assert len(a.state.seguridad.testigo) >= 32


def test_api_sin_testigo_o_incorrecto(cliente: TestClient, raiz: Path) -> None:
    assert cliente.get("/api/trabajos/x").status_code == 403
    assert cliente.get("/api/trabajos/x", headers={"X-Testigo": "malo"}).status_code == 403
    assert cliente.post("/api/analisis", json={"ruta": str(raiz)}).status_code == 403
    assert cliente.post("/api/aplicar", json={"analisis": "x", "ids": ["d0-0"]}).status_code == 403
    assert cliente.post("/api/trabajos/x/cancelar").status_code == 403


def test_host_ajeno_rechazado(app) -> None:
    for host in ("http://evil.example:8765", "http://127.0.0.1:9999", "http://testserver"):
        otro = TestClient(app, base_url=host)
        assert otro.get("/").status_code == 403
        assert otro.get("/api/trabajos/x", headers=CAB).status_code == 403


def test_origen_ajeno_rechazado(cliente: TestClient, raiz: Path) -> None:
    for origen in ("http://evil.example", "http://127.0.0.1:9999", "null"):
        r = cliente.post("/api/analisis", json={"ruta": str(raiz)}, headers={**CAB, "Origin": origen})
        assert r.status_code == 403
    # Preflight CORS desde otro origen: rechazado, sin cabeceras CORS.
    r = cliente.options("/api/analisis", headers={"Origin": "http://evil.example",
                                                  "Access-Control-Request-Method": "POST"})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    r = cliente.get("/api/trabajos/x", headers={**CAB, "Origin": BASE})
    assert r.status_code == 404  # mismo origen: pasa la seguridad


def test_ruta_de_sistema_rechazada(cliente: TestClient) -> None:
    r = cliente.post("/api/analisis", json={"ruta": os.path.abspath(os.sep)}, headers=CAB)
    assert r.status_code == 400


# ------------------------------------------------------------------ flujo
def test_flujo_completo(cliente: TestClient, raiz: Path) -> None:
    preparar(raiz)
    datos = analizar(cliente, raiz)
    res = datos["resultados"]
    assert res["resumen"]["grupos_duplicados"] == 1
    archivos = res["duplicados"][0]["archivos"]
    conservada = [a for a in archivos if a["conservada"]]
    sobrante = [a for a in archivos if not a["conservada"]]
    assert len(conservada) == 1 and len(sobrante) == 1
    # Se piden también la conservada: el servidor la protege igualmente.
    ids = [a["id"] for a in archivos] + [res["basura"][0]["id"], res["vacias"][0]["id"]]
    r = cliente.post("/api/aplicar", json={"analisis": datos["id"], "ids": ids}, headers=CAB)
    assert r.status_code == 202, r.text
    fin = esperar(cliente, r.json()["id"])
    assert fin["estado"] == "completado", fin
    assert fin["aplicado"]["movidos"] == 2 and fin["aplicado"]["carpetas"] == 1
    assert (raiz / "a.txt").exists() and not (raiz / "sub/b.txt").exists()
    assert (raiz / NOMBRE_CUARENTENA / "sub" / "b.txt").exists()
    assert Path(fin["aplicado"]["log"]).exists()


def test_apply_solo_acepta_ids(cliente: TestClient, raiz: Path, tmp_path: Path) -> None:
    preparar(raiz)
    datos = analizar(cliente, raiz)
    victima = crear(tmp_path, "victima.txt", "x")
    for ids in ([str(victima)], [str(raiz / "t.tmp")], ["d0-0", "../../etc/passwd"], ["z9"]):
        r = cliente.post("/api/aplicar", json={"analisis": datos["id"], "ids": ids}, headers=CAB)
        assert r.status_code == 400
    r = cliente.post("/api/aplicar", json={"analisis": datos["id"], "ids": ["b0"], "ruta": str(victima)},
                     headers=CAB)
    assert r.status_code == 202  # campos extra ignorados: solo cuentan los IDs
    esperar(cliente, r.json()["id"])
    assert victima.exists()
    r = cliente.post("/api/aplicar", json={"analisis": "no-existe", "ids": ["b0"]}, headers=CAB)
    assert r.status_code == 404


def test_descargas(cliente: TestClient, raiz: Path) -> None:
    preparar(raiz)
    datos = analizar(cliente, raiz)
    r = cliente.get(f"/api/trabajos/{datos['id']}/informe.json", headers=CAB)
    assert r.status_code == 200 and r.json()["raiz"] == str(raiz)
    r = cliente.get(f"/api/trabajos/{datos['id']}/informe.csv", headers=CAB)
    assert r.status_code == 200 and r.text.lstrip("﻿").startswith("categoria,")
    assert cliente.get(f"/api/trabajos/{datos['id']}/informe.xml", headers=CAB).status_code == 404
    assert cliente.get(f"/api/trabajos/{datos['id']}/informe.json").status_code == 403


# ------------------------------------------------------ un trabajo y cancelar
def _trabajo_bloqueante(app, liberar: threading.Event):
    def funcion(t):
        while not t.cancelar.is_set() and not liberar.is_set():
            time.sleep(0.01)

    return app.state.gestor.lanzar("analisis", funcion)


def test_un_solo_trabajo_a_la_vez(app, cliente: TestClient, raiz: Path) -> None:
    preparar(raiz)
    liberar = threading.Event()
    bloqueante = _trabajo_bloqueante(app, liberar)
    try:
        r = cliente.post("/api/analisis", json={"ruta": str(raiz)}, headers=CAB)
        assert r.status_code == 409
    finally:
        liberar.set()
    assert esperar(cliente, bloqueante.id)["estado"] == "completado"
    assert cliente.post("/api/analisis", json={"ruta": str(raiz)}, headers=CAB).status_code == 202


def test_apply_bloqueado_mientras_analiza(app, cliente: TestClient, raiz: Path) -> None:
    preparar(raiz)
    datos = analizar(cliente, raiz)
    liberar = threading.Event()
    bloqueante = _trabajo_bloqueante(app, liberar)
    r = cliente.post("/api/aplicar", json={"analisis": datos["id"], "ids": ["b0"]}, headers=CAB)
    assert r.status_code == 409
    liberar.set()
    esperar(cliente, bloqueante.id)


def test_cancelacion(app, cliente: TestClient) -> None:
    bloqueante = _trabajo_bloqueante(app, threading.Event())
    r = cliente.post(f"/api/trabajos/{bloqueante.id}/cancelar", headers=CAB)
    assert r.status_code == 200
    assert esperar(cliente, bloqueante.id)["estado"] == "cancelado"


def test_cancelacion_real_de_analisis(cliente: TestClient, raiz: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import organizador.web.app as modulo

    original = modulo.analizar

    def lento(ruta, opciones, cancelar, progreso):
        def progreso_lento(fase, hechos, total):
            time.sleep(0.05)
            progreso(fase, hechos, total)
        return original(ruta, opciones, cancelar=cancelar, progreso=progreso_lento)

    monkeypatch.setattr(modulo, "analizar", lento)
    for i in range(30):
        crear(raiz, f"d{i}/a.txt", "igual")
    r = cliente.post("/api/analisis", json={"ruta": str(raiz)}, headers=CAB)
    id_ = r.json()["id"]
    cliente.post(f"/api/trabajos/{id_}/cancelar", headers=CAB)
    assert esperar(cliente, id_)["estado"] == "cancelado"


def test_trabajo_inexistente(cliente: TestClient) -> None:
    assert cliente.get("/api/trabajos/nada", headers=CAB).status_code == 404
    assert cliente.post("/api/trabajos/nada/cancelar", headers=CAB).status_code == 404
