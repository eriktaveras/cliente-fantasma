"""La app web con TestClient sobre el Servicio de verdad y el LLM guionado de test_servicio: formularios, dueño por cookie,
guardia de origen, SSE, boleta privada y pública."""

import json
import re
import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from fantasma import app as web
from fantasma import costos, remoto
from fantasma.cuentas import CLAVES
from fantasma.eventos import Emisor
from fantasma.servicio import Servicio
from tests.test_servicio import COLMADO, RESPUESTA_BOT, LLMGuionado

ORIGEN = {"origin": "http://testserver"}
HASH_PRUEBA = CLAVES.hash("clave-de-prueba-segura")


def autenticar_cliente(c, email="erik@example.com"):
    cuentas = c.app.state.cuentas
    usuario = cuentas.crear("Erik", email, HASH_PRUEBA)
    c.cookies.set("fantasma_sesion", cuentas.sesion(usuario["id"]), domain="testserver.local")
    return usuario


@pytest.fixture
def servicio(tmp_path, config, monkeypatch):
    monkeypatch.setitem(costos.PRECIOS, "falso", (1.0, 3.0))
    for var in ("TAVILY_API_KEY", "TOPE_DIARIO_USD", "CORRIDAS_POR_DIA", "FANTASMA_SECRETO"):
        monkeypatch.delenv(var, raising=False)
    return Servicio(raiz=tmp_path, config=config, fabrica_llm=LLMGuionado, eventos=Emisor())


@pytest.fixture
def cliente(servicio, monkeypatch):
    monkeypatch.setattr(web, "LATIDO_S", 0.05)
    with TestClient(web.crear_app(servicio), headers=ORIGEN) as c:
        autenticar_cliente(c)
        yield c


@pytest.fixture
def bot_remoto(monkeypatch):
    async def resolver(host, puerto):
        return ["93.184.216.34"]

    monkeypatch.setattr(remoto, "resolver", resolver)
    monkeypatch.setattr(remoto, "TRANSPORTE", httpx.MockTransport(lambda req: httpx.Response(200, json={"respuesta": RESPUESTA_BOT})))


def crear(cliente, **form) -> str:
    r = cliente.post("/probar", data={"modo": "generado", "texto": COLMADO, **form}, follow_redirects=False)
    assert r.status_code == 303, r.text
    m = re.fullmatch(r"/corrida/(\d{8}-[0-9a-f]{8})/ficha", r.headers["location"])
    return m.group(1)


def form_ficha(servicio, run_id, **cambios) -> dict:
    """El formulario tal como lo manda el navegador con la ficha guardada."""
    f = servicio.ficha(run_id)
    datos = {
        "negocio": f.negocio, "rubro": f.rubro, "descripcion": f.descripcion, "pais": f.pais, "moneda": f.moneda,
        "horario": f.horario, "tono": f.tono, "zonas_entrega": "\n".join(f.zonas_entrega), "formas_pago": "\n".join(f.formas_pago),
        "delivery": "", "devoluciones": "", "garantia": "", "faltantes": "\n".join(f.faltantes),
        "producto_nombre": [p.nombre for p in f.productos] + [""],
        "producto_precio": [str(p.precio) for p in f.productos] + [""],
        "producto_moneda": ["" for _ in f.productos] + [""],
        "producto_categoria": [p.categoria for p in f.productos] + [""],
        "producto_disponible": ["si" for _ in f.productos] + ["si"],
        "producto_detalle": ["" for _ in f.productos] + [""],
        "faq_pregunta": [q.pregunta for q in f.preguntas_frecuentes] + [""],
        "faq_respuesta": [q.respuesta for q in f.preguntas_frecuentes] + [""],
        "modo": "generado", "tamano": "ligera", "accion": "guardar",
    }
    return {**datos, **cambios}


def esperar(cliente, run_id) -> dict:
    for _ in range(400):
        e = cliente.get(f"/corrida/{run_id}/estado").json()
        if not e["activa"] and e["estado"] not in ("generando", "corriendo"):
            return e
        time.sleep(0.05)
    raise AssertionError("la corrida no terminó")


def lanzar(cliente, servicio, run_id) -> dict:
    r = cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, accion="lanzar"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/corrida/{run_id}/vivo", r.text
    return esperar(cliente, run_id)


# ---------------------------------------------------------------- páginas fijas


@pytest.mark.parametrize("ruta", ["/", "/como-funciona", "/static/css/taller.css", "/static/css/fantasma.css", "/static/js/fantasma.js", "/salud"])
def test_paginas_y_estaticos(cliente, ruta):
    r = cliente.get(ruta)
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"


