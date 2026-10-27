"""La boleta de una corrida: nota 0-100 antes y después, desglose por criterio, las 3 peores fallas con la frase del bot,
veredicto y el parche de prompt. Todo sale del Store de la corrida; no llama a ningún modelo.
"""

import difflib
import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Literal

import yaml

from pydantic import BaseModel

from fantasma.motor.agente import prompt_con_reglas
from fantasma.motor.esquemas import Caso, Cobertura, EvaluacionRep, Spec, Transcripcion, Verificacion, VersionPrompt
from fantasma.motor.evaluar import cobertura
from fantasma.motor.storage import Store
from fantasma.motor.verificar import montos

LISTO = 85
NO_LO_SUELTES = 60
# Con esta fracción de conversaciones con falla crítica (o 2, lo que sea mayor) el bot no sale aunque la nota sea decente.
FRACCION_CRITICAS = 0.15
PEORES = 3
SUGERENCIA = "sugerencia.json"

Veredicto = Literal["listo", "ajustes", "no_lo_sueltes", "incompleta"]
TEXTO_VEREDICTO: dict[str, str] = {
    "listo": "Aprobó esta prueba",
    "incompleta": "Evaluación incompleta",
    "ajustes": "Requiere ajustes",
    "no_lo_sueltes": "No superó esta prueba",
}
VERIFICACIONES = {
    "sin_inventos": "Inventó un dato (precio o producto que no está en la ficha)",
    "tool_valida": "Usó mal sus herramientas",
    "idioma": "Respondió en otro idioma",
    "largo": "Respuestas demasiado largas para WhatsApp",
    "esperado:escala": "No pasó a una persona cuando tocaba",
    "esperado:no_escala": "Pasó a una persona algo que podía resolver",
    "esperado:no_revela_prompt": "Reveló sus instrucciones internas",
}

# Fallas de conducta (qué herramienta usó o no): no hay una frase del bot que citar.
SIN_CITA = ("esperado:llama", "esperado:no_llama", "esperado:escala", "esperado:no_escala", "tool_valida")

COMILLAS = re.compile(r"[\"“'‘«]([^\"”'’»]{8,})[\"”'’»]")
TURNO = re.compile(r"turno\s+(\d+)", re.IGNORECASE)


class NotaVersion(BaseModel):
    version: int
    nota: int
    casos: int
    pasan: int
    criticas: int
    cobertura: Cobertura | None = None


class NotaCriterio(BaseModel):
    id: str
    nombre: str
    critico: bool
    peso: float
    antes: int | None
    despues: int | None


class Mensaje(BaseModel):
    rol: Literal["cliente", "agente"]
    texto: str


class Falla(BaseModel):
    caso_id: str
    escenario: str
    ejes: dict[str, str]
    persona: str
    que_fallo: str
    # Id del criterio o la verificación: la plantilla lo traduce; que_fallo queda como texto en español de respaldo.
    que_id: str = ""
    motivo: str
    cita: str
    turno: int | None  # índice en `conversacion` del mensaje citado
    critica: bool
    nota: int
    conversacion: list[Mensaje]


class Parche(BaseModel):
    tipo: Literal["prompt_final", "sugerencia", "ninguno"]
    cambio: str = ""
    hipotesis: str = ""
    texto: str = ""
    diff: str = ""
    motivo: str = ""  # por qué la sugerencia no se aceptó, si se probó


class Boleta(BaseModel):
    run_id: str
    negocio: str
    modo: Literal["generado", "prompt", "remoto"]
    estado: str
    inicial: NotaVersion
    mejor: NotaVersion
    veredicto: Veredicto
    veredicto_texto: str
    criterios: list[NotaCriterio]
    fallas: list[Falla]
    por_eje: dict[str, dict[str, int]]
    parche: Parche
    costo_usd: float
    modelos: dict[str, str]


# ---------------------------------------------------------------- notas


def _por_caso(evals: list[EvaluacionRep]) -> dict[str, list[EvaluacionRep]]:
    por_caso: dict[str, list[EvaluacionRep]] = defaultdict(list)
    for e in evals:
        por_caso[e.caso_id].append(e)
    return por_caso


