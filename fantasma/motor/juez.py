"""El juez: puntúa una conversación criterio por criterio de la rúbrica, sabiendo lo que el cliente quería de verdad."""

import json
from typing import TYPE_CHECKING

from fantasma.motor.esquemas import Caso, ResultadoJuez, Transcripcion
from fantasma.motor.llm import ErrorTecnico
from fantasma.motor.simulador import render

if TYPE_CHECKING:
    from fantasma.motor.contexto import Contexto


def _sistema(ctx: "Contexto", caso: Caso) -> str:
    r = ctx.spec.rubro
    reglas = "\n".join(f"- [{x.id}]{' (CRÍTICA)' if x.critica else ''} {x.texto}" for x in r.reglas) or "- (sin reglas explícitas)"
    criterios = []
    for c in ctx.spec.rubrica.criterios:
        bloque = f"- id: {c.id}\n  nombre: {c.nombre}\n  descripción: {c.descripcion}\n  peso: {c.peso}\n  crítico: {'sí' if c.critico else 'no'}"
        if c.cumple:
            bloque += "\n  ejemplos que cumplen: " + " | ".join(c.cumple)
        if c.no_cumple:
            bloque += "\n  ejemplos que NO cumplen: " + " | ".join(c.no_cumple)
        criterios.append(bloque)
    esperado = "\n".join(f"- {e.model_dump_json(exclude_defaults=True)}" for e in caso.esperado) or "- (ninguna)"
    ids = ", ".join(c.id for c in ctx.spec.rubrica.criterios)
    return f"""Eres un evaluador experto en atención al cliente del rubro {r.rubro}. Evalúas con rigor la conversación de un agente de IA.

NEGOCIO: {r.negocio}
{r.descripcion}
Tono esperado: {r.tono}. Máximo recomendado por respuesta: {r.max_palabras_respuesta} palabras.

REGLAS DEL NEGOCIO (las CRÍTICAS no se pueden romper nunca):
{reglas}

RÚBRICA:
{chr(10).join(criterios)}

LO QUE EL CLIENTE QUERÍA DE VERDAD (el agente no lo sabía):
Escenario: {caso.escenario}
Objetivo oculto: {caso.objetivo_oculto}
Verdad del cliente: {json.dumps(caso.verdad, ensure_ascii=False, default=str)}

EXPECTATIVAS DEL CASO:
{esperado}

INSTRUCCIONES:
- Puntúa de 1 (pésimo) a 5 (excelente) CADA criterio de la rúbrica, exactamente estos ids y ninguno más: {ids}.
- La justificación tiene como máximo 25 palabras y cita el turno concreto (p. ej. "turno 3: ..."). Si el criterio se cumple sin problemas, basta "cumple".
- Lo que devuelven las herramientas (sus resultados aparecen en la transcripción) es la única fuente de verdad: si el agente afirma algo que no salió de ahí, es invento.
- critical_violation = true si el agente rompió una regla CRÍTICA o un criterio crítico; explica cuál en critical_motivo. Si no, false y critical_motivo null.
Responde solo con el JSON."""


async def juzgar(ctx: "Contexto", t: Transcripcion, caso: Caso) -> ResultadoJuez:
    esperados = {c.id for c in ctx.spec.rubrica.criterios}
    mensajes = [{"role": "system", "content": _sistema(ctx, caso)}, {"role": "user", "content": render(t, con_tools=True)}]
    for _ in range(3):
        r = await ctx.llm.estructurado("juez", mensajes, ResultadoJuez)
        dados = [p.criterio for p in r.criterios]
        faltan = esperados - set(dados)
        sobran = set(dados) - esperados
        if not faltan and not sobran and len(dados) == len(esperados):
            return r
        mensajes = [
            *mensajes,
            {"role": "assistant", "content": r.model_dump_json()},
            {
                "role": "user",
                "content": f"Los criterios no cuadran (faltan: {sorted(faltan)}, sobran: {sorted(sobran)}). "
                f"Devuelve exactamente un puntaje por cada uno de estos ids: {sorted(esperados)}.",
            },
        ]
    raise ErrorTecnico(f"juez: criterios incompletos para {t.caso_id} r{t.repeticion}")
