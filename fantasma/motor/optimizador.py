"""El optimizador: lee fallos de train y propone UN cambio al prompt; validar_propuesta filtra lo que no debe entrar."""

import json
import re
import unicodedata
from typing import Any

from fantasma.motor.esquemas import Agregado, Caso, EvaluacionRep, Propuesta, Rubrica, Spec, Transcripcion, VersionPrompt
from fantasma.motor.simulador import render

MAX_FALLOS = 5
PRESENCIA_REGLA = 0.6

VACIAS = set(
    "a al ante bajo con contra de del desde el en entre hacia hasta la las le les lo los mas me mi mis muy ni no o "
    "os para pero por que se si sin sobre su sus te tu tus un una uno unos unas y ya es son ser como cuando donde".split()
)

SISTEMA = """Eres el optimizador de la cuna: mejoras el system prompt de un agente de atención al cliente de un negocio.
Te doy el prompt actual, cómo se evalúa, sus resultados en un conjunto de entrenamiento y algunas conversaciones fallidas.

Tu trabajo:
1. Diagnostica la causa MÁS PROBABLE de los fallos (qué del prompt falta, sobra, es ambiguo o se contradice).
2. Propón UN SOLO cambio enfocado y general: una regla de conducta que sirva a cualquier cliente, no un parche para una conversación concreta.
3. Escribe la hipótesis: qué esperas que mejore y por qué.
4. Devuelve el prompt COMPLETO nuevo (no un diff), igual al actual salvo por tu cambio.

Restricciones:
- No quites ni debilites las reglas críticas.
- No repitas cambios ya rechazados o revertidos del historial.
- No menciones nombres, frases, cifras ni datos concretos de las conversaciones de ejemplo: el prompt debe generalizar.
- El prompt nuevo no puede pasar de {max_tokens} tokens (unos {max_chars} caracteres).

Responde solo con JSON: {{"hipotesis": "...", "cambio": "...", "prompt": "..."}}."""


async def proponer(
    ctx,
    prompt_actual: str,
    agregado: Agregado,
    fallos: list[tuple[Caso, Transcripcion, EvaluacionRep]],
    versiones: list[VersionPrompt],
    lecciones: list[str],
) -> Propuesta:
    max_tokens = ctx.config.optimizacion.max_prompt_tokens
    spec: Spec = ctx.spec
    partes = [
        "## Prompt actual\n\n" + prompt_actual,
        "## Rúbrica\n\n" + _rubrica(spec.rubrica),
        "## Reglas críticas (no se pueden quitar)\n\n" + ("\n".join(f"- {r.texto}" for r in spec.rubro.reglas if r.critica) or "(ninguna)"),
        "## Resultados en entrenamiento\n\n" + _agregado(agregado),
        "## Conversaciones con fallos\n\n" + "\n\n".join(_fallo(i, *f) for i, f in enumerate(fallos[:MAX_FALLOS], 1)),
        "## Historial de versiones\n\n" + ("\n".join(_version(v) for v in versiones) or "(sin historial)"),
        "## Lecciones\n\n" + ("\n".join(f"- {l}" for l in lecciones) or "(ninguna)"),
    ]
    mensajes = [
        {"role": "system", "content": SISTEMA.format(max_tokens=max_tokens, max_chars=int(max_tokens * 3.5))},
        {"role": "user", "content": "\n\n".join(partes)},
    ]
    return await ctx.llm.estructurado("optimizador", mensajes, Propuesta)


def _rubrica(r: Rubrica) -> str:
    lineas = []
    for c in r.criterios:
        lineas.append(f"- {c.id} ({c.nombre}, peso {c.peso:g}{', CRÍTICO' if c.critico else ''}): {c.descripcion}")
        lineas += [f"    cumple: {x}" for x in c.cumple] + [f"    no cumple: {x}" for x in c.no_cumple]
    return "\n".join(lineas)


def _agregado(a: Agregado) -> str:
    criterios = ", ".join(f"{k} {v:.2f}" for k, v in sorted(a.por_criterio.items()))
    return (
        f"puntaje {a.puntaje:.3f} | pasan {a.pasan}/{a.casos} | críticos {a.criticos_pasan}/{a.criticos_total} | "
        f"inestables {a.inestables}\npor criterio: {criterios or '(sin datos)'}"
    )