def test_conector_n8n_se_descarga(cliente):
    r = cliente.get("/n8n/conector.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert json.loads(r.content)["nodes"]


def test_como_funciona_muestra_configuracion_activa(cliente):
    html = cliente.get("/como-funciona").text
    assert "Proveedores activos: local" in html and "X-Cliente-Fantasma: true" in html
    assert "falso" in html and "Todo corre en NVIDIA Nemotron" not in html


def test_idioma_por_accept_language_y_selector(cliente):
    assert "What do you have today?" in cliente.get("/app/nuevo", headers={"accept-language": "en-US,en;q=0.9"}).text
    assert "¿Qué tienes hoy?" in cliente.get("/app/nuevo", headers={"accept-language": "es-DO"}).text
    r = cliente.get("/idioma/en?volver=/como-funciona", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/como-funciona"
    assert "One reference, three starting points" in cliente.get("/como-funciona", headers={"accept-language": "es"}).text  # la cookie manda
    assert cliente.get("/idioma/es?volver=//malo.example", follow_redirects=False).headers["location"] == "/"
    assert cliente.get("/idioma/fr", follow_redirects=False).status_code == 404


def test_textos_con_las_mismas_claves_en_los_dos_idiomas():
    def claves(d, pref=""):
        out = set()
        for k, v in d.items():
            out.add(pref + k)
            if isinstance(v, dict):
                out |= claves(v, f"{pref}{k}.")
        return out

    assert claves(web.TEXTOS["es"]) == claves(web.TEXTOS["en"])
    for k in web.TEXTOS_JS:
        assert k in web.TEXTOS["es"]


def test_inicio_ofrece_tres_puntos_de_entrada_y_paneles_exclusivos(cliente):
    html = cliente.get("/app/nuevo").text
    assert html.count('name="modo"') == 3
    assert all(f'value="{modo}"' in html for modo in ("generado", "prompt", "remoto"))
    assert 'data-panel-modo="prompt"' in html and 'name="prompt"' in html
    assert 'data-panel-modo="remoto"' in html and 'name="url_bot"' in html
    assert "Los datos del negocio serán la referencia en los tres casos" in html


# ---------------------------------------------------------------- flujo


def test_flujo_completo_ficha_vivo_boleta_publica(cliente, servicio):
    run_id = crear(cliente)
    html = cliente.get(f"/corrida/{run_id}/ficha").text
    assert "Colmado La Esquina" in html and 'value="Arroz"' in html and "Lanzar la prueba" in html

    # Editar: el aceite sube y aparece un producto nuevo; las fuentes no viajan en el formulario y se conservan.
    f = form_ficha(servicio, run_id)
    f["producto_precio"][2] = "RD$1,250.50"
    f["producto_nombre"][-1], f["producto_precio"][-1] = "Salami", "180"
    r = cliente.post(f"/corrida/{run_id}/ficha", data=f, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?guardada=1")
    ficha = servicio.ficha(run_id)
    assert [p.nombre for p in ficha.productos] == ["Arroz", "Habichuelas", "Aceite", "Salami"]
    assert ficha.productos[2].precio == Decimal("1250.50")

    e = lanzar(cliente, servicio, run_id)
    assert e["estado"] == "terminada" and e["evaluadas"] > 0

    # Lanzada, la ficha ya no se edita: la ruta manda a la vista en vivo.
    assert cliente.get(f"/corrida/{run_id}/ficha", follow_redirects=False).headers["location"] == f"/corrida/{run_id}/vivo"
    vivo = cliente.get(f"/corrida/{run_id}/vivo").text
    assert "Esta prueba ya terminó" in vivo and f"/corrida/{run_id}/boleta" in vivo

    boleta = cliente.get(f"/corrida/{run_id}/boleta")
    assert boleta.status_code == 200
    b = servicio.boleta(run_id)
    assert f">{b.mejor.nota}<" in boleta.text and "AQUÍ FALLÓ" in boleta.text and RESPUESTA_BOT in boleta.text
    assert {"Aprobó esta prueba", "Requiere ajustes", "No superó esta prueba"} & {x for x in re.findall(r"</span>([^<]+)</p>", boleta.text)}
    assert "conversaciones.jsonl" in boleta.text
    # Modo generado: no hay bot del dueño; la nota es del bot de prueba y el entregable es el prompt.
    assert "prototipo creado con la ficha" in boleta.text and "INSTRUCCIONES PARA IMPLEMENTAR" in boleta.text and "ANTES Y DESPUÉS" not in boleta.text
    token = servicio.indice.obtener(run_id).token_publico
    assert f"http://testserver/b/{token}" in boleta.text

    publica = TestClient(cliente.app).get(f"/b/{token}")
    assert publica.status_code == 200 and RESPUESTA_BOT in publica.text
    assert run_id not in publica.text and "conversaciones.jsonl" not in publica.text and "ip_hash" not in publica.text
    assert "Prueba tu propio bot" in publica.text

    jsonl = cliente.get(f"/corrida/{run_id}/conversaciones.jsonl")
    assert jsonl.status_code == 200 and "attachment" in jsonl.headers["content-disposition"]
    assert {"caso_id", "turnos", "juez"} <= set(json.loads(jsonl.text.splitlines()[0]))


def test_boleta_en_ingles(cliente, servicio):
    run_id = crear(cliente)
    lanzar(cliente, servicio, run_id)
    html = cliente.get(f"/corrida/{run_id}/boleta", headers={"accept-language": "en"}).text
    assert "FAILED HERE" in html and "Score by kind of ghost" in html and "Dominican Republic" in html
    assert "INSTRUCTIONS TO IMPLEMENT" in html and "prototype" in html


def test_modo_prompt_exige_y_conserva_instrucciones_en_ambos_formularios(cliente, servicio):
    r = cliente.post("/probar", data={"modo": "prompt", "texto": COLMADO, "prompt": "   "})
    assert r.status_code == 400 and "Pega las instrucciones" in r.text

    prompt = "Eres el asistente del colmado. MARCA-INTERNA-9831. Consulta el catálogo antes de responder."
    run_id = crear(cliente, modo="prompt", prompt=prompt)
    html = cliente.get(f"/corrida/{run_id}/ficha").text
    assert prompt in html and 'value="prompt" checked' in html

    r = cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, modo="prompt", prompt="", accion="lanzar"))
    assert r.status_code == 400 and "Pega las instrucciones" in r.text
    r = cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, modo="prompt", prompt=prompt, accion="guardar"), follow_redirects=False)
    assert r.status_code == 303
    cliente.app.state.pendientes.clear()
    assert prompt in cliente.get(f"/corrida/{run_id}/ficha").text
    r = cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, modo="prompt", prompt=prompt, accion="lanzar"), follow_redirects=False)
    assert r.status_code == 303
    esperar(cliente, run_id)
    boleta = cliente.get(f"/corrida/{run_id}/boleta").text
    assert "COMPARACIÓN DEL PROMPT" in boleta and "VERSIÓN DE TUS INSTRUCCIONES" in boleta
    assert "herramientas simuladas" in boleta and "bot tuyo" not in boleta and "MARCA-INTERNA-9831" in boleta
    token = servicio.indice.obtener(run_id).token_publico
    publica = TestClient(cliente.app).get(f"/b/{token}").text
    assert "MARCA-INTERNA-9831" not in publica and RESPUESTA_BOT not in publica
    assert "solo aparecen en la boleta privada" in publica


