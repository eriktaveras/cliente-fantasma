"""report.md de una corrida: estado, evolución, cambios, fallos que quedan, inestables, diff y recomendación."""

import difflib
import json
from pathlib import Path

from fantasma.motor.config import Config
from fantasma.motor.esquemas import Corrida, ResultadoCaso, Spec
from fantasma.motor.storage import Store

ESTADOS = {
    "listo": "LISTO: cumple las metas de train, holdout y críticos",
    "meseta": "MESETA: dejó de mejorar",
    "presupuesto": "PRESUPUESTO AGOTADO: se acabaron las iteraciones o las horas",
    "corriendo": "EN CURSO (o cortada sin terminar)",
    "esperando_aprobacion": "ESPERANDO APROBACIÓN DE LA SPEC",
    "error": "ERROR",
}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}"


def escribir(store: Store, spec: Spec, config: Config) -> Path:
    corrida = store.corrida()
    versiones = store.versiones()
    final = corrida.mejor_version if corrida else 0
    casos = {c.id: c for c in spec.casos}
    l: list[str] = [f"# Reporte de la cuna: {spec.rubro.negocio} ({spec.rubro.rubro})", ""]

    l += ["## Estado final", ""]
    if corrida:
        l.append(f"- **{ESTADOS.get(corrida.estado, corrida.estado)}**" + (f" ({corrida.motivo_parada})" if corrida.motivo_parada else ""))
        l.append(f"- Iteraciones: {corrida.iteracion} · horas: {corrida.horas:.1f} · versión final: v{final:03d}")
        if corrida.min_improvement is not None:
            l.append(f"- Mejora mínima efectiva (tras medir el ruido de v000): {corrida.min_improvement * 100:.1f} puntos")
    agf = {c: store.agregado(final, c) for c in ("train", "holdout", "produccion")}
    if agf["train"]:
        a = agf["train"]
        l.append(f"- Train: {_pct(a.puntaje)} · casos que pasan {a.pasan}/{a.casos} · críticos {a.criticos_pasan}/{a.criticos_total} · inestables {a.inestables}")
    if agf["holdout"]:
        a = agf["holdout"]
        l.append(f"- Holdout: {_pct(a.puntaje)} · casos que pasan {a.pasan}/{a.casos} · críticos {a.criticos_pasan}/{a.criticos_total}")
    l.append(f"- Metas: train {_pct(config.metas.target_train_score)} · holdout {_pct(config.metas.target_holdout_score)} · críticos 100%")
    l.append("")

    l += ["## Evolución por versión", "", "| v | estado | corto | train | holdout | cambio | motivo |", "|---|---|---|---|---|---|---|"]
    for v in versiones:
        l.append(f"| {v.n:03d} | {v.estado} | {_pct(v.corto)} | {_pct(v.train)} | {_pct(v.holdout)} | {(v.cambio or '').replace('|', '/')} | {v.motivo.replace('|', '/')} |")
    l.append("")

    aceptadas = [v for v in versiones if v.estado == "aceptada"]
    l += ["## Cambios aceptados", ""]
    if not aceptadas:
        l.append("Ninguno.")
    for v in aceptadas:
        padre = store.version(v.padre) if v.padre is not None else None
        delta = (v.train - padre.train) * 100 if padre and padre.train is not None and v.train is not None else None
        l.append(f"- **v{v.n:03d}** {v.cambio}" + (f" (train {delta:+.1f})" if delta is not None else ""))
        l.append(f"  - Hipótesis: {v.hipotesis}")
    l.append("")

    resultados: list[tuple[str, ResultadoCaso]] = [("train", r) for r in store.resultados(final, "train")] + [("holdout", r) for r in store.resultados(final, "holdout")]
    fallan = sorted((x for x in resultados if not x[1].paso), key=lambda x: x[1].puntaje)
    l += [f"## Casos que siguen fallando en v{final:03d} ({len(fallan)})", ""]
    for conjunto, r in fallan:
        c = casos.get(r.caso_id)
        etiquetas = ",".join(sorted(c.etiquetas)) if c else ""
        l.append(f"- `{r.caso_id}` ({conjunto}, {etiquetas}) {_pct(r.puntaje)}{' · INESTABLE' if r.inestable else ''}: falla {', '.join(r.fallos) or '—'}")
        causa = _causa(store, final, r.caso_id)
        if causa:
            l.append(f"  - Causa probable: {causa}")
    l.append("")

    inestables = [r for _, r in resultados if r.inestable]
    l += ["## Casos inestables", ""]
    l += [f"- `{r.caso_id}` puntajes por repetición: {', '.join(_pct(p) for p in r.puntajes)}" for r in inestables] or ["Ninguno."]
    l.append("")

    cal = store.dir / "calibracion.json"
    if cal.exists():
        c = json.loads(cal.read_text())
        l += ["## Calibración del juez", "", f"- Concordancia juez-humano: {c['concordancia'] * 100:.0f}% en {c['n']} transcripciones" + (" (BAJO 85%: no confiar del todo en el juez)" if c["concordancia"] < 0.85 else ""), ""]
    else:
        l += ["## Calibración del juez", "", "- Sin calibrar todavía (`python -m fantasma.motor calibrate`).", ""]

    if agf["produccion"] and agf["holdout"]:
        caida = (agf["holdout"].puntaje - agf["produccion"].puntaje) * 100
        l += ["## Validación con el modelo de producción", "", f"- Holdout local {_pct(agf['holdout'].puntaje)} vs producción {_pct(agf['produccion'].puntaje)} ({-caida:+.1f})"]
        if caida > 10:
            l.append("- **AVISO: el prompt no se transfirió bien al modelo de producción.**")
        l.append("")

    l += [f"## Diff v000 → v{final:03d}", "", "```diff"]
    l += list(difflib.unified_diff(store.texto(0).splitlines(), store.texto(final).splitlines(), "v000", f"v{final:03d}", lineterm=""))
    l += ["```", ""]

    l += ["## Recomendación", "", _recomendacion(corrida, agf, fallan, inestables, cal.exists()), ""]
    ruta = store.dir / "report.md"
    ruta.write_text("\n".join(l))
    return ruta


