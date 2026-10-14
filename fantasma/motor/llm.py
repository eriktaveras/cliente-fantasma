"""Cliente OpenAI-compatible (Chat Completions) por rol: tool calling, salida estructurada, reintentos y contabilidad."""

import asyncio
import json
import random
import time
import weakref
from contextlib import asynccontextmanager
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from fantasma.motor.config import Config, RolNombre

T = TypeVar("T", bound=BaseModel)


# Compartido por todos los LLM/servicios del proceso; por bucle evita semáforos
# ligados a un asyncio.run anterior en pruebas y herramientas CLI.
_cupos = weakref.WeakKeyDictionary()


@asynccontextmanager
async def cupo_global(limite: int):
    loop = asyncio.get_running_loop()
    estado = _cupos.setdefault(loop, {"condicion": asyncio.Condition(), "activos": 0, "limite": limite})
    estado["limite"] = min(estado["limite"], limite)
    condicion = estado["condicion"]
    async with condicion:
        await condicion.wait_for(lambda: estado["activos"] < estado["limite"])
        estado["activos"] += 1
    try:
        yield
    finally:
        async with condicion:
            estado["activos"] -= 1
            condicion.notify_all()


class ErrorTecnico(Exception):
    """Falla del servidor o del formato que no es culpa del prompt: el caso se reintenta, no se cuenta."""


@dataclass
class LlamadaPedida:
    id: str
    nombre: str
    argumentos: str  # JSON crudo; lo valida la herramienta


@dataclass
class Respuesta:
    texto: str
    tool_calls: list[LlamadaPedida]
    tokens_entrada: int
    tokens_salida: int
    ms: int


@dataclass
class Uso:
    llamadas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    ms: int = 0


@dataclass
class LLM:
    config: Config
    _clientes: dict[str, AsyncOpenAI] = field(default_factory=dict)
    uso: dict[str, Uso] = field(default_factory=lambda: defaultdict(Uso))

    _control_presupuesto: Any = field(default=None, repr=False)

    async def _pedir(self, rol: RolNombre, kwargs: dict):
        from fantasma.costos import tarifa
        # También el uso directo/CLI falla antes de llamar con modelo sin precio.
        entrada, salida = tarifa(self.config, rol)
        async with cupo_global(self.config.concurrency_global):
            control = self._control_presupuesto
            reserva = control.reservar(rol, kwargs) if control else 0.0
            gasto = None
            try:
                c = await self._cliente(rol).chat.completions.create(**kwargs)
                u = self.uso[rol]
                u.llamadas += 1
                if c.usage:
                    u.tokens_entrada += c.usage.prompt_tokens
                    u.tokens_salida += c.usage.completion_tokens
                    gasto = (c.usage.prompt_tokens * entrada + c.usage.completion_tokens * salida) / 1_000_000
                return c
            finally:
                if control:
                    control.liquidar(reserva, gasto)

    def _cliente(self, rol: RolNombre) -> AsyncOpenAI:
        nombre = self.config.roles[rol].proveedor
        if nombre not in self._clientes:
            p = self.config.proveedores[nombre]
            self._clientes[nombre] = AsyncOpenAI(base_url=p.base_url, api_key=p.clave(), timeout=self.config.timeout_s, max_retries=0)
        return self._clientes[nombre]

    def modelo(self, rol: RolNombre) -> str:
        return self.config.roles[rol].modelo

    async def chat(
        self,
        rol: RolNombre,
        mensajes: list[dict[str, Any]],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
    ) -> Respuesta:
        r = self.config.roles[rol]
        extra: dict[str, Any] = {}
        if self.config.proveedor_de(rol).local:
            extra["chat_template_kwargs"] = {"enable_thinking": r.razonamiento}
        kwargs: dict[str, Any] = {"model": r.modelo, "messages": mensajes, "temperature": r.temperatura, "max_tokens": r.max_tokens}
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format
        if extra:
            kwargs["extra_body"] = extra

        ultimo: Exception | None = None
        for intento in range(self.config.reintentos):
            t0 = time.monotonic()
            try:
                c = await self._pedir(rol, kwargs)
            except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError) as exc:
                ultimo = exc
                await asyncio.sleep(min(60, 2**intento + random.random()))
                continue
            ms = int((time.monotonic() - t0) * 1000)
            msg = c.choices[0].message
            u = self.uso[rol]
            u.ms += ms
            llamadas = [LlamadaPedida(tc.id, tc.function.name, tc.function.arguments or "{}") for tc in (msg.tool_calls or [])]
            return Respuesta((msg.content or "").strip(), llamadas, c.usage.prompt_tokens if c.usage else 0, c.usage.completion_tokens if c.usage else 0, ms)
        raise ErrorTecnico(f"{rol}: {ultimo!r}")

    async def estructurado(self, rol: RolNombre, mensajes: list[dict[str, Any]], esquema: type[T]) -> T:
        """Respuesta validada con pydantic. Si el JSON viene mal, reintenta mostrándole el error al modelo."""
        formato = {"type": "json_schema", "json_schema": {"name": esquema.__name__, "schema": esquema.model_json_schema()}}
        conversacion = list(mensajes)
        for _ in range(3):
            r = await self.chat(rol, conversacion, response_format=formato)
            try:
                return esquema.model_validate_json(_sin_cercas(r.texto))
            except (ValidationError, json.JSONDecodeError) as exc:
                conversacion += [
                    {"role": "assistant", "content": r.texto},
                    {"role": "user", "content": f"El JSON no valida: {str(exc)[:800]}. Devuélvelo completo y corregido, solo el JSON."},
                ]
        raise ErrorTecnico(f"{rol}: JSON inválido tras 3 intentos para {esquema.__name__}")


def _sin_cercas(texto: str) -> str:
    t = texto.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
