import hashlib
import hmac
import sqlite3

import pytest
from fastapi.testclient import TestClient

from fantasma import app as web
from fantasma.cuentas import huella
from tests.test_app import cliente, servicio, crear, lanzar, ORIGEN  # noqa: F401

CLAVE = "una-frase-larga-para-pruebas"


def registrar(c, email="ana@example.com", **datos):
    return c.post("/registro", data={"nombre": "Ana", "email": email, "clave": CLAVE, "confirmar": CLAVE, **datos}, follow_redirects=False)


def visitante(cliente, https=False):
    return TestClient(cliente.app, base_url="https://testserver" if https else "http://testserver",
                      headers={"origin": "https://testserver" if https else "http://testserver"})


def test_portada_publica_y_panel_privado(cliente):
    c = visitante(cliente)
    r = c.get("/")
    assert r.status_code == 200 and "Tres recorridos" in r.text
    assert 'action="/probar"' not in r.text
    assert c.get("/como-funciona").status_code == 200
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login?")
    assert c.get("/corrida/20261014-deadbeef/estado").status_code == 401
    assert c.post("/probar", data={}).status_code == 401


def test_registro_persistencia_y_hashes(cliente, servicio):
    c = visitante(cliente)
    r = registrar(c)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    token = c.cookies.get("fantasma_sesion")
    fila = cliente.app.state.cuentas.por_email("ana@example.com")
    assert fila["clave_hash"].startswith("$argon2id$") and CLAVE not in fila["clave_hash"]
    assert servicio.indice.db.execute("SELECT 1 FROM sesiones WHERE token_hash = ?", (huella(token),)).fetchone()
    assert servicio.indice.db.execute("SELECT 1 FROM sesiones WHERE token_hash = ?", (token,)).fetchone() is None
    assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=lax' in r.headers['set-cookie']
    assert "Ana" in c.get("/app").text
    fresh = visitante(cliente)
    fresh.cookies.set("fantasma_sesion", token)
    assert fresh.get("/app").status_code == 200
    assert c.get("/login", follow_redirects=False).status_code == 303


def test_login_logout_revoca_y_no_confia_en_basic(cliente):
    c = visitante(cliente)
    registrar(c)
    token = c.cookies.get("fantasma_sesion")
    assert c.post("/logout", data={}, follow_redirects=False).status_code == 303
    c.cookies.set("fantasma_sesion", token, domain="testserver.local")
    assert c.get("/app", follow_redirects=False).status_code == 303
    assert c.get("/app", auth=("ana", CLAVE), follow_redirects=False).status_code == 303
    assert c.post("/login", data={"email": "ana@example.com", "clave": "incorrecta"}).status_code == 400
    r = c.post("/login", data={"email": "ANA@EXAMPLE.COM", "clave": CLAVE}, follow_redirects=False)
    assert r.status_code == 303 and c.cookies.get("fantasma_sesion") != token


@pytest.mark.parametrize("volver", ["https://evil.example", "//evil.example", "/\\evil.example", "/login", "/registro"])
def test_login_no_redirige_fuera_del_panel(cliente, volver):
    c = visitante(cliente)
    assert registrar(c, volver=volver).headers["location"] == "/app"


def test_camino_de_landing_se_conserva(cliente):
    c = visitante(cliente)
    assert registrar(c, volver="/app/nuevo?modo=prompt").headers["location"] == "/app/nuevo?modo=prompt"
    assert 'value="prompt" checked' in c.get("/app/nuevo?modo=prompt").text