def test_modo_desconocido_no_se_degrada_a_generado(cliente, servicio):
    r = cliente.post("/probar", data={"modo": "misterio", "texto": COLMADO})
    assert r.status_code == 400 and "tres puntos de partida" in r.text
    run_id = crear(cliente)
    r = cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, modo="misterio"))
    assert r.status_code == 400 and "tres puntos de partida" in r.text


def test_remoto_es_auditoria_sin_antes_despues_ni_arreglo_validado(cliente, servicio, bot_remoto):
    url = "https://bot.ejemplo.com/webhook"
    run_id = crear(cliente, modo="remoto", url_bot=url)
    r = cliente.post(
        f"/corrida/{run_id}/ficha",
        data=form_ficha(servicio, run_id, modo="remoto", url_bot=url, accion="lanzar"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    esperar(cliente, run_id)
    html = cliente.get(f"/corrida/{run_id}/boleta").text
    assert "AUDITORÍA DEL BOT" in html and "RECOMENDACIONES" in html
    assert "no se instaló ni se validó" in html
    assert "ANTES Y DESPUÉS" not in html and "Prompt corregido" not in html
    vivo = cliente.get(f"/corrida/{run_id}/vivo").text
    assert 'data-modo="remoto"' in vivo and ">Mejora<" in vivo


def test_sse_repite_lo_guardado_y_respeta_last_event_id(cliente, servicio):
    run_id = crear(cliente)
    lanzar(cliente, servicio, run_id)
    eventos = servicio.eventos.recientes(run_id)
    fin = next(e for e in eventos if e["tipo"] == "fin")

    def leer(**headers) -> list[dict]:
        vistos = []
        with cliente.stream("GET", f"/corrida/{run_id}/eventos", headers=headers) as r:
            assert r.headers["content-type"].startswith("text/event-stream") and r.headers["x-accel-buffering"] == "no"
            for linea in r.iter_lines():
                if linea.startswith("data: "):
                    vistos.append(json.loads(linea[6:]))
                    if vistos[-1]["tipo"] == "fin":
                        break
        return vistos

    todos = leer()
    assert [e["id"] for e in todos] == [e["id"] for e in eventos]
    assert {"conversacion", "turno", "evaluacion", "costo"} <= {e["tipo"] for e in todos}
    conv = next(e for e in todos if e["tipo"] == "conversacion")
    assert set(conv["ejes"]) == {"intencion", "ataque", "dialecto", "estilo"} and conv["persona"]
    assert leer(**{"last-event-id": str(fin["id"] - 1)}) == [fin]


# ---------------------------------------------------------------- permisos y seguridad


def test_otro_navegador_no_ve_la_corrida_privada(cliente, servicio):
    run_id = crear(cliente)
    ajeno = TestClient(cliente.app, headers=ORIGEN)
    autenticar_cliente(ajeno, "otro@example.com")
    for ruta in ("ficha", "vivo", "boleta", "eventos", "estado", "conversaciones.jsonl"):
        assert ajeno.get(f"/corrida/{run_id}/{ruta}").status_code == 404
    r = ajeno.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, negocio="Otro"))
    assert r.status_code == 404 and servicio.ficha(run_id).negocio == "Colmado La Esquina"
    # Una cookie con la firma alterada tampoco sirve.
    ajeno.cookies.set(web.COOKIE_MIAS, f"{run_id}.{'0' * 32}")
    assert ajeno.get(f"/corrida/{run_id}/ficha").status_code == 404


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://malo.example"}, {"origin": "null"}, {"origin": "", "referer": ""}, {"sec-fetch-site": "cross-site"}],
)
def test_post_de_otro_origen_se_rechaza(cliente, servicio, headers):
    r = cliente.post("/probar", data={"texto": COLMADO}, headers=headers)
    assert r.status_code == 403
    assert servicio.indice.listar() == []


