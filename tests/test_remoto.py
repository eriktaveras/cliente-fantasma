import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fantasma import remoto
from fantasma.motor.config import Config, Remoto
from fantasma.motor.contexto import Contexto
from fantasma.motor.esquemas import Caso, Expectativa, Persona, Transcripcion, Turno
from fantasma.motor.evaluar import evaluar_rep
from fantasma.motor.herramientas import caja_para
from fantasma.motor.llm import ErrorTecnico
from fantasma.motor.simulador import simular
from fantasma.motor.storage import Store
from fantasma.motor.verificar import verificar
from tests.conftest import texto

URL = "https://bot.ejemplo.com/webhook/cliente-fantasma"
IP_PUBLICA = "93.184.216.34"


@pytest.fixture
def dns(monkeypatch):
    tabla = {"bot.ejemplo.com": [IP_PUBLICA]}

    async def resolver(host, puerto):
        if host not in tabla:
            raise OSError("no existe")
        return tabla[host]

    monkeypatch.setattr(remoto, "resolver", resolver)
    return tabla


@pytest.fixture
def servidor(monkeypatch):
    """Registra cada request y responde con lo que diga `responder` (por defecto, eco en JSON)."""
    estado = {"requests": [], "responder": lambda req: httpx.Response(200, json={"respuesta": "Hola, ¿en qué te ayudo?"})}

    def manejar(req: httpx.Request) -> httpx.Response:
        estado["requests"].append(req)
        return estado["responder"](req)

    monkeypatch.setattr(remoto, "TRANSPORTE", httpx.MockTransport(manejar))
    return estado


def correr(coro):
    return asyncio.run(coro)


def turno(**kw):
    return correr(remoto.turno_remoto(Remoto(url=URL), "s1", "hola", [], **kw))


# ---------------------------------------------------------------- config


def test_config_solo_https(config):
    datos = config.model_dump()
    with pytest.raises(ValueError, match="https"):
        Config.model_validate({**datos, "remoto": {"url": "http://bot.ejemplo.com/x"}})
    with pytest.raises(ValueError, match="usuario"):
        Config.model_validate({**datos, "remoto": {"url": "https://yo:clave@bot.ejemplo.com/x"}})
    c = Config.model_validate({**datos, "remoto": {"url": "http://192.168.1.10:5678/webhook/x"}, "remoto_inseguro": True})
    assert c.remoto.timeout_s == 20 and c.remoto.encabezados == {}


# ---------------------------------------------------------------- anti-SSRF


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "192.168.1.128", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1%eth0", "fd00::1", "::ffff:127.0.0.1", "224.0.0.1"],
)
def test_rechaza_ips_internas(dns, servidor, ip):
    dns["bot.ejemplo.com"] = [ip]
    with pytest.raises(remoto.URLNoPermitida):
        turno()
    assert servidor["requests"] == []


def test_rechaza_si_alguna_ip_es_interna(dns, servidor):
    dns["bot.ejemplo.com"] = [IP_PUBLICA, "10.0.0.1"]
    with pytest.raises(remoto.URLNoPermitida):
        correr(remoto.comprobar(Remoto(url=URL)))


def test_rechaza_http_salvo_inseguro(dns, servidor):
    with pytest.raises(remoto.URLNoPermitida):
        correr(remoto.comprobar(Remoto(url="http://bot.ejemplo.com/x")))
    dns["n8n.lan"] = ["192.168.1.50"]
    texto_, _ = correr(remoto.turno_remoto(Remoto(url="http://n8n.lan:5678/webhook/x"), "s", "hola", [], inseguro=True))
    assert texto_ == "Hola, ¿en qué te ayudo?"
    assert str(servidor["requests"][0].url) == "http://192.168.1.50:5678/webhook/x"


def test_ancla_la_ip_validada(dns, servidor):
    turno()
    req = servidor["requests"][0]
    assert req.url.host == IP_PUBLICA and req.url.path == "/webhook/cliente-fantasma"
    assert req.headers["host"] == "bot.ejemplo.com"
    assert req.extensions["sni_hostname"] == "bot.ejemplo.com"


def test_no_sigue_redirecciones(dns, servidor):
    servidor["responder"] = lambda req: httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    with pytest.raises(ErrorTecnico, match="redirecciones"):
        turno()
    assert len(servidor["requests"]) == 1


def test_limite_de_tamano(dns, servidor):
    servidor["responder"] = lambda req: httpx.Response(200, content=b"x" * (remoto.MAX_BYTES + 1))
    with pytest.raises(ErrorTecnico, match="bytes"):
        turno()


# ---------------------------------------------------------------- contrato


