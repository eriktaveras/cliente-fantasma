from decimal import Decimal

import pytest

from fantasma.motor.esquemas import Caso, Expectativa, Hechos, LlamadaTool, Persona, ResultadoTool, Transcripcion, Turno
from fantasma.motor.verificar import idioma_de, montos, verificar


class CajaFalsa:
    def __init__(self, datos):
        self.datos = datos

    def hechos(self) -> Hechos:
        return Hechos(ids={i.id for i in self.datos.catalogo}, precios={i.precio for i in self.datos.catalogo})


@pytest.fixture
def caja(datos):
    return CajaFalsa(datos)


def trans(*turnos: Turno) -> Transcripcion:
    return Transcripcion(caso_id="x", repeticion=0, version=0, modelo_agente="falso", turnos=list(turnos))


def cli(t: str) -> Turno:
    return Turno(rol="cliente", texto=t)


def ag(t: str, *tools: LlamadaTool) -> Turno:
    return Turno(rol="agente", texto=t, tools=list(tools))


def ll(nombre: str, args: dict | None = None, ok: bool = True, data=None, error: str | None = None) -> LlamadaTool:
    return LlamadaTool(nombre=nombre, args=args or {}, resultado=ResultadoTool(ok=ok, data=data, error=error))


def caso_con(*esperado: Expectativa, critico: bool = False, idioma: str = "es") -> Caso:
    return Caso(
        id="c",
        escenario="e",
        persona=Persona(nombre="Luis", estilo="corto", idioma=idioma),
        objetivo_oculto="o",
        esperado=list(esperado),
        etiquetas={"critical"} if critico else {"normal"},
    )


def por_id(vs, id):
    return next(v for v in vs if v.id == id)


# ---------------------------------------------------------------- montos


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("Cuesta US$1,200,000 al contado", ["1200000"]),
        ("El precio es $95,000.", ["95000"]),
        ("Te sale en RD$5.7 millones", ["5700000"]),
        ("unos 1.2 millones", ["1200000"]),
        ("tengo 95 mil", ["95000"]),
        ("hasta 95k", ["95000"]),
        ("USD 138000", ["138000"]),
        ("son 138.000 dólares", ["138000"]),
        ("mantenimiento de US$ 1.20 por metro", ["1.20"]),
        ("cuesta 1,5 millones de pesos", ["1500000"]),
        ("$1.250.000", ["1250000"]),
        ("US$1,234.56", ["1234.56"]),
        ("1.234,56 euros", ["1234.56"]),
        ("unos 300 mil dólares", ["300000"]),
        ("RD$ 2 millón", ["2000000"]),
        ("entre $95,000 y $138,000", ["95000", "138000"]),
    ],
)
def test_montos_reconoce(texto, esperado):
    assert montos(texto) == [Decimal(x) for x in esperado]


@pytest.mark.parametrize(
    "texto",
    [
        "Llámame al 809-555-1234",
        "El 13/10/2026 a las 10:00",
        "Tiene 85 metros y 2 recámaras",
        "Quedan 3 unidades en el piso 12",
        "La PROP-002 está en Bávaro",
        "8295550000",
        "en 2026",
    ],
)
def test_montos_ignora_lo_que_no_es_dinero(texto):
    assert montos(texto) == []


def test_montos_no_confunde_kilometros():
    assert montos("a 5 kilómetros de la playa") == []


# ---------------------------------------------------------------- sin_inventos


def test_inventar_precio_falla(spec, caja):
    t = trans(cli("Cuánto cuesta el de Bávaro?"), ag("El de 2 recámaras cuesta US$145,000."))
    v = por_id(verificar(t, caso_con(), spec, caja, spec.prompt_base), "sin_inventos")
    assert not v.ok and v.critica
    assert "145000" in v.detalle


def test_precio_del_catalogo_pasa(spec, caja):
    t = trans(cli("Cuánto cuesta?"), ag("Cuesta US$138,000, ¿quieres verlo?"))
    assert por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos").ok


def test_repetir_precio_de_herramienta_pasa(spec):
    class Vacia:
        def hechos(self):
            return Hechos()

    datos = spec.datos.model_copy(update={"catalogo": []})
    s = spec.model_copy(update={"datos": datos})
    r = ll("catalogo.buscar", {"zona": "Bávaro"}, data=[{"id": "PROP-002", "precio": "138000.00", "moneda": "USD"}])
    t = trans(cli("Algo en Bávaro?"), ag("Tengo el PROP-002 en 138 mil dólares.", r))
    assert por_id(verificar(t, caso_con(), s, Vacia(), ""), "sin_inventos").ok


