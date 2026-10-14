"""Del caso simulado al puntaje: verificaciones + juez por repetición, consolidado por caso y agregado por conjunto."""

import asyncio
from collections import defaultdict
from statistics import mean
from typing import TYPE_CHECKING, Callable

from fantasma.motor.esquemas import Agregado, Caso, Cobertura, EvaluacionRep, ResultadoCaso, ResultadoJuez, Rubrica, Transcripcion, Verificacion
from fantasma.motor.juez import juzgar
from fantasma.motor.llm import ErrorTecnico
from fantasma.motor.simulador import simular
from fantasma.motor.verificar import verificar

if TYPE_CHECKING:
    from fantasma.motor.contexto import Contexto

PESO_JUEZ = 0.7
INTENTOS_SIMULACION = 3
INTENTOS_JUEZ = 2


async def reunir(*coros):
    """gather que, si una falla, cancela a las demás y espera a que paren: con gather solo, seguían llamando al LLM y
    publicando eventos después de cerrar la corrida. No es TaskGroup porque este cancela tarde: las tareas que ya estaban
    en cola corren antes de que el padre despierte para abortarlas."""
    tareas = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tareas)
    except BaseException:
        for t in tareas:
            t.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        raise


def puntuar(verificaciones: list[Verificacion], juez: ResultadoJuez | None, rubrica: Rubrica, umbral: float) -> tuple[float, bool, bool]:
    no_criticas = [v for v in verificaciones if not v.critica]
    det = sum(v.ok for v in no_criticas) / len(no_criticas) if no_criticas else 1.0
    critica = any(v.critica and not v.ok for v in verificaciones)
    if juez is None:
        puntaje = det
    else:
        pesos = {c.id: c.peso for c in rubrica.criterios}
        criticos = {c.id for c in rubrica.criterios if c.critico}
        rubro = sum(pesos[p.criterio] * (p.puntaje - 1) / 4 for p in juez.criterios) / sum(pesos.values())
        puntaje = PESO_JUEZ * rubro + (1 - PESO_JUEZ) * det
        critica = critica or juez.critical_violation or any(p.criterio in criticos and p.puntaje <= 2 for p in juez.criterios)
    if critica:
        puntaje = 0.0
    esperados_ok = all(v.ok for v in verificaciones if v.id.startswith("esperado:"))
    return puntaje, critica, not critica and puntaje >= umbral and esperados_ok


def consolidar(evals: list[EvaluacionRep], reps: int | None = None) -> ResultadoCaso:
    pasos = [e.paso for e in evals]
    fallos = [v.id for e in evals for v in e.verificaciones if not v.ok]
    fallos += [p.criterio for e in evals if e.juez for p in e.juez.criterios if p.puntaje <= 2]
    return ResultadoCaso(
        caso_id=evals[0].caso_id,
        version=evals[0].version,
        puntajes=[e.puntaje for e in evals],
        puntaje=mean(e.puntaje for e in evals),
        paso=all(pasos) and (reps is None or len({e.repeticion for e in evals}) == reps),
        inestable=any(pasos) and not all(pasos),
        fallos=list(dict.fromkeys(fallos)),
        repeticiones=sorted({e.repeticion for e in evals}),
        completo=reps is None or {e.repeticion for e in evals} == set(range(reps)),
    )


def cobertura(casos: list[Caso], evals: list[EvaluacionRep], reps: int, solicitados: int | None = None) -> Cobertura:
    solicitados = len(casos) if solicitados is None else max(solicitados, len(casos))
    esperadas = {(c.id, r) for c in casos for r in range(reps)}
    evaluadas = {(e.caso_id, e.repeticion) for e in evals} & esperadas
    completos = {c.id for c in casos if all((c.id, r) in evaluadas for r in range(reps))}
    presentes = {id_ for id_, _ in evaluadas}
    faltantes = sorted(c.id for c in casos if c.critico and c.id not in completos)
    return Cobertura(
        casos_solicitados=solicitados,
        casos_generados=len(casos),
        casos_evaluados=len(presentes),
        casos_completos=len(completos),
        casos_excluidos=solicitados - len(completos),
        repeticiones_solicitadas=solicitados * reps,
        repeticiones_evaluadas=len(evaluadas),
        repeticiones_excluidas=solicitados * reps - len(evaluadas),
        criticos_faltantes=faltantes,
        completa=bool(casos) and reps > 0 and len(completos) == solicitados,
    )