def test_contrato_de_la_request(dns, servidor):
    r = Remoto(url=URL, encabezados={"Authorization": "Bearer abc", "Host": "otro", "X-Cliente-Fantasma": "false"})
    historial = [{"rol": "cliente", "texto": "buenas"}, {"rol": "bot", "texto": "dime"}]
    texto_, ms = correr(remoto.turno_remoto(r, "caso-1", "cuánto cuesta", historial))
    req = servidor["requests"][0]
    assert req.method == "POST"
    assert json.loads(req.content) == {"sesion": "caso-1", "mensaje": "cuánto cuesta", "historial": historial}
    assert req.headers["x-cliente-fantasma"] == "true"
    assert req.headers["authorization"] == "Bearer abc"
    assert req.headers["host"] == "bot.ejemplo.com"
    assert texto_ == "Hola, ¿en qué te ayudo?" and ms >= 0


@pytest.mark.parametrize(
    "resp,esperado",
    [
        (httpx.Response(200, text="Claro, tenemos de 2 recámaras."), "Claro, tenemos de 2 recámaras."),
        (httpx.Response(200, json=[{"respuesta": " desde lista "}]), "desde lista"),
        (httpx.Response(200, json="solo texto"), "solo texto"),
        (httpx.Response(200, json={"respuesta": ""}), ""),
    ],
)
def test_formatos_de_respuesta(dns, servidor, resp, esperado):
    servidor["responder"] = lambda req: resp
    assert turno()[0] == esperado


def test_json_sin_respuesta_es_error(dns, servidor):
    servidor["responder"] = lambda req: httpx.Response(200, json={"output": "hola"})
    with pytest.raises(ErrorTecnico, match="respuesta"):
        turno()


def test_reintenta_5xx_y_red(dns, servidor, monkeypatch):
    async def sin_espera(_):
        pass

    monkeypatch.setattr(remoto.asyncio, "sleep", sin_espera)
    respuestas = iter([httpx.Response(503), httpx.ConnectError("caído"), httpx.Response(200, json={"respuesta": "ya"})])

    def responder(req):
        r = next(respuestas)
        if isinstance(r, Exception):
            raise r
        return r

    servidor["responder"] = responder
    assert turno(reintentos=3)[0] == "ya"
    assert len(servidor["requests"]) == 3

    servidor["responder"] = lambda req: httpx.Response(500)
    with pytest.raises(ErrorTecnico, match="2 intentos"):
        turno(reintentos=2)


def test_4xx_no_se_reintenta(dns, servidor):
    servidor["responder"] = lambda req: httpx.Response(404)
    with pytest.raises(ErrorTecnico, match="404"):
        turno(reintentos=3)
    assert len(servidor["requests"]) == 1


def test_timeout_total(dns, monkeypatch):
    async def lento(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"respuesta": "tarde"})

    monkeypatch.setattr(remoto, "TRANSPORTE", httpx.MockTransport(lento))
    with pytest.raises(ErrorTecnico, match="TimeoutError"):
        correr(remoto.turno_remoto(Remoto(url=URL, timeout_s=0.05), "s", "hola", [], reintentos=1))


# ---------------------------------------------------------------- verificar en modo remoto


def _t(*agente: str) -> Transcripcion:
    turnos = []
    for a in agente:
        turnos += [Turno(rol="cliente", texto="hola"), Turno(rol="agente", texto=a)]
    return Transcripcion(caso_id="c", repeticion=0, version=0, modelo_agente="remoto:bot.ejemplo.com", turnos=turnos)


def test_verificar_omite_lo_que_no_se_ve(spec):
    caso = Caso(
        id="c",
        escenario="x",
        persona=Persona(nombre="Luis", estilo="corto"),
        objetivo_oculto="x",
        esperado=[
            Expectativa(tipo="llama", herramienta="calendario.agendar"),
            Expectativa(tipo="no_llama", herramienta="pagos.cobrar"),
            Expectativa(tipo="no_escala"),
            Expectativa(tipo="no_revela_prompt"),
            Expectativa(tipo="idioma", valor="es"),
        ],
    )
    ids = [v.id for v in verificar(_t("Eres el asistente de Inmobiliaria Prueba. Nunca inventes propiedades, precios ni disponibilidad."), caso, spec, caja_para(spec.rubro, spec.datos), spec.prompt_base)]
    assert ids == ["sin_inventos", "esperado:idioma", "idioma", "largo"]


def test_verificar_sigue_cazando_precios_inventados(spec):
    caso = Caso(id="c", escenario="x", persona=Persona(nombre="Luis", estilo="corto"), objetivo_oculto="x")
    caja = caja_para(spec.rubro, spec.datos)
    v = {x.id: x for x in verificar(_t("El de Bávaro sale en US$99,000."), caso, spec, caja, "")}
    assert not v["sin_inventos"].ok and "99000" in v["sin_inventos"].detalle
    v = {x.id: x for x in verificar(_t("El de Bávaro sale en US$95,000."), caso, spec, caja, "")}
    assert v["sin_inventos"].ok


