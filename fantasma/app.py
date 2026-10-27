"""Cliente Fantasma en el navegador: inicio → ficha → prueba en vivo (SSE) → boleta, y la boleta pública por token.

Toda la lógica de corridas vive en fantasma.servicio; aquí solo hay formularios, permisos y plantillas.
Arranque: `uv run uvicorn fantasma.app:app --host 127.0.0.1 --port <puerto>` detrás del túnel.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool
from email_validator import validate_email, EmailNotValidError

from fantasma import remoto
from fantasma.cuentas import Cuentas, CLAVES, HASH_FICTICIO, DURACION_SESION
from fantasma.costos import PrecioDesconocido, PresupuestoAgotado
from fantasma.ficha import Ficha
from fantasma.motor.config import Remoto
from fantasma.motor.esquemas import Moneda
from fantasma.motor.llm import ErrorTecnico
from fantasma.servicio import Limite, Servicio

BASE = Path(__file__).parent
CONECTOR = BASE.parent / "n8n" / "conector.json"
TEXTOS: dict[str, dict[str, Any]] = json.loads((BASE / "plantillas" / "textos.json").read_text())
IDIOMAS = tuple(TEXTOS)
MONEDAS = get_args(Moneda)

COOKIE_IDIOMA = "fantasma_idioma"
COOKIE_MIAS = "fantasma_mias"
MAX_MIAS = 20
MAX_CUERPO = 256 * 1024
MAX_TEXTO = 20000
MAX_URLS = 5
MAX_ENCABEZADOS = 5
# Cloudflare corta a los 100 s una respuesta que no manda nada.
LATIDO_S = 25
# Quien abre el enlace de la prueba un rato después todavía ve las conversaciones; más allá, la memoria se libera.
OLVIDAR_S = 3600
RUN_ID = re.compile(r"^\d{8}-[0-9a-f]{8}$")
NOMBRE_ENCABEZADO = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ESTADOS_ACTIVOS = ("investigando", "generando", "corriendo")
# Lo que el JS necesita para pintar la vista en vivo y los botones de copiar.
TEXTOS_JS = (
    "ui", "etapas", "valores", "ejes", "cliente", "bot", "usa", "pasa", "falla", "critica", "version_0", "version_n",
    "version_0_generado", "version_0_prompt", "version_0_remoto", "etapa_corrigiendo_generado", "etapa_corrigiendo_prompt",
    "vivo_conectando", "vivo_conectado", "vivo_reconectando", "fin_terminada", "fin_presupuesto", "fin_error",
    "ver_boleta", "sin_boleta", "copiado", "copiar", "esperando_ficha", "lanzando", "etapa_investigando", "etapa_generando",
    "etapa_conversando", "etapa_sugerencia", "etapa_corrigiendo", "etapa_fin",
)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class Rechazo(Exception):
    """Un dato del formulario que no sirve: se vuelve a mostrar la página con el mensaje."""


# ---------------------------------------------------------------- utilidades sin estado


def idioma_de(request: Request) -> str:
    elegido = request.cookies.get(COOKIE_IDIOMA)
    if elegido in IDIOMAS:
        return elegido
    for parte in request.headers.get("accept-language", "").split(","):
        codigo = parte.split(";")[0].strip().lower()[:2]
        if codigo in IDIOMAS:
            return codigo
    return "es"


def ip_de(request: Request) -> str:
    directa = request.client.host if request.client else "desconocida"
    if proxy_confiable(request):
        try:
            return str(ipaddress.ip_address(request.headers.get("cf-connecting-ip", "")))
        except ValueError:
            pass
    return directa


def proxy_confiable(request: Request) -> bool:
    if not request.client:
        return False
    try:
        ip = ipaddress.ip_address(request.client.host)
        redes = [ipaddress.ip_network(x.strip()) for x in os.environ.get("FANTASMA_PROXY_CIDRS", "").split(",") if x.strip()]
    except ValueError:
        return False
    return any(ip in red for red in redes)


def es_https(request: Request) -> bool:
    publico = urlsplit(os.environ.get("FANTASMA_URL_PUBLICA", ""))
    return (request.url.scheme == "https"
            or (publico.scheme == "https" and publico.netloc == request.headers.get("host"))
            or (proxy_confiable(request) and request.headers.get("x-forwarded-proto") == "https"))


def mismo_origen(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return False
    origen = request.headers.get("origin") or request.headers.get("referer")
    if not origen or origen == "null":
        return False
    try:
        u = urlsplit(origen)
        return u.scheme == ("https" if es_https(request) else request.url.scheme) and not u.username and u.netloc == request.headers.get("host")
    except ValueError:
        return False


def lineas(texto: str) -> list[str]:
    return [x.strip() for x in texto.splitlines() if x.strip()]


def url_valida(url: str, esquemas: tuple[str, ...] = ("https", "http")) -> bool:
    try:
        u = urlsplit(url)
        puerto = u.port
        return (len(url) <= 500 and u.scheme in esquemas and bool(u.hostname)
                and not u.username and not u.password and (puerto is None or 1 <= puerto <= 65535))
    except ValueError:
        return False


def encabezados_de(texto: str) -> dict[str, str]:
    filas = lineas(texto)
    if len(filas) > MAX_ENCABEZADOS:
        raise Rechazo("err_encabezados")
    out = {}
    for fila in filas:
        nombre, sep, valor = fila.partition(":")
        if not sep or not NOMBRE_ENCABEZADO.match(nombre.strip()) or not valor.strip() or len(valor) > 1000:
            raise Rechazo("err_encabezados")
        out[nombre.strip()] = valor.strip()
    return out


def precio_de(texto: str) -> Decimal | None:
    # "RD$1,250" o "1,250.50": la coma es separador de miles, como se escribe en RD y México.
    limpio = re.sub(r"[^\d.]", "", texto.replace(",", ""))
    if not limpio:
        return None
    try:
        return Decimal(limpio)
    except InvalidOperation:
        raise Rechazo("err_ficha", texto) from None


def ficha_de_form(form, base: Ficha) -> dict:
    """La ficha editada sobre la guardada: lo que el formulario no trae (las fuentes) se conserva."""
    g = lambda k: str(form.get(k, "")).strip()
    lista = lambda k: [str(x) for x in form.getlist(k)]
    productos = [
        {"nombre": n.strip(), "categoria": c.strip(), "precio": precio_de(p), "moneda": m or None, "disponible": d != "no", "detalle": det.strip()}
        for n, c, p, m, d, det in zip(
            lista("producto_nombre"), lista("producto_categoria"), lista("producto_precio"),
            lista("producto_moneda"), lista("producto_disponible"), lista("producto_detalle"), strict=False,
        )
        if n.strip()
    ]
    faq = [{"pregunta": p.strip(), "respuesta": r.strip()} for p, r in zip(lista("faq_pregunta"), lista("faq_respuesta"), strict=False) if p.strip() and r.strip()]
    return {
        **base.model_dump(mode="json"),
        "negocio": g("negocio") or base.negocio,
        "descripcion": g("descripcion"),
        "rubro": g("rubro") or base.rubro,
        "pais": g("pais"),
        "moneda": g("moneda") or base.moneda,
        "horario": g("horario"),
        "tono": g("tono") or base.tono,
        "zonas_entrega": lineas(g("zonas_entrega")),
        "formas_pago": lineas(g("formas_pago")),
        "politicas": {"delivery": g("delivery"), "devoluciones": g("devoluciones"), "garantia": g("garantia")},
        "productos": productos,
        "preguntas_frecuentes": faq,
        "faltantes": lineas(g("faltantes")),
    }


def cookie_sesion(request: Request) -> str:
    return "__Host-fantasma_sesion" if es_https(request) else "fantasma_sesion"


def destino_login(valor: str) -> str:
    if len(valor) <= 1024 and not any(c in valor for c in "\\\r\n") and valor.startswith(("/app", "/corrida/", "/cuenta")):
        return valor
    return "/app"


class Guardia:
    """Tamaño y mismo origen en todo lo que escribe, y cabeceras de seguridad en toda respuesta. ASGI puro y no
    BaseHTTPMiddleware: ese envuelve el SSE y deja de ver la desconexión del cliente."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        async def con_cabeceras(mensaje) -> None:
            if mensaje["type"] == "http.response.start":
                h = MutableHeaders(scope=mensaje)
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "same-origin")
                h.setdefault("X-Frame-Options", "DENY")
                if not request.url.path.startswith("/static/"):
                    h.setdefault("Cache-Control", "no-store")
            await send(mensaje)

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            largo = request.headers.get("content-length", "")
            rechazo = None
            if largo and (not largo.isdigit() or int(largo) > MAX_CUERPO):
                rechazo = PlainTextResponse(TEXTOS[idioma_de(request)]["err_grande"], status_code=413)
            elif largo and request.headers.get("transfer-encoding"):
                rechazo = PlainTextResponse("Content-Length y Transfer-Encoding incompatibles", status_code=400)
            elif not mismo_origen(request):
                rechazo = PlainTextResponse(TEXTOS[idioma_de(request)]["err_origen"], status_code=403)
            if rechazo:
                return await rechazo(scope, receive, con_cabeceras)
            cuerpo = bytearray()
            try:
                async with asyncio.timeout(30):
                    while True:
                        mensaje = await receive()
                        if mensaje["type"] == "http.disconnect":
                            return
                        trozo = mensaje.get("body", b"")
                        if len(cuerpo) + len(trozo) > MAX_CUERPO:
                            rechazo = PlainTextResponse(TEXTOS[idioma_de(request)]["err_grande"], status_code=413)
                            return await rechazo(scope, receive, con_cabeceras)
                        cuerpo.extend(trozo)
                        if not mensaje.get("more_body", False):
                            break
            except TimeoutError:
                return await PlainTextResponse("Tiempo de recepción agotado", status_code=408)(scope, receive, con_cabeceras)
            recibir_original = receive
            pendiente = True

            async def recibir_cuerpo():
                nonlocal pendiente
                if pendiente:
                    pendiente = False
                    return {"type": "http.request", "body": bytes(cuerpo), "more_body": False}
                return await recibir_original()

            receive = recibir_cuerpo

        cuentas = request.app.state.cuentas
        usuario = cuentas.usuario(request.cookies.get(cookie_sesion(request)))
        scope.setdefault("state", {})["usuario"] = usuario
        publica = (
            request.method in ("GET", "HEAD") and (
                request.url.path in ("/", "/demo", "/login", "/registro", "/como-funciona", "/salud", "/n8n/conector.json")
                or request.url.path.startswith("/static/")
                or re.fullmatch(r"/b/[A-Za-z0-9_-]{20,64}", request.url.path)
                or re.fullmatch(r"/idioma/(es|en)", request.url.path)
            )
            or request.method == "POST" and request.url.path in ("/login", "/registro")
        )
        if not publica and not usuario:
            if request.method == "GET" and not request.url.path.endswith(("/estado", "/eventos", ".jsonl")):
                respuesta = RedirectResponse("/login?" + urlencode({"volver": str(request.url.path)}), status_code=303)
            else:
                respuesta = PlainTextResponse(TEXTOS[idioma_de(request)]["auth"]["necesitas_sesion"], status_code=401)
            return await respuesta(scope, receive, con_cabeceras)

        await self.app(scope, receive, con_cabeceras)