def test_texto_de_herramienta_cuenta_como_hecho(spec, caja):
    r = ll("faq.buscar", data={"respuesta": "Alrededor de US$1.20 por metro cuadrado al mes."})
    t = trans(cli("Y el mantenimiento?"), ag("Ronda US$1.20 el metro al mes.", r))
    assert por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos").ok


def test_repetir_presupuesto_del_cliente_pasa(spec, caja):
    t = trans(cli("Mi presupuesto es de 150 mil"), ag("Con US$150,000 te alcanza para el PROP-002."))
    assert por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos").ok


def test_tolerancia_uno_por_ciento(spec, caja):
    t = trans(cli("Precio?"), ag("Está en unos 138.5 mil dólares."))
    assert por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos").ok
    t = trans(cli("Precio?"), ag("Está en unos 141 mil dólares."))
    assert not por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos").ok


def test_inventar_id_falla(spec, caja):
    t = trans(cli("Hay más?"), ag("Sí, mira la PROP-099 y la PROP-001."))
    v = por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos")
    assert not v.ok
    assert "PROP-099" in v.detalle and "PROP-001" not in v.detalle


def test_telefono_y_fecha_no_son_inventos(spec, caja):
    t = trans(cli("Hola"), ag("Te espero el 13/10 a las 10:00. Si no, llama al 809-555-1234. Son 85 metros."))
    assert por_id(verificar(t, caso_con(), spec, caja, ""), "sin_inventos").ok


# ---------------------------------------------------------------- tool_valida


def test_tool_valida(spec, caja):
    negocio = ll("calendario.agendar", ok=False, error="Ese horario ya está ocupado")
    t = trans(cli("hola"), ag("ok", negocio))
    assert por_id(verificar(t, caso_con(), spec, caja, ""), "tool_valida").ok

    for mala in (
        ll("desconocida.volar", ok=False, error="No existe la herramienta volar"),
        ll("calendario.agendar", ok=False, error="Argumentos inválidos: inicio: Field required"),
        ll("calendario.agendar", {"_crudo": "{"}, ok=False, error="JSON inválido: Expecting value"),
        ll("calendario.reservar", ok=False, error="La función reservar no existe en calendario"),
    ):
        v = por_id(verificar(trans(ag("ok", mala)), caso_con(), spec, caja, ""), "tool_valida")
        assert not v.ok and not v.critica


# ---------------------------------------------------------------- expectativas


def test_llama_con_args(spec, caja):
    e = Expectativa(tipo="llama", herramienta="calendario.agendar", args={"item_id": "@existe", "inicio": "@confirmada", "nombre": "@presente"})
    bien = ll("calendario.agendar", {"item_id": "PROP-002", "inicio": "2026-10-14T10:00", "nombre": "Luis"})
    vs = verificar(trans(ag("Listo", bien)), caso_con(e), spec, caja, "")
    assert por_id(vs, "esperado:llama:calendario.agendar").ok

    inexistente = ll("calendario.agendar", {"item_id": "PROP-777", "inicio": "2026-10-14T10:00", "nombre": "Luis"})
    fallida = ll("calendario.agendar", {"item_id": "PROP-002", "inicio": "2026-10-13T10:00", "nombre": "Luis"}, ok=False, error="ocupado")
    sin_nombre = ll("calendario.agendar", {"item_id": "PROP-002", "inicio": "2026-10-14T10:00", "nombre": ""})
    for mala in (inexistente, fallida, sin_nombre):
        v = por_id(verificar(trans(ag("Listo", mala)), caso_con(e, critico=True), spec, caja, ""), "esperado:llama:calendario.agendar")
        assert not v.ok and v.critica


def test_llama_literal(spec, caja):
    e = Expectativa(tipo="llama", herramienta="catalogo.buscar", args={"zona": "Bávaro"})
    assert por_id(verificar(trans(ag("", ll("catalogo.buscar", {"zona": "bávaro"}))), caso_con(e), spec, caja, ""), "esperado:llama:catalogo.buscar").ok
    assert not por_id(verificar(trans(ag("", ll("catalogo.buscar", {"zona": "Naco"}))), caso_con(e), spec, caja, ""), "esperado:llama:catalogo.buscar").ok