def _fallo(i: int, caso: Caso, t: Transcripcion, e: EvaluacionRep) -> str:
    lineas = [f"### Fallo {i} (escenario: {caso.escenario}{', crítico' if caso.critico else ''}; puntaje {e.puntaje:.2f})", render(t, con_tools=True)]
    malas = [v for v in e.verificaciones if not v.ok]
    if malas:
        lineas.append("Verificaciones fallidas:\n" + "\n".join(f"- {v.id}{' (CRÍTICA)' if v.critica else ''}: {v.detalle}" for v in malas))
    if e.juez:
        juicio = [f"- {p.criterio}: {p.puntaje}/5. {p.justificacion}" for p in e.juez.criterios if p.puntaje <= 3]
        if e.juez.critical_violation:
            juicio.insert(0, f"- VIOLACIÓN CRÍTICA: {e.juez.critical_motivo or ''}")
        if juicio:
            lineas.append("Juez:\n" + "\n".join(juicio))
    return _anonimizar("\n".join(lineas), caso.persona.nombre)


def _anonimizar(texto: str, nombre: str) -> str:
    variantes = sorted({nombre, nombre.split()[0]}, key=len, reverse=True)
    for v in variantes:
        texto = re.sub(rf"\b{re.escape(v)}\b", "CLIENTE", texto, flags=re.IGNORECASE)
    return texto


def _version(v: VersionPrompt) -> str:
    return f"- v{v.n:03d} [{v.estado}] cambio: {v.cambio or '-'} | hipótesis: {v.hipotesis or '-'} | motivo: {v.motivo or '-'}"


# ---------------------------------------------------------------- validación


def tokens(texto: str) -> int:
    return round(len(texto) / 3.5)


def validar_propuesta(p: Propuesta, prompt_actual: str, spec: Spec, max_tokens: int) -> str | None:
    nuevo, actual = _palabras(p.prompt), _palabras(prompt_actual)
    if nuevo == actual:
        return "prompt idéntico al actual"
    if len(p.prompt) / 3.5 > max_tokens:
        return f"demasiado largo: ~{tokens(p.prompt)} tokens > {max_tokens}"

    texto_nuevo, texto_actual = " ".join(nuevo), " ".join(actual)
    for caso in spec.casos:
        for nombre in _nombres(caso.persona.nombre):
            if _contiene(texto_nuevo, nombre) and not _contiene(texto_actual, nombre):
                return f"filtra el nombre de una persona del caso {caso.id}"

    nuevos_5 = _ngramas(nuevo) - _ngramas(actual)
    for caso in spec.casos:
        fuente = _palabras(caso.objetivo_oculto + " " + " ".join(_textos(caso.verdad)))
        if nuevos_5 & _ngramas(fuente):
            return f"filtra datos del caso {caso.id}"

    vocab_nuevo, vocab_actual = set(nuevo), set(actual)
    for regla in spec.rubro.reglas:
        if regla.critica and _presente(regla.texto, vocab_actual) and not _presente(regla.texto, vocab_nuevo):
            return f"quita la regla crítica {regla.id}"
    return None


def _normalizar(texto: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return sin_tildes.lower()


def _palabras(texto: str) -> list[str]:
    return re.findall(r"\w+", _normalizar(texto))


def _ngramas(palabras: list[str], n: int = 5) -> set[tuple[str, ...]]:
    return {tuple(palabras[i : i + n]) for i in range(len(palabras) - n + 1)}


def _nombres(nombre: str) -> set[str]:
    completo = _palabras(nombre)
    return {" ".join(completo), completo[0]} if completo else set()


def _contiene(texto: str, frase: str) -> bool:
    return re.search(rf"\b{re.escape(frase)}\b", texto) is not None


def _textos(valor: Any) -> list[str]:
    if isinstance(valor, dict):
        return [t for v in valor.values() for t in _textos(v)]
    if isinstance(valor, list | tuple | set):
        return [t for v in valor for t in _textos(v)]
    return [valor if isinstance(valor, str) else json.dumps(valor, default=str)]


def _presente(regla: str, vocab: set[str]) -> bool:
    contenido = [w for w in _palabras(regla) if w not in VACIAS]
    return bool(contenido) and sum(w in vocab for w in contenido) / len(contenido) >= PRESENCIA_REGLA