# ---------------------------------------------------------------- la app


def crear_app(servicio: Servicio | None = None) -> FastAPI:
    """`servicio` lo inyectan las pruebas; sin él se crea al arrancar (no al importar: lee config.yaml y abre runs/)."""

    @asynccontextmanager
    async def vida(app: FastAPI):
        if app.state.servicio is None:
            app.state.servicio = Servicio()
        app.state.servicio.indice.marcar_interrumpidas()
        app.state.secreto = _secreto(app.state.servicio.raiz)
        app.state.cuentas = Cuentas(app.state.servicio.indice.db)
        app.state.hash_slots = asyncio.Semaphore(2)
        app.state.servicio.raiz.chmod(0o700)
        for nombre in ("indice.sqlite", "indice.sqlite-wal", "indice.sqlite-shm"):
            ruta = app.state.servicio.raiz / nombre
            if ruta.exists():
                ruta.chmod(0o600)
        yield

    app = FastAPI(lifespan=vida, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.servicio = servicio
    # Modo y webhook elegidos en el inicio, para precargar la ficha. Si el proceso se reinicia, se vuelven a escribir.
    app.state.pendientes = {}
    app.add_middleware(Guardia)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    plantillas = Jinja2Templates(directory=BASE / "plantillas")

    def srv(request: Request) -> Servicio:
        return request.app.state.servicio

    def hay_tavily() -> bool:
        return bool(os.environ.get("TAVILY_API_KEY"))

    def pagina(request: Request, nombre: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        idioma = idioma_de(request)
        t = TEXTOS[idioma]
        textos_js = {k: t[k] for k in TEXTOS_JS}
        config = srv(request).config_base
        nebius_nemotron = any(
            r.modelo.lower().startswith("nvidia/") and "nemotron" in r.modelo.lower()
            and urlsplit(config.proveedor_de(rol).base_url).scheme == "https"
            and urlsplit(config.proveedor_de(rol).base_url).hostname == "api.tokenfactory.nebius.com"
            for rol, r in config.roles.items()
        )
        max_prompt_chars = int(config.optimizacion.max_prompt_tokens * 3.5)
        return plantillas.TemplateResponse(
            request,
            nombre,
            {
                "t": t,
                "idioma": idioma,
                "otro_idioma": "en" if idioma == "es" else "es",
                "textos_js": textos_js,
                "tavily": hay_tavily(),
                "nebius_nemotron": nebius_nemotron,
                "max_prompt_chars": max_prompt_chars,
                "usuario": getattr(request.state, "usuario", None),
                **ctx,
            },
            status_code=status,
        )

    # -------------------------------------------- dueño de cada corrida: cookie firmada con los run_id de este navegador

    def _firma(valor: str, secreto: bytes) -> str:
        return hmac.new(secreto, valor.encode(), hashlib.sha256).hexdigest()[:32]

    def mias(request: Request) -> list[str]:
        crudo = request.cookies.get(COOKIE_MIAS, "")
        valor, _, firma = crudo.rpartition(".")
        if not valor or not hmac.compare_digest(firma, _firma(valor, request.app.state.secreto)):
            return []
        return [x for x in valor.split("~") if RUN_ID.match(x)]

    def marcar_mia(request: Request, respuesta: Response, run_id: str) -> None:
        request.app.state.cuentas.asociar(request.state.usuario["id"], [run_id])
        ids = [run_id, *[x for x in mias(request) if x != run_id]][:MAX_MIAS]
        valor = "~".join(ids)
        respuesta.set_cookie(
            COOKIE_MIAS, f"{valor}.{_firma(valor, request.app.state.secreto)}", max_age=30 * 24 * 3600, httponly=True,
            secure=es_https(request), samesite="lax",
        )

    def mia(request: Request, run_id: str) -> str:
        if not request.app.state.cuentas.pertenece(request.state.usuario["id"], run_id):
            raise HTTPException(404)
        return run_id

    # -------------------------------------------- guardia: tamaño, mismo origen y cabeceras de seguridad

    @app.exception_handler(StarletteHTTPException)
    async def error_http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return pagina(request, "error.html", 404, mensaje=TEXTOS[idioma_de(request)]["error_404"])
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # -------------------------------------------- páginas fijas

    @app.get("/app/nuevo", response_class=HTMLResponse)
    async def inicio(request: Request, modo: str = "generado", ejemplo: int = 0):
        datos = {"modo": modo if modo in ("generado", "prompt", "remoto") else "generado"}
        if ejemplo == 1:
            ui = TEXTOS[idioma_de(request)]["ui"]
            datos.update(nombre=ui["ejemplo_nombre"], texto=ui["ejemplo_texto"])
        return pagina(request, "inicio.html", previas=_previas(request), datos=datos)

    @app.get("/", response_class=HTMLResponse)
    async def portada(request: Request):
        return pagina(request, "portada.html", publica=True, marketing=True)

    @app.get("/app", response_class=HTMLResponse)
    async def panel(request: Request, q: str = "", estado: str = ""):
        cuentas = request.app.state.cuentas
        busqueda = q[:120].strip()
        filas = cuentas.corridas(request.state.usuario["id"], busqueda, estado)
        return pagina(request, "panel.html", corridas=filas, **cuentas.resumen(request.state.usuario["id"]),
                      q=busqueda, filtro_estado=estado)


    def abrir_sesion(request: Request, usuario: dict, volver: str = "/app"):
        cuentas = request.app.state.cuentas
        cuentas.cerrar(request.cookies.get(cookie_sesion(request)))
        cuentas.asociar(usuario["id"], mias(request))
        duracion = DURACION_SESION
        token = cuentas.sesion(usuario["id"], duracion)
        respuesta = RedirectResponse(destino_login(volver), status_code=303)
        respuesta.set_cookie(cookie_sesion(request), token, max_age=duracion,
                             httponly=True, secure=es_https(request), samesite="lax")
        respuesta.delete_cookie(COOKIE_MIAS)
        return respuesta

    async def procesar_clave(funcion, *args):
        async with app.state.hash_slots:
            return await run_in_threadpool(funcion, *args)

    def limitar_acceso(request: Request, email: str, accion: str) -> bool:
        cuentas = request.app.state.cuentas
        secreto = request.app.state.secreto
        ip = hmac.new(secreto, ("auth:" + ip_de(request)).encode(), hashlib.sha256).hexdigest()
        correo = hmac.new(secreto, ("email:" + email).encode(), hashlib.sha256).hexdigest()
        return cuentas.permitir(accion + ":" + ip, 40) and cuentas.permitir(accion + ":" + correo, 10)

    @app.get("/login", response_class=HTMLResponse)
    @app.get("/registro", response_class=HTMLResponse)
    async def acceso(request: Request, volver: str = "/app"):
        if request.state.usuario:
            return RedirectResponse(destino_login(volver), status_code=303)
        return pagina(request, "acceso.html", publica=True, marketing=True,
                      registro=request.url.path == "/registro", volver=destino_login(volver), datos={})

    @app.post("/login")
    @app.post("/registro")
    async def autenticar(request: Request):
        form = await request.form()
        registro = request.url.path == "/registro"
        email = str(form.get("email", "")).strip().casefold()[:255]
        nombre = str(form.get("nombre", "")).strip()[:80]
        clave = str(form.get("clave", ""))
        volver = destino_login(str(form.get("volver", "/app")))
        t = TEXTOS[idioma_de(request)]["auth"]
        def error(mensaje, status=400):
            return pagina(request, "acceso.html", status, publica=True, marketing=True, registro=registro,
                          volver=volver, datos={"email": email, "nombre": nombre}, error=mensaje)
        if not limitar_acceso(request, email, "registro" if registro else "login"):
            return error(t["limite"], 429)
        if registro:
            try:
                email = validate_email(email, check_deliverability=False).normalized.casefold()
            except EmailNotValidError:
                return error(t["email_invalido"])
            if not nombre or len(clave) < 12 or len(clave) > 128 or clave != form.get("confirmar"):
                return error(t["datos_invalidos"])
            clave_hash = await procesar_clave(CLAVES.hash, clave)
            try:
                usuario = request.app.state.cuentas.crear(nombre, email, clave_hash)
            except sqlite3.IntegrityError:
                return error(t["registro_error"])
        else:
            fila = request.app.state.cuentas.por_email(email)
            valida = await procesar_clave(CLAVES.verify, clave[:129], fila["clave_hash"] if fila else HASH_FICTICIO)
            if not fila or not valida or len(clave) > 128:
                return error(t["credenciales"], 400)
            usuario = dict(fila)
        return abrir_sesion(request, usuario, volver)

    @app.get("/demo")
    async def demo(request: Request):
        # Un informe existente: jamás crea cuentas, corridas ni llamadas de inferencia.
        token = os.environ.get("FANTASMA_DEMO_TOKEN", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token) and srv(request).indice.por_token(token):
            return RedirectResponse("/b/" + token, status_code=303)
        return RedirectResponse("/#recorridos", status_code=303)

    @app.post("/logout")
    async def salir(request: Request):
        request.app.state.cuentas.cerrar(request.cookies.get(cookie_sesion(request)))
        respuesta = RedirectResponse("/login", status_code=303)
        respuesta.delete_cookie(cookie_sesion(request), secure=es_https(request), httponly=True, samesite="lax")
        respuesta.delete_cookie(COOKIE_MIAS)
        return respuesta

    @app.get("/cuenta", response_class=HTMLResponse)
    async def cuenta(request: Request):
        return pagina(request, "cuenta.html")

    @app.post("/cuenta/clave")
    async def cambiar_clave(request: Request):
        usuario = request.state.usuario
        t = TEXTOS[idioma_de(request)]["auth"]
        if not limitar_acceso(request, usuario["email"], "clave"):
            return pagina(request, "cuenta.html", 429, error=t["limite"])
        form = await request.form()
        fila = request.app.state.cuentas.por_email(usuario["email"])
        actual = str(form.get("actual", ""))
        nueva = str(form.get("nueva", ""))
        valida = await procesar_clave(CLAVES.verify, actual[:129], fila["clave_hash"])
        if not valida or len(actual) > 128:
            return pagina(request, "cuenta.html", 400, error=t["credenciales"])
        if not 12 <= len(nueva) <= 128 or nueva != form.get("confirmar"):
            return pagina(request, "cuenta.html", 400, error=t["datos_invalidos"])
        clave_hash = await procesar_clave(CLAVES.hash, nueva)
        request.app.state.cuentas.cambiar_clave(usuario["id"], clave_hash)
        return abrir_sesion(request, usuario, "/cuenta?guardada=1")

    @app.get("/como-funciona", response_class=HTMLResponse)
    async def como_funciona(request: Request):
        config = srv(request).config_base
        modelos = {rol: r.modelo for rol, r in config.roles.items()}
        proveedores = sorted({r.proveedor for r in config.roles.values()})
        return pagina(request, "como.html", modelos=modelos, proveedores=proveedores, publica=True, marketing=True)

    @app.get("/idioma/{codigo}")
    async def cambiar_idioma(codigo: str, volver: str = "/"):
        if codigo not in IDIOMAS:
            raise HTTPException(404)
        # Solo rutas propias: nada de //otro-sitio ni URLs absolutas.
        destino = volver if volver.startswith("/") and not volver.startswith("//") and "\\" not in volver else "/"
        r = RedirectResponse(destino, status_code=303)
        r.set_cookie(COOKIE_IDIOMA, codigo, max_age=365 * 24 * 3600, samesite="lax")
        return r

    @app.get("/n8n/conector.json")
    async def conector():
        return FileResponse(CONECTOR, media_type="application/json", filename="cliente-fantasma-conector.json")

    @app.get("/salud")
    async def salud():
        return {"ok": True}

    def _previas(request: Request) -> list[dict]:
        out = []
        for run_id in [f["id"] for f in request.app.state.cuentas.corridas(request.state.usuario["id"])][:5]:
            try:
                e = srv(request).estado(run_id)
            except KeyError:
                continue
            out.append({"id": run_id, "negocio": e["negocio"], "estado": e["estado"]})
        return out

    # -------------------------------------------- 1. ficha

    @app.post("/probar")
    async def probar(request: Request):
        form = await request.form()
        g = lambda k: str(form.get(k, "")).strip()
        datos = {k: g(k) for k in ("modo", "nombre", "urls", "texto", "prompt", "url_bot", "encabezados")}
        try:
            urls = lineas(datos["urls"])
            if len(urls) > MAX_URLS:
                raise Rechazo("err_urls_max")
            for u in urls:
                if not url_valida(u):
                    raise Rechazo("err_url", u)
            if len(datos["texto"]) > MAX_TEXTO:
                raise Rechazo("err_texto_largo")
            if not urls and not datos["texto"]:
                raise Rechazo("err_sin_material")
            if not datos["texto"] and not hay_tavily():
                raise Rechazo("err_sin_tavily")
            modo = datos["modo"] or "generado"
            if modo not in ("generado", "prompt", "remoto"):
                raise Rechazo("err_modo")
            datos["modo"] = modo
            encabezados = {}
            if modo == "prompt":
                validar_prompt(request, datos["prompt"])
            elif modo == "remoto":
                encabezados = encabezados_de(datos["encabezados"])
                # Antes de pagar la ficha: una URL interna se rechazaría igual al lanzar.
                await comprobar_bot(request, datos["url_bot"], encabezados)
        except Rechazo as r:
            return pagina(request, "inicio.html", 400, error=_mensaje(request, r), previas=_previas(request), datos=datos)
        try:
            run_id = await srv(request).crear_ficha(
                urls, datos["texto"][:MAX_TEXTO], ip_de(request), datos["nombre"][:200],
                al_crear=lambda rid: request.app.state.cuentas.asociar(request.state.usuario["id"], [rid]),
            )
        except Limite as exc:
            return pagina(request, "inicio.html", 429, error=str(exc), previas=_previas(request), datos=datos)
        except PresupuestoAgotado as exc:
            return pagina(request, "inicio.html", 429, error=str(exc), previas=_previas(request), datos=datos)
        except PrecioDesconocido:
            return pagina(request, "inicio.html", 503, error=TEXTOS[idioma_de(request)]["err_ficha_tecnico"], previas=_previas(request), datos=datos)
        except ValueError as exc:
            return pagina(request, "inicio.html", 400, error=str(exc), previas=_previas(request), datos=datos)
        except ErrorTecnico:
            return pagina(request, "inicio.html", 502, error=TEXTOS[idioma_de(request)]["err_ficha_tecnico"], previas=_previas(request), datos=datos)
        request.app.state.pendientes[run_id] = {
            "modo": modo,
            "prompt": datos["prompt"] if modo == "prompt" else "",
            "url_bot": datos["url_bot"] if modo == "remoto" else "",
            "encabezados": encabezados,
        }
        srv(request).guardar_seleccion(run_id, modo, datos["prompt"], datos["url_bot"])
        r = RedirectResponse(f"/corrida/{run_id}/ficha", status_code=303)
        marcar_mia(request, r, run_id)
        return r

    async def comprobar_bot(request: Request, url: str, encabezados: dict[str, str]) -> None:
        inseguro = srv(request).config_base.remoto_inseguro
        if not url_valida(url, ("https", "http") if inseguro else ("https",)):
            raise Rechazo("err_url_bot")
        try:
            await remoto.comprobar(Remoto(url=url, encabezados=encabezados), inseguro)
        except (ErrorTecnico, ValueError) as exc:
            raise Rechazo("err_url_bot_rechazada", str(exc)) from None

    def validar_prompt(request: Request, prompt: str) -> None:
        if not prompt.strip():
            raise Rechazo("err_prompt_vacio")
        max_chars = int(srv(request).config_base.optimizacion.max_prompt_tokens * 3.5)
        if len(prompt) > max_chars:
            raise Rechazo("err_prompt_largo")

    def _mensaje(request: Request, r: Rechazo) -> str:
        clave, *args = r.args
        texto = TEXTOS[idioma_de(request)][clave]
        return texto.format(url=args[0], detalle=args[0]) if args else texto

    def _vista_ficha(request: Request, run_id: str, status: int = 200, ficha: Any = None, **ctx: Any) -> HTMLResponse:
        s = srv(request)
        pendiente = {**s.seleccion(run_id), "encabezados": {}, **request.app.state.pendientes.get(run_id, {})}
        return pagina(
            request, "ficha.html", status, run_id=run_id, ficha=ficha or s.ficha(run_id), monedas=MONEDAS, tamaños=s.tamaños,
            pendiente=pendiente, encabezados_txt="\n".join(f"{k}: {v}" for k, v in pendiente["encabezados"].items()), **ctx,
        )

    @app.get("/corrida/{run_id}/ficha", response_class=HTMLResponse)
    async def ver_ficha(request: Request, run_id: str, guardada: int = 0):
        mia(request, run_id)
        estado = srv(request).estado(run_id)["estado"]
        if estado != "ficha":
            return RedirectResponse(f"/corrida/{run_id}/vivo", status_code=303)
        return _vista_ficha(request, run_id, guardada=bool(guardada))

    @app.post("/corrida/{run_id}/ficha")
    async def guardar_ficha(request: Request, run_id: str):
        mia(request, run_id)
        s = srv(request)
        form = await request.form()
        accion = form.get("accion", "guardar")
        modo = str(form.get("modo", ""))
        tamaño = "completa" if form.get("tamano") == "completa" else "ligera"
        prompt = str(form.get("prompt", "")).strip()
        url_bot = str(form.get("url_bot", "")).strip()
        encabezados_txt = str(form.get("encabezados", ""))
        datos: dict | None = None
        try:
            if modo not in ("generado", "prompt", "remoto"):
                raise Rechazo("err_modo")
            datos = ficha_de_form(form, s.ficha(run_id))
            s.actualizar_ficha(run_id, datos)
            encabezados = encabezados_de(encabezados_txt) if modo == "remoto" else {}
            request.app.state.pendientes[run_id] = {
                "modo": modo,
                "prompt": prompt if modo == "prompt" else "",
                "url_bot": url_bot if modo == "remoto" else "",
                "encabezados": encabezados,
            }
            if modo == "prompt":
                validar_prompt(request, prompt)
            s.guardar_seleccion(run_id, modo, prompt, url_bot)
            if accion != "lanzar":
                return RedirectResponse(f"/corrida/{run_id}/ficha?guardada=1", status_code=303)
            if modo == "remoto" and not url_valida(url_bot, ("https", "http") if s.config_base.remoto_inseguro else ("https",)):
                raise Rechazo("err_url_bot")
            tarea = await s.lanzar(
                run_id,
                modo,
                tamaño,
                url_bot=url_bot or None,
                encabezados=encabezados or None,
                prompt_usuario=prompt or None,
            )
        except Rechazo as r:
            return _vista_ficha(request, run_id, 400, ficha=datos, error=_mensaje(request, r))
        except ValidationError as exc:
            detalle = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
            return _vista_ficha(request, run_id, 422, ficha=datos, error=_mensaje(request, Rechazo("err_ficha", detalle)))
        except Limite as exc:
            return _vista_ficha(request, run_id, 429, ficha=datos, error=str(exc))
        except (ValueError, ErrorTecnico) as exc:
            return _vista_ficha(request, run_id, 400, ficha=datos, error=str(exc))
        request.app.state.pendientes.pop(run_id, None)
        loop = asyncio.get_running_loop()
        tarea.add_done_callback(lambda _: loop.call_later(OLVIDAR_S, s.eventos.olvidar, run_id))
        return RedirectResponse(f"/corrida/{run_id}/vivo", status_code=303)

    # -------------------------------------------- 2. en vivo

    @app.get("/corrida/{run_id}/vivo", response_class=HTMLResponse)
    async def vivo(request: Request, run_id: str):
        mia(request, run_id)
        estado = srv(request).estado(run_id)
        if estado["estado"] == "ficha":
            return RedirectResponse(f"/corrida/{run_id}/ficha", status_code=303)
        return pagina(request, "vivo.html", run_id=run_id, estado=estado, activa=estado["estado"] in ESTADOS_ACTIVOS)

    @app.get("/corrida/{run_id}/eventos")
    async def eventos(request: Request, run_id: str):
        mia(request, run_id)
        ultimo = request.headers.get("last-event-id", "")
        emisor = srv(request).eventos
        if srv(request).estado(run_id)["estado"] not in ESTADOS_ACTIVOS and not emisor.recientes(run_id):
            # Terminó hace rato o antes de reiniciar: el `fin` ya no va a llegar. Con 204 el navegador no reconecta.
            return Response(status_code=204)
        cola = emisor.suscribir(run_id, int(ultimo) if ultimo.isdigit() else 0)

        async def flujo() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        evento = await asyncio.wait_for(cola.get(), timeout=LATIDO_S)
                    except TimeoutError:
                        yield ": latido\n\n"
                        continue
                    if not request.app.state.cuentas.usuario(request.cookies.get(cookie_sesion(request))):
                        break
                    yield f"id: {evento['id']}\ndata: {json.dumps(evento, ensure_ascii=False, default=str)}\n\n"
                    # Después del fin esa corrida no publica nada más: se cierra en vez de mandar latidos sin fin.
                    if evento["tipo"] == "fin":
                        break
            finally:
                emisor.desuscribir(run_id, cola)

        return StreamingResponse(flujo(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/corrida/{run_id}/estado")
    async def estado(request: Request, run_id: str):
        mia(request, run_id)
        e = srv(request).estado(run_id)
        return JSONResponse({k: e.get(k) for k in ("estado", "motivo", "costo_usd", "activa", "evaluadas")})

    # -------------------------------------------- 3. boleta

    def _vista_boleta(request: Request, run_id: str, publica: bool, token: str = "") -> HTMLResponse:
        s = srv(request)
        try:
            b = s.boleta(run_id, publica=publica)
        except (ValueError, FileNotFoundError):
            if publica:
                raise HTTPException(404) from None
            return pagina(request, "boleta.html", boleta=None, publica=False, run_id=run_id, estado=s.estado(run_id))
        base_publica = os.environ.get("FANTASMA_URL_PUBLICA", str(request.base_url)).rstrip("/")
        enlace = base_publica + f"/b/{token}" if token else ""
        return pagina(request, "boleta.html", boleta=b, publica=publica, run_id=run_id if not publica else "", enlace=enlace)

    @app.get("/corrida/{run_id}/boleta", response_class=HTMLResponse)
    async def boleta(request: Request, run_id: str):
        mia(request, run_id)
        token = srv(request).estado(run_id)["token_publico"]
        return _vista_boleta(request, run_id, publica=False, token=token)

    @app.get("/b/{token}", response_class=HTMLResponse)
    async def boleta_publica(request: Request, token: str):
        if len(token) > 64:
            raise HTTPException(404)
        fila = srv(request).indice.por_token(token)
        if fila is None:
            raise HTTPException(404)
        return _vista_boleta(request, fila.id, publica=True)

    @app.get("/corrida/{run_id}/conversaciones.jsonl")
    async def exportar(request: Request, run_id: str):
        mia(request, run_id)
        try:
            ruta = srv(request).exportar_jsonl(run_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404) from None
        return FileResponse(ruta, media_type="application/x-ndjson", filename=f"cliente-fantasma-{run_id}.jsonl")

    @app.exception_handler(KeyError)
    async def no_existe(request: Request, exc: KeyError):
        return pagina(request, "error.html", 404, mensaje=TEXTOS[idioma_de(request)]["error_404"])

    return app


def _secreto(raiz: Path) -> bytes:
    """Firma la cookie de dueño. Va en un archivo junto a las corridas para que sobreviva a los reinicios."""
    if env := os.environ.get("FANTASMA_SECRETO"):
        return env.encode()
    ruta = raiz / ".secreto"
    if not ruta.exists():
        raiz.mkdir(parents=True, exist_ok=True)
        ruta.write_text(secrets.token_hex(32))
        ruta.chmod(0o600)
    return ruta.read_text().strip().encode()


app = crear_app()
