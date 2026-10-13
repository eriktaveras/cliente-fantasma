"""El bucle de optimización: mide la base, propone, filtra en dos etapas, vigila el sobreajuste y para a tiempo.

Todo el estado sale del store (versiones + agregados + corrida), así que --resume es volver a llamar a correr():
las evaluaciones ya hechas se reusan por checkpoint y una versión "candidata" sin estado final se retoma.
"""

import time
from collections import defaultdict
from collections.abc import Callable

from fantasma.motor.config import Config
from fantasma.motor.esquemas import Agregado, Caso, Corrida, ResultadoCaso, VersionPrompt
from fantasma.motor.evaluar import agregar, consolidar, evaluar_conjunto, evaluar_rep, reunir
from fantasma.motor.optimizador import proponer, tokens, validar_propuesta

Progreso = Callable[[str], None]


# ---------------------------------------------------------------- decisiones puras


def aceptar(
    actual_ag: Agregado,
    cand_ag: Agregado,
    actual_res: list[ResultadoCaso],
    cand_res: list[ResultadoCaso],
    min_imp: float,
    criticos: set[str],
    tolerados: int = 0,
) -> tuple[bool, str]:
    pasaban = {r.caso_id for r in actual_res if r.paso and r.caso_id in criticos}
    rotos = sorted(r.caso_id for r in cand_res if r.caso_id in pasaban and not r.paso)
    delta = cand_ag.puntaje - actual_ag.puntaje
    if len(rotos) > tolerados:
        return False, f"rompe críticos que pasaban: {', '.join(rotos)}"
    if delta < min_imp:
        return False, f"mejora {delta:+.3f} < {min_imp:.3f} ({cand_ag.puntaje:.3f} vs {actual_ag.puntaje:.3f})"
    riesgo = f"; con riesgo: rompe {', '.join(rotos)}" if rotos else ""
    return True, f"mejora {delta:+.3f} ({cand_ag.puntaje:.3f} vs {actual_ag.puntaje:.3f}){riesgo}"


def debe_parar(corrida: Corrida, actual: VersionPrompt, mejor: VersionPrompt, config: Config, horas: float, sin_mejora: int) -> str | None:
    metas, pres = config.metas, config.presupuesto
    for v in (actual, mejor):
        if (
            v.train is not None
            and v.train >= metas.target_train_score
            and v.criticos_ok
            and v.holdout is not None
            and v.holdout >= metas.target_holdout_score
        ):
            return "listo"
    if sin_mejora >= pres.plateau_patience:
        return "meseta"
    if corrida.iteracion >= pres.max_iterations or horas >= pres.max_hours:
        return "presupuesto"
    return None


def banco_corto(casos: list[Caso], resultados: list[ResultadoCaso], tam: int) -> list[Caso]:
    """Críticos + los que fallan en la actual, y relleno por id hasta `tam`. Determinista para que el banco no cambie al azar."""
    fallidos = {r.caso_id for r in resultados if not r.paso}
    ordenados = sorted(casos, key=lambda c: c.id)
    banco = [c for c in ordenados if c.critico or c.id in fallidos]
    elegidos = {c.id for c in banco}
    relleno = [c for c in ordenados if c.id not in elegidos][: max(0, tam - len(banco))]
    return sorted(banco + relleno, key=lambda c: c.id)


def mejor_por_holdout(versiones: list[VersionPrompt]) -> VersionPrompt:
    candidatas = [v for v in versiones if v.estado in ("base", "aceptada") and v.holdout is not None]
    return max(candidatas, key=lambda v: (v.holdout, v.train or 0.0, v.n))


def version_actual(versiones: list[VersionPrompt]) -> int:
    """La última aceptada; si después hubo una reversión, la mejor por holdout."""
    ultima_ok = max(v.n for v in versiones if v.estado in ("base", "aceptada"))
    revertidas = [v.n for v in versiones if v.estado == "revertida"]
    if revertidas and max(revertidas) > ultima_ok:
        return mejor_por_holdout(versiones).n
    return ultima_ok


def sin_mejora(versiones: list[VersionPrompt]) -> int:
    n = 0
    for v in reversed(versiones):
        if v.estado in ("rechazada", "revertida"):
            n += 1
        elif v.estado in ("base", "aceptada"):
            break
    return n


# ---------------------------------------------------------------- bucle


