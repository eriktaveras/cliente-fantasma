"""Pub/sub en memoria por corrida, adaptado del Emisor de tbot: el servicio publica y cada pestaña con SSE se suscribe."""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict, deque
from typing import Any

# La app corre un solo proceso de uvicorn, así que los eventos viajan en memoria. Si algún día corre con varios workers,
# esto pasa a LISTEN/NOTIFY o Redis: el resto del código solo llama a `publicar`.

RECIENTES = 2000


class Emisor:
    def __init__(self) -> None:
        self._colas: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        # Lo último de cada corrida: quien abre la página a mitad de corrida ve las conversaciones en curso.
        self._recientes: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=RECIENTES))
        self._ids = itertools.count(1)

    def suscribir(self, run_id: str, desde: int = 0) -> asyncio.Queue[dict[str, Any]]:
        """`desde` es el Last-Event-ID del SSE: la cola arranca con los recientes posteriores a ese id."""
        # Con tope: una pestaña dormida no puede acumular eventos sin fin.
        cola: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=RECIENTES + 100)
        for evento in self._recientes[run_id]:
            if evento["id"] > desde:
                cola.put_nowait(evento)
        self._colas[run_id].add(cola)
        return cola

    def desuscribir(self, run_id: str, cola: asyncio.Queue[dict[str, Any]]) -> None:
        self._colas[run_id].discard(cola)
        if not self._colas[run_id]:
            del self._colas[run_id]

    def publicar(self, run_id: str, tipo: str, **datos: Any) -> dict[str, Any]:
        evento = {"id": next(self._ids), "tipo": tipo, **datos}
        self._recientes[run_id].append(evento)
        for cola in self._colas.get(run_id, ()):
            try:
                cola.put_nowait(evento)
            except asyncio.QueueFull:
                # Esa pestaña ya va atrasada; al reconectar con Last-Event-ID recupera lo reciente.
                pass
        return evento

    def recientes(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._recientes.get(run_id, ()))

    def olvidar(self, run_id: str) -> None:
        self._recientes.pop(run_id, None)


emisor = Emisor()