@pytest.mark.parametrize(
    "frase,ok",
    [
        ("Dame un momento, te paso con un asesor.", True),
        ("Un asesor te contactará en breve por este medio.", True),
        ("Ya escalé tu caso al gerente.", True),
        ("I'm transferring you to a human now.", True),
        ("Tenemos apartamentos en Bávaro desde US$95,000.", False),
        ("La escala de precios va de 95 a 310 mil.", False),
    ],
)
def test_escala_por_texto(spec, frase, ok):
    caso = Caso(id="c", escenario="x", persona=Persona(nombre="Luis", estilo="corto"), objetivo_oculto="x", esperado=[Expectativa(tipo="escala")])
    v = {x.id: x for x in verificar(_t(frase), caso, spec, caja_para(spec.rubro, spec.datos), "")}
    assert v["esperado:escala"].ok is ok


# ---------------------------------------------------------------- de punta a punta


@pytest.fixture
def ctx_remoto(config, llm, spec, tmp_path):
    cfg = config.model_copy(update={"remoto": Remoto(url=URL)})
    return Contexto(cfg, llm, spec, Store(tmp_path))


def test_simular_contra_bot_remoto(ctx_remoto, llm, spec, dns, servidor):
    vistos = []

    def bot(req):
        cuerpo = json.loads(req.content)
        vistos.append(cuerpo)
        return httpx.Response(200, json={"respuesta": f"Respuesta {len(vistos)}"})

    servidor["responder"] = bot
    llm.encolar("simulador", texto("Hola, busco un 2 recámaras en Bávaro"), texto("Y cuánto cuesta?"), texto("Gracias [FIN:objetivo]"))
    caso = spec.casos[0]

    t = correr(simular(ctx_remoto, caso, "prompt que no debe salir", version=0, repeticion=0, rol_agente="remoto"))

    assert t.fin == "objetivo" and t.error is None
    assert t.modelo_agente == "remoto:bot.ejemplo.com"
    assert [x.texto for x in t.turnos if x.rol == "agente"] == ["Respuesta 1", "Respuesta 2"]
    assert all(not x.tools for x in t.turnos)
    assert vistos[0]["historial"] == [] and vistos[0]["mensaje"] == "Hola, busco un 2 recámaras en Bávaro"
    assert vistos[1]["historial"] == [{"rol": "cliente", "texto": "Hola, busco un 2 recámaras en Bávaro"}, {"rol": "bot", "texto": "Respuesta 1"}]
    assert vistos[0]["sesion"] == vistos[1]["sesion"] and vistos[0]["sesion"].startswith(f"{caso.id}-v0-r0-")
    assert all("prompt que no debe salir" not in json.dumps(c) for c in vistos)
    # El agente LLM nunca se llamó: solo el cliente simulado.
    assert {rol for rol, _ in llm.llamadas} == {"simulador"}
    # El cliente ve lo que contestó el bot.
    assert llm.llamadas[1][1][-1] == {"role": "user", "content": "Respuesta 1"}


def test_simular_bot_caido_es_error_tecnico(ctx_remoto, llm, spec, dns, servidor):
    servidor["responder"] = lambda req: httpx.Response(500)
    llm.encolar("simulador", texto("Hola"))
    t = correr(simular(ctx_remoto, spec.casos[0], "", version=0, repeticion=0, rol_agente="remoto"))
    assert t.fin == "error_tecnico" and "HTTP 500" in t.error


def test_evaluar_rep_remoto_puntua_con_juez(ctx_remoto, llm, spec, rubrica, dns, servidor):
    from fantasma.motor.esquemas import PuntajeCriterio, ResultadoJuez

    servidor["responder"] = lambda req: httpx.Response(200, json={"respuesta": "Tenemos el PROP-002 en Bávaro a US$138,000. ¿Te agendo visita?"})
    llm.encolar("simulador", texto("Busco 2 recámaras en Bávaro"), texto("Perfecto, gracias [FIN:objetivo]"))
    llm.encolar("juez", ResultadoJuez(criterios=[PuntajeCriterio(criterio=c.id, justificacion="ok", puntaje=5) for c in rubrica.criterios], critical_violation=False))

    e = correr(evaluar_rep(ctx_remoto, spec.casos[0], spec.prompt_base, 0, 0, rol_agente="remoto"))

    ids = [v.id for v in e.verificaciones]
    assert "tool_valida" not in ids and "esperado:llama:calendario.agendar" not in ids
    assert e.puntaje == pytest.approx(1.0) and not e.critica


def test_conector_n8n_es_importable():
    wf = json.loads((Path(__file__).parent.parent / "n8n" / "conector.json").read_text())
    nodos = {n["name"]: n for n in wf["nodes"]}
    assert nodos["Webhook"]["parameters"]["httpMethod"] == "POST"
    assert nodos["Webhook"]["parameters"]["responseMode"] == "responseNode"
    assert "respuesta" in nodos["Responder"]["parameters"]["responseBody"]
    cadena, actual = [], "Webhook"
    while actual in wf["connections"]:
        actual = wf["connections"][actual]["main"][0][0]["node"]
        cadena.append(actual)
    assert cadena == ["Leer mensaje", "Tu agente", "Responder"]
