from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from conftest import T_ANTIGUO, T_NUEVO, crear, puede_enlace_duro, puede_enlace_simbolico

from organizador import NOMBRE_CUARENTENA
from organizador.analyzer import (
    BLOQUE_PARCIAL,
    Cancelado,
    Opciones,
    analizar,
    elegir_conservada,
    es_basura,
    normalizar_extensiones,
)
from organizador.report import Archivo, Informe, InformeInvalido
from organizador.rutas import (
    ES_WINDOWS,
    RutaNoPermitida,
    motivo_bloqueo_sistema,
    para_os,
    quitar_prefijo,
    validar_raiz,
)


def rutas(archivos) -> set[str]:
    return {a.ruta for a in archivos}


# ------------------------------------------------------------------ duplicados
def test_detecta_duplicados_y_elige_conservada(raiz: Path) -> None:
    a = crear(raiz, "a.txt", "igual")
    b = crear(raiz, "sub/b.txt", "igual")
    crear(raiz, "c.txt", "distinto")
    informe = analizar(raiz)
    assert len(informe.duplicados) == 1
    grupo = informe.duplicados[0]
    assert rutas(grupo.archivos) == {str(a), str(b)}
    assert grupo.conservar == str(a)  # ruta más corta
    assert informe.resumen()["bytes_recuperables"] == len("igual")


def test_criterio_conservada_desempates() -> None:
    corta = Archivo("C:/x/aa", 1, T_NUEVO)
    larga = Archivo("C:/x/aaa", 1, T_ANTIGUO)
    assert elegir_conservada([larga, corta]) is corta  # 1) ruta más corta
    vieja = Archivo("C:/x/bb", 1, T_ANTIGUO)
    assert elegir_conservada([corta, vieja]) is vieja  # 2) mtime más antiguo
    alfa = Archivo("C:/x/ab", 1, T_NUEVO)
    assert elegir_conservada([corta, alfa]) is corta  # 3) orden alfabético


def test_conservada_por_mtime_en_disco(raiz: Path) -> None:
    crear(raiz, "b1.txt", "mismo", mtime_ns=T_NUEVO)
    vieja = crear(raiz, "b2.txt", "mismo", mtime_ns=T_ANTIGUO)
    assert analizar(raiz).duplicados[0].conservar == str(vieja)


def test_cero_bytes_fuera_de_duplicados(raiz: Path) -> None:
    crear(raiz, "vacio1", b"")
    crear(raiz, "vacio2", b"")
    informe = analizar(raiz)
    assert informe.duplicados == []
    assert len(informe.cero_bytes) == 2


def test_hash_parcial_no_confunde_contenido_distinto(raiz: Path) -> None:
    tam = 3 * BLOQUE_PARCIAL
    comun_ini, comun_fin = b"A" * BLOQUE_PARCIAL, b"Z" * BLOQUE_PARCIAL
    crear(raiz, "x.bin", comun_ini + b"1" * (tam - 2 * BLOQUE_PARCIAL) + comun_fin)
    crear(raiz, "y.bin", comun_ini + b"2" * (tam - 2 * BLOQUE_PARCIAL) + comun_fin)
    crear(raiz, "z.bin", comun_ini + b"1" * (tam - 2 * BLOQUE_PARCIAL) + comun_fin)
    informe = analizar(raiz)
    assert len(informe.duplicados) == 1
    assert {Path(a.ruta).name for a in informe.duplicados[0].archivos} == {"x.bin", "z.bin"}


# ---------------------------------------------------- grandes, basura, vacías
def test_grandes_con_umbral(raiz: Path) -> None:
    crear(raiz, "grande.bin", b"g" * 2000)
    crear(raiz, "chico.bin", b"c" * 10)
    informe = analizar(raiz, Opciones(umbral_grande=1000))
    assert [Path(a.ruta).name for a in informe.grandes] == ["grande.bin"]