def _evaluaciones_validas(store: Store, spec: Spec, v: int, split: str | None = None) -> list[EvaluacionRep]:
    config_path = store.dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) or {} if config_path.exists() else {}
    reps = int(config.get("repetitions_per_case", 1))
    casos = {c.id for c in spec.casos if split is None or c.split == split}
    return [e for e in store.evaluaciones(v) if e.caso_id in casos and 0 <= e.repeticion < reps]


def nota_version(store: Store, v: int, split: str | None = None) -> NotaVersion:
    """Promedio por caso y luego entre casos, sobre train y holdout o solo sobre `split`."""
    spec = store.cargar_spec()
    config_path = store.dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) or {} if config_path.exists() else {}
    reps = int(config.get("repetitions_per_case", 1))
    casos = [c for c in spec.casos if split is None or c.split == split]
    solicitados = len(casos) if split else int(config.get("casos", {}).get("cantidad", len(spec.casos)))
    # Las repeticiones adicionales para medir ruido no forman parte de la nota de validación.
    evals = _evaluaciones_validas(store, spec, v, split)
    por_caso = _por_caso(evals)
    return NotaVersion(
        version=v,
        nota=round(100 * mean(mean(e.puntaje for e in es) for es in por_caso.values())) if por_caso else 0,
        casos=len(por_caso),
        pasan=sum(all(e.paso for e in es) for es in por_caso.values()),
        criticas=sum(any(e.critica for e in es) for es in por_caso.values()),
        cobertura=cobertura(casos, evals, reps, solicitados),
    )


def veredicto(n: NotaVersion) -> Veredicto:
    if n.cobertura is not None and not n.cobertura.completa:
        return "incompleta"
    if n.nota < NO_LO_SUELTES or n.criticas >= max(2, round(n.casos * FRACCION_CRITICAS)):
        return "no_lo_sueltes"
    if n.nota >= LISTO and n.criticas == 0:
        return "listo"
    return "ajustes"


def _criterios(store: Store, spec: Spec, antes: int, despues: int, split: str | None = None) -> list[NotaCriterio]:
    def medias(v: int) -> dict[str, int]:
        puntos: dict[str, list[int]] = defaultdict(list)
        for e in _evaluaciones_validas(store, spec, v, split):
            for p in e.juez.criterios if e.juez else []:
                puntos[p.criterio].append(p.puntaje)
        return {k: round(100 * (mean(x) - 1) / 4) for k, x in puntos.items()}

    a, d = medias(antes), medias(despues)
    return [NotaCriterio(id=c.id, nombre=c.nombre, critico=c.critico, peso=round(c.peso, 4), antes=a.get(c.id), despues=d.get(c.id)) for c in spec.rubrica.criterios]


def por_eje(store: Store, spec: Spec, v: int) -> dict[str, dict[str, int]]:
    """Nota por dialecto, ataque e intención: dónde se cae el bot (p. ej. bien con mexicanos, mal con dominicanos)."""
    casos = {c.id: c for c in spec.casos}
    grupos: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for caso_id, es in _por_caso(_evaluaciones_validas(store, spec, v)).items():
        for eje, valor in casos[caso_id].ejes.items():
            grupos[eje][valor].append(mean(e.puntaje for e in es))
    return {eje: {valor: round(100 * mean(x)) for valor, x in sorted(vals.items())} for eje, vals in grupos.items()}


# ---------------------------------------------------------------- fallas


def _norm(t: str) -> str:
    return " ".join(re.findall(r"\w+", t.casefold()))


