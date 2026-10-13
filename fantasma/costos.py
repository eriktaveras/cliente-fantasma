"""Costo en US$ de una corrida a partir del uso por rol que lleva motor/llm.py."""

from collections.abc import Callable

from fantasma.motor.config import Config
from fantasma.motor.llm import LLM

# US$ por millón de tokens (entrada, salida), precios publicados de Nebius Token Factory, oct-2026.
PRECIOS: dict[str, tuple[float, float]] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
}


class PresupuestoAgotado(Exception):
    pass


def por_rol(llm: LLM, config: Config) -> dict[str, float]:
    costos = {}
    for rol, u in llm.uso.items():
        entrada, salida = PRECIOS.get(config.roles[rol].modelo, (0.0, 0.0))
        costos[rol] = (u.tokens_entrada * entrada + u.tokens_salida * salida) / 1_000_000
    return costos


def total(llm: LLM, config: Config) -> float:
    return sum(por_rol(llm, config).values())


def verificar(llm: LLM, config: Config, tope_usd: float) -> None:
    gastado = total(llm, config)
    if gastado > tope_usd:
        raise PresupuestoAgotado(f"La corrida gastó US${gastado:.2f}, por encima del tope de US${tope_usd:.2f}")


class Medido:
    """Envuelve un LLM y, tras cada respuesta, avisa el gasto acumulado: así el servicio corta la corrida al pasar el tope.

    El estructurado del LLM real reintenta por dentro con su propio chat; el aviso llega al terminar, con todo sumado.
    """

    def __init__(self, llm: LLM, al_gastar: Callable[[float], None]) -> None:
        self._llm = llm
        self._al_gastar = al_gastar

    def __getattr__(self, nombre: str):
        return getattr(self._llm, nombre)

    def gastado(self) -> float:
        return total(self._llm, self._llm.config)

    async def chat(self, *args, **kwargs):
        r = await self._llm.chat(*args, **kwargs)
        self._al_gastar(self.gastado())
        return r

    async def estructurado(self, *args, **kwargs):
        r = await self._llm.estructurado(*args, **kwargs)
        self._al_gastar(self.gastado())
        return r
