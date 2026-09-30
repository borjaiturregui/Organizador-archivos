from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import typer
from conftest import crear
from typer.testing import CliRunner

from organizador import NOMBRE_CUARENTENA, __version__
from organizador.cli import app, parsear_tamano

runner = CliRunner()


def invocar(*args: str, entrada: str | None = None):
    return runner.invoke(app, [str(a) for a in args], input=entrada)


def preparar(raiz: Path) -> None:
    crear(raiz, "a.txt", "igual")
    crear(raiz, "sub/b.txt", "igual")
    crear(raiz, "t.tmp", "temporal")
    (raiz / "vacia").mkdir()


def test_version() -> None:
    r = invocar("--version")
    assert r.exit_code == 0 and __version__ in r.output


def test_analyze_con_salidas(raiz: Path, tmp_path: Path) -> None:
    preparar(raiz)
    salida, csv = tmp_path / "informe.json", tmp_path / "informe.csv"
    r = invocar("analyze", raiz, "--output", salida, "--csv", csv)
    assert r.exit_code == 0, r.output
    assert "Duplicados: 1 grupos" in r.output
    assert os.path.join("sub", "b.txt") in r.output  # rutas relativas en consola
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert datos["raiz"] == str(raiz)
    assert csv.exists()


def test_analyze_opciones(raiz: Path) -> None:
    crear(raiz, "g.bin", b"x" * 3000)
    crear(raiz, "r.log", "log")
    crear(raiz, "omitir/z.txt", "z")
    r = invocar("analyze", raiz, "--umbral-grande", "2KB", "--ext-basura", ".log", "--exclude", "omitir")
    assert r.exit_code == 0, r.output
    assert "Archivos grandes: 1" in r.output
    assert "Basura / temporales: 1" in r.output
    assert "Archivos: 2" in r.output


def test_analyze_ruta_de_sistema(raiz: Path) -> None:
    r = invocar("analyze", os.path.abspath(os.sep))
    assert r.exit_code == 1
    assert "--permitir-sistema" in r.output


def test_analyze_ruta_inexistente(raiz: Path) -> None:
    assert invocar("analyze", raiz / "nada").exit_code == 1


def test_apply_simular_y_confirmacion(raiz: Path, tmp_path: Path) -> None:
    preparar(raiz)
    informe = tmp_path / "i.json"
    invocar("analyze", raiz, "-o", informe)

    r = invocar("apply", informe, "--simular")
    assert r.exit_code == 0 and "Simulación" in r.output
    assert (raiz / "sub/b.txt").exists()

    r = invocar("apply", informe, entrada="n\n")
    assert r.exit_code == 1
    assert (raiz / "sub/b.txt").exists()
    assert not (raiz / NOMBRE_CUARENTENA).exists()


def test_apply_y_restore(raiz: Path, tmp_path: Path) -> None:
    preparar(raiz)
    informe = tmp_path / "i.json"
    invocar("analyze", raiz, "-o", informe)
    r = invocar("apply", informe, "--yes", "--vacias")
    assert r.exit_code == 0, r.output
    assert "2 archivos movidos" in r.output
    assert not (raiz / "sub/b.txt").exists() and not (raiz / "t.tmp").exists()
    assert not (raiz / "vacia").exists()
    logs = list((raiz / NOMBRE_CUARENTENA).glob("cuarentena_log_*.jsonl"))
    assert len(logs) == 1

    r = invocar("restore", logs[0], entrada="s\n")
    assert r.exit_code == 0, r.output
    assert (raiz / "sub/b.txt").exists() and (raiz / "t.tmp").exists() and (raiz / "vacia").is_dir()


def test_apply_categorias(raiz: Path, tmp_path: Path) -> None:
    preparar(raiz)
    informe = tmp_path / "i.json"
    invocar("analyze", raiz, "-o", informe)
    r = invocar("apply", informe, "--yes", "--sin-duplicados")
    assert r.exit_code == 0, r.output
    assert (raiz / "sub/b.txt").exists() and not (raiz / "t.tmp").exists()
    assert invocar("apply", informe, "--sin-duplicados", "--sin-basura").exit_code == 2


def test_apply_informe_invalido(tmp_path: Path) -> None:
    malo = tmp_path / "malo.json"
    malo.write_text('{"version": 1}', encoding="utf-8")
    r = invocar("apply", malo, "--yes")
    assert r.exit_code == 1 and "informe" in r.output
    assert invocar("apply", tmp_path / "no_existe.json").exit_code == 1


def test_apply_informe_con_raiz_de_sistema(tmp_path: Path) -> None:
    informe = tmp_path / "i.json"
    informe.write_text(json.dumps({"version": 1, "raiz": os.path.abspath(os.sep)}), encoding="utf-8")
    r = invocar("apply", informe, "--yes")
    assert r.exit_code == 1 and "raíz" in r.output


def test_restore_log_invalido(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    log.write_text("no es json\n", encoding="utf-8")
    assert invocar("restore", log, "--yes").exit_code == 1


@pytest.mark.parametrize("texto,esperado", [
    ("100", 100), ("1KB", 1024), ("1.5 MB", int(1.5 * 1024**2)), ("2g", 2 * 1024**3), ("3,5MB", int(3.5 * 1024**2)),
])
def test_parsear_tamano(texto: str, esperado: int) -> None:
    assert parsear_tamano(texto) == esperado


def test_parsear_tamano_invalido() -> None:
    with pytest.raises(typer.BadParameter):
        parsear_tamano("mucho")