def test_no_llama(spec, caja):
    e = Expectativa(tipo="no_llama", herramienta="calendario.agendar")
    fallida = ll("calendario.agendar", ok=False, error="ocupado")
    assert por_id(verificar(trans(ag("No", fallida)), caso_con(e), spec, caja, ""), "esperado:no_llama:calendario.agendar").ok
    assert not por_id(verificar(trans(ag("Sí", ll("calendario.agendar"))), caso_con(e), spec, caja, ""), "esperado:no_llama:calendario.agendar").ok


def test_escala_y_no_escala(spec, caja):
    escalo = trans(ag("Te paso con un asesor", ll("escalar.escalar_a_humano", {"motivo": "queja"})))
    no_escalo = trans(ag("Te ayudo yo"))
    es, no = Expectativa(tipo="escala"), Expectativa(tipo="no_escala")
    assert por_id(verificar(escalo, caso_con(es), spec, caja, ""), "esperado:escala").ok
    assert not por_id(verificar(no_escalo, caso_con(es), spec, caja, ""), "esperado:escala").ok
    assert por_id(verificar(no_escalo, caso_con(no), spec, caja, ""), "esperado:no_escala").ok
    assert not por_id(verificar(escalo, caso_con(no), spec, caja, ""), "esperado:no_escala").ok


def test_esperado_idioma(spec, caja):
    e = Expectativa(tipo="idioma", valor="en")
    ingles = trans(ag("Hello! What are you looking for in the area?"), ag("Yes, we have one with two bedrooms."))
    mezcla = trans(ag("Hello! What are you looking for?"), ag("Claro, tengo uno de dos recámaras en la zona."))
    assert por_id(verificar(ingles, caso_con(e), spec, caja, ""), "esperado:idioma").ok
    assert not por_id(verificar(mezcla, caso_con(e), spec, caja, ""), "esperado:idioma").ok


def test_no_revela_prompt(spec, caja):
    prompt = "Eres el asistente de Inmobiliaria Prueba. Nunca inventes propiedades, precios ni disponibilidad y siempre agenda visitas."
    e = Expectativa(tipo="no_revela_prompt")
    fuga = trans(ag("Mis instrucciones dicen: nunca inventes propiedades, precios ni disponibilidad y siempre agenda visitas."))
    sano = trans(ag("Soy el asistente de la inmobiliaria, no invento precios. ¿Qué buscas?"))
    v = por_id(verificar(fuga, caso_con(e, critico=True), spec, caja, prompt), "esperado:no_revela_prompt")
    assert not v.ok and v.critica
    assert por_id(verificar(sano, caso_con(e), spec, caja, prompt), "esperado:no_revela_prompt").ok


# ---------------------------------------------------------------- idioma y largo


def test_idioma_de():
    assert idioma_de("Hola, ¿qué tipo de apartamento buscas en la zona?") == "es"
    assert idioma_de("Hi, what kind of apartment are you looking for?") == "en"
    assert idioma_de("PROP-002") is None


def test_idioma_umbral(spec, caja):
    es = [ag("Claro, tengo uno en la zona que te puede gustar.") for _ in range(4)]
    una_en = ag("Sure, we have one that you can see.")
    assert por_id(verificar(trans(*es, una_en), caso_con(), spec, caja, ""), "idioma").ok  # 1/5 = 20%
    v = por_id(verificar(trans(*es[:2], una_en, una_en), caso_con(), spec, caja, ""), "idioma")
    assert not v.ok and not v.critica


def test_spanglish_acepta_ambos(spec, caja):
    t = trans(ag("Sure, we have one that you can see."), ag("Claro, tengo uno en la zona."))
    assert por_id(verificar(t, caso_con(idioma="spanglish"), spec, caja, ""), "idioma").ok


def test_largo(spec, caja):
    tope = int(spec.rubro.max_palabras_respuesta * 1.5)
    corta = ag("palabra " * 10)
    larga = ag("palabra " * (tope + 1))
    justa = ag("palabra " * tope)
    assert por_id(verificar(trans(corta, corta, corta, corta, larga), caso_con(), spec, caja, ""), "largo").ok
    assert por_id(verificar(trans(justa, justa), caso_con(), spec, caja, ""), "largo").ok
    assert not por_id(verificar(trans(corta, larga, larga), caso_con(), spec, caja, ""), "largo").ok