def _causa(store: Store, version: int, caso_id: str) -> str:
    """Lo que dijo el juez en la peor repetición, o la verificación determinista que falló."""
    evals = [e for e in store.evaluaciones(version) if e.caso_id == caso_id]
    if not evals:
        return ""
    peor = min(evals, key=lambda e: e.puntaje)
    det = [f"{v.id}: {v.detalle}" for v in peor.verificaciones if not v.ok and v.detalle]
    if peor.juez and peor.juez.critical_violation and peor.juez.critical_motivo:
        return peor.juez.critical_motivo
    bajos = sorted((p for p in peor.juez.criterios if p.puntaje <= 2), key=lambda p: p.puntaje) if peor.juez else []
    return (det[0] if det else "") or (f"{bajos[0].criterio}: {bajos[0].justificacion}" if bajos else "")


def _recomendacion(corrida: Corrida | None, agf, fallan, inestables, calibrado: bool) -> str:
    if corrida and corrida.estado == "listo" and calibrado:
        return "Listo para entregar: cumple las metas y el juez está calibrado. Revisar el diff a mano antes de publicarlo."
    pendientes = []
    if not calibrado:
        pendientes.append("calibrar el juez con 20 transcripciones")
    criticos = [r for _, r in fallan if "critical" in r.fallos or r.puntaje == 0]
    if agf["train"] and not agf["train"].criticos_ok:
        pendientes.append("resolver los casos críticos que fallan (ver arriba)")
    if inestables:
        pendientes.append(f"estabilizar {len(inestables)} casos inestables (instrucción más explícita o temperatura del agente)")
    if agf["holdout"] and agf["train"] and agf["train"].puntaje - agf["holdout"].puntaje > 0.08:
        pendientes.append("revisar sobreajuste: train y holdout se separan más de 8 puntos")
    if not pendientes and criticos:
        pendientes.append("revisar los casos con puntaje 0")
    return "No está listo. Falta: " + "; ".join(pendientes) + "." if pendientes else "Casi listo: revisar los fallos restantes y el diff."
