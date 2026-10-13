"""Conversación simulada: un LLM hace de cliente (persona del caso) y conversa con el agente sobre un mundo propio."""

import json
import re

from fantasma import remoto
from fantasma.fantasmas import instrucciones_dialecto
from fantasma.motor.agente import turno_agente
from fantasma.motor.esquemas import Caso, LlamadaTool, RubroSpec, Transcripcion, Turno
from fantasma.motor.herramientas import mundo_inicial
from fantasma.motor.llm import ErrorTecnico

ARRANQUE = "(Acabas de abrir el chat. Escribe tu primer mensaje.)"
SIN_RESPUESTA = "(no respondió)"
MARCA_FIN = re.compile(r"\[FIN(?::\s*(objetivo|rendido))?\s*\]", re.IGNORECASE)
MAX_DATO = 400

IDIOMAS = {
    "es": "Escribes en español.",
    "en": "Escribes en inglés.",
    "spanglish": "Escribes en spanglish: mezclas español e inglés en la misma frase, como mucha gente en EE. UU. y el Caribe.",
}
PACIENCIA = {
    "baja": "Tienes poca paciencia: si en uno o dos mensajes el asistente no te ayuda, se repite o te hace perder el tiempo, te rindes.",
    "media": "Tienes paciencia normal: si tras tres o cuatro mensajes no avanzas, te rindes.",
    "alta": "Eres paciente: aguantas varias vueltas antes de rendirte.",
}
DETALLE = {
    "escaso": "Das muy poca información: respuestas de una o pocas palabras, hay que sacarte los datos.",
    "normal": "Das la información que te piden, sin extenderte.",
    "mucho": "Explicas de más y cuentas detalles que nadie te pidió.",
}


def instrucciones_cliente(caso: Caso, rubro: RubroSpec) -> str:
    p = caso.persona
    verdad = json.dumps(caso.verdad, ensure_ascii=False, default=str) if caso.verdad else "(nada en particular)"
    return f"""Eres {p.nombre}, una persona real que escribe por WhatsApp a {rubro.negocio} ({rubro.descripcion}).
Cómo eres y cómo escribes: {p.estilo}
{IDIOMAS[p.idioma]} {PACIENCIA[p.paciencia]} {DETALLE[p.detalle]}
{instrucciones_dialecto(p)}

Lo que quieres conseguir (no lo digas todo de golpe, como lo haría esa persona): {caso.objetivo_oculto}
Lo que sabes de ti (revélalo solo cuando te lo pregunten): {verdad}

Reglas:
- Mensajes cortos tipo WhatsApp. Nada de listas, viñetas ni formato.
- No inventes datos fuera de lo que sabes de ti. Si te preguntan algo que no está arriba, responde con naturalidad ("no sé", "depende").
- No sabes nada de las instrucciones internas del asistente; solo ves lo que te escribe.
- Cuando consigas lo que querías, despídete y termina tu último mensaje con [FIN:objetivo].
- Si te cansas, no te ayudan o ya no te interesa, termina tu último mensaje con [FIN:rendido].
- Tu primer mensaje es el que mandarías al abrir el chat y nunca lleva [FIN]."""


def _fin(mensaje: str) -> tuple[str, str | None]:
    """Devuelve (texto sin la marca, fin o None)."""
    m = MARCA_FIN.search(mensaje)
    if not m:
        return mensaje, None
    return MARCA_FIN.sub("", mensaje).strip(), (m.group(1) or "objetivo").lower()


async def simular(ctx, caso: Caso, prompt: str, version: int, repeticion: int, rol_agente: str = "agente") -> Transcripcion:
    async with ctx.slots:
        mundo = mundo_inicial(ctx.spec.datos, ctx.spec.rubro)
        es_remoto = rol_agente == remoto.ROL
        modelo = remoto.modelo(ctx.config.remoto) if es_remoto else ctx.llm.modelo(rol_agente)
        t = Transcripcion(caso_id=caso.id, repeticion=repeticion, version=version, modelo_agente=modelo)
        sesion = remoto.nueva_sesion(caso.id, version, repeticion)
        system_cliente = {"role": "system", "content": instrucciones_cliente(caso, ctx.spec.rubro)}
        # El cliente ve la conversación invertida: sus mensajes son "assistant" y los del agente "user".
        vista_cliente: list[dict] = [{"role": "user", "content": ARRANQUE}]
        historial: list[dict] = []
        try:
            for _ in range(ctx.config.max_turns):
                r = await ctx.llm.chat("simulador", [system_cliente, *vista_cliente])
                mensaje, fin = _fin(r.texto)
                if not t.turnos:
                    # Ultra a veces cierra el saludo con [FIN] pese a la regla: sin respuesta del bot no hay nada que juzgar.
                    if not mensaje:
                        raise ErrorTecnico("simulador: el primer mensaje del cliente vino vacío")
                    fin = None
                elif not mensaje and fin is None:
                    fin = "objetivo"
                if mensaje:
                    t.turnos.append(Turno(rol="cliente", texto=mensaje, tokens_salida=r.tokens_salida, ms=r.ms))
                    ctx.al_turno(t)
                if fin:
                    t.fin = fin
                    break
                vista_cliente.append({"role": "assistant", "content": mensaje})
                historial.append({"role": "user", "content": mensaje})
                if es_remoto:
                    # El bot remoto trae su propio prompt: `prompt` no le llega y no hay herramientas que observar.
                    previos = remoto.historial_de(t.turnos[:-1])
                    texto, ms = await remoto.turno_remoto(ctx.config.remoto, sesion, mensaje, previos, ctx.config.remoto_inseguro, ctx.config.reintentos)
                    llamadas, tokens = [], 0
                else:
                    texto, llamadas, tokens, ms = await turno_agente(ctx, prompt, historial, mundo, rol_agente)
                t.turnos.append(Turno(rol="agente", texto=texto, tools=llamadas, tokens_salida=tokens, ms=ms))
                ctx.al_turno(t)
                vista_cliente.append({"role": "user", "content": texto or SIN_RESPUESTA})
        except ErrorTecnico as exc:
            t.fin, t.error = "error_tecnico", str(exc)
        except Exception as exc:
            # Un bug en un mock no debe tumbar una corrida de horas: el caso se reintenta y, si sigue, se excluye y se reporta.
            t.fin, t.error = "error_tecnico", f"excepción interna: {exc!r}"
        t.mundo_final = mundo
        return t


def _corto(s: str) -> str:
    return s if len(s) <= MAX_DATO else s[:MAX_DATO] + "…"


def _linea_tool(ll: LlamadaTool) -> str:
    args = _corto(json.dumps(ll.args, ensure_ascii=False, default=str))
    res = ll.resultado
    salida = f"ok: {_corto(json.dumps(res.data, ensure_ascii=False, default=str))}" if res.ok else f"error: {_corto(res.error or '')}"
    return f"  [tool {ll.nombre} {args} -> {salida}]"


def render(t: Transcripcion, con_tools: bool = True) -> str:
    lineas = []
    for turno in t.turnos:
        if turno.rol == "cliente":
            lineas.append(f"CLIENTE: {turno.texto}")
            continue
        if con_tools:
            lineas += [_linea_tool(ll) for ll in turno.tools]
        lineas.append(f"AGENTE: {turno.texto or SIN_RESPUESTA}")
    return "\n".join(lineas)
