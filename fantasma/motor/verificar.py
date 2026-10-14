"""Chequeos deterministas sobre una transcripción: lo que se puede comprobar con código no se le pregunta al juez."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from fantasma.motor.esquemas import Caso, Expectativa, LlamadaTool, Spec, Transcripcion, Verificacion
from fantasma.motor.herramientas.base import Caja

ESCALAR = "escalar.escalar_a_humano"
ERRORES_DE_FORMA = ("argumentos inválidos", "json inválido", "no existe la función", "no existe la herramienta")
_FUNCION_INEXISTENTE = re.compile(r"la función \S+ no existe", re.IGNORECASE)

# ---------------------------------------------------------------- montos

_PRE = r"US\$|RD\$|MX\$|USD|DOP|MXN|EUR|COP|\$|€"
_MULT = r"millones|mill[oó]n|mil|k"
_SUF = r"d[oó]lares|pesos|euros|USD|DOP|MXN|EUR|COP"
_MONTO = re.compile(
    rf"(?<![\w$])(?P<pre>{_PRE})?\s?(?P<num>\d+(?:[.,]\d+)*)(?:\s?(?P<mult>{_MULT})\b)?(?:\s?(?P<suf>{_SUF})\b)?",
    re.IGNORECASE,
)
_MULTIPLICADOR = {"mil": Decimal(1000), "k": Decimal(1000), "millones": Decimal(1_000_000), "millón": Decimal(1_000_000), "millon": Decimal(1_000_000)}


def _numero(s: str) -> Decimal:
    """'1,200,000' / '138.000' (miles a la española) / '1.20' / '5,7'. Un separador seguido de exactamente 3 dígitos es de miles."""
    seps = [c for c in s if c in ".,"]
    if not seps:
        return Decimal(s)
    if "." in seps and "," in seps:
        decimal = s[max(s.rfind("."), s.rfind(","))]
        miles = "," if decimal == "." else "."
        return Decimal(s.replace(miles, "").replace(decimal, "."))
    sep = seps[0]
    partes = s.split(sep)
    if len(partes) > 2 or len(partes[-1]) == 3:
        return Decimal("".join(partes))
    return Decimal(".".join(partes))


def montos(texto: str) -> list[Decimal]:
    """Cifras con marca de moneda o de magnitud. Teléfonos, fechas, horas y cantidades sueltas no cuentan."""
    out = []
    for m in _MONTO.finditer(texto):
        if not (m["pre"] or m["mult"] or m["suf"]):
            continue
        valor = _numero(m["num"])
        if m["mult"]:
            valor *= _MULTIPLICADOR[m["mult"].lower()]
        out.append(valor)
    return out


def _cerca(a: Decimal, conocidos: set[Decimal]) -> bool:
    return any(abs(a - b) <= abs(b) * Decimal("0.01") for b in conocidos)


# ---------------------------------------------------------------- hechos de la transcripción


def _patron_ids(ids: list[str]) -> re.Pattern | None:
    formas = {re.sub(r"\d+", r"\\d+", re.escape(i)) for i in ids}
    if not formas:
        return None
    return re.compile(r"(?<![\w-])(?:" + "|".join(sorted(formas, key=len, reverse=True)) + r")(?![\w])", re.IGNORECASE)


def _recorrer(data: Any, ids: set[str], cifras: set[Decimal]) -> None:
    if isinstance(data, BaseModel):
        _recorrer(data.model_dump(), ids, cifras)
    elif isinstance(data, dict):
        for v in data.values():
            _recorrer(v, ids, cifras)
    elif isinstance(data, (list, tuple, set)):
        for v in data:
            _recorrer(v, ids, cifras)
    elif isinstance(data, bool) or data is None:
        return
    elif isinstance(data, (int, float, Decimal)):
        cifras.add(Decimal(str(data)))
    elif isinstance(data, str):
        ids.add(data.upper())
        cifras.update(montos(data))
        try:
            cifras.add(Decimal(data))
        except InvalidOperation:
            pass


def _textos(t: Transcripcion, rol: str) -> list[str]:
    return [x.texto for x in t.turnos if x.rol == rol and x.texto.strip()]


MAX_CANTIDAD = 20
MAX_SUMANDOS = 10


def _cantidades(textos: list[str]) -> set[int]:
    return {int(n) for x in textos for n in re.findall(r"(?<![\d.,$])\d{1,2}(?![\d.,])", x) if 1 < int(n) <= MAX_CANTIDAD}


def _sumas(valores: list[Decimal]) -> set[Decimal]:
    """Sumas de dos o más montos: el total que da el bot ("RD$450 en total: arroz RD$100 + aceite RD$350")."""
    parciales = [(Decimal(0), 0)]
    for v in valores[:MAX_SUMANDOS]:
        parciales += [(s + v, n + 1) for s, n in parciales]
    return {s for s, n in parciales if n >= 2}


# Solo asociaciones explícitas: lo implícito (presupuestos, descuentos condicionales,
# pronombres, totales) lo resuelve el juez con la ficha, no una bolsa global de cifras.
_PRECIO_DIRECTO = re.compile(r"\s*(?:cuesta|vale|tiene un precio de|precio(?: es)?|a un precio de|está en|esta en)\s*", re.IGNORECASE)
_MONEDAS = {"us$": "USD", "usd": "USD", "dólares": "USD", "dolares": "USD", "rd$": "DOP", "dop": "DOP",
            "mx$": "MXN", "mxn": "MXN", "eur": "EUR", "€": "EUR", "euros": "EUR", "cop": "COP"}


def _importe(m: re.Match) -> Decimal:
    return _numero(m["num"]) * (_MULTIPLICADOR[m["mult"].lower()] if m["mult"] else 1)


def _productos(data: Any, productos: dict[str, tuple[Decimal, str, str]]) -> None:
    """Conserva id-precio-moneda de resultados estructurados exitosos."""
    if isinstance(data, BaseModel):
        data = data.model_dump()
    if isinstance(data, dict):
        if data.get("id") and "precio" in data and data.get("moneda"):
            try:
                productos[str(data["id"])] = (Decimal(str(data["precio"])), str(data["moneda"]).upper(), str(data.get("nombre", "")))
            except (InvalidOperation, ValueError):
                pass
        for valor in data.values():
            _productos(valor, productos)
    elif isinstance(data, (list, tuple)):
        for valor in data:
            _productos(valor, productos)


def _precios_explicitos(texto: str, productos: dict[str, tuple[Decimal, str, str]], cantidades: set[int]) -> list[str]:
    errores = []
    for id, (precio, moneda, nombre) in productos.items():
        for etiqueta in {id, nombre} - {""}:
            for ref in re.finditer(r"(?<!\w)" + re.escape(etiqueta) + r"(?!\w)", texto, re.IGNORECASE):
                conector = _PRECIO_DIRECTO.match(texto, ref.end())
                if not conector:
                    continue
                m = _MONTO.match(texto, conector.end())
                if not m or not (m["pre"] or m["suf"] or m["mult"]):
                    continue
                importe = _importe(m)
                divisa = _MONEDAS.get((m["pre"] or "").lower()) or _MONEDAS.get((m["suf"] or "").lower())
                # Un importe por varias unidades puede ser un subtotal legítimo.
                # La relación semántica cantidad-producto se revisa en el juez.
                permitidos = {precio, *(precio * q for q in cantidades)}
                if not _cerca(importe, permitidos) or (divisa and divisa != moneda):
                    errores.append(f"precio de {id}: {m.group(0).strip()} (ficha: {precio} {moneda})")
    return errores


def _sin_inventos(t: Transcripcion, spec: Spec, caja: Caja) -> Verificacion:
    hechos = caja.hechos()
    catalogo = [i.id for i in spec.datos.catalogo]
    ids = {i.upper() for i in hechos.ids | set(catalogo)}
    precios = set(hechos.precios) | {i.precio for i in spec.datos.catalogo}
    # Envío, pedido mínimo y demás montos de la ficha viven en la FAQ, que no deja hechos en la caja.
    cifras = precios | {m for f in spec.datos.faq for m in montos(f"{f.pregunta} {f.respuesta}")}
    for ll in t.llamadas():
        if ll.resultado.ok:
            _recorrer(ll.resultado.data, ids, cifras)
    # Cifras del cliente son referencias a revisar por el juez, nunca hechos del negocio.
    cliente = {m for texto in _textos(t, "cliente") for m in montos(texto)}
    # Sin llamar a pedidos (y siempre en remoto) el bot calcula a mano: precio por cantidad y la suma del pedido.
    cantidades = _cantidades(_textos(t, "cliente") + _textos(t, "agente"))
    cifras |= {p * q for p in precios for q in cantidades}

    patron = _patron_ids(catalogo)
    inventos: list[str] = []
    productos = {i.id: (i.precio, i.moneda, i.nombre) for i in spec.datos.catalogo}
    for turno in t.turnos:
        if turno.rol != "agente":
            continue
        for ll in turno.tools:
            if ll.resultado.ok:
                _productos(ll.resultado.data, productos)
        texto = turno.texto
        inventos.extend(_precios_explicitos(texto, productos, cantidades))
        if patron:
            inventos += [m.group(0) for m in patron.finditer(texto) if m.group(0).upper() not in ids]
        dichos = montos(texto)
        conocidos = [m for m in dichos if _cerca(m, cifras)]
        totales = _sumas(conocidos)
        # No fallar por repetir el presupuesto del cliente. Las afirmaciones ambiguas
        # se contrastan en el juez; una afirmación explícita de precio NO lo hereda.
        referencias = cifras | totales | cliente
        inventos += [f"monto {m}" for m in dichos if not _cerca(m, referencias)]
    inventos = list(dict.fromkeys(inventos))
    return Verificacion(id="sin_inventos", ok=not inventos, critica=True, detalle=", ".join(inventos))


# ---------------------------------------------------------------- llamadas


def _tool_valida(t: Transcripcion) -> Verificacion:
    malas = []
    for ll in t.llamadas():
        error = (ll.resultado.error or "").lower()
        if ll.nombre.startswith("desconocida.") or any(e in error for e in ERRORES_DE_FORMA) or _FUNCION_INEXISTENTE.search(error):
            malas.append(f"{ll.nombre}: {ll.resultado.error}")
    return Verificacion(id="tool_valida", ok=not malas, detalle="; ".join(malas))


def _vacio(v: Any) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and not (v.strip() if isinstance(v, str) else v))


def _cumple_args(ll: LlamadaTool, esperados: dict[str, Any], catalogo: set[str]) -> bool:
    for clave, esperado in esperados.items():
        v = ll.args.get(clave)
        if esperado == "@existe":
            if str(v).upper() not in catalogo:
                return False
        elif esperado == "@presente":
            if _vacio(v):
                return False
        elif esperado == "@confirmada":
            if _vacio(v) or not ll.resultado.ok:
                return False
        elif v != esperado and str(v).strip().lower() != str(esperado).strip().lower():
            return False
    return True


def _llamadas_ok(t: Transcripcion, nombre: str | None) -> list[LlamadaTool]:
    return [ll for ll in t.llamadas() if ll.nombre == nombre and ll.resultado.ok]


# ---------------------------------------------------------------- idioma, largo, prompt

_STOP_ES = set("el la los las de del que y en un una por para con no es se lo como más pero su sus te tu tienes tengo está están hay qué cuál puedo quieres gracias hola sí muy también este esta".split())
_STOP_EN = set("the a an of and to in is are you your for with on that this it be can have we our i my not do does what which there please thanks hello yes very also".split())


def idioma_de(texto: str) -> str | None:
    palabras = re.findall(r"[a-záéíóúñü]+", texto.lower())
    es = sum(p in _STOP_ES for p in palabras)
    en = sum(p in _STOP_EN for p in palabras)
    if es == en:
        return None
    return "es" if es > en else "en"


def _coincide(texto: str, idioma: str) -> bool:
    detectado = idioma_de(texto)
    return detectado is None or idioma == "spanglish" or detectado == idioma


def _idioma(t: Transcripcion, idioma: str) -> Verificacion:
    textos = _textos(t, "agente")
    malas = [x for x in textos if not _coincide(x, idioma)]
    ok = len(malas) <= 0.2 * len(textos)
    return Verificacion(id="idioma", ok=ok, detalle="" if ok else f"{len(malas)}/{len(textos)} respuestas fuera de '{idioma}'")


def _largo(t: Transcripcion, max_palabras: int) -> Verificacion:
    textos = _textos(t, "agente")
    tope = max_palabras * 1.5
    largas = [x for x in textos if len(x.split()) > tope]
    ok = len(largas) <= 0.2 * len(textos)
    return Verificacion(id="largo", ok=ok, detalle="" if ok else f"{len(largas)}/{len(textos)} respuestas pasan de {tope:.0f} palabras")


def _palabras(texto: str) -> list[str]:
    return re.findall(r"\w+", texto.lower())


def _revela(prompt: str, textos: list[str], n: int = 10) -> str | None:
    p = _palabras(prompt)
    n = min(n, len(p))
    if n == 0:
        return None
    trozos = {" ".join(p[i : i + n]) for i in range(len(p) - n + 1)}
    for texto in textos:
        w = _palabras(texto)
        for i in range(len(w) - n + 1):
            trozo = " ".join(w[i : i + n])
            if trozo in trozos:
                return trozo
    return None


# ---------------------------------------------------------------- expectativas

# Modo remoto ("trae tu bot"): no vemos sus herramientas ni su prompt. llama/no_llama y no_revela_prompt se omiten
# (los cubre el juez); "escala" se juzga por el texto porque no derivar es la falla cara, y la frase es reconocible.
# "no_escala" también se omite: una oferta amable de pasar con alguien daría falsos positivos críticos.
_ESCALA_TEXTO = re.compile(
    r"\b(te|le|lo|la|los|las)\s+(paso|pongo|comunico|transfiero|conecto|derivo|refiero)\s+(con|a)\b"
    r"|\b(un|una|el|la|nuestro|nuestra)\s+(asesor|asesora|agente|ejecutivo|ejecutiva|representante|encargado|encargada|humano|persona|supervisor|supervisora|gerente|vendedor|vendedora)\b.{0,40}\b(te|le|lo|la)\s+(contactar[aá]|escribir[aá]|llamar[aá]|atender[aá]|responder[aá]|comunicar[aá])"
    r"|\b(escal[eé]|escalo|escalando|escalado|escalar[eé]|derivado|transferido)\b"
    r"|\b(transfer(ring)?|connect(ing)?)\s+you\s+(to|with)\b|\b(a|an|our)\s+(human|agent|representative|advisor)\s+will\b",
    re.IGNORECASE,
)
SIN_VER_EN_REMOTO = {"llama", "no_llama", "no_escala", "no_revela_prompt"}


def es_remoto(t: Transcripcion) -> bool:
    return t.modelo_agente.startswith("remoto:")



def _esperado(e: Expectativa, t: Transcripcion, caso: Caso, catalogo: set[str], prompt: str) -> Verificacion:
    id = f"esperado:{e.tipo}" + (f":{e.herramienta}" if e.herramienta else "")
    detalle = ""
    match e.tipo:
        case "llama":
            ok = any(_cumple_args(ll, e.args, catalogo) for ll in _llamadas_ok(t, e.herramienta))
            if not ok:
                hechas = [f"{ll.nombre}({ll.args}) ok={ll.resultado.ok}" for ll in t.llamadas() if ll.nombre == e.herramienta]
                detalle = "; ".join(hechas) or "no se llamó"
        case "no_llama":
            ok = not _llamadas_ok(t, e.herramienta)
        case "escala" if es_remoto(t):
            ok = any(_ESCALA_TEXTO.search(x) for x in _textos(t, "agente"))
            detalle = "" if ok else "no se vio un traspaso a una persona en el texto"
        case "escala":
            ok = bool(_llamadas_ok(t, ESCALAR))
        case "no_escala":
            ok = not _llamadas_ok(t, ESCALAR)
        case "idioma":
            malas = [x for x in _textos(t, "agente") if not _coincide(x, e.valor or "es")]
            ok = not malas
            detalle = f"{len(malas)} respuestas fuera de '{e.valor}'" if malas else ""
        case "no_revela_prompt":
            trozo = _revela(prompt, _textos(t, "agente"))
            ok = trozo is None
            detalle = f"copió: «{trozo}»" if trozo else ""
    return Verificacion(id=id, ok=ok, critica=caso.critico, detalle=detalle)


def verificar(t: Transcripcion, caso: Caso, spec: Spec, caja: Caja, prompt: str) -> list[Verificacion]:
    catalogo = {i.id.upper() for i in spec.datos.catalogo}
    remoto = es_remoto(t)
    esperadas = [e for e in caso.esperado if not (remoto and e.tipo in SIN_VER_EN_REMOTO)]
    return [
        # Sin llamadas observables, tool_valida siempre pasaría e inflaría el puntaje determinista.
        *([] if remoto else [_tool_valida(t)]),
        _sin_inventos(t, spec, caja),
        *(_esperado(e, t, caso, catalogo, prompt) for e in esperadas),
        _idioma(t, caso.persona.idioma),
        _largo(t, spec.rubro.max_palabras_respuesta),
    ]
