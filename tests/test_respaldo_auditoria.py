import json
import sqlite3
from pathlib import Path

import pytest

from deploy.respaldo import respaldar, restaurar


def origen(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    with sqlite3.connect(runs / "indice.sqlite") as db:
        db.execute("CREATE TABLE corridas (id TEXT, estado TEXT)")
        db.execute("INSERT INTO corridas VALUES ('a', 'terminada')")
    (runs / "a").mkdir()
    (runs / "a/ficha.json").write_text('{"negocio":"Prueba"}')
    return runs


def test_backup_y_restore_conservan_sqlite_y_datos(tmp_path):
    runs = origen(tmp_path)
    backup = respaldar(runs, tmp_path / "backup")
    restore = restaurar(backup, tmp_path / "restore")
    with sqlite3.connect(restore / "indice.sqlite") as db:
        assert db.execute("SELECT * FROM corridas").fetchall() == [("a", "terminada")]
    assert (restore / "a/ficha.json").read_text() == (runs / "a/ficha.json").read_text()
    assert restore.stat().st_mode & 0o777 == 0o700
    assert (backup / "manifest.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="destino nuevo"):
        restaurar(backup, runs)


def test_no_restaura_respaldo_alterado(tmp_path):
    backup = respaldar(origen(tmp_path), tmp_path / "backup")
    (backup / "a/ficha.json").write_text("alterado")
    with pytest.raises(ValueError, match="manifiesto"):
        restaurar(backup, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


def test_no_respalda_corridas_activas(tmp_path):
    runs = origen(tmp_path)
    with sqlite3.connect(runs / "indice.sqlite") as db:
        db.execute("UPDATE corridas SET estado='corriendo'")
    with pytest.raises(ValueError, match="activas"):
        respaldar(runs, tmp_path / "backup")


def test_no_sigue_enlaces_fuera_de_runs(tmp_path):
    runs = origen(tmp_path)
    (runs / "link").symlink_to(tmp_path / "privado")
    with pytest.raises(ValueError, match="simbólicos"):
        respaldar(runs, tmp_path / "backup")


def test_snapshot_wal_abierto_se_restaura_sin_ficheros_transitorios(tmp_path):
    runs = origen(tmp_path)
    db = sqlite3.connect(runs / "indice.sqlite")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO corridas VALUES ('b', 'terminada')")
        db.commit()
        backup = respaldar(runs, tmp_path / "backup")
        esperado = json.loads((backup / "manifest.json").read_text())
        assert not any(x.endswith(("-wal", "-shm")) for x in esperado)
        restore = restaurar(backup, tmp_path / "restore")
        with sqlite3.connect(restore / "indice.sqlite") as restaurada:
            assert restaurada.execute("SELECT count(*) FROM corridas").fetchone()[0] == 2
    finally:
        db.close()
