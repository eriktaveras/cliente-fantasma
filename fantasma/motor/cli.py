"""python -m fantasma.motor init | run | calibrate | report | exportar"""

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console

from fantasma.motor import calibrar, reporte, webhook
from fantasma.motor.config import Config, cargar
from fantasma.motor.contexto import Contexto
from fantasma.motor.esquemas import Corrida, RubroSpec, Spec
from fantasma.motor.llm import LLM
from fantasma.motor.storage import Store

consola = Console()
RUNS = Path("runs")


def progreso(msg: str) -> None:
    consola.print(f"[dim]{datetime.now():%H:%M:%S}[/dim] {msg}")


def _dir(run_id: str) -> Path:
    d = RUNS / run_id
    if not (d / "results.sqlite").exists():
        sys.exit(f"No existe la corrida {run_id} en {RUNS}/")
    return d


def _config_de(d: Path) -> Config:
    # Cada corrida guarda su copia de config.yaml: reanudar no depende de que el global siga igual.
    return cargar(d / "config.yaml")


def _recortar(spec: Spec, n: int) -> Spec:
    """Para corridas de prueba: n casos manteniendo la proporción train/holdout y los críticos primero."""
    from fantasma.motor.generador import estratificar

    ordenados = sorted(spec.casos, key=lambda c: (not c.critico, c.id))[:n]
    holdout = sum(c.split == "holdout" for c in spec.casos) / len(spec.casos)
    return spec.model_copy(update={"casos": estratificar(ordenados, holdout)})


# ------------------------------------------------------------------ init


async def init(args) -> None:
    from fantasma.motor.generador import generar_spec

    config = cargar(args.config)
    rubro = RubroSpec.model_validate(yaml.safe_load(args.objective.read_text()))
    run_id = args.run_id or f"{datetime.now():%Y%m%d-%H%M}-{re.sub(r'[^a-z0-9]+', '-', rubro.rubro.lower())}"
    d = RUNS / run_id
    if (d / "results.sqlite").exists():
        sys.exit(f"Ya existe la corrida {run_id}")
    store = Store(d)
    shutil.copy(args.config, d / "config.yaml")
    store.guardar_corrida(Corrida(id=run_id, rubro=rubro.rubro, estado="spec"))

    llm = LLM(config)
    t0 = time.monotonic()
    spec = await generar_spec(llm, config, rubro, progreso)
    store.guardar_spec(spec)
    store.guardar_corrida(Corrida(id=run_id, rubro=rubro.rubro, estado="esperando_aprobacion"))
    train, hold = len(spec.de("train")), len(spec.de("holdout"))
    progreso(f"Spec lista en {(time.monotonic() - t0) / 60:.1f} min: {len(spec.datos.catalogo)} items, {len(spec.rubrica.criterios)} criterios, {train} casos train + {hold} holdout")
    _uso(llm)
    consola.print(f"\nRevisa y edita [bold]{d}/spec/[/bold] y luego: [bold]python -m fantasma.motor run --run-id {run_id} --approve-spec[/bold]")
    if args.auto:
        await run(argparse.Namespace(run_id=run_id, auto=True, approve_spec=True, resume=False, max_iterations=None, casos=None))


# ------------------------------------------------------------------ run


async def run(args) -> None:
    from fantasma.motor.evaluar import evaluar_conjunto
    from fantasma.motor.loop import correr

    d = _dir(args.run_id)
    store = Store(d)
    corrida = store.corrida()
    if corrida.estado == "esperando_aprobacion" and not (args.approve_spec or args.auto):
        sys.exit(f"La spec espera revisión: {d}/spec/. Corre con --approve-spec cuando esté lista.")
    if corrida.estado in ("listo", "meseta", "presupuesto") and not args.resume:
        sys.exit(f"La corrida ya terminó ({corrida.estado}). Usa --resume para seguir con más presupuesto o `report`.")
    if corrida.estado in ("esperando_aprobacion", "spec", "listo", "meseta", "presupuesto"):
        corrida.estado = "corriendo"
        store.guardar_corrida(corrida)

    config = _config_de(d)
    if args.max_iterations:
        config.presupuesto.max_iterations = args.max_iterations
    spec = store.cargar_spec()
    if args.casos:
        spec = _recortar(spec, args.casos)
    ctx = Contexto(config, LLM(config), spec, store)
    progreso(f"Corrida {args.run_id}: {len(spec.de('train'))} train / {len(spec.de('holdout'))} holdout · concurrencia {config.concurrency}")

    try:
        corrida = await correr(ctx, progreso)
    except Exception:
        c = store.corrida()
        c.estado, c.motivo_parada = "error", "excepción, ver consola; --resume para seguir"
        store.guardar_corrida(c)
        raise

    if "produccion" in config.roles:
        # Fase 5: mismo prompt final contra el modelo de producción. Versión desplazada para no chocar con los checkpoints locales.
        final = corrida.mejor_version
        casos = spec.de("holdout") + [c for c in spec.de("train") if c.critico]
        progreso(f"Validación con producción ({config.roles['produccion'].modelo}): {len(casos)} casos")
        ag, _ = await evaluar_conjunto(ctx, casos, store.texto(final), 1000 + final, "produccion", config.repetitions_per_case, "produccion", progreso)
        store.guardar_agregado(ag.model_copy(update={"version": final}))

    ruta = reporte.escribir(store, spec, config)
    _uso(ctx.llm)
    progreso(f"[bold]{corrida.estado}[/bold] · v{corrida.mejor_version:03d} · reporte en {ruta}")
    if config.webhook_url:
        progreso(webhook.avisar(config.webhook_url, store, corrida, str(ruta.resolve())))


