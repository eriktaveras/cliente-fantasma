"""Cuentas, sesiones revocables y propiedad de corridas en el índice existente."""

import hashlib
import secrets
import sqlite3
import time

from pwdlib import PasswordHash

CLAVES = PasswordHash.recommended()
HASH_FICTICIO = CLAVES.hash(secrets.token_urlsafe(32))
DURACION_SESION = 7 * 24 * 3600

ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
  id TEXT PRIMARY KEY,
  nombre TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  clave_hash TEXT NOT NULL,
  creado INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sesiones (
  token_hash TEXT PRIMARY KEY,
  usuario_id TEXT NOT NULL REFERENCES usuarios(id),
  expira INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sesiones_usuario ON sesiones(usuario_id);
CREATE TABLE IF NOT EXISTS corridas_cuentas (
  corrida_id TEXT PRIMARY KEY REFERENCES corridas(id),
  usuario_id TEXT NOT NULL REFERENCES usuarios(id)
);
CREATE INDEX IF NOT EXISTS corridas_usuario ON corridas_cuentas(usuario_id);
CREATE TABLE IF NOT EXISTS accesos_auth (
  clave TEXT PRIMARY KEY,
  intentos INTEGER NOT NULL,
  hasta INTEGER NOT NULL
);
"""


def huella(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Cuentas:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(ESQUEMA)
    def crear(self, nombre: str, email: str, clave_hash: str) -> dict:
        usuario = {"id": secrets.token_hex(16), "nombre": nombre, "email": email}
        self.db.execute(
            "INSERT INTO usuarios (id, nombre, email, clave_hash, creado) VALUES (?, ?, ?, ?, ?)",
            (usuario["id"], nombre, email, clave_hash, int(time.time())),
        )
        return usuario

    def por_email(self, email: str):
        return self.db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()

    def sesion(self, usuario_id: str, duracion: int = DURACION_SESION) -> str:
        ahora = int(time.time())
        self.db.execute("DELETE FROM sesiones WHERE expira <= ?", (ahora,))
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sesiones VALUES (?, ?, ?)", (huella(token), usuario_id, ahora + duracion))
        return token

    def usuario(self, token: str | None):
        if not token or len(token) > 128:
            return None
        fila = self.db.execute(
            "SELECT u.id, u.nombre, u.email FROM usuarios u JOIN sesiones s ON s.usuario_id = u.id "
            "WHERE s.token_hash = ? AND s.expira > ?", (huella(token), int(time.time())),
        ).fetchone()
        return dict(fila) if fila else None

    def cerrar(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sesiones WHERE token_hash = ?", (huella(token),))

    def cambiar_clave(self, usuario_id: str, clave_hash: str) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE usuarios SET clave_hash = ? WHERE id = ?", (clave_hash, usuario_id))
            self.db.execute("DELETE FROM sesiones WHERE usuario_id = ?", (usuario_id,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def permitir(self, clave: str, limite: int, ventana: int = 900) -> bool:
        ahora = int(time.time())
        self.db.execute("DELETE FROM accesos_auth WHERE hasta <= ?", (ahora,))
        self.db.execute(
            "INSERT INTO accesos_auth VALUES (?, 1, ?) ON CONFLICT(clave) DO UPDATE SET intentos = intentos + 1",
            (clave, ahora + ventana),
        )
        return self.db.execute("SELECT intentos FROM accesos_auth WHERE clave = ?", (clave,)).fetchone()[0] <= limite

    def asociar(self, usuario_id: str, ids: list[str]) -> None:
        for corrida in ids:
            self.db.execute(
                "INSERT INTO corridas_cuentas SELECT id, ? FROM corridas WHERE id = ? "
                "ON CONFLICT(corrida_id) DO NOTHING", (usuario_id, corrida),
            )

    def pertenece(self, usuario_id: str, corrida_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM corridas_cuentas WHERE usuario_id = ? AND corrida_id = ?", (usuario_id, corrida_id),
        ).fetchone() is not None

    def resumen(self, usuario_id: str) -> dict:
        fila = self.db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(c.estado = 'terminada'), 0) terminadas, "
            "COALESCE(SUM(c.estado IN ('investigando','generando','corriendo')), 0) activas, "
            "COALESCE(SUM(c.costo_usd), 0) costo FROM corridas c "
            "JOIN corridas_cuentas p ON p.corrida_id = c.id WHERE p.usuario_id = ?", (usuario_id,),
        ).fetchone()
        return dict(fila)

    def corridas(self, usuario_id: str, busqueda: str = "", estado: str = "") -> list[dict]:
        return [dict(f) for f in self.db.execute(
            "SELECT c.* FROM corridas c JOIN corridas_cuentas p ON p.corrida_id = c.id "
            "WHERE p.usuario_id = ? AND instr(lower(c.negocio), lower(?)) > 0 "
            "AND (? = '' OR c.estado = ?) ORDER BY c.creado DESC LIMIT 100", (usuario_id, busqueda, estado, estado),
        )]

