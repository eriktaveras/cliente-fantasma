"""Lo que comparte todo lo que corre dentro de una corrida."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from fantasma.motor.config import Config
from fantasma.motor.esquemas import Spec, Transcripcion
from fantasma.motor.herramientas import Caja, caja_para
from fantasma.motor.llm import LLM
from fantasma.motor.storage import Store


@dataclass
class Contexto:
    config: Config
    llm: LLM
    spec: Spec
    store: Store
    # Lo llama el simulador con cada mensaje nuevo: la app muestra las conversaciones en vivo.
    al_turno: Callable[[Transcripcion], None] = lambda t: None
    caja: Caja = field(init=False)
    # Limita las conversaciones simultáneas a los slots del servidor; las llamadas sueltas (juez, optimizador) no lo usan.
    slots: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.caja = caja_para(self.spec.rubro, self.spec.datos)
        self.slots = asyncio.Semaphore(self.config.concurrency)