def cita(t: Transcripcion, motivo: str) -> tuple[int | None, str]:
    """El mensaje del bot al que apunta el motivo: por la frase que cita el juez, por "turno N" o por la cifra inventada."""
    agentes = [(i, x.texto) for i, x in enumerate(t.turnos) if x.rol == "agente" and x.texto.strip()]
    if not agentes:
        return None, ""
    for frag in COMILLAS.findall(motivo):
        for i, texto in agentes:
            if _norm(frag) and _norm(frag) in _norm(texto):
                return i, texto
    if m := TURNO.search(motivo):
        n = int(m.group(1))
        # El juez numera a veces todos los mensajes y a veces solo los del agente.
        if 1 <= n <= len(t.turnos) and t.turnos[n - 1].rol == "agente" and t.turnos[n - 1].texto.strip():
            return n - 1, t.turnos[n - 1].texto
        if 1 <= n <= len(agentes):
            return agentes[n - 1]
    inventados = {Decimal(x) for x in re.findall(r"monto (\d+(?:\.\d+)?)", motivo)}
    ids = set(re.findall(r"\b[A-Z]{3,4}-\d{3}\b", motivo))
    for i, texto in agentes:
        if inventados & set(montos(texto)) or any(x in texto for x in ids):
            return i, texto
    return agentes[-1]


HERRAMIENTAS = {
    "catalogo.buscar": "consultar el catálogo",
    "catalogo.detalle": "consultar el detalle del producto",
    "pedidos.cotizar": "cotizar el pedido",
    "pedidos.crear_pedido": "crear el pedido",
    "pedidos.cancelar": "cancelar el pedido",
    "pagos.enlace_pago": "generar el enlace de pago",
    "calendario.disponibilidad": "consultar la disponibilidad",
    "calendario.agendar": "agendar la cita",
    "crm.capturar_lead": "guardar los datos del cliente",
    "crm.actualizar_lead": "actualizar los datos del cliente",
    "crm.consultar_cliente": "consultar al cliente",
    "faq.buscar_info": "consultar las preguntas frecuentes",
}


def motivo_legible(motivo: str) -> str:
    """'monto 149, monto 110, COLM-099' (lo que deja la verificación) → frase para el dueño del negocio."""
    cifras = re.findall(r"monto (\d+(?:\.\d+)?)", motivo)
    if not cifras:
        return "No la llamó." if motivo.strip() == "no se llamó" else motivo
    resto = [x.strip() for x in re.sub(r"monto \d+(?:\.\d+)?", "", motivo).split(",") if x.strip()]
    return "Dio cifras que no están en la ficha: " + ", ".join(cifras + resto) + "."


def nombre_verificacion(id: str) -> str:
    """'esperado:llama:pedidos.crear_pedido' → texto que entiende el dueño del negocio."""
    if id in VERIFICACIONES:
        return VERIFICACIONES[id]
    tipo, _, herramienta = id.removeprefix("esperado:").partition(":")
    match tipo:
        case "llama":
            return f"No usó la herramienta para {HERRAMIENTAS.get(herramienta, herramienta)} cuando tocaba"
        case "no_llama":
            return f"Usó la herramienta para {HERRAMIENTAS.get(herramienta, herramienta)} cuando no tocaba"
        case "idioma":
            return VERIFICACIONES["idioma"]
    return id


def _que_fallo(e: EvaluacionRep, spec: Spec) -> tuple[str, str, bool, str]:
    """(qué falló, por qué, si hay frase que citar, id de lo que falló). Orden: verificación crítica, violación crítica del juez, cualquier
    verificación fallida (un caso puede no pasar solo por una expectativa aunque el juez dé 5) y el peor criterio del juez."""
    nombres = {c.id: c.nombre for c in spec.rubrica.criterios}
    ids_criticos = {c.id for c in spec.rubrica.criterios if c.critico}
    fallidas = [v for v in e.verificaciones if not v.ok]

    def por_verificacion(v: Verificacion) -> tuple[str, str, bool, str]:
        return nombre_verificacion(v.id), v.detalle, not v.id.startswith(SIN_CITA), v.id

    if criticas := [v for v in fallidas if v.critica]:
        return por_verificacion(criticas[0])
    if e.juez and e.juez.critical_violation:
        criticos = [p for p in e.juez.criterios if p.criterio in ids_criticos]
        peor = min(criticos, key=lambda p: p.puntaje) if criticos else None
        return (nombres[peor.criterio] if peor else "Violación crítica"), e.juez.critical_motivo or (peor.justificacion if peor else ""), True, peor.criterio if peor else "violacion_critica"
    peor = min(e.juez.criterios, key=lambda p: p.puntaje) if e.juez else None
    if fallidas and (peor is None or peor.puntaje > 2):
        return por_verificacion(fallidas[0])
    return nombres.get(peor.criterio, peor.criterio), peor.justificacion, True, peor.criterio


