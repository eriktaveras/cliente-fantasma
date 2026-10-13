"""Un turno del agente: system prompt + historial, con bucle de herramientas contra el mundo de la conversación."""

import json
from datetime import datetime

from fantasma.motor.esquemas import LlamadaTool, Mundo

MAX_RONDAS = 6
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def fecha_es(d: datetime) -> str:
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]} de {d.year}, {d:%H:%M}"


def system_prompt(prompt: str, mundo: Mundo) -> str:
    # La fecha la agrega el código y no el prompt optimizado, para que el optimizador no pueda tocarla ni dejarla vieja.
    return f"{prompt.rstrip()}\n\nFecha y hora actual: {fecha_es(mundo.reloj)}"


async def turno_agente(ctx, prompt: str, historial: list[dict], mundo: Mundo, rol: str = "agente") -> tuple[str, list[LlamadaTool], int, int]:
    """Muta `historial` en sitio: agrega los mensajes assistant/tool del turno y el texto final como assistant."""
    tools = ctx.caja.definiciones()
    llamadas: list[LlamadaTool] = []
    tokens = ms = 0
    texto = ""
    for _ in range(MAX_RONDAS):
        r = await ctx.llm.chat(rol, [{"role": "system", "content": system_prompt(prompt, mundo)}, *historial], tools=tools)
        tokens += r.tokens_salida
        ms += r.ms
        if r.texto:
            texto = r.texto
        if not r.tool_calls:
            texto = r.texto
            break
        historial.append(
            {
                "role": "assistant",
                "content": r.texto or None,
                "tool_calls": [{"id": tc.id, "type": "function", "function": {"name": tc.nombre, "arguments": tc.argumentos}} for tc in r.tool_calls],
            }
        )
        for tc in r.tool_calls:
            ll = ctx.caja.ejecutar(tc.nombre, tc.argumentos, mundo)
            llamadas.append(ll)
            historial.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(ll.resultado.model_dump(mode="json"), ensure_ascii=False)})
    historial.append({"role": "assistant", "content": texto})
    return texto, llamadas, tokens, ms
