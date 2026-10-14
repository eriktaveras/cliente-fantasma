"""Calibración del juez: Erik califica 20 transcripciones a mano y se mide cuánto coincide el juez."""

import csv
import json
from pathlib import Path

from fantasma.motor.esquemas import EvaluacionRep, Spec
from fantasma.motor.simulador import render
from fantasma.motor.storage import Store

COLUMNAS = ["n", "caso_id", "version", "repeticion", "archivo", "humano_paso", "humano_critica", "notas"]


def _muestra(evals: list[EvaluacionRep], n: int) -> list[EvaluacionRep]:
    """Variada y determinista: críticas, fallidas, aprobadas justas y aprobadas holgadas, alternando."""
    orden = sorted(evals, key=lambda e: (e.caso_id, e.repeticion))
    grupos = [
        [e for e in orden if e.critica],
        [e for e in orden if not e.critica and not e.paso],
        [e for e in orden if e.paso and e.puntaje < 0.85],
        [e for e in orden if e.paso and e.puntaje >= 0.85],
    ]
    elegidos: list[EvaluacionRep] = []
    vistos: set[str] = set()
    while len(elegidos) < n and any(grupos):
        for g in grupos:
            while g:
                e = g.pop(0)
                if e.caso_id not in vistos:
                    elegidos.append(e)
                    vistos.add(e.caso_id)
                    break
            if len(elegidos) == n:
                break
    return elegidos


def exportar(store: Store, spec: Spec, version: int, n: int = 20) -> Path:
    dir = store.dir / "calibracion"
    dir.mkdir(exist_ok=True)
    casos = {c.id: c for c in spec.casos}
    filas = []
    for i, e in enumerate(_muestra(store.evaluaciones(version), n), 1):
        t = store.transcripcion(e.version, e.caso_id, e.repeticion)
        c = casos[e.caso_id]
        archivo = dir / f"{i:02d}-{e.caso_id}.md"
        archivo.write_text(
            f"# {i:02d} · {e.caso_id}\n\n- Escenario: {c.escenario} · etiquetas: {', '.join(sorted(c.etiquetas))}\n"
            f"- Lo que el cliente quería: {c.objetivo_oculto}\n\n"
            f"## Datos autorizados del negocio\n\n```json\n{json.dumps({'catalogo': [x.model_dump(mode='json') for x in spec.datos.catalogo], 'faq': [x.model_dump(mode='json') for x in spec.datos.faq], 'reglas': [x.model_dump(mode='json') for x in spec.rubro.reglas]}, ensure_ascii=False, indent=2)}\n```\n\n"
            f"## Conversación\n\n```\n{render(t, con_tools=True)}\n```\n"
        )
        filas.append({"n": i, "caso_id": e.caso_id, "version": e.version, "repeticion": e.repeticion, "archivo": archivo.name, "humano_paso": "", "humano_critica": "", "notas": ""})
    ruta = dir / "calibracion.csv"
    with ruta.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(filas)
    return ruta


def _si(valor: str) -> bool | None:
    v = valor.strip().lower()
    if v in ("si", "sí", "s", "1", "true", "pasa", "x"):
        return True
    if v in ("no", "n", "0", "false", "falla"):
        return False
    return None


def importar(store: Store, ruta: Path) -> dict:
    acuerdos = total = acuerdos_critica = total_critica = 0
    desacuerdos = []
    vistos = set()
    confusion = {"paso": {"vp": 0, "vn": 0, "fp": 0, "fn": 0}, "critica": {"vp": 0, "vn": 0, "fp": 0, "fn": 0}}

    def contar(tipo: str, humano: bool, automatico: bool) -> None:
        clave = "vp" if humano and automatico else "vn" if not humano and not automatico else "fp" if automatico else "fn"
        confusion[tipo][clave] += 1

    with ruta.open() as f:
        for fila in csv.DictReader(f):
            humano = _si(fila["humano_paso"])
            if humano is None:
                continue
            clave = (int(fila["version"]), fila["caso_id"], int(fila["repeticion"]))
            if clave in vistos:
                raise ValueError(f"Evaluación repetida en el CSV: {clave}")
            vistos.add(clave)
            e = store.evaluacion(*clave)
            if e is None:
                raise ValueError(f"La evaluación del CSV no existe: {clave}")
            total += 1
            acuerdos += humano == e.paso
            contar("paso", humano, e.paso)
            if humano != e.paso:
                desacuerdos.append({"caso_id": e.caso_id, "humano": humano, "juez": e.paso, "notas": fila.get("notas", "")})
            critica = _si(fila["humano_critica"])
            if critica is not None:
                total_critica += 1
                acuerdos_critica += critica == e.critica
                contar("critica", critica, e.critica)
    if not total:
        raise SystemExit("El CSV no tiene ninguna fila calificada en humano_paso (si/no).")
    resultado = {
        "n": total,
        "concordancia": acuerdos / total,
        "concordancia_critica": acuerdos_critica / total_critica if total_critica else None,
        "n_critica": total_critica,
        "matriz_confusion": confusion,
        "validada": total >= 20 and total_critica == total and acuerdos / total >= 0.85 and acuerdos_critica / total_critica >= 0.85,
        "desacuerdos": desacuerdos,
    }
    (store.dir / "calibracion.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=2))
    return resultado
