from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from conftest import T_ANTIGUO, T_NUEVO, crear, puede_enlace_simbolico

from organizador import NOMBRE_CUARENTENA, quarantine
from organizador.analyzer import Opciones, analizar
from organizador.quarantine import (
    OperacionRechazada,
    aplicar,
    leer_log,
    mover_sin_sobrescribir,
    planificar,
    restaurar,
    seleccion_por_categorias,
)
from organizador.report import Archivo, GrupoDuplicados, Informe


def plan_por_categorias(informe: Informe, *categorias: str, **kw):
    return planificar(informe, seleccion_por_categorias(informe, categorias), **kw)


def cuarentena(raiz: Path) -> Path:
    return raiz / NOMBRE_CUARENTENA


def lineas_log(ruta: str) -> list[dict]:
    with open(ruta, encoding="utf-8") as f:
        return [json.loads(linea) for linea in f]


# ------------------------------------------------------------ flujo principal
def test_mueve_copias_y_conserva_una(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "sub/dir/b.txt", "igual")
    crear(raiz, "otra/c.txt", "igual")
    informe = analizar(raiz)
    plan = plan_por_categorias(informe, "duplicados")
    assert len(plan.a_mover) == 2
    resultado = aplicar(plan)

    assert (raiz / "a.txt").exists()
    assert not (raiz / "sub/dir/b.txt").exists()
    # Estructura relativa conservada dentro de la cuarentena.
    assert (cuarentena(raiz) / "sub/dir/b.txt").read_text() == "igual"
    assert (cuarentena(raiz) / "otra/c.txt").exists()
    entradas = lineas_log(resultado.log)
    assert entradas[0]["tipo"] == "cabecera"
    assert [e["tipo"] for e in entradas].count("movido") == 2


def test_categorias_solapadas_respetan_una_copia(raiz: Path) -> None:
    # Tres .bak idénticos: son duplicados y basura a la vez.
    for nombre in ("x1.bak", "x2.bak", "x3.bak"):
        crear(raiz, nombre, "idéntico")
    informe = analizar(raiz)
    assert len(informe.basura) == 3
    plan = plan_por_categorias(informe, "basura")  # solo basura: no debe vaciar el grupo
    assert len(plan.a_mover) == 2
    aplicar(plan)
    restantes = [p.name for p in raiz.glob("*.bak")]
    assert restantes == ["x1.bak"]


def test_union_de_categorias_sin_repetidos(raiz: Path) -> None:
    crear(raiz, "a.bak", "igual")
    crear(raiz, "bb.bak", "igual")
    informe = analizar(raiz)
    plan = plan_por_categorias(informe, "duplicados", "basura")
    assert len(plan.a_mover) == 1
    assert sorted(plan.a_mover[0].categorias) == ["basura", "duplicados"]
    assert not plan.a_mover[0].verificar_contenido


def test_grandes_solo_si_se_piden(raiz: Path) -> None:
    crear(raiz, "enorme.bin", b"g" * 5000)
    informe = analizar(raiz, Opciones(umbral_grande=1000))
    assert plan_por_categorias(informe, "duplicados", "basura").a_mover == []
    plan = plan_por_categorias(informe, "grandes")
    assert len(plan.a_mover) == 1
    aplicar(plan)
    assert (cuarentena(raiz) / "enorme.bin").exists()


def test_carpetas_vacias_rmdir_y_restaurar(raiz: Path) -> None:
    (raiz / "v1" / "v2").mkdir(parents=True)
    crear(raiz, "f.txt", "x")
    informe = analizar(raiz)
    plan = plan_por_categorias(informe, "vacias")
    assert [Path(a.ruta).name for a in plan.a_eliminar] == ["v2", "v1"]  # más profunda primero
    resultado = aplicar(plan)
    assert not (raiz / "v1").exists()
    assert len(resultado.carpetas) == 2
    restaurar(resultado.log)
    assert (raiz / "v1" / "v2").is_dir()


