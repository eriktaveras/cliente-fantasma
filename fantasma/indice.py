"""Índice de corridas (runs/indice.sqlite): lo que la app lista y lo que hace falta para los topes por IP y por día.

El detalle de cada corrida vive en su propio Store (runs/<id>/results.sqlite); aquí va solo lo que se consulta entre corridas.
"""

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Modo = Literal["generado", "remoto"]
Tamaño = Literal["ligera", "completa"]
Estado = Literal["investigando", "ficha", "generando", "corriendo", "terminada", "presupuesto", "error"]
ACTIVOS: tuple[Estado, ...] = ("investigando", "generando", "corriendo")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS corridas (
  id TEXT PRIMARY KEY,
  token_publico TEXT NOT NULL UNIQUE,
  modo TEXT NOT NULL DEFAULT 'generado',
  tamaño TEXT NOT NULL DEFAULT 'ligera',
  estado TEXT NOT NULL,
  negocio TEXT NOT NULL DEFAULT '',
  creado TEXT NOT NULL,
  costo_usd REAL NOT NULL DEFAULT 0,
  ip_hash TEXT NOT NULL,
  motivo TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS corridas_ip ON corridas (ip_hash, creado);
CREATE INDEX IF NOT EXISTS corridas_creado ON corridas (creado);
"""
CAMPOS = ("modo", "tamaño", "estado", "negocio", "costo_usd", "motivo")


class FilaCorrida(BaseModel):
    id: str
    token_publico: str
    modo: Modo
    tamaño: Tamaño
    estado: Estado
    negocio: str
    creado: datetime
    costo_usd: float
    ip_hash: str
    motivo: str


def _hoy() -> str:
    # Día en UTC: el tope diario es una protección de gasto, no un calendario para el usuario.
    return datetime.now(UTC).date().isoformat()


class Indice:
    def __init__(self, ruta: Path) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(ruta, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(ESQUEMA)

    def crear(self, id: str, ip_hash: str, estado: Estado = "investigando", negocio: str = "") -> FilaCorrida:
        self.db.execute(
            "INSERT INTO corridas (id, token_publico, estado, negocio, creado, ip_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (id, secrets.token_urlsafe(18), estado, negocio, datetime.now(UTC).isoformat(), ip_hash),
        )
        return self.obtener(id)

    def actualizar(self, id: str, **campos) -> None:
        desconocidos = set(campos) - set(CAMPOS)
        if desconocidos:
            raise ValueError(f"campos no actualizables: {sorted(desconocidos)}")
        asignaciones = ", ".join(f"{k} = ?" for k in campos)
        self.db.execute(f"UPDATE corridas SET {asignaciones} WHERE id = ?", (*campos.values(), id))

    def obtener(self, id: str) -> FilaCorrida | None:
        fila = self.db.execute("SELECT * FROM corridas WHERE id = ?", (id,)).fetchone()
        return FilaCorrida.model_validate(dict(fila)) if fila else None

    def por_token(self, token: str) -> FilaCorrida | None:
        fila = self.db.execute("SELECT * FROM corridas WHERE token_publico = ?", (token,)).fetchone()
        return FilaCorrida.model_validate(dict(fila)) if fila else None

    def listar(self, limite: int = 50) -> list[FilaCorrida]:
        filas = self.db.execute("SELECT * FROM corridas ORDER BY creado DESC LIMIT ?", (limite,))
        return [FilaCorrida.model_validate(dict(f)) for f in filas]

    def activas_de(self, ip_hash: str) -> int:
        marcas = ",".join("?" * len(ACTIVOS))
        return self.db.execute(f"SELECT COUNT(*) FROM corridas WHERE ip_hash = ? AND estado IN ({marcas})", (ip_hash, *ACTIVOS)).fetchone()[0]

    def del_dia(self, ip_hash: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM corridas WHERE ip_hash = ? AND substr(creado, 1, 10) = ?", (ip_hash, _hoy())).fetchone()[0]

    def gasto_del_dia(self) -> float:
        return self.db.execute("SELECT COALESCE(SUM(costo_usd), 0) FROM corridas WHERE substr(creado, 1, 10) = ?", (_hoy(),)).fetchone()[0]

    def marcar_interrumpidas(self) -> int:
        """Al arrancar la app: lo que quedó activo murió con el proceso anterior."""
        marcas = ",".join("?" * len(ACTIVOS))
        cur = self.db.execute(f"UPDATE corridas SET estado = 'error', motivo = 'interrumpida al reiniciar' WHERE estado IN ({marcas})", ACTIVOS)
        return cur.rowcount

    def gasto_del_dia_de(self, ip_hash: str) -> float:
        return self.db.execute(
            "SELECT COALESCE(SUM(costo_usd), 0) FROM corridas WHERE ip_hash = ? AND substr(creado, 1, 10) = ?", (ip_hash, _hoy())
        ).fetchone()[0]