async def correr(ctx, progreso: Progreso | None = None) -> Corrida:
    cfg, store, spec = ctx.config, ctx.store, ctx.spec
    opt, metas = cfg.optimizacion, cfg.metas
    avisar = progreso or (lambda _: None)
    train, holdout = spec.de("train"), spec.de("holdout")
    criticos = {c.id for c in train if c.critico}
    reps = cfg.repetitions_per_case

    t0 = time.monotonic()
    corrida = store.corrida() or Corrida(id=store.dir.name, rubro=spec.rubro.rubro)
    horas_previas = corrida.horas
    corrida.estado = "corriendo"

    def horas() -> float:
        return horas_previas + (time.monotonic() - t0) / 3600

    def guardar_corrida() -> None:
        corrida.horas = horas()
        store.guardar_corrida(corrida)

    async def medir(v: VersionPrompt, casos: list[Caso], conjunto: str, n_reps: int) -> tuple[Agregado, list[ResultadoCaso]]:
        guardado = store.agregado(v.n, conjunto)
        if guardado is not None and conjunto != "corto":
            return guardado, store.resultados(v.n, conjunto)
        ag, res = await evaluar_conjunto(ctx, casos, store.texto(v.n), v.n, conjunto, n_reps, progreso=avisar)
        store.guardar_agregado(ag)
        store.guardar_resultados(conjunto, res)
        if conjunto == "train":
            v.train, v.criticos_ok = ag.puntaje, ag.criticos_ok
        elif conjunto == "holdout":
            v.holdout = ag.puntaje
        else:
            return ag, res
        store.guardar_version(v)
        return ag, res

    if store.version(0) is None:
        store.guardar_version(VersionPrompt(n=0, estado="base", tokens=tokens(spec.prompt_base)), spec.prompt_base)
    base = store.version(0)
    avisar("línea base: v000")
    base_ag, _ = await medir(base, train, "train", reps)
    await medir(base, holdout, "holdout", reps)
    if corrida.min_improvement is None:
        ruido = await _ruido(ctx, train, store.texto(0), reps, base_ag.puntaje) if opt.medir_ruido else 0.0
        corrida.min_improvement = max(opt.min_improvement, ruido)
        avisar(f"ruido de v000: {ruido:.3f}; mejora mínima efectiva {corrida.min_improvement:.3f}")
    guardar_corrida()

    while True:
        versiones = store.versiones()
        actual = store.version(version_actual(versiones))
        mejor = mejor_por_holdout(versiones)
        corrida.iteracion = sum(v.estado in ("aceptada", "rechazada", "revertida") for v in versiones)
        corrida.mejor_version = mejor.n
        motivo = debe_parar(corrida, actual, mejor, cfg, horas(), sin_mejora(versiones))
        if motivo:
            break
        guardar_corrida()
        avisar(
            f"iteración {corrida.iteracion + 1}: actual v{actual.n:03d} train {actual.train:.3f} | "
            f"mejor v{mejor.n:03d} holdout {mejor.holdout:.3f} | quedan {cfg.presupuesto.max_hours - horas():.1f} h"
        )
        act_ag, act_res = await medir(actual, train, "train", reps)
        texto_actual = store.texto(actual.n)

        cand = next((v for v in versiones if v.estado == "candidata"), None)
        if cand is None:
            fallos = _fallos(store, train, act_res, actual.n, reps, min(5, max(3, opt.muestra_fallos)))
            p = await proponer(ctx, texto_actual, act_ag, fallos, versiones, store.historial())
            cand = VersionPrompt(n=versiones[-1].n + 1, padre=actual.n, hipotesis=p.hipotesis, cambio=p.cambio, tokens=tokens(p.prompt))
            rechazo = validar_propuesta(p, texto_actual, spec, opt.max_prompt_tokens)
            if rechazo:
                _rechazar(store, cand, rechazo, p.prompt)
                avisar(f"v{cand.n:03d} rechazada: {rechazo}")
                continue
            store.guardar_version(cand, p.prompt)
            avisar(f"v{cand.n:03d} candidata: {cand.cambio}")

        banco = banco_corto(train, act_res, opt.banco_corto)
        corto_act, _ = await medir(actual, banco, "corto", 1)
        corto_cand, _ = await medir(cand, banco, "corto", 1)
        cand.corto = corto_cand.puntaje
        if corto_cand.puntaje <= corto_act.puntaje:
            _rechazar(store, cand, f"etapa A: {corto_cand.puntaje:.3f} vs {corto_act.puntaje:.3f}")
            avisar(f"v{cand.n:03d} rechazada en etapa A")
            continue

        cand_ag, cand_res = await medir(cand, train, "train", reps)
        ok, motivo_b = aceptar(act_ag, cand_ag, act_res, cand_res, corrida.min_improvement, criticos, opt.criticos_rotos_tolerados)
        if not ok:
            _rechazar(store, cand, f"etapa B: {motivo_b}")
            avisar(f"v{cand.n:03d} rechazada en etapa B: {motivo_b}")
            continue

        cand.estado, cand.motivo = "aceptada", motivo_b
        store.guardar_version(cand)
        avisar(f"v{cand.n:03d} aceptada: {motivo_b}")
        aceptadas = sum(v.estado in ("aceptada", "revertida") for v in versiones) + 1
        cerca_de_meta = cand_ag.puntaje >= metas.target_train_score and cand_ag.criticos_ok
        if aceptadas % opt.holdout_cada == 0 or cerca_de_meta:
            await medir(cand, holdout, "holdout", reps)
            if cand.train > mejor.train and cand.holdout < mejor.holdout - opt.sobreajuste:
                cand.estado = "revertida"
                cand.motivo = f"sobreajuste: holdout {cand.holdout:.3f} vs {mejor.holdout:.3f} de v{mejor.n:03d}"
                store.guardar_version(cand)
                store.anotar(
                    f"v{cand.n:03d} ({cand.cambio}) subió train a {cand.train:.3f} pero bajó holdout a {cand.holdout:.3f} "
                    f"(v{mejor.n:03d} tenía {mejor.holdout:.3f}): sobreajuste, se volvió a v{mejor.n:03d}"
                )
                avisar(f"v{cand.n:03d} revertida por sobreajuste; vuelve v{mejor.n:03d}")

    if actual.holdout is None:
        await medir(actual, holdout, "holdout", reps)
    mejor = mejor_por_holdout(store.versiones())
    (store.dir / "final_prompt.md").write_text(store.texto(mejor.n))
    corrida.mejor_version = mejor.n
    corrida.estado = motivo
    corrida.motivo_parada = motivo
    guardar_corrida()
    avisar(f"fin ({motivo}): mejor v{mejor.n:03d} train {mejor.train:.3f} holdout {mejor.holdout:.3f}")
    return corrida