def test_cuenta_ajena_no_accede_a_corrido_ni_firma_antigua(cliente, servicio):
    rid = crear(cliente)
    otro = visitante(cliente)
    registrar(otro)
    for path in ["ficha", "boleta", "vivo", "eventos", "estado", "conversaciones.jsonl"]:
        assert otro.get(f"/corrida/{rid}/{path}").status_code == 404
    valor = rid
    firma = hmac.new(cliente.app.state.secreto, valor.encode(), hashlib.sha256).hexdigest()[:32]
    otro.cookies.set(web.COOKIE_MIAS, f"{valor}.{firma}")
    otro.post("/logout", data={})
    otro.cookies.set(web.COOKIE_MIAS, f"{valor}.{firma}")
    otro.post("/login", data={"email": "ana@example.com", "clave": CLAVE})
    assert otro.get(f"/corrida/{rid}/ficha").status_code == 404
    assert servicio.indice.obtener(rid) is not None
    assert "Colmado La Esquina" not in otro.get("/app").text
    assert "Colmado La Esquina" in cliente.get("/app").text


def test_cookie_anonima_migra_solo_con_firma_valida(cliente, servicio):
    rid = servicio.indice.crear("20261015-deadbeef", "anonimo", estado="ficha").id
    c = visitante(cliente)
    firma = hmac.new(cliente.app.state.secreto, rid.encode(), hashlib.sha256).hexdigest()[:32]
    c.cookies.set(web.COOKIE_MIAS, f"{rid}.{firma}", domain="testserver.local")
    registrar(c)
    usuario = cliente.app.state.cuentas.usuario(c.cookies.get("fantasma_sesion"))
    assert cliente.app.state.cuentas.pertenece(usuario["id"], rid)
    assert web.COOKIE_MIAS not in c.cookies


def test_cambio_clave_revoca_todas_las_sesiones(cliente):
    c = visitante(cliente)
    registrar(c)
    otra = visitante(cliente)
    otra.post("/login", data={"email": "ana@example.com", "clave": CLAVE})
    nuevo = "otra-clave-bastante-larga"
    r = c.post("/cuenta/clave", data={"actual": CLAVE, "nueva": nuevo, "confirmar": nuevo}, follow_redirects=False)
    assert r.status_code == 303 and "guardada=1" in r.headers["location"]
    assert c.get("/cuenta").status_code == 200
    assert otra.get("/app", follow_redirects=False).status_code == 303
    assert otra.post("/login", data={"email": "ana@example.com", "clave": CLAVE}).status_code == 400
    assert otra.post("/login", data={"email": "ana@example.com", "clave": nuevo}, follow_redirects=False).status_code == 303


def test_auth_rechaza_csrf_y_limita_intentos(cliente):
    c = visitante(cliente)
    assert registrar(c, email="x@example.com", **{}).status_code == 303
    c.post("/logout", data={})
    bad = c.post("/login", data={"email": "x@example.com", "clave": CLAVE}, headers={"origin": "https://evil.example"})
    assert bad.status_code == 403
    for _ in range(10):
        assert c.post("/login", data={"email": "nobody@example.com", "clave": "incorrecta"}).status_code == 400
    assert c.post("/login", data={"email": "nobody@example.com", "clave": "incorrecta"}).status_code == 429


def test_sesion_https_y_expiracion(cliente, servicio):
    c = visitante(cliente, https=True)
    r = registrar(c)
    assert 'Secure' in r.headers['set-cookie'] and '__Host-fantasma_sesion=' in r.headers['set-cookie']
    assert c.get("/app").status_code == 200
    token = c.cookies.get("__Host-fantasma_sesion")
    servicio.indice.db.execute("UPDATE sesiones SET expira = 0 WHERE token_hash = ?", (huella(token),))
    assert c.get("/app", follow_redirects=False).status_code == 303


def test_registro_validaciones_no_guarda_clave_en_html(cliente):
    c = visitante(cliente)
    r = registrar(c, clave="corta", confirmar="corta")
    assert r.status_code == 400 and 'value="corta"' not in r.text
    assert registrar(c, email="no-es-correo").status_code == 400
    assert registrar(c, confirmar="diferente").status_code == 400
    assert registrar(c).status_code == 303
    otro = visitante(cliente)
    assert registrar(otro).status_code == 400
    assert otro.get("/app", follow_redirects=False).status_code == 303


