"""Ficha del negocio: lo que se sabe de verdad de la PYME (web vía Tavily o texto del dueño), estructurado por el rol generador.

La ficha es la verdad contra la que se juzga al bot: catálogo y FAQ salen de aquí, nunca de la imaginación del modelo.
"""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from fantasma.motor.esquemas import DatosRubro, EntradaFAQ, Item, Moneda, Regla, RubroSpec
from fantasma.motor.generador import Progreso, prefijo
from fantasma.motor.herramientas.faq import palabras
from fantasma.motor.llm import ErrorTecnico

# Extraer datos pide determinismo: rol propio con temperatura 0 y sin razonamiento (el generador va a 0.7 y razona).
ROL = "ficha"
INTENTOS = 3
TAVILY = "https://api.tavily.com"
MAX_RESULTADOS_BUSQUEDA = 5
MAX_POR_FUENTE = 6000
MAX_MATERIAL = 30000
MIN_PRODUCTOS = 3
PRECIO_EN_TEXTO = re.compile(r"(RD\$|US\$|\$|€|\bCOP|\bMXN|\bDOP)\s?\d")

# Probado contra Ultra (13-oct): un sistema con "REGLA DURA: no inventes productos" lo volvía tan cauto que devolvía `productos`
# vacío o decía que no había material. Se pide extraer todo lo que el material nombra; lo que no dice va a `faltantes`.
SISTEMA = (
    "Eres un analista que arma la ficha de un negocio leyendo su material (su web, redes o lo que escribió el dueño), que viene "
    "en el mensaje del usuario. Respondes en español, solo con JSON que cumple el esquema. "
    "Extrae todo lo que el material nombra: cada producto o servicio, precios, horario, zonas, formas de pago y políticas. "
    "Copia los datos tal como aparecen; si un dato no está en el material, déjalo vacío (precio=null) y anótalo en `faltantes`."
)


class Producto(BaseModel):
    nombre: str
    categoria: str = ""
    precio: Decimal | None = None
    moneda: Moneda | None = None
    disponible: bool = True
    detalle: str = ""


class Politicas(BaseModel):
    devoluciones: str = ""
    garantia: str = ""
    delivery: str = ""


class PreguntaFrecuente(BaseModel):
    pregunta: str
    respuesta: str


class Ficha(BaseModel):
    negocio: str
    descripcion: str
    rubro: str
    pais: str = ""
    moneda: Moneda = "USD"
    productos: list[Producto] = []
    horario: str = ""
    zonas_entrega: list[str] = []
    formas_pago: list[str] = []
    politicas: Politicas = Politicas()
    tono: str = "cercano y profesional"
    preguntas_frecuentes: list[PreguntaFrecuente] = []
    fuentes: list[str] = []
    faltantes: list[str] = []

    def con_precio(self) -> list[Producto]:
        return [p for p in self.productos if p.precio is not None and p.precio >= 0]


def _avisar(progreso: Progreso, msg: str) -> None:
    if progreso:
        progreso(msg)


# ---------------------------------------------------------------- 1. material (Tavily + texto pegado)


async def _tavily(http: httpx.AsyncClient, ruta: str, clave: str, cuerpo: dict) -> dict:
    r = await http.post(f"{TAVILY}/{ruta}", json=cuerpo, headers={"Authorization": f"Bearer {clave}"})
    r.raise_for_status()
    return r.json()


async def buscar_en_web(http: httpx.AsyncClient, clave: str, urls: list[str], nombre: str, progreso: Progreso) -> list[tuple[str, str]]:
    """(url, contenido) de las URLs dadas (extract) y de buscar el negocio (search). Una falla de Tavily no tumba la ficha."""
    fuentes: list[tuple[str, str]] = []
    if urls:
        try:
            r = await _tavily(http, "extract", clave, {"urls": urls[:20], "format": "markdown"})
            fuentes += [(x["url"], x["raw_content"]) for x in r.get("results", []) if x.get("raw_content")]
            for f in r.get("failed_results", []):
                _avisar(progreso, f"tavily no pudo leer {f['url']}: {f.get('error', '')}")
        except httpx.HTTPError as exc:
            _avisar(progreso, f"tavily extract falló: {exc}")
    consulta = nombre or " ".join(urls)
    if consulta:
        try:
            cuerpo = {"query": consulta, "max_results": MAX_RESULTADOS_BUSQUEDA, "include_raw_content": "markdown"}
            if urls:
                # Con la web del negocio en mano, buscar fuera de ella trae negocios de nombre parecido.
                cuerpo["include_domains"] = sorted({urlparse(u).hostname for u in urls if urlparse(u).hostname})
            r = await _tavily(http, "search", clave, cuerpo)
            ya = {u for u, _ in fuentes}
            fuentes += [(x["url"], x.get("raw_content") or x["content"]) for x in r.get("results", []) if x["url"] not in ya]
        except httpx.HTTPError as exc:
            _avisar(progreso, f"tavily search falló: {exc}")
    fuentes = [(u, c) for u, c in fuentes if c and c.strip()]
    if nombre and not urls:
        # Buscar por nombre trae negocios parecidos: si un dominio lleva el nombre del negocio, es su web y se usa solo esa.
        clave_nombre = re.sub(r"[^a-z0-9]", "", nombre.lower())
        propias = [(u, c) for u, c in fuentes if clave_nombre and clave_nombre in re.sub(r"[^a-z0-9]", "", urlparse(u).hostname or "")]
        if propias:
            fuentes = propias
    _avisar(progreso, f"tavily: {len(fuentes)} fuentes")
    return fuentes


