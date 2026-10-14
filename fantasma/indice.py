"""Índice de corridas (runs/indice.sqlite): lo que la app lista y lo que hace falta para los topes por IP y por día.

El detalle de cada corrida vive en su propio Store (runs/<id>/results.sqlite); aquí va solo lo que se consulta entre corridas.
"""

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Modo = Literal["generado", "prompt", "remoto"]
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
        if "reserva_usd" not in {r[1] for r in self.db.execute("PRAGMA table_info(corridas)")}:
            self.db.execute("ALTER TABLE corridas ADD COLUMN reserva_usd REAL NOT NULL DEFAULT 0")

        self.db.execute("BEGIN IMMEDIATE")
        try:
            existe = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gastos_diarios'").fetchone()
            self.db.execute("CREATE TABLE IF NOT EXISTS gastos_diarios (corrida TEXT NOT NULL, dia TEXT NOT NULL, usd REAL NOT NULL, PRIMARY KEY (corrida, dia))")
            if not existe:
                # Migración única: la fecha real del gasto antiguo no se conoce.
                self.db.execute("INSERT INTO gastos_diarios SELECT id, substr(creado, 1, 10), costo_usd FROM corridas WHERE costo_usd != 0")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _anotar_gasto(self, id: str, usd: float) -> None:
        self.db.execute("INSERT INTO gastos_diarios (corrida, dia, usd) VALUES (?, ?, ?) ON CONFLICT(corrida, dia) DO UPDATE SET usd = usd + excluded.usd", (id, _hoy(), usd))

    def _pendiente(self, ip_hash: str | None = None) -> float:
        # La deuda en vuelo sigue comprometiendo saldo al cruzar medianoche.
        if ip_hash is None:
            return self.db.execute("SELECT COALESCE(SUM(reserva_usd), 0) FROM corridas").fetchone()[0]
        return self.db.execute("SELECT COALESCE(SUM(reserva_usd), 0) FROM corridas WHERE ip_hash = ?", (ip_hash,)).fetchone()[0]

    def reservar(self, id: str, usd: float, tope: float, diario: float, visitante: float) -> None:
        """Reserva atómica entre instancias del servicio que comparten el índice."""
        from fantasma.costos import PresupuestoAgotado
        self.db.execute("BEGIN IMMEDIATE")
        try:
            fila = self.obtener(id)
            pendiente = self.db.execute("SELECT reserva_usd FROM corridas WHERE id = ?", (id,)).fetchone()[0]
            consultas = (
                (fila.costo_usd + pendiente, tope, "tope de la corrida"),
                (self.gasto_del_dia() + self._pendiente(), diario, "tope diario de la demo"),
                (self.gasto_del_dia_de(fila.ip_hash) + self._pendiente(fila.ip_hash), visitante, "tope diario por visitante"),
            )
            for usado, limite, nombre in consultas:
                if usado + usd > limite + 1e-12:
                    raise PresupuestoAgotado(f"{nombre} (US${limite:.2f}): saldo insuficiente para la siguiente llamada")
            self.db.execute("UPDATE corridas SET reserva_usd = reserva_usd + ? WHERE id = ?", (usd, id))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def liquidar(self, id: str, reserva: float, gasto: float) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE corridas SET reserva_usd = MAX(0, reserva_usd - ?), costo_usd = costo_usd + ? WHERE id = ?", (reserva, gasto, id))
            self._anotar_gasto(id, gasto)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if "costo_usd" in campos:
                self._anotar_gasto(id, campos["costo_usd"] - self.obtener(id).costo_usd)
            self.db.execute(f"UPDATE corridas SET {asignaciones} WHERE id = ?", (*campos.values(), id))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

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
        # Un error que no gastó nada (ficha sin material, Tavily caído) no le quita una prueba al visitante.
        return self.db.execute(
            "SELECT COUNT(*) FROM corridas WHERE ip_hash = ? AND substr(creado, 1, 10) = ? AND NOT (estado = 'error' AND costo_usd = 0)",
            (ip_hash, _hoy()),
        ).fetchone()[0]

    def gasto_del_dia(self) -> float:
        return self.db.execute("SELECT COALESCE(SUM(usd), 0) FROM gastos_diarios WHERE dia = ?", (_hoy(),)).fetchone()[0]

    def marcar_interrumpidas(self) -> int:
        """Al arrancar la app: lo que quedó activo murió con el proceso anterior."""
        marcas = ",".join("?" * len(ACTIVOS))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for fila in self.db.execute(f"SELECT id, reserva_usd FROM corridas WHERE estado IN ({marcas})", ACTIVOS).fetchall():
                self._anotar_gasto(fila["id"], fila["reserva_usd"])
            cur = self.db.execute(f"UPDATE corridas SET estado = 'error', motivo = 'interrumpida al reiniciar; reserva pendiente contabilizada conservadoramente', costo_usd = costo_usd + reserva_usd, reserva_usd = 0 WHERE estado IN ({marcas})", ACTIVOS)
            self.db.execute("COMMIT")
            return cur.rowcount
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def gasto_del_dia_de(self, ip_hash: str) -> float:
        return self.db.execute(
            "SELECT COALESCE(SUM(g.usd), 0) FROM gastos_diarios g JOIN corridas c ON c.id = g.corrida WHERE c.ip_hash = ? AND g.dia = ?", (ip_hash, _hoy())
        ).fetchone()[0]