def agregar(version: int, conjunto: str, resultados: list[ResultadoCaso], evals: list[EvaluacionRep], casos: list[Caso], reps: int | None = None) -> Agregado:
    criticos = {c.id for c in casos if c.critico}
    incluidos = {r.caso_id for r in resultados}
    por_criterio: dict[str, list[float]] = defaultdict(list)
    for e in evals:
        if e.juez and e.caso_id in incluidos:
            for p in e.juez.criterios:
                por_criterio[p.criterio].append((p.puntaje - 1) / 4)
    return Agregado(
        version=version,
        conjunto=conjunto,
        puntaje=mean(r.puntaje for r in resultados) if resultados else 0.0,
        casos=len(resultados),
        pasan=sum(r.paso for r in resultados),
        criticos_total=len(criticos),
        criticos_pasan=sum(r.paso and r.caso_id in criticos for r in resultados),
        inestables=sum(r.inestable for r in resultados),
        por_criterio={k: mean(v) for k, v in por_criterio.items()},
        cobertura=cobertura(casos, evals, reps) if reps is not None else None,
    )


async def _simular(ctx: "Contexto", caso: Caso, prompt: str, version: int, repeticion: int, rol_agente: str) -> Transcripcion | None:
    for _ in range(INTENTOS_SIMULACION):
        try:
            t = await simular(ctx, caso, prompt, version, repeticion, rol_agente=rol_agente)
        except ErrorTecnico:
            continue
        if t.fin != "error_tecnico":
            return t
    return None


async def evaluar_rep(ctx: "Contexto", caso: Caso, prompt: str, version: int, repeticion: int, rol_agente: str = "agente") -> EvaluacionRep | None:
    """None = falla técnica persistente (servidor, formato): no se le cobra al prompt."""
    previa = ctx.store.evaluacion(version, caso.id, repeticion)
    if previa is not None:
        return previa
    t = await _simular(ctx, caso, prompt, version, repeticion, rol_agente)
    if t is None:
        return None
    ctx.store.guardar_transcripcion(t)
    verificaciones = verificar(t, caso, ctx.spec, ctx.caja, prompt)
    juez = None
    for _ in range(INTENTOS_JUEZ):
        try:
            juez = await juzgar(ctx, t, caso)
            break
        except ErrorTecnico:
            continue
    if juez is None:
        return None
    puntaje, critica, paso = puntuar(verificaciones, juez, ctx.spec.rubrica, ctx.config.metas.umbral_paso_caso)
    e = EvaluacionRep(
        caso_id=caso.id,
        repeticion=repeticion,
        version=version,
        verificaciones=verificaciones,
        juez=juez,
        puntaje=puntaje,
        critica=critica,
        paso=paso,
    )
    ctx.store.guardar_evaluacion(e)
    return e


async def evaluar_conjunto(
    ctx: "Contexto",
    casos: list[Caso],
    prompt: str,
    version: int,
    conjunto: str,
    reps: int,
    rol_agente: str = "agente",
    progreso: Callable[[str], None] | None = None,
) -> tuple[Agregado, list[ResultadoCaso]]:
    total = len(casos) * reps
    hechas = 0

    async def una(caso: Caso, rep: int) -> EvaluacionRep | None:
        nonlocal hechas
        e = await evaluar_rep(ctx, caso, prompt, version, rep, rol_agente)
        hechas += 1
        if progreso:
            estado = "error técnico" if e is None else f"{'pasa' if e.paso else 'falla'} {e.puntaje:.2f}{' CRÍTICA' if e.critica else ''}"
            progreso(f"[{conjunto} v{version:03d}] {hechas}/{total} {caso.id} r{rep}: {estado}")
        return e

    evals = await reunir(*(una(c, r) for c in casos for r in range(reps)))
    por_caso: dict[str, list[EvaluacionRep]] = defaultdict(list)
    for e in evals:
        if e is not None:
            por_caso[e.caso_id].append(e)

    resultados = []
    for c in casos:
        if por_caso[c.id]:
            resultados.append(consolidar(por_caso[c.id], reps))
        elif progreso:
            progreso(f"[{conjunto} v{version:03d}] {c.id} excluido: ninguna repetición válida (error técnico)")

    validas = [e for e in evals if e is not None]
    agregado = agregar(version, conjunto, resultados, validas, casos, reps)
    ctx.store.guardar_resultados(conjunto, resultados)
    ctx.store.guardar_agregado(agregado)
    return agregado, resultados