def test_referer_propio_sirve_sin_origin(cliente):
    r = cliente.post("/probar", data={"texto": COLMADO}, headers={"origin": "", "referer": "http://testserver/"}, follow_redirects=False)
    assert r.status_code == 303


def test_cuerpo_demasiado_grande(cliente, servicio):
    r = cliente.post("/probar", data={"texto": "x" * (web.MAX_CUERPO + 10)})
    assert r.status_code == 413 and servicio.indice.listar() == []


@pytest.mark.parametrize(
    ("form", "mensaje"),
    [
        ({"texto": ""}, "Pega un texto"),
        ({"texto": "", "urls": "javascript:alert(1)"}, "Esta URL no sirve"),
        ({"urls": "\n".join(f"https://a{i}.example" for i in range(6))}, "Hasta 5 URLs"),
        ({"texto": "x" * 20001}, "20,000"),
        ({"modo": "remoto", "url_bot": "http://bot.example/hook"}, "URL https"),
        ({"modo": "remoto", "url_bot": "https://bot.example/hook", "encabezados": "sin dos puntos"}, "encabezados"),
    ],
)
def test_inicio_valida_antes_de_gastar(cliente, servicio, form, mensaje):
    r = cliente.post("/probar", data={"modo": "generado", "texto": COLMADO, **form})
    assert r.status_code == 400 and mensaje in r.text and 'role="alert"' in r.text
    assert servicio.indice.listar() == []


def test_limite_por_dia_responde_429(cliente, servicio):
    servicio.corridas_por_dia = 1
    crear(cliente)
    r = cliente.post("/probar", data={"texto": COLMADO})
    assert r.status_code == 429 and "máximo de 1 pruebas" in r.text


def test_ip_de_cloudflare_no_se_confia_sin_proxy_autorizado(cliente, servicio):
    r = cliente.post("/probar", data={"texto": COLMADO}, headers={"cf-connecting-ip": "203.0.113.9"}, follow_redirects=False)
    run_id = r.headers["location"].split("/")[2]
    assert servicio.indice.obtener(run_id).ip_hash == servicio.hash_ip("testclient")