def _uso(llm: LLM) -> None:
    from fantasma import costos

    usd = costos.por_rol(llm, llm.config)
    for rol, u in llm.uso.items():
        if u.llamadas:
            progreso(f"uso {rol}: {u.llamadas} llamadas · {u.tokens_entrada:,} entrada · {u.tokens_salida:,} salida · "
                     f"{u.tokens_salida / max(u.ms / 1000, 1):.0f} tok/s por llamada · US${usd[rol]:.3f}")
    progreso(f"costo total: US${sum(usd.values()):.3f}")


# ------------------------------------------------------------------ calibrate / report / exportar


def calibrate(args) -> None:
    d = _dir(args.run_id)
    store = Store(d)
    if args.importar:
        r = calibrar.importar(store, args.importar)
        progreso(f"Concordancia juez-humano: {r['concordancia'] * 100:.0f}% en {r['n']} transcripciones")
        if r["concordancia"] < 0.85:
            consola.print("[bold red]Por debajo del 85%: ajusta la rúbrica o el modelo del juez antes de confiar en las corridas.[/bold red]")
        for x in r["desacuerdos"]:
            progreso(f"  desacuerdo {x['caso_id']}: humano {'pasa' if x['humano'] else 'falla'}, juez {'pasa' if x['juez'] else 'falla'}")
        return
    corrida = store.corrida()
    version = args.version if args.version is not None else corrida.mejor_version
    ruta = calibrar.exportar(store, store.cargar_spec(), version, args.n)
    consola.print(f"Califica {ruta} (humano_paso y humano_critica con si/no; las conversaciones están en la misma carpeta) y luego:\n"
                  f"[bold]python -m fantasma.motor calibrate --run-id {args.run_id} --importar {ruta}[/bold]")


def report(args) -> None:
    d = _dir(args.run_id)
    store = Store(d)
    progreso(f"Reporte: {reporte.escribir(store, store.cargar_spec(), _config_de(d))}")


def exportar(args) -> None:
    from fantasma.motor.herramientas import caja_para

    d = _dir(args.run_id)
    store = Store(d)
    spec = store.cargar_spec()
    final = store.corrida().mejor_version
    prompt = store.texto(final)
    if args.formato == "md":
        ruta = d / "export" / "system_prompt.md"
        ruta.parent.mkdir(exist_ok=True)
        ruta.write_text(prompt)
    else:
        ruta = d / "export" / "agente_n8n.json"
        ruta.parent.mkdir(exist_ok=True)
        ruta.write_text(json.dumps({
            "rubro": spec.rubro.rubro,
            "version": final,
            "modelo_afinado": spec.rubro.modelo_destino,
            "systemMessage": prompt,
            "herramientas": caja_para(spec.rubro, spec.datos).definiciones(),
            "nota": "Las herramientas son las de la simulación: en n8n cada una se conecta a su sistema real (CRM, calendario, inventario).",
        }, ensure_ascii=False, indent=2))
    progreso(f"Exportado: {ruta}")


# ------------------------------------------------------------------ main


def main() -> None:
    ap = argparse.ArgumentParser(prog="fantasma.motor", description="Forja agentes IA por rubro con simulaciones en IA local")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="genera la spec (datos, rúbrica, casos, prompt v000) a partir de un rubro")
    p.add_argument("--objective", type=Path, required=True)
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--run-id")
    p.add_argument("--auto", action="store_true", help="no esperar la revisión de la spec")

    p = sub.add_parser("run", help="corre (o reanuda) el loop de optimización")
    p.add_argument("--run-id", required=True)
    p.add_argument("--approve-spec", action="store_true")
    p.add_argument("--auto", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--max-iterations", type=int)
    p.add_argument("--casos", type=int, help="usar solo N casos (corrida de prueba)")

    p = sub.add_parser("calibrate", help="exporta transcripciones para calificar a mano, o importa las calificaciones")
    p.add_argument("--run-id", required=True)
    p.add_argument("--importar", type=Path)
    p.add_argument("--version", type=int)
    p.add_argument("-n", type=int, default=20)

    p = sub.add_parser("report", help="regenera report.md")
    p.add_argument("--run-id", required=True)

    p = sub.add_parser("exportar", help="exporta el prompt final")
    p.add_argument("--run-id", required=True)
    p.add_argument("--formato", choices=["n8n", "md"], default="n8n")

    args = ap.parse_args()
    match args.cmd:
        case "init":
            asyncio.run(init(args))
        case "run":
            asyncio.run(run(args))
        case "calibrate":
            calibrate(args)
        case "report":
            report(args)
        case "exportar":
            exportar(args)