def test_basura_por_defecto_y_configurable(raiz: Path) -> None:
    crear(raiz, "a.tmp", "1")
    crear(raiz, "b.BAK", "2")
    crear(raiz, "Thumbs.db", "3")
    crear(raiz, "copia~", "4")
    crear(raiz, "registro.log", "5")
    crear(raiz, "normal.txt", "6")
    nombres = {Path(a.ruta).name for a in analizar(raiz).basura}
    assert nombres == {"a.tmp", "b.BAK", "Thumbs.db", "copia~"}
    ext = normalizar_extensiones(["log", ".TMP"])
    assert ext == {".log", ".tmp"}
    nombres = {Path(a.ruta).name for a in analizar(raiz, Opciones(extensiones_basura=ext)).basura}
    assert nombres == {"a.tmp", "registro.log", "Thumbs.db", "copia~"}
    assert not es_basura("~", ext)


def test_carpetas_vacias_anidadas(raiz: Path) -> None:
    (raiz / "vacia" / "dentro").mkdir(parents=True)
    (raiz / "con_git" / ".git").mkdir(parents=True)  # contenido excluido: no está vacía
    crear(raiz, "llena/f.txt", "x")
    vacias = {Path(v).relative_to(raiz).as_posix() for v in analizar(raiz).vacias}
    assert vacias == {"vacia", "vacia/dentro"}


# ---------------------------------------------------------------- exclusiones
def test_exclusiones_por_defecto_y_personalizadas(raiz: Path) -> None:
    crear(raiz, ".git/objeto", "igual")
    crear(raiz, "node_modules/lib.js", "igual")
    crear(raiz, f"{NOMBRE_CUARENTENA}/viejo.txt", "igual")
    crear(raiz, "fotos/originales/foto.jpg", "igual")
    crear(raiz, "a.txt", "igual")
    crear(raiz, "b.iso", "igual")
    informe = analizar(raiz, Opciones(excluir=("*.iso", "fotos/originales")))
    assert informe.duplicados == []
    assert informe.total_archivos == 1
    omitidos = {Path(i.ruta).name for i in informe.omitidos}
    assert {".git", "node_modules", NOMBRE_CUARENTENA, "originales", "b.iso"} <= omitidos


def test_cuarentena_excluida_aunque_se_quiten_las_exclusiones(raiz: Path) -> None:
    crear(raiz, f"{NOMBRE_CUARENTENA}/a.txt", "igual")
    crear(raiz, ".git/b.txt", "igual")
    crear(raiz, "c.txt", "igual")
    informe = analizar(raiz, Opciones(usar_exclusiones_por_defecto=False))
    assert {Path(a.ruta).name for g in informe.duplicados for a in g.archivos} == {"b.txt", "c.txt"}


# ----------------------------------------------------------- enlaces y Windows
def test_enlaces_duros_solo_se_informan(raiz: Path) -> None:
    if not puede_enlace_duro(raiz):
        pytest.skip("el sistema de archivos no admite enlaces duros")
    a = crear(raiz, "a.txt", "contenido")
    os.link(a, raiz / "b.txt")
    crear(raiz, "c.tmp", "otro")
    informe = analizar(raiz)
    assert len(informe.enlaces_duros) == 1
    assert len(informe.enlaces_duros[0].archivos) == 2
    assert informe.duplicados == []


def test_enlaces_simbolicos_no_se_siguen(raiz: Path, tmp_path: Path) -> None:
    if not puede_enlace_simbolico(raiz):
        pytest.skip("sin permiso para crear enlaces simbólicos")
    fuera = tmp_path / "fuera"
    crear(fuera, "secreto.txt", "igual")
    crear(raiz, "a.txt", "igual")
    (raiz / "enlace").symlink_to(fuera, target_is_directory=True)
    informe = analizar(raiz)
    assert informe.duplicados == []
    assert any(i.motivo.startswith("enlace") for i in informe.omitidos)


def test_rutas_largas(raiz: Path) -> None:
    actual = str(raiz)
    for i in range(6):
        actual = os.path.join(actual, f"carpeta_con_un_nombre_bastante_largo_{i:02d}_" + "x" * 20)
        os.mkdir(para_os(actual))
    assert len(actual) > 300
    for nombre in ("uno.txt", "dos.txt"):
        with open(para_os(os.path.join(actual, nombre)), "wb") as f:
            f.write(b"igual en ruta larga")
    informe = analizar(raiz)
    assert len(informe.duplicados) == 1
    assert all(len(a.ruta) > 300 and not a.ruta.startswith("\\\\?\\") for a in informe.duplicados[0].archivos)


