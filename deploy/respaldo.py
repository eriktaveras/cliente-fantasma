"""Snapshots privados de runs con SQLite backup y restauración verificada sin sobrescribir destinos."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def comprobar(raiz: Path) -> dict[str, str]:
    hashes = {}
    for p in sorted(raiz.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"No se aceptan enlaces simbólicos: {p}")
        if not p.is_file() or p == raiz / "manifest.json" or p.name.endswith(("-wal", "-shm")):
            continue
        if p.suffix == ".sqlite":
            with closing(sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError(f"SQLite no íntegro: {p}")
        hashes[str(p.relative_to(raiz))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def copiar(origen: Path, destino: Path) -> None:
    shutil.copytree(origen, destino, ignore=shutil.ignore_patterns("*-wal", "*-shm", "manifest.json"))
    destino.chmod(0o700)
    for p in destino.rglob("*"):
        if p.is_file():
            p.chmod(0o600)
        elif p.is_dir():
            p.chmod(0o700)


def respaldar(origen: Path, destino: Path) -> Path:
    origen, destino = origen.resolve(), destino.absolute()
    if destino.exists() or origen == destino or origen in destino.parents:
        raise ValueError("El destino debe ser nuevo y estar fuera de runs")
    comprobar(origen)
    with closing(sqlite3.connect((origen / "indice.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        activas = db.execute("SELECT COUNT(*) FROM corridas WHERE estado IN ('investigando','generando','corriendo')").fetchone()[0]
        if activas:
            raise ValueError("Hay corridas activas: espera a que terminen antes de respaldar")
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fantasma-backup-", dir=destino.parent) as temporal:
        snapshot = Path(temporal) / "runs"
        copiar(origen, snapshot)
        for fuente in origen.rglob("*.sqlite"):
            salida = snapshot / fuente.relative_to(origen)
            with closing(sqlite3.connect(fuente.as_uri() + "?mode=ro", uri=True)) as entrada, closing(sqlite3.connect(salida)) as db:
                entrada.backup(db)
        hashes = comprobar(snapshot)
        manifiesto = snapshot / "manifest.json"
        manifiesto.write_text(json.dumps(hashes, sort_keys=True, indent=2) + "\n")
        manifiesto.chmod(0o600)
        snapshot.rename(destino)
    return destino


def restaurar(origen: Path, destino: Path) -> Path:
    origen, destino = origen.resolve(), destino.absolute()
    if destino.exists():
        raise ValueError("La restauración requiere un destino nuevo; nunca sobrescribe runs")
    esperado = json.loads((origen / "manifest.json").read_text())
    if comprobar(origen) != esperado:
        raise ValueError("El respaldo no coincide con su manifiesto")
    destino.parent.mkdir(parents=True, exist_ok=True)
    copiar(origen, destino)
    if comprobar(destino) != esperado:
        raise ValueError("La restauración no coincide con el respaldo")
    return destino


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("accion", choices=("backup", "restore"))
    parser.add_argument("origen", type=Path)
    parser.add_argument("destino", type=Path)
    args = parser.parse_args()
    accion = respaldar if args.accion == "backup" else restaurar
    print(accion(args.origen, args.destino))
