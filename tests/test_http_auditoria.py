import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from fantasma import app as web
from tests.test_app import cliente, crear, servicio  # noqa: F401
from tests.test_servicio import COLMADO


def request(ip, **headers):
    return Request({"type": "http", "method": "GET", "path": "/", "scheme": "http", "server": ("localhost", 80),
                    "client": (ip, 1234), "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})


def test_ip_y_esquema_solo_del_proxy_configurado(monkeypatch):
    monkeypatch.setenv("FANTASMA_PROXY_CIDRS", "127.0.0.1/32,::1/128")
    headers = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-proto": "https"}
    directa = request("192.168.1.22", **headers)
    proxy = request("127.0.0.1", **headers)
    assert web.ip_de(directa) == "192.168.1.22" and not web.es_https(directa)
    assert web.ip_de(proxy) == "203.0.113.9" and web.es_https(proxy)
    assert web.ip_de(request("127.0.0.1", **{"cf-connecting-ip": "no-es-ip"})) == "127.0.0.1"


def test_cookie_https_canonica_no_rompe_acceso_lan(monkeypatch):
    monkeypatch.setenv("FANTASMA_URL_PUBLICA", "https://fantasma.example.com")
    assert web.es_https(request("172.17.0.1", host="fantasma.example.com"))
    assert not web.es_https(request("172.17.0.1", host="192.168.1.165:8134"))


@pytest.mark.parametrize("url", ["https://example.com:notaport", "https://example.com:0", "https://example.com:65536", "https://[invalid", "https://yo:clave@example.com"])
def test_url_invalida_es_400_sin_gasto(cliente, servicio, url):
    r = cliente.post("/probar", data={"texto": COLMADO, "modo": "remoto", "url_bot": url})
    assert r.status_code == 400
    assert servicio.indice.listar() == []


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")], [(b"transfer-encoding", b"chunked")]])
def test_limite_cuenta_cuerpo_real_antes_del_handler(headers, monkeypatch):
    monkeypatch.delenv("FANTASMA_CLAVE", raising=False)
    llamadas = []
    respuestas = []
    trozos = iter([{"type": "http.request", "body": b"x" * web.MAX_CUERPO, "more_body": True},
                   {"type": "http.request", "body": b"x", "more_body": False}])

    async def recibir():
        return next(trozos)

    async def enviar(mensaje):
        respuestas.append(mensaje)

    async def handler(*args):
        llamadas.append(True)

    scope = {"type": "http", "method": "POST", "path": "/probar", "scheme": "http", "query_string": b"",
             "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver"), *headers]}
    asyncio.run(web.Guardia(handler)(scope, recibir, enviar))
    assert respuestas[0]["status"] == 413 and not llamadas


def test_boleta_compartida_sin_clave_no_abre_rutas_privadas(cliente, servicio, monkeypatch):
    run_id = crear(cliente)
    from tests.test_app import lanzar
    lanzar(cliente, servicio, run_id)
    token = servicio.indice.obtener(run_id).token_publico
    monkeypatch.setenv("FANTASMA_CLAVE", "prueba-auditoria")
    with TestClient(cliente.app, headers={"origin": "http://testserver"}) as visitante:
        r = visitante.get(f"/b/{token}")
        assert r.status_code == 200 and "Cache-Control" in r.headers and r.headers["cache-control"] == "no-store"
        assert visitante.get("/static/js/fantasma.js").status_code == 200
        assert visitante.get("/").status_code == 200
        assert visitante.get(f"/corrida/{run_id}/estado").status_code == 401
        assert visitante.post(f"/b/{token}", data={}).status_code == 401
        assert visitante.get("/app", auth=("usuario", "prueba-auditoria"), follow_redirects=False).status_code == 303


def test_cabeceras_seguridad_tambien_en_rechazos(cliente, monkeypatch):
    monkeypatch.setenv("FANTASMA_CLAVE", "prueba")
    r = TestClient(cliente.app).get("/corrida/no-existe/estado")
    assert r.status_code == 401
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_cobertura_incompleta_visible_en_boleta(cliente, servicio):
    from tests.test_app import lanzar
    run_id = crear(cliente)
    lanzar(cliente, servicio, run_id)
    servicio.indice.actualizar(run_id, estado="error", motivo="interrumpida")
    r = cliente.get(f"/corrida/{run_id}/boleta")
    assert r.status_code == 200 and "Evaluación incompleta" in r.text
    assert "Cobertura de la evaluación" in r.text