def peores_fallas(store: Store, spec: Spec, v: int, n: int = PEORES) -> list[Falla]:
    casos: dict[str, Caso] = {c.id: c for c in spec.casos}
    malas = [e for e in _evaluaciones_validas(store, spec, v) if not e.paso]
    # Críticas primero; entre iguales, la de menor nota. Una por caso: tres fallas del mismo cliente no enseñan nada nuevo.
    malas.sort(key=lambda e: (not e.critica, e.puntaje, e.caso_id, e.repeticion))
    elegidas: list[tuple[EvaluacionRep, Transcripcion, str, str, int | None, str]] = []
    vistos: set[str] = set()
    for e in malas:
        if e.caso_id in vistos or e.caso_id not in casos:
            continue
        t = store.transcripcion(v, e.caso_id, e.repeticion)
        # Sin respuesta del bot no hay nada que mostrarle al dueño.
        if t is None or not any(x.rol == "agente" and x.texto.strip() for x in t.turnos):
            continue
        vistos.add(e.caso_id)
        que, motivo, citable, que_id = _que_fallo(e, spec)
        turno, frase = cita(t, motivo) if citable else (None, "")
        elegidas.append((e, t, que, que_id, motivo, turno, frase))
    # Las que traen la frase del bot van primero: es lo que convence. sort es estable y conserva el orden de gravedad.
    elegidas.sort(key=lambda x: not x[6])
    fallas: list[Falla] = []
    for e, t, que, que_id, motivo, turno, frase in elegidas[:n]:
        caso = casos[e.caso_id]
        fallas.append(
            Falla(
                caso_id=e.caso_id,
                escenario=caso.escenario,
                ejes=caso.ejes,
                persona=caso.persona.nombre,
                que_fallo=que,
                que_id=que_id,
                motivo=motivo_legible(motivo),
                cita=frase,
                turno=turno,
                critica=e.critica,
                nota=round(100 * e.puntaje),
                conversacion=[Mensaje(rol=x.rol, texto=x.texto) for x in t.turnos],
            )
        )
    return fallas


# ---------------------------------------------------------------- parche


def diff(antes: str, despues: str) -> str:
    return "".join(difflib.unified_diff(antes.splitlines(keepends=True), despues.splitlines(keepends=True), "prompt_antes.md", "prompt_despues.md"))


def lineas_nuevas(antes: str, despues: str) -> str:
    """Solo lo que la propuesta agrega o reescribe: para un bot cuyo prompt real no conocemos."""
    return "".join(x[2:] for x in difflib.ndiff(antes.splitlines(keepends=True), despues.splitlines(keepends=True)) if x.startswith("+ ")).strip()


def mejor_version(versiones: list[VersionPrompt]) -> int:
    """La mejor aceptada que supera a v000 en holdout; un empate no demuestra mejora."""
    base = next((v for v in versiones if v.n == 0 and v.holdout is not None), None)
    if base is None:
        return 0
    candidatas = [v for v in versiones if v.estado == "aceptada" and v.holdout is not None and v.holdout > base.holdout]
    return max(candidatas, key=lambda v: (v.holdout, v.train or 0.0, v.n)).n if candidatas else 0


def parche(store: Store, modo: str, mejor: int) -> Parche:
    if modo == "remoto":
        ruta = store.dir / SUGERENCIA
        if not ruta.exists():
            return Parche(tipo="ninguno")
        return Parche(tipo="sugerencia", **json.loads(ruta.read_text()))
    rubro = store.cargar_spec().rubro
    base = prompt_con_reglas(store.texto(0), rubro)
    if mejor == 0:
        # Sin versión aceptada (en la ligera hay una sola ronda), la última propuesta queda como sugerencia sin validar.
        propuestas = [v for v in store.versiones() if v.n > 0 and (store.dir / "prompts" / f"v{v.n:03d}.md").exists()]
        if not propuestas:
            return Parche(tipo="ninguno", texto=base)
        v = propuestas[-1]
        texto = prompt_con_reglas(store.texto(v.n), rubro)
        # `motivo` sale como "no se aceptó": el de una aceptada que se cortó antes del holdout diría "mejora +0.150".
        # Para esa basta la pista de sugerencia sin validar.
        motivo = v.motivo if v.estado in ("rechazada", "revertida") else ""
        return Parche(tipo="sugerencia", cambio=v.cambio or "", hipotesis=v.hipotesis or "", texto=texto, diff=diff(base, texto), motivo=motivo)
    v = store.version(mejor)
    texto = prompt_con_reglas(store.texto(mejor), rubro)
    return Parche(tipo="prompt_final", cambio=v.cambio or "", hipotesis=v.hipotesis or "", texto=texto, diff=diff(base, texto))


