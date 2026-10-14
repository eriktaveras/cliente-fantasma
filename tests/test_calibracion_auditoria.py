import csv

import pytest

from fantasma.motor import calibrar
from fantasma.motor.storage import Store
from tests.test_boleta import conversacion, evaluacion, guardar


def test_calibracion_no_se_valida_con_muestra_minima_y_mide_criticos(tmp_path, spec):
    store = Store(tmp_path)
    store.guardar_spec(spec)
    caso = spec.casos[0].id
    guardar(store, conversacion("hola", "respuesta", caso_id=caso), evaluacion(caso, 0.9, paso=True))
    ruta = calibrar.exportar(store, spec, 0, 20)
    with ruta.open() as f:
        filas = list(csv.DictReader(f))
    filas[0].update(humano_paso="si", humano_critica="si")
    with ruta.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=calibrar.COLUMNAS)
        w.writeheader()
        w.writerows(filas)
    r = calibrar.importar(store, ruta)
    assert r["n"] == 1 and r["n_critica"] == 1 and not r["validada"]
    assert r["matriz_confusion"]["critica"]["fn"] == 1
    assert "Datos autorizados del negocio" in (ruta.parent / filas[0]["archivo"]).read_text()


def test_calibracion_no_duplica_etiquetas(tmp_path, spec):
    store = Store(tmp_path)
    caso = spec.casos[0].id
    guardar(store, conversacion("hola", "respuesta", caso_id=caso), evaluacion(caso, 0.9, paso=True))
    ruta = tmp_path / "etiquetas.csv"
    with ruta.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=calibrar.COLUMNAS)
        w.writeheader()
        fila = {"caso_id": caso, "version": 0, "repeticion": 0, "humano_paso": "si"}
        w.writerows([fila, fila])
    with pytest.raises(ValueError, match="repetida"):
        calibrar.importar(store, ruta)
