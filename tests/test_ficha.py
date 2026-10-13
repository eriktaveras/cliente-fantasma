import json
import os
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from fantasma import costos
from fantasma import ficha as f
from fantasma.motor.esquemas import Mundo
from fantasma.motor.herramientas import caja_para
from fantasma.motor.herramientas.faq import ArgsBuscarInfo
from fantasma.motor.llm import ErrorTecnico

RAIZ = Path(__file__).resolve().parents[1]

TEXTO_COLMADO = """Colmado La Esquina de Doña Rosa, en Villa Consuelo, Santo Domingo.
Precios de hoy:
- Arroz Selecto 5 lb: RD$245
- Salami Induveca 1 lb: RD$185
- Habichuelas rojas La Famosa (lata): RD$75
- Presidente grande (cerveza): RD$190
- Pan sobado (funda de 10): RD$60
También tenemos plátanos, pero el precio cambia cada día.
Abrimos de lunes a sábado de 7:00 am a 10:00 pm y domingo de 8:00 am a 2:00 pm.
Delivery gratis en Villa Consuelo y Villa Juana con compra mínima de RD$500. Pagos en efectivo o transferencia."""


def _ficha(**cambios) -> f.Ficha:
    base = f.Ficha(
        negocio="Colmado La Esquina",
        descripcion="Colmado de barrio en Villa Consuelo.",
        rubro="colmado",
        pais="República Dominicana",
        moneda="DOP",
        productos=[
            f.Producto(nombre="Arroz Selecto 5 lb", categoria="granos", precio=Decimal(245)),
            f.Producto(nombre="Salami Induveca 1 lb", categoria="embutidos", precio=Decimal(185), disponible=False),
            f.Producto(nombre="Plátanos", categoria="víveres"),
        ],
        horario="Lunes a sábado 7:00 am a 10:00 pm",
        zonas_entrega=["Villa Consuelo", "Villa Juana"],
        formas_pago=["efectivo", "transferencia"],
        politicas=f.Politicas(delivery="Gratis con compra mínima de RD$500."),
        preguntas_frecuentes=[f.PreguntaFrecuente(pregunta="Fían", respuesta="No, solo contado.")],
    )
    return base.model_copy(update=cambios)


# ---------------------------------------------------------------- validación anti-invento


def test_numeros_en_lee_miles_y_decimales():
    n = f.numeros_en("RD$1,250.00 o 1.250 y US$12,50 y 85")
    assert {Decimal(1250), Decimal("12.50"), Decimal(85)} <= n


def test_validar_ficha_rechaza_precio_que_no_esta_en_el_material():
    ficha = _ficha(productos=[f.Producto(nombre="Arroz", precio=Decimal(999))])
    problemas = f.validar_ficha(ficha, TEXTO_COLMADO)
    assert len(problemas) == 1 and "999" in problemas[0]
    assert f.validar_ficha(_ficha(), TEXTO_COLMADO) == []


def test_validar_ficha_rechaza_productos_vacios_si_el_material_tiene_precios():
    assert "productos" in f.validar_ficha(_ficha(productos=[]), TEXTO_COLMADO)[0]
    assert f.validar_ficha(_ficha(productos=[]), "Barbería, cortes a domicilio") == []


def test_completar_faltantes_anota_precios_y_horario():
    ficha = f.completar_faltantes(_ficha(horario="", formas_pago=[]))
    assert "precio de Plátanos" in ficha.faltantes
    assert "horario de atención" in ficha.faltantes
    assert "formas de pago" in ficha.faltantes


# ---------------------------------------------------------------- investigar


async def test_investigar_solo_texto_estructura_y_reintenta_si_inventa_precio(llm):
    inventada = _ficha(productos=[f.Producto(nombre="Arroz Selecto 5 lb", precio=Decimal(300))])
    llm.encolar("generador", inventada, _ficha())
    avisos: list[str] = []
    ficha = await f.investigar(llm, texto=TEXTO_COLMADO, progreso=avisos.append)
    assert [p.precio for p in ficha.con_precio()] == [Decimal(245), Decimal(185)]
    assert ficha.fuentes == []
    assert "precio de Plátanos" in ficha.faltantes
    assert any("no valida" in a for a in avisos)
    # El reintento le muestra al modelo el problema concreto.
    assert "300" in llm.llamadas[1][1][-1]["content"]


async def test_investigar_sin_material_falla(llm):
    with pytest.raises(ValueError):
        await f.investigar(llm, texto="   ")


async def test_investigar_se_rinde_tras_intentos(llm):
    mala = _ficha(productos=[f.Producto(nombre="Arroz", precio=Decimal(777))])
    llm.encolar("generador", mala, mala, mala)
    with pytest.raises(ErrorTecnico):
        await f.investigar(llm, texto=TEXTO_COLMADO)


