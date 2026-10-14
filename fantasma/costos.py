"""Contabilidad y reservas conservadoras antes de cada petición facturable."""

import asyncio
import json
from collections.abc import Callable

from fantasma.motor.config import Config
from fantasma.motor.llm import LLM

# US$ por millón de tokens (entrada, salida).
PRECIOS: dict[str, tuple[float, float]] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
}


class PresupuestoAgotado(Exception):
    pass


class PrecioDesconocido(ValueError):
    pass


def tarifa(config: Config, rol: str) -> tuple[float, float]:
    r = config.roles[rol]
    precios = config.proveedor_de(rol).precios
    if r.modelo in precios:
        return precios[r.modelo]
    if r.modelo in PRECIOS:
        return PRECIOS[r.modelo]
    raise PrecioDesconocido(
        f"Sin tarifa para {r.proveedor}/{r.modelo}. Configura proveedores.{r.proveedor}.precios "
        "con [entrada, salida] en US$/millón de tokens; [0, 0] declara gratuito."
    )


def por_rol(llm: LLM, config: Config) -> dict[str, float]:
    costos = {}
    for rol, u in llm.uso.items():
        entrada, salida = tarifa(config, rol)
        costos[rol] = (u.tokens_entrada * entrada + u.tokens_salida * salida) / 1_000_000
    return costos


def total(llm: LLM, config: Config) -> float:
    return sum(por_rol(llm, config).values())


def verificar(llm: LLM, config: Config, tope_usd: float) -> None:
    gastado = total(llm, config)
    if gastado > tope_usd:
        raise PresupuestoAgotado(f"La corrida gastó US${gastado:.2f}, por encima del tope de US${tope_usd:.2f}")


class Medido:
    """El cliente real reserva por intento, también dentro de estructurado.

    Un intento enviado sin uso confirmado (timeout/cancelación) consume su reserva:
    no se supone que el proveedor lo haya procesado gratis. Los clientes inyectados
    conservan duck typing, con una reserva por operación exterior.
    """

    def __init__(self, llm: LLM, al_gastar: Callable[[float], None],
                 reservar: Callable[[float], None] | None = None,
                 liquidar: Callable[[float, float], None] | None = None) -> None:
        self._llm = llm
        self._al_gastar = al_gastar
        self._reservar = reservar or (lambda _: None)
        self._liquidar = liquidar or (lambda reserva, gasto: None)
        self._gastado = 0.0
        self._inyeccion_lock = asyncio.Lock()
        self.sin_confirmar = 0.0
        self._real = isinstance(llm, LLM)
        if self._real:
            llm._control_presupuesto = self

    def __getattr__(self, nombre: str):
        return getattr(self._llm, nombre)

    def gastado(self) -> float:
        return self._gastado

    def reservar(self, rol: str, peticion: dict) -> float:
        entrada, salida = tarifa(self._llm.config, rol)
        # Un token textual no excede un byte de esta cota conservadora. Incluye
        # esquemas, herramientas y sobrecarga por mensaje; no usa len()/4.
        bytes_entrada = len(json.dumps(peticion, ensure_ascii=False).encode("utf-8"))
        tokens_entrada = bytes_entrada + 256 * (1 + len(peticion.get("messages", [])))
        reserva = (tokens_entrada * entrada + self._llm.config.roles[rol].max_tokens * salida) / 1_000_000
        self._reservar(reserva)
        return reserva

    def liquidar(self, reserva: float, gasto: float | None) -> None:
        if gasto is None:
            gasto = reserva
            self.sin_confirmar += reserva
        self._liquidar(reserva, gasto)
        self._gastado += gasto
        self._al_gastar(self._gastado)

    async def _inyectado(self, metodo: str, *args, **kwargs):
        from fantasma.motor.llm import cupo_global
        rol = args[0] if args else kwargs["rol"]
        mensajes = args[1] if len(args) > 1 else kwargs.get("mensajes", [])
        peticion = {"messages": mensajes, **{k: v for k, v in kwargs.items() if k not in ("mensajes", "rol")}}
        if metodo == "chat" and len(args) > 2:
            peticion["tools"] = args[2]
        if metodo == "estructurado":
            esquema = args[2] if len(args) > 2 else kwargs.get("esquema")
            peticion["schema"] = esquema.model_json_schema()
        async with self._inyeccion_lock, cupo_global(self._llm.config.concurrency_global):
            reserva = self.reservar(rol, peticion)
            antes = total(self._llm, self._llm.config)
            gasto = None
            try:
                r = await getattr(self._llm, metodo)(*args, **kwargs)
                gasto = total(self._llm, self._llm.config) - antes
                return r
            finally:
                # Si el falso incrementó uso antes de fallar, también se contabiliza.
                delta = total(self._llm, self._llm.config) - antes
                self.liquidar(reserva, delta if delta else gasto)

    async def chat(self, *args, **kwargs):
        if self._real:
            return await self._llm.chat(*args, **kwargs)
        return await self._inyectado("chat", *args, **kwargs)

    async def estructurado(self, *args, **kwargs):
        if self._real:
            return await self._llm.estructurado(*args, **kwargs)
        return await self._inyectado("estructurado", *args, **kwargs)