def test_carpeta_que_ya_no_esta_vacia_no_se_toca(raiz: Path) -> None:
    (raiz / "v").mkdir()
    informe = analizar(raiz)
    plan = plan_por_categorias(informe, "vacias")
    crear(raiz, "v/nuevo.txt", "x")
    resultado = aplicar(plan)
    assert (raiz / "v" / "nuevo.txt").exists()
    assert resultado.carpetas == [] and len(resultado.omitidos) == 1


# ---------------------------------------------------- revalidación en apply
def test_origen_modificado_se_omite(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    b = crear(raiz, "bb.txt", "igual")
    plan = plan_por_categorias(analizar(raiz), "duplicados")
    crear(raiz, "bb.txt", "IGUAL", mtime_ns=T_NUEVO)  # mismo tamaño, otra fecha
    resultado = aplicar(plan)
    assert b.exists() and resultado.movidos == []
    assert "fecha" in resultado.omitidos[-1].motivo


def test_conservada_borrada_tras_planificar_no_se_mueve_nada(raiz: Path) -> None:
    a = crear(raiz, "a.txt", "igual")
    b = crear(raiz, "bb.txt", "igual")
    plan = plan_por_categorias(analizar(raiz), "duplicados")
    a.unlink()
    resultado = aplicar(plan)
    assert b.exists(), "nunca se puede vaciar un grupo"
    assert "copia conservada" in resultado.omitidos[0].motivo


def test_conservada_modificada_tras_planificar(raiz: Path) -> None:
    a = crear(raiz, "a.txt", "igual")
    b = crear(raiz, "bb.txt", "igual")
    plan = plan_por_categorias(analizar(raiz), "duplicados")
    a.write_text("cambio")
    assert aplicar(plan).movidos == []
    assert b.exists()


def test_informe_que_ya_no_cuadra_al_planificar(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "bb.txt", "igual")
    informe = analizar(raiz)
    crear(raiz, "bb.txt", "igual", mtime_ns=T_ANTIGUO)
    plan = plan_por_categorias(informe, "duplicados")
    # bb.txt no supera la revalidación; a.txt queda sola como conservada.
    assert plan.a_mover == []


def test_contenido_distinto_con_metadatos_falsificados(raiz: Path) -> None:
    a = crear(raiz, "a.txt", "AAAA", mtime_ns=T_ANTIGUO)
    b = crear(raiz, "bb.txt", "BBBB", mtime_ns=T_ANTIGUO)
    informe = Informe(raiz=str(raiz), duplicados=[GrupoDuplicados(
        sha256="falso", tamano=4, conservar=str(b),
        archivos=[Archivo(str(a), 4, T_ANTIGUO), Archivo(str(b), 4, T_ANTIGUO)])])
    resultado = aplicar(plan_por_categorias(informe, "duplicados"))
    assert resultado.movidos == [] and b.exists()
    assert "contenido" in resultado.omitidos[-1].motivo


# ------------------------------------------------ informe manipulado (no fiable)
def test_conservada_se_recalcula_ignorando_el_informe(raiz: Path) -> None:
    a = crear(raiz, "a.txt", "igual")
    b = crear(raiz, "bb.txt", "igual")
    informe = analizar(raiz)
    informe.duplicados[0].conservar = str(b)  # el informe miente
    plan = plan_por_categorias(informe, "duplicados")
    assert [x.ruta for x in plan.a_mover] == [str(b)]
    aplicar(plan)
    assert a.exists()


def test_rutas_fuera_de_la_raiz_se_rechazan(raiz: Path, tmp_path: Path) -> None:
    fuera = crear(tmp_path, "fuera/victima.tmp", "x")
    crear(raiz, "a.tmp", "y")
    informe = analizar(raiz)
    st = fuera.stat()
    informe.basura.append(Archivo(str(fuera), st.st_size, st.st_mtime_ns))
    informe.basura.append(Archivo(os.path.join(str(raiz), "..", "fuera", "victima.tmp"), st.st_size, st.st_mtime_ns))
    informe.basura.append(Archivo("relativa.tmp", 1, 1))
    plan = plan_por_categorias(informe, "basura")
    assert [Path(a.ruta).name for a in plan.a_mover] == ["a.tmp"]
    assert len(plan.omitidos) == 3
    aplicar(plan)
    assert fuera.exists()


def test_ruta_que_atraviesa_enlace_se_rechaza(raiz: Path, tmp_path: Path) -> None:
    if not puede_enlace_simbolico(raiz):
        pytest.skip("sin permiso para crear enlaces simbólicos")
    fuera = crear(tmp_path, "fuera/victima.tmp", "x")
    (raiz / "puente").symlink_to(fuera.parent, target_is_directory=True)
    st = fuera.stat()
    informe = Informe(raiz=str(raiz), basura=[Archivo(str(raiz / "puente" / "victima.tmp"), st.st_size, st.st_mtime_ns)])
    plan = plan_por_categorias(informe, "basura")
    assert plan.a_mover == [] and fuera.exists()


def test_raiz_de_sistema_en_informe_se_rechaza(raiz: Path) -> None:
    informe = Informe(raiz=os.path.abspath(os.sep))
    with pytest.raises(OperacionRechazada):
        plan_por_categorias(informe, "basura")


def test_elemento_que_no_esta_en_el_informe(raiz: Path) -> None:
    otro = crear(raiz, "no_listado.txt", "x")
    informe = analizar(raiz)
    plan = planificar(informe, [("basura", str(otro)), ("duplicados", str(otro)), ("inventada", str(otro))])
    assert plan.a_mover == [] and len(plan.omitidos) == 3


def test_nada_dentro_de_la_cuarentena_se_mueve(raiz: Path) -> None:
    dentro = crear(raiz, f"{NOMBRE_CUARENTENA}/viejo.tmp", "x")
    st = dentro.stat()
    informe = Informe(raiz=str(raiz), basura=[Archivo(str(dentro), st.st_size, st.st_mtime_ns)])
    assert plan_por_categorias(informe, "basura").a_mover == []


# ------------------------------------------------------- volumen y colisiones
def test_cuarentena_en_otro_volumen_se_rechaza(raiz: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crear(raiz, "a.tmp", "x")
    informe = analizar(raiz)
    real = quarantine._dispositivo
    monkeypatch.setattr(quarantine, "_dispositivo",
                        lambda ruta: real(ruta) + (1 if NOMBRE_CUARENTENA in ruta else 0))
    with pytest.raises(OperacionRechazada, match="otro volumen"):
        plan_por_categorias(informe, "basura")


def test_cuarentena_no_puede_contener_la_raiz(raiz: Path) -> None:
    informe = analizar(raiz)
    with pytest.raises(OperacionRechazada):
        plan_por_categorias(informe, "basura", cuarentena=raiz.parent)


def test_cuarentena_personalizada_mismo_volumen(raiz: Path, tmp_path: Path) -> None:
    crear(raiz, "a.tmp", "x")
    destino = tmp_path / "mi_cuarentena"
    resultado = aplicar(plan_por_categorias(analizar(raiz), "basura", cuarentena=destino))
    assert (destino / "a.tmp").exists() and len(resultado.movidos) == 1


def test_colision_en_cuarentena_no_sobrescribe(raiz: Path) -> None:
    crear(raiz, f"{NOMBRE_CUARENTENA}/a.tmp", "anterior")
    crear(raiz, "a.tmp", "nuevo")
    resultado = aplicar(plan_por_categorias(analizar(raiz), "basura"))
    assert (cuarentena(raiz) / "a.tmp").read_text() == "anterior"
    assert Path(resultado.movidos[0][1]).name == "a (2).tmp"


def test_mover_sin_sobrescribir(tmp_path: Path) -> None:
    a = crear(tmp_path, "a", "A")
    b = crear(tmp_path, "b", "B")
    with pytest.raises(FileExistsError):
        mover_sin_sobrescribir(str(a), str(b))
    assert a.read_text() == "A" and b.read_text() == "B"
    mover_sin_sobrescribir(str(a), str(tmp_path / "c"))
    assert not a.exists() and (tmp_path / "c").read_text() == "A"


# ----------------------------------------------------------------- restaurar
def test_restaurar_ida_y_vuelta(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "sub/b.txt", "igual")
    crear(raiz, "t.tmp", "t")
    resultado = aplicar(plan_por_categorias(analizar(raiz), "duplicados", "basura"))
    assert len(resultado.movidos) == 2
    rest = restaurar(resultado.log)
    assert len(rest.movidos) == 2
    assert (raiz / "sub/b.txt").read_text() == "igual" and (raiz / "t.tmp").exists()
    assert not (cuarentena(raiz) / "sub").exists()  # subcarpetas vacías de la cuarentena limpiadas
    # Una segunda restauración no hace nada.
    assert restaurar(resultado.log).movidos == []


def test_restaurar_no_sobrescribe(raiz: Path) -> None:
    crear(raiz, "t.tmp", "original")
    resultado = aplicar(plan_por_categorias(analizar(raiz), "basura"))
    crear(raiz, "t.tmp", "nuevo")
    rest = restaurar(resultado.log)
    assert rest.movidos == []
    assert (raiz / "t.tmp").read_text() == "nuevo"
    assert (cuarentena(raiz) / "t.tmp").read_text() == "original"
    assert "no se sobrescribe" in rest.omitidos[0].motivo


def test_restaurar_log_manipulado(raiz: Path, tmp_path: Path) -> None:
    crear(raiz, "t.tmp", "x")
    resultado = aplicar(plan_por_categorias(analizar(raiz), "basura"))
    lineas = Path(resultado.log).read_text(encoding="utf-8").splitlines()
    malicioso = json.dumps({"tipo": "movido", "origen": str(tmp_path / "fuera.txt"),
                            "destino": str(cuarentena(raiz) / "t.tmp")})
    Path(resultado.log).write_text("\n".join([lineas[0], malicioso]) + "\n", encoding="utf-8")
    rest = restaurar(resultado.log)
    assert rest.movidos == [] and not (tmp_path / "fuera.txt").exists()
    assert "fuera de la raíz" in rest.omitidos[0].motivo


def test_log_sin_cabecera(tmp_path: Path) -> None:
    (tmp_path / "x.jsonl").write_text('{"tipo": "movido"}\n', encoding="utf-8")
    with pytest.raises(OperacionRechazada):
        leer_log(tmp_path / "x.jsonl")


# --------------------------------------------------------------- cancelación
def test_cancelar_apply(raiz: Path) -> None:
    for i in range(3):
        crear(raiz, f"f{i}.tmp", str(i))
    plan = plan_por_categorias(analizar(raiz), "basura")
    evento = threading.Event()
    evento.set()
    resultado = aplicar(plan, cancelar=evento)
    assert resultado.cancelado and resultado.movidos == []
    assert lineas_log(resultado.log)[-1]["tipo"] == "cancelado"


def test_cancelar_a_mitad_deja_log_coherente(raiz: Path) -> None:
    for i in range(4):
        crear(raiz, f"f{i}.tmp", str(i))
    plan = plan_por_categorias(analizar(raiz), "basura")
    evento = threading.Event()

    def progreso(fase: str, hechos: int, total: int) -> None:
        if hechos == 2:
            evento.set()

    resultado = aplicar(plan, cancelar=evento, progreso=progreso)
    assert resultado.cancelado and len(resultado.movidos) == 2
    movidos_log = [e for e in lineas_log(resultado.log) if e["tipo"] == "movido"]
    assert len(movidos_log) == 2
    restaurar(resultado.log)
    assert len(list(raiz.glob("*.tmp"))) == 4