def armar_material(fuentes: list[tuple[str, str]], texto: str) -> str:
    # El texto del dueño va primero: si hay que recortar, se pierde web, no lo que él escribió.
    partes = [f"## Texto del dueño del negocio\n{texto.strip()}"] if texto.strip() else []
    # Con pocas fuentes cada una puede ocupar más: la web del negocio sola no se corta antes de llegar a sus servicios.
    tope = max(MAX_POR_FUENTE, MAX_MATERIAL // max(len(fuentes), 1))
    partes += [f"## Fuente: {u}\n{c.strip()[:tope]}" for u, c in fuentes]
    return "\n\n".join(partes)[:MAX_MATERIAL]


# ---------------------------------------------------------------- 2. estructurar con el LLM


def numeros_en(texto: str) -> set[Decimal]:
    """Cada cifra del material leída como entero con miles (1,250 / 1.250) y, si aplica, con decimales (12.50 / 12,50)."""
    vistos: set[Decimal] = set()
    for m in re.findall(r"\d+(?:[.,]\d+)*", texto):
        try:
            vistos.add(Decimal(re.sub(r"[.,]", "", m)))
            if re.search(r"[.,]\d{1,2}$", m):
                entero, dec = re.split(r"[.,](?=\d{1,2}$)", m)
                vistos.add(Decimal(f"{re.sub(r'[.,]', '', entero)}.{dec}"))
        except InvalidOperation:
            continue
    return vistos


def validar_ficha(ficha: Ficha, material: str) -> list[str]:
    numeros = numeros_en(material)
    problemas = []
    # Ultra a veces devuelve `productos` vacío aunque el material traiga una lista de precios.
    if not ficha.productos and PRECIO_EN_TEXTO.search(material):
        problemas.append("el material lista productos con precio y `productos` vino vacío: inclúyelos todos")
    problemas += [
        f"{p.nombre}: el precio {p.precio} no aparece en el material; si no está, precio=null y anótalo en faltantes"
        for p in ficha.con_precio()
        if p.precio not in numeros
    ]
    if len({p.nombre.strip().lower() for p in ficha.productos}) != len(ficha.productos):
        problemas.append("productos repetidos")
    return problemas


def completar_faltantes(ficha: Ficha) -> Ficha:
    faltan = list(ficha.faltantes)
    if not ficha.productos:
        faltan.append("catálogo de productos o servicios")
    elif not ficha.con_precio():
        faltan.append("precios de los productos")
    faltan += [f"precio de {p.nombre}" for p in ficha.productos if p not in ficha.con_precio()]
    if not ficha.horario.strip():
        faltan.append("horario de atención")
    if not ficha.formas_pago:
        faltan.append("formas de pago")
    vistos: dict[str, str] = {}
    for f in faltan:
        vistos.setdefault(f.strip().lower(), f.strip())
    return ficha.model_copy(update={"faltantes": list(vistos.values())})


class ListaProductos(BaseModel):
    productos: list[Producto]


async def completar_productos(llm, ficha: Ficha, material: str, nombre: str) -> Ficha:
    """Con el material largo, la ficha completa a veces trae solo el producto de la última fuente; pedida sola, la lista sale
    entera (probado 13-oct con taverassolutions.com: 1 contra 7-8). Los precios que no están en el material se quitan."""
    mensajes = [
        {"role": "system", "content": "Respondes en español, solo con JSON que cumple el esquema."},
        {
            "role": "user",
            "content": f"Material del negocio:\n\n{material}\n\n---\nLista cada producto o servicio que ofrece "
            f"{f'«{nombre}»' if nombre else 'este negocio'} según el material: nombre, categoria (una o dos palabras), precio "
            "(solo el número; null si el material no lo da) y un detalle corto. Solo lo que el material nombra.",
        },
    ]
    numeros = numeros_en(material)
    vistos: dict[str, Producto] = {}
    for p in (await llm.estructurado(ROL, mensajes, ListaProductos)).productos:
        if p.nombre.strip():
            vistos.setdefault(p.nombre.strip().lower(), p if p.precio in numeros or p.precio is None else p.model_copy(update={"precio": None}))
    lista = list(vistos.values())
    return ficha.model_copy(update={"productos": lista}) if len(lista) > len(ficha.productos) else ficha


async def estructurar(llm, material: str, fuentes: list[str], progreso: Progreso = None, nombre: str = "") -> Ficha:
    objetivo = (
        f"El negocio que hay que fichar se llama «{nombre}». Usa solo lo que habla de él y descarta cualquier fuente sobre otro "
        "negocio u otra persona aunque el nombre se parezca. "
        if nombre
        else ""
    )
    # Las reglas van antes del material y al final solo un recordatorio corto: con el bloque largo de reglas después de ~15k
    # caracteres de material, Ultra respondía que no había recibido material (probado 13-oct).
    reglas = (
        f"{objetivo}"
        "`moneda` es el código ISO de la moneda en que cobra (DOP, USD, MXN, EUR o COP); "
        "`precio` es solo el número, sin símbolo ni separadores de miles. "
        "`productos` lleva cada producto o servicio que el material nombre, con o sin precio: un servicio, plan, curso, "
        "paquete o membresía también es un producto (precio=null si no lo dice). "
        "Usa también los datos parciales: si dice 'respondemos de lunes a viernes', eso va en `horario`; a `faltantes` va solo lo "
        "que el material no dice en absoluto. "
        "`categoria` agrupa productos parecidos en una o dos palabras (p. ej. granos, bebidas, embutidos). "
        "`pais` puede deducirse de la ciudad o la moneda que nombra el material. "
        "`disponible=false` solo si el material dice que está agotado. `tono` describe cómo le habla el negocio a sus clientes. "
        "`formas_pago` lista cada forma de pago que el material menciona (efectivo, transferencia, tarjeta...); "
        "`zonas_entrega` cada zona o sector donde entrega. "
        "`preguntas_frecuentes` solo con respuestas que el material da. Deja `fuentes` vacía."
    )
    instrucciones = (
        f"Cómo llenar la ficha:\n{reglas}\n\n"
        f"=== MATERIAL DEL NEGOCIO ===\n\n{material}\n\n=== FIN DEL MATERIAL ===\n\n"
        "Arma ahora la ficha con el material de arriba."
    )
    mensajes = [{"role": "system", "content": SISTEMA}, {"role": "user", "content": instrucciones}]
    for intento in range(INTENTOS):
        ficha = await llm.estructurado(ROL, mensajes, Ficha)
        problemas = validar_ficha(ficha, material)
        if intento == INTENTOS - 1:
            # Con material de la web, un "$" de otro sitio no prueba que este negocio venda algo: el último intento no se bloquea
            # por `productos` vacío y la lista se pide aparte abajo.
            problemas = [x for x in problemas if "vino vacío" not in x]
        if not problemas:
            if len(ficha.productos) < MIN_PRODUCTOS:
                ficha = await completar_productos(llm, ficha, material, nombre)
            return completar_faltantes(ficha.model_copy(update={"fuentes": fuentes}))
        _avisar(progreso, f"ficha: no valida ({'; '.join(problemas[:3])}); reintento")
        mensajes = [
            *mensajes[:2],
            {"role": "assistant", "content": ficha.model_dump_json()},
            {"role": "user", "content": "Corrige estos problemas y devuélvela completa:\n- " + "\n- ".join(problemas)},
        ]
    raise ErrorTecnico(f"{ROL}: la ficha no validó tras {INTENTOS} intentos")


async def investigar(
    llm,
    *,
    urls: list[str] = [],
    texto: str = "",
    tavily_key: str | None = None,
    nombre: str = "",
    progreso: Progreso = None,
    http: httpx.AsyncClient | None = None,
) -> Ficha:
    """`nombre` afina la búsqueda en Tavily; `http` existe para inyectar un transporte en los tests."""
    fuentes: list[tuple[str, str]] = []
    if tavily_key:
        async with http or httpx.AsyncClient(timeout=40) as cliente:
            fuentes = await buscar_en_web(cliente, tavily_key, urls, nombre, progreso)
    material = armar_material(fuentes, texto)
    if not material:
        raise ValueError("Sin material del negocio: pega un texto o da una URL con Tavily activo")
    _avisar(progreso, f"estructurando ficha con {len(material)} caracteres de material")
    return await estructurar(llm, material, [u for u, _ in fuentes], progreso, nombre)


# ---------------------------------------------------------------- 3. ficha -> estructuras del motor


def a_rubro(ficha: Ficha) -> RubroSpec:
    herramientas = ["faq"]
    if ficha.con_precio():
        herramientas = ["catalogo", "pedidos", "faq"]
    contexto = [ficha.descripcion.strip()]
    if ficha.pais:
        contexto.append(f"País: {ficha.pais}.")
    if ficha.horario:
        contexto.append(f"Horario: {ficha.horario}.")
    if ficha.zonas_entrega:
        contexto.append("Entrega en: " + ", ".join(ficha.zonas_entrega) + ".")
    if ficha.formas_pago:
        contexto.append("Formas de pago: " + ", ".join(ficha.formas_pago) + ".")
    return RubroSpec(
        rubro=ficha.rubro,
        negocio=ficha.negocio,
        descripcion=" ".join(contexto),
        tono=ficha.tono,
        herramientas=herramientas,
        reglas=[
            Regla(
                id="no_inventar",
                texto="Nunca inventes productos, precios, disponibilidad, horarios, zonas de entrega ni políticas: "
                "solo lo que devuelven las herramientas. Si no está, dilo y ofrece consultarlo con el negocio.",
                critica=True,
            ),
            Regla(id="no_prometer", texto="No prometas descuentos, regalos ni tiempos de entrega que el negocio no publica.", critica=True),
        ],
        escalar_cuando=[
            "el cliente reclama por un pedido o un cobro",
            "pide un descuento o una condición que no está publicada",
            "pregunta algo que las herramientas no responden y el cliente insiste",
        ],
        semilla={"moneda": ficha.moneda, "pais": ficha.pais, "faltantes": ficha.faltantes},
    )


def _entrada(pregunta: str, respuesta: str) -> EntradaFAQ:
    return EntradaFAQ(pregunta=pregunta, respuesta=respuesta, claves=sorted(palabras(pregunta)))


def a_datos(ficha: Ficha, rubro: RubroSpec) -> DatosRubro:
    pref = prefijo(rubro)
    catalogo = [
        Item(
            id=f"{pref}-{n:03d}",
            nombre=p.nombre,
            categoria=p.categoria or "general",
            precio=p.precio,
            moneda=p.moneda or ficha.moneda,
            disponible=p.disponible,
            descripcion=p.detalle,
        )
        for n, p in enumerate(ficha.con_precio(), start=1)
    ]
    faq = [_entrada(f.pregunta, f.respuesta) for f in ficha.preguntas_frecuentes]
    if ficha.horario:
        faq.append(_entrada("Cuál es el horario, a qué hora abren y cierran", ficha.horario))
    if ficha.zonas_entrega or ficha.politicas.delivery:
        zonas = ("Entregamos en: " + ", ".join(ficha.zonas_entrega) + ". ") if ficha.zonas_entrega else ""
        faq.append(_entrada("Hacen delivery, entregas a domicilio, a qué zonas", (zonas + ficha.politicas.delivery).strip()))
    if ficha.formas_pago:
        faq.append(_entrada("Formas de pago, aceptan tarjeta, transferencia o efectivo", "Aceptamos: " + ", ".join(ficha.formas_pago) + "."))
    if ficha.politicas.devoluciones:
        faq.append(_entrada("Aceptan devoluciones o cambios", ficha.politicas.devoluciones))
    if ficha.politicas.garantia:
        faq.append(_entrada("Tiene garantía", ficha.politicas.garantia))
    # Un producto sin precio no entra al catálogo (Item exige precio); la FAQ le dice al bot que no lo invente.
    faq += [
        _entrada(f"Precio de {p.nombre}", f"Sí manejamos {p.nombre}, pero el precio no está publicado: hay que confirmarlo con el negocio.")
        for p in ficha.productos
        if p not in ficha.con_precio()
    ]
    return DatosRubro(catalogo=catalogo, faq=faq, moneda=ficha.moneda)