def test_todo_se_escapa(cliente, servicio):
    run_id = crear(cliente)
    malo = "<script>alert(1)</script>"
    cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, negocio=malo, horario='"><img src=x onerror=alert(1)>'))
    html = cliente.get(f"/corrida/{run_id}/ficha").text
    assert malo not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html


def test_ficha_invalida_se_vuelve_a_mostrar(cliente, servicio):
    run_id = crear(cliente)
    f = form_ficha(servicio, run_id, moneda="XYZ")
    r = cliente.post(f"/corrida/{run_id}/ficha", data=f)
    assert r.status_code == 422 and "Revisa la ficha" in r.text
    f = form_ficha(servicio, run_id)
    f["producto_precio"][0] = "1.2.3"
    r = cliente.post(f"/corrida/{run_id}/ficha", data=f)
    assert r.status_code == 400 and "1.2.3" in r.text
    assert servicio.ficha(run_id).moneda == "DOP"


def test_remoto_con_url_interna_no_lanza(cliente, servicio):
    # Se rechaza en el inicio, antes de pagar la ficha.
    r = cliente.post("/probar", data={"modo": "remoto", "texto": COLMADO, "url_bot": "https://127.0.0.1/hook"}, follow_redirects=False)
    assert r.status_code == 400 and "no pública" in r.text
    assert servicio.indice.listar() == []
    # Y también al lanzar, si alguien la cambia en la ficha.
    run_id = crear(cliente)
    r = cliente.post(f"/corrida/{run_id}/ficha", data=form_ficha(servicio, run_id, accion="lanzar", modo="remoto", url_bot="https://127.0.0.1/hook"))
    assert r.status_code == 400 and "no pública" in r.text
    assert servicio.indice.obtener(run_id).estado == "ficha"


def test_boleta_publica_con_token_falso(cliente):
    assert cliente.get("/b/no-existe").status_code == 404
    assert cliente.get("/b/" + "x" * 100).status_code == 404


def test_al_arrancar_marca_interrumpidas(servicio):
    servicio.indice.crear("20261012-deadbeef", "h", estado="corriendo")
    with TestClient(web.crear_app(servicio)):
        assert servicio.indice.obtener("20261012-deadbeef").estado == "error"


@pytest.mark.parametrize(("texto", "precio"), [("RD$1,250.50", Decimal("1250.50")), ("350", Decimal(350)), ("", None), ("  ", None)])
def test_precio_de(texto, precio):
    assert web.precio_de(texto) == precio


def test_encabezados_de():
    assert web.encabezados_de("Authorization: Bearer abc\nX-Tienda: 1") == {"Authorization": "Bearer abc", "X-Tienda": "1"}
    with pytest.raises(web.Rechazo):
        web.encabezados_de("Mal Nombre: x")


def test_sin_tavily_solo_url_pide_texto(cliente, servicio, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert 'name="urls"' not in cliente.get("/app/nuevo").text
    r = cliente.post("/probar", data={"texto": "", "urls": "https://negocio.example"})
    assert r.status_code == 400 and "no lee URLs" in r.text
    assert servicio.indice.listar() == []


def test_con_tavily_muestra_urls(cliente, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-prueba")
    assert 'name="urls"' in cliente.get("/app/nuevo").text


@pytest.mark.parametrize("endpoint,modelo,visible", [
    ("https://api.tokenfactory.nebius.com/v1", "nvidia/Nemotron-3-Ultra-550b-a55b", True),
    ("http://127.0.0.1:8081/v1", "nvidia/Nemotron-3-Ultra-550b-a55b", False),
    ("https://api.tokenfactory.nebius.com.invalid/v1", "nvidia/Nemotron-3-Ultra-550b-a55b", False),
    ("https://api.tokenfactory.nebius.com/v1", "ornith-1.5-35b", False),
])
def test_inicio_solo_atribuye_nebius_nemotron_a_config_real(cliente, servicio, endpoint, modelo, visible):
    from fantasma.motor.config import Proveedor

    servicio.config_base.proveedores["demo"] = Proveedor(base_url=endpoint)
    for rol in servicio.config_base.roles.values():
        rol.proveedor = "demo"
        rol.modelo = modelo
    respuesta = cliente.get("/app/nuevo")
    assert respuesta.status_code == 200
    assert ("NVIDIA Nemotron · Nebius Token Factory" in respuesta.text) is visible