def _rechazar(store, v: VersionPrompt, motivo: str, texto: str | None = None) -> None:
    v.estado, v.motivo = "rechazada", motivo
    store.guardar_version(v, texto)
    store.anotar(f"v{v.n:03d} rechazada ({v.cambio}): {motivo}")


async def _ruido(ctx, casos: list[Caso], prompt: str, reps: int, puntaje: float) -> float:
    """Segunda medida independiente de v000 con las repeticiones reps..2*reps-1; la diferencia es el ruido del banco."""
    evals = await reunir(*(evaluar_rep(ctx, c, prompt, 0, r) for c in casos for r in range(reps, 2 * reps)))
    por_caso = defaultdict(list)
    for e in evals:
        if e is not None:
            por_caso[e.caso_id].append(e)
    validos = [e for es in por_caso.values() for e in es]
    ag = agregar(0, "train", [consolidar(es) for es in por_caso.values()], validos, casos)
    return abs(ag.puntaje - puntaje)


def _fallos(store, casos: list[Caso], resultados: list[ResultadoCaso], version: int, reps: int, cuantos: int):
    """Los peores casos de la actual: primero los que fallan (críticos antes), luego por puntaje. Si nada falla, los más flojos."""
    por_id = {c.id: c for c in casos}
    orden = sorted(resultados, key=lambda r: (r.paso, not por_id[r.caso_id].critico, r.puntaje, r.caso_id))
    fallos = []
    for r in orden:
        evals = [e for rep in range(reps) if (e := store.evaluacion(version, r.caso_id, rep)) is not None]
        if not evals:
            continue
        peor = min(evals, key=lambda e: (e.puntaje, e.repeticion))
        t = store.transcripcion(version, r.caso_id, peor.repeticion)
        if t is not None:
            fallos.append((por_id[r.caso_id], t, peor))
        if len(fallos) == cuantos:
            break
    return fallos