def _transporte(pedidos: list[httpx.Request], extract_status: int = 200) -> httpx.MockTransport:
    def responder(req: httpx.Request) -> httpx.Response:
        pedidos.append(req)
        if req.url.path == "/extract":
            return httpx.Response(
                extract_status,
                json={
                    "results": [{"url": "https://colmado.do/precios", "raw_content": "Arroz Selecto 5 lb RD$245"}],
                    "failed_results": [{"url": "https://colmado.do/caida", "error": "timeout"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "query": "x",
                "results": [
                    {"url": "https://colmado.do/precios", "title": "t", "content": "duplicada", "score": 0.9},
                    {"url": "https://ig.com/colmado", "title": "IG", "content": "Salami Induveca RD$185", "raw_content": None, "score": 0.8},
                ],
            },
        )

    return httpx.MockTransport(responder)


async def test_investigar_con_tavily_usa_extract_y_search(llm):
    pedidos: list[httpx.Request] = []
    llm.encolar("generador", _ficha())
    avisos: list[str] = []
    ficha = await f.investigar(
        llm,
        urls=["https://colmado.do/precios", "https://colmado.do/caida"],
        nombre="Colmado La Esquina Villa Consuelo",
        tavily_key="tvly-prueba",
        texto="Plátanos y RD$245 y RD$185",
        progreso=avisos.append,
        http=httpx.AsyncClient(transport=_transporte(pedidos)),
    )
    assert [r.url.path for r in pedidos] == ["/extract", "/search"]
    assert all(r.headers["Authorization"] == "Bearer tvly-prueba" for r in pedidos)
    cuerpo = json.loads(pedidos[1].content)
    assert cuerpo["query"] == "Colmado La Esquina Villa Consuelo" and cuerpo["include_raw_content"] == "markdown"
    assert ficha.fuentes == ["https://colmado.do/precios", "https://ig.com/colmado"]
    assert any("caida" in a for a in avisos)
    material = llm.llamadas[0][1][1]["content"]
    assert material.index("Texto del dueño") < material.index("Fuente: https://colmado.do/precios")
    assert "duplicada" not in material


async def test_investigar_si_tavily_falla_sigue_con_el_texto(llm):
    pedidos: list[httpx.Request] = []
    llm.encolar("generador", _ficha())
    ficha = await f.investigar(
        llm, urls=["https://colmado.do/precios"], tavily_key="tvly-mala", texto=TEXTO_COLMADO,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pedidos.append(r) or httpx.Response(401, json={}))),
    )
    assert len(pedidos) == 2 and ficha.fuentes == []


# ---------------------------------------------------------------- ficha -> motor


def test_a_rubro_y_a_datos_salen_de_la_ficha():
    ficha = _ficha()
    rubro = f.a_rubro(ficha)
    assert rubro.herramientas == ["catalogo", "pedidos", "faq", "escalar"]
    assert rubro.semilla["moneda"] == "DOP"
    assert "Villa Juana" in rubro.descripcion
    assert any(r.critica and r.id == "no_inventar" for r in rubro.reglas)

    datos = f.a_datos(ficha, rubro)
    assert [(i.id, i.nombre, i.precio, i.moneda, i.disponible) for i in datos.catalogo] == [
        ("COLM-001", "Arroz Selecto 5 lb", Decimal(245), "DOP", True),
        ("COLM-002", "Salami Induveca 1 lb", Decimal(185), "DOP", False),
    ]
    assert datos.moneda == "DOP" and datos.horario is None and datos.clientes == []
    respuestas = {e.pregunta: e.respuesta for e in datos.faq}
    assert respuestas["Fían"] == "No, solo contado."
    assert "precio no está publicado" in respuestas["Precio de Plátanos"]
    assert all(e.claves for e in datos.faq)

    # Las herramientas del motor aceptan lo que sale de la ficha y encuentran la info.
    faq = caja_para(rubro, datos).por_nombre["faq"]
    r = faq.buscar_info(ArgsBuscarInfo(consulta="hacen delivery a Villa Juana"), Mundo(reloj=rubro.hoy))
    assert "Villa Juana" in r.data[0]["respuesta"]


def test_a_rubro_sin_precios_solo_faq():
    ficha = _ficha(productos=[f.Producto(nombre="Plátanos")])
    rubro = f.a_rubro(ficha)
    assert rubro.herramientas == ["faq", "escalar"]
    assert f.a_datos(ficha, rubro).catalogo == []


# ---------------------------------------------------------------- prueba real (Nebius), opt-in


@pytest.mark.skipif(not os.environ.get("FICHA_REAL"), reason="llamada real a Nebius: FICHA_REAL=1 y NEBIUS_API_KEY")
async def test_real_colmado_nebius():
    from fantasma.motor.config import cargar
    from fantasma.motor.llm import LLM

    config = cargar(RAIZ / "config.yaml")
    llm = LLM(config)
    ficha = await f.investigar(llm, texto=TEXTO_COLMADO, progreso=print)
    print(ficha.model_dump_json(indent=2))
    print(f"costo: US${costos.total(llm, config):.4f} uso={dict(llm.uso)}")
    assert ficha.moneda == "DOP"
    assert {p.precio for p in ficha.con_precio()} == {Decimal(245), Decimal(185), Decimal(75), Decimal(190), Decimal(60)}
    assert any("plátano" in x.lower() for x in ficha.faltantes)
    datos = f.a_datos(ficha, f.a_rubro(ficha))
    assert len(datos.catalogo) == 5