def test_prefijo_largo() -> None:
    assert quitar_prefijo("\\\\?\\C:\\a") == "C:\\a"
    assert quitar_prefijo("\\\\?\\UNC\\srv\\rec\\a") == "\\\\srv\\rec\\a"
    corta = os.path.join(os.sep, "tmp", "a")
    assert para_os(corta) == corta
    if ES_WINDOWS:
        larga = "C:\\" + "a" * 300
        assert para_os(larga) == "\\\\?\\" + larga


# ----------------------------------------------------------- seguridad de raíz
def test_rutas_de_sistema_bloqueadas() -> None:
    raiz_unidad = os.path.abspath(os.sep)
    assert motivo_bloqueo_sistema(raiz_unidad)
    with pytest.raises(RutaNoPermitida):
        validar_raiz(raiz_unidad)
    if ES_WINDOWS:
        assert motivo_bloqueo_sistema(os.environ.get("SystemRoot", r"C:\Windows"))
        assert motivo_bloqueo_sistema(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "App"))
    else:
        assert motivo_bloqueo_sistema("/usr/lib")
        assert motivo_bloqueo_sistema("/etc")


def test_ruta_inexistente(raiz: Path) -> None:
    with pytest.raises(RutaNoPermitida):
        analizar(raiz / "no_existe")


def test_cancelacion(raiz: Path) -> None:
    crear(raiz, "a.txt", "x")
    evento = threading.Event()
    evento.set()
    with pytest.raises(Cancelado):
        analizar(raiz, cancelar=evento)


def test_progreso(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "b.txt", "igual")
    fases = []
    analizar(raiz, progreso=lambda fase, hechos, total: fases.append(fase))
    assert "recorrido" in fases and "hash parcial" in fases


# -------------------------------------------------------------------- informe
def test_informe_json_ida_y_vuelta(raiz: Path, tmp_path: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "b.txt", "igual")
    crear(raiz, "c.tmp", "t")
    (raiz / "vacia").mkdir()
    informe = analizar(raiz)
    destino = tmp_path / "informe.json"
    informe.guardar_json(destino)
    datos = json.loads(destino.read_text(encoding="utf-8"))
    assert datos["raiz"] == str(raiz)
    assert datos["resumen"]["grupos_duplicados"] == 1
    cargado = Informe.cargar_json(destino)
    assert cargado.a_dict() == informe.a_dict()


def test_informe_csv(raiz: Path, tmp_path: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "b.txt", "igual")
    informe = analizar(raiz)
    texto = informe.a_csv()
    assert texto.splitlines()[0] == "categoria,grupo,ruta,tamano,modificado,conservar"
    assert texto.count("duplicado,1,") == 2
    informe.guardar_csv(tmp_path / "i.csv")
    assert (tmp_path / "i.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    assert "Duplicados: 1 grupo, 1 copia sobrante" in informe.a_texto()


@pytest.mark.parametrize("datos", [
    [],
    {"version": 99, "raiz": "C:/x"},
    {"version": 1},
    {"version": 1, "raiz": "C:/x", "basura": "no"},
    {"version": 1, "raiz": "C:/x", "basura": [{"ruta": "a", "tamano": -1, "mtime_ns": 0}]},
    {"version": 1, "raiz": "C:/x", "basura": [{"ruta": "a", "tamano": True, "mtime_ns": 0}]},
])
def test_informe_invalido(datos) -> None:
    with pytest.raises(InformeInvalido):
        Informe.desde_dict(datos)


def test_json_mal_formado(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text("{no es json", encoding="utf-8")
    with pytest.raises(InformeInvalido):
        Informe.cargar_json(tmp_path / "x.json")


def test_sin_duplicados_con_tamanos_distintos(raiz: Path) -> None:
    crear(raiz, "a", "1")
    crear(raiz, "b", "22")
    assert analizar(raiz).duplicados == []


def test_mtime_se_guarda_en_ns(raiz: Path) -> None:
    crear(raiz, "a.tmp", "x", mtime_ns=T_ANTIGUO)
    assert analizar(raiz).basura[0].mtime_ns == T_ANTIGUO


def test_plural() -> None:
    from organizador.report import plural

    assert plural(1, "archivo") == "1 archivo"
    assert plural(0, "archivo") == "0 archivos"
    assert plural(2, "carpeta vacía", "carpetas vacías") == "2 carpetas vacías"