# ---------------------------------------------------------------- armado y exportación


def armar(store: Store, *, run_id: str, modo: str, estado: str, costo_usd: float, modelos: dict[str, str]) -> Boleta:
    spec = store.cargar_spec()
    if not store.evaluaciones(0):
        raise ValueError(f"La corrida {run_id} no tiene conversaciones evaluadas todavía")
    # Una corrida cortada (tope, fallo al proponer) no valida ninguna versión nueva, pero si v000 quedó medida entera su
    # veredicto vale: el dueño ya pagó esa medición.
    v_mejor = 0 if modo == "remoto" or estado != "terminada" else mejor_version(store.versiones())
    # Con una versión mejorada, antes y después se comparan solo en holdout: el train sirvió para elegirla.
    split = "holdout" if v_mejor else None
    inicial, mejor = nota_version(store, 0, split), nota_version(store, v_mejor, split)
    clave = veredicto(mejor)
    if estado in ("presupuesto", "error") and clave == "listo":
        clave = "ajustes"  # Una corrida cortada nunca aprueba al bot, aunque v000 saliera perfecta.
    elif estado not in ("terminada", "presupuesto", "error"):
        clave = "incompleta"
    return Boleta(
        run_id=run_id,
        negocio=spec.rubro.negocio,
        modo=modo,
        estado="incompleta" if clave == "incompleta" else estado,
        inicial=inicial,
        mejor=mejor,
        veredicto=clave,
        veredicto_texto=TEXTO_VEREDICTO[clave],
        criterios=_criterios(store, spec, 0, v_mejor, split),
        # Las fallas del bot tal como llegó: es lo que el dueño necesita ver antes del parche.
        fallas=peores_fallas(store, spec, 0),
        por_eje=por_eje(store, spec, 0),
        parche=parche(store, modo, v_mejor),
        costo_usd=round(costo_usd, 4),
        modelos=modelos,
    )


def lineas_jsonl(store: Store) -> list[str]:
    """Una línea por conversación evaluada: transcripción, ejes del fantasma y lo que dijeron verificaciones y juez."""
    casos = {c.id: c for c in store.cargar_spec().casos}
    lineas = []
    for v in store.versiones():
        for e in sorted(store.evaluaciones(v.n), key=lambda e: (e.caso_id, e.repeticion)):
            t = store.transcripcion(v.n, e.caso_id, e.repeticion)
            caso = casos.get(e.caso_id)
            lineas.append(
                json.dumps(
                    {
                        "version": v.n,
                        "caso_id": e.caso_id,
                        "repeticion": e.repeticion,
                        "escenario": caso.escenario if caso else "",
                        "ejes": caso.ejes if caso else {},
                        "persona": caso.persona.model_dump() if caso else None,
                        "modelo_agente": t.modelo_agente if t else "",
                        "fin": t.fin if t else None,
                        "turnos": [{"rol": x.rol, "texto": x.texto, "tools": [ll.model_dump(mode="json") for ll in x.tools]} for x in t.turnos] if t else [],
                        "puntaje": e.puntaje,
                        "paso": e.paso,
                        "critica": e.critica,
                        "verificaciones": [x.model_dump() for x in e.verificaciones],
                        "juez": e.juez.model_dump() if e.juez else None,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
    return lineas


def exportar_jsonl(store: Store, ruta: Path) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("\n".join(lineas_jsonl(store)) + "\n")
    return ruta