def test_dashboard_metricas_y_filtros(cliente, servicio):
    rid = crear(cliente)
    assert rid in cliente.get("/app").text
    assert rid in cliente.get("/app?q=Colmado").text
    assert rid not in cliente.get("/app?q=no-existe").text
    assert rid not in cliente.get("/app?estado=terminada").text
    assert rid in cliente.get("/app?estado=ficha").text


def test_ficha_fallida_tambien_pertenece_a_la_cuenta(cliente, servicio, monkeypatch):
    from fantasma.motor.llm import ErrorTecnico
    from tests.test_servicio import COLMADO

    async def fallar(*args, **kwargs):
        raise ErrorTecnico("fallo de prueba")

    monkeypatch.setattr("fantasma.servicio.investigar", fallar)
    r = cliente.post("/probar", data={"modo": "generado", "texto": COLMADO, "nombre": "Prueba interrumpida"})
    assert r.status_code == 502
    corrida = servicio.indice.listar()[0]
    assert corrida.estado == "error"
    usuario = cliente.app.state.cuentas.usuario(cliente.cookies.get("fantasma_sesion"))
    assert cliente.app.state.cuentas.pertenece(usuario["id"], corrida.id)
    assert "Prueba interrumpida" in cliente.get("/app").text



def test_demo_solo_lectura_no_crea_cuentas_ni_corridas(cliente, servicio, monkeypatch):
    c = visitante(cliente)
    rid = crear(cliente)
    token = servicio.estado(rid)["token_publico"]
    monkeypatch.setenv("FANTASMA_DEMO_TOKEN", token)
    db = servicio.indice.db
    antes = (len(servicio.indice.listar()), db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0])
    def prohibido(*args, **kwargs):
        pytest.fail("La demo no puede ejecutar inferencia")
    monkeypatch.setattr(servicio, "crear_ficha", prohibido)
    monkeypatch.setattr(servicio, "lanzar", prohibido)
    for _ in range(3):
        r = c.get("/demo", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/b/" + token
        assert "set-cookie" not in r.headers
    assert c.post("/demo", data={}).status_code == 401
    assert antes == (len(servicio.indice.listar()), db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0])


def test_demo_sin_configuracion_no_ejecuta(cliente, monkeypatch):
    monkeypatch.delenv("FANTASMA_DEMO_TOKEN", raising=False)
    assert visitante(cliente).get("/demo", follow_redirects=False).headers["location"] == "/#recorridos"



def test_registro_permite_usar_la_app_sin_aprobacion(cliente, servicio):
    with visitante(cliente) as c:
        registrar(c)
        assert "pendiente de aprobación" not in c.get("/app").text
        assert 'disabled data-enviar' not in c.get("/app/nuevo").text
        rid = crear(c)
        assert c.get(f"/corrida/{rid}/ficha").status_code == 200
        lanzar(c, servicio, rid)
        assert servicio.estado(rid)["estado"] in ("terminada", "presupuesto")


def test_cuenta_existente_pendiente_funciona_con_login(cliente, servicio):
    # Los índices de la versión anterior conservan esta columna: ya no condiciona el acceso.
    db = servicio.indice.db
    db.execute("ALTER TABLE usuarios ADD COLUMN puede_ejecutar INTEGER NOT NULL DEFAULT 0")
    with visitante(cliente) as c:
        registrar(c)
        assert cliente.app.state.cuentas.por_email("ana@example.com")["puede_ejecutar"] == 0
        c.post("/logout", data={})
        assert c.post("/probar", data={}).status_code == 401
        r = c.post("/login", data={"email": "ana@example.com", "clave": CLAVE}, follow_redirects=False)
        assert r.status_code == 303
        rid = crear(c)
        lanzar(c, servicio, rid)
        assert servicio.estado(rid)["estado"] in ("terminada", "presupuesto")
