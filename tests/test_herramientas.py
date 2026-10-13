import json
from datetime import date
from decimal import Decimal

import pytest

from fantasma.motor.esquemas import Horario
from fantasma.motor.herramientas import REGISTRO, SEPARADOR, caja_para, mundo_inicial
from fantasma.motor.herramientas.calendario import Calendario
from fantasma.motor.herramientas.catalogo import Catalogo


@pytest.fixture
def caja(rubro, datos):
    rubro = rubro.model_copy(update={"herramientas": ["catalogo", "calendario", "crm", "pedidos", "pagos", "faq", "escalar"]})
    return caja_para(rubro, datos)


@pytest.fixture
def mundo(rubro, datos):
    return mundo_inicial(datos, rubro)


def llamar(caja, funcion, mundo, /, **args):
    return caja.ejecutar(funcion, json.dumps(args), mundo).resultado


# ---------------------------------------------------------------- caja


def test_caja_para_solo_las_del_rubro_mas_escalar(rubro, datos):
    caja = caja_para(rubro, datos)
    assert set(caja.por_nombre) == {"catalogo", "calendario", "crm", "faq", "escalar"}
    nombres = {d["function"]["name"] for d in caja.definiciones()}
    assert "escalar__escalar_a_humano" in nombres
    assert not any(n.startswith("pedidos") for n in nombres)
    assert all(SEPARADOR in n and "." not in n for n in nombres)


def test_caja_para_agrega_escalar_aunque_falte(rubro, datos):
    sin = rubro.model_construct(**{**rubro.__dict__, "herramientas": ["faq"]})
    assert set(caja_para(sin, datos).por_nombre) == {"faq", "escalar"}


def test_registro_completo():
    assert set(REGISTRO) == {"catalogo", "calendario", "crm", "pedidos", "pagos", "escalar", "faq"}


def test_caja_herramienta_desconocida(caja, mundo):
    ll = caja.ejecutar("inventario__buscar", "{}", mundo)
    assert not ll.resultado.ok and ll.nombre == "desconocida.inventario__buscar"


def test_caja_funcion_desconocida(caja, mundo):
    ll = caja.ejecutar("catalogo__comprar", "{}", mundo)
    assert ll.nombre == "catalogo.comprar"
    assert not ll.resultado.ok and "no existe" in ll.resultado.error


def test_caja_json_invalido(caja, mundo):
    ll = caja.ejecutar("catalogo__buscar", "{zona: Bávaro", mundo)
    assert not ll.resultado.ok and "JSON inválido" in ll.resultado.error
    assert ll.args == {"_crudo": "{zona: Bávaro"}
    assert not caja.ejecutar("catalogo__buscar", "[1]", mundo).resultado.ok


def test_hechos_del_catalogo(caja):
    h = caja.hechos()
    assert "PROP-001" in h.ids and Decimal(95000) in h.precios and "Apartamento Piantini 3" in h.nombres


def test_definiciones_serializables(caja):
    for d in caja.definiciones():
        assert json.loads(json.dumps(d))["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------- catálogo


def test_buscar_filtros_dinamicos(caja, mundo):
    props = caja.definiciones()[0]["function"]["parameters"]["properties"]
    assert {"zona", "recamaras_min", "metros_min", "precio_min"} <= set(props)
    r = llamar(caja, "catalogo__buscar", mundo, zona="bavaro", recamaras_min=2)
    assert r.ok and r.data["total"] == 1 and r.data["items"][0]["id"] == "PROP-002"
    assert set(r.data["items"][0]) == {"id", "nombre", "categoria", "precio", "moneda", "disponible", "atributos"}


def test_descripcion_lista_valores_validos(caja):
    desc = caja.definiciones()[0]["function"]["description"]
    assert "apartamento" in desc and "Bávaro" in desc and "Piantini" in desc and "Naco" in desc


def test_buscar_precio_y_texto(caja, mundo):
    r = llamar(caja, "catalogo__buscar", mundo, precio_max=150000)
    assert [i["id"] for i in r.data["items"]] == ["PROP-001", "PROP-002"]
    r = llamar(caja, "catalogo__buscar", mundo, texto="APARTAMENTO piantini")
    assert [i["id"] for i in r.data["items"]] == ["PROP-003"]


def test_buscar_sin_resultados_no_es_error(caja, mundo):
    r = llamar(caja, "catalogo__buscar", mundo, zona="Marte")
    assert r.ok and r.data == {"total": 0, "items": []}


def test_buscar_arg_invalido(caja, mundo):
    r = llamar(caja, "catalogo__buscar", mundo, recamaras_min="muchas")
    assert not r.ok and "recamaras_min" in r.error


def test_buscar_limita_a_cinco(rubro, datos):
    datos = datos.model_copy(update={"catalogo": datos.catalogo * 3})
    r = Catalogo(datos, rubro).ejecutar("buscar", {}, mundo_inicial(datos, rubro))
    assert r.data["total"] == 12 and len(r.data["items"]) == 5


def test_filtro_bool(rubro, datos):
    cat = [i.model_copy(update={"atributos": {**i.atributos, "piscina": n % 2 == 0}}) for n, i in enumerate(datos.catalogo)]
    datos = datos.model_copy(update={"catalogo": cat})
    r = Catalogo(datos, rubro).ejecutar("buscar", {"piscina": True}, mundo_inicial(datos, rubro))
    assert [i["id"] for i in r.data["items"]] == ["PROP-001", "PROP-003"]


def test_detalle(caja, mundo):
    assert llamar(caja, "catalogo__detalle", mundo, id="PROP-004").data["disponible"] is False
    r = llamar(caja, "catalogo__detalle", mundo, id="PROP-999")
    assert not r.ok and "no existe" in r.error


# ---------------------------------------------------------------- calendario
# hoy = lunes 2026-10-12 09:00; atiende lun-sáb 9-17 en bloques de 60; ocupado 13-oct 10:00


def test_disponibilidad_excluye_ocupados_y_pasado(caja, mundo):
    r = llamar(caja, "calendario__disponibilidad", mundo, fecha="2026-10-13")
    assert r.ok and "2026-10-13T10:00" not in r.data["huecos"]
    assert r.data["huecos"][0] == "2026-10-13T09:00" and r.data["huecos"][-1] == "2026-10-13T16:00"
    hoy = llamar(caja, "calendario__disponibilidad", mundo, fecha="2026-10-12")
    assert "2026-10-12T09:00" not in hoy.data["huecos"] and "2026-10-12T10:00" in hoy.data["huecos"]
    assert llamar(caja, "calendario__disponibilidad", mundo, fecha="2026-10-01").data["huecos"] == []


def test_disponibilidad_domingo_y_feriado(rubro, datos):
    datos = datos.model_copy(update={"horario": datos.horario.model_copy(update={"feriados": [date(2026, 10, 14)]})})
    cal, mundo = Calendario(datos, rubro), mundo_inicial(datos, rubro)
    domingo = cal.ejecutar("disponibilidad", {"fecha": "2026-10-18"}, mundo)
    assert domingo.data["huecos"] == [] and "domingo" in domingo.data["motivo"]
    assert cal.ejecutar("disponibilidad", {"fecha": "2026-10-14"}, mundo).data["motivo"] == "feriado"
    assert not cal.ejecutar("agendar", {"inicio": "2026-10-14T10:00", "nombre": "Luis", "telefono": "8095550000"}, mundo).ok


def test_horario_por_defecto(rubro, datos):
    datos = datos.model_copy(update={"horario": None})
    r = Calendario(datos, rubro).ejecutar("disponibilidad", {"fecha": "2026-10-13"}, mundo_inicial(datos, rubro))
    assert r.data["huecos"][-1] == "2026-10-13T17:00" and len(r.data["huecos"]) == 9
    assert Calendario(datos, rubro).horario == Horario()


@pytest.mark.parametrize(
    "inicio,fragmento",
    [
        ("2026-10-12T08:00", "ya pasó"),
        ("2026-10-12T09:00", "ya pasó"),
        ("2026-10-13T08:00", "no es un hueco"),
        ("2026-10-13T17:00", "no es un hueco"),
        ("2026-10-13T11:30", "no es un hueco"),
        ("2026-10-13T10:00", "ocupado"),
        ("2026-10-18T10:00", "no se atiende"),
    ],
)
def test_agendar_bordes(caja, mundo, inicio, fragmento):
    r = llamar(caja, "calendario__agendar", mundo, inicio=inicio, nombre="Luis", telefono="809-555-0000")
    assert not r.ok and fragmento in r.error
    assert mundo.citas == []


def test_agendar_valida_contacto_e_item(caja, mundo):
    base = {"inicio": "2026-10-13T11:00", "nombre": "Luis", "telefono": "8095550000"}
    r = llamar(caja, "calendario__agendar", mundo, **{**base, "nombre": "L"})
    assert not r.ok and "2 letras" in r.error
    r = llamar(caja, "calendario__agendar", mundo, **{**base, "telefono": "12-34"})
    assert not r.ok and "7 dígitos" in r.error
    r = llamar(caja, "calendario__agendar", mundo, **{**base, "item_id": "PROP-999"})
    assert not r.ok and "no existe" in r.error
    r = llamar(caja, "calendario__agendar", mundo, **{**base, "inicio": "mañana"})
    assert not r.ok and "inicio" in r.error


def test_agendar_ocupa_y_cancelar_libera(caja, mundo):
    args = {"inicio": "2026-10-13T11:00", "nombre": "Luis", "telefono": "809 555 0000", "item_id": "PROP-002"}
    r = llamar(caja, "calendario__agendar", mundo, **args)
    assert r.ok and r.data["id"] == "CITA-001" and r.data["telefono"] == "8095550000"
    assert not llamar(caja, "calendario__agendar", mundo, **args).ok
    assert "2026-10-13T11:00" not in llamar(caja, "calendario__disponibilidad", mundo, fecha="2026-10-13").data["huecos"]
    assert llamar(caja, "calendario__cancelar", mundo, cita_id="CITA-001").ok
    assert not llamar(caja, "calendario__cancelar", mundo, cita_id="CITA-001").ok
    assert not llamar(caja, "calendario__cancelar", mundo, cita_id="CITA-404").ok
    r = llamar(caja, "calendario__agendar", mundo, **args)
    assert r.ok and r.data["id"] == "CITA-002"


def test_agendar_con_zona_horaria(caja, mundo):
    # 15:00Z = 11:00 en Santo Domingo (UTC-4)
    r = llamar(caja, "calendario__agendar", mundo, inicio="2026-10-13T15:00:00Z", nombre="Luis", telefono="8095550000")
    assert r.ok and r.data["inicio"] == "2026-10-13T11:00:00"


# ---------------------------------------------------------------- crm


def test_capturar_lead_deduplica_por_telefono(caja, mundo):
    r = llamar(caja, "crm__capturar_lead", mundo, nombre="Ana Pérez", telefono="+1 (809) 555-1234", interes="3 recámaras", presupuesto=200000)
    assert r.ok and r.data["nuevo"] is False and r.data["lead"]["id"] == "LEAD-000"
    assert len(mundo.leads) == 1 and mundo.leads[0].presupuesto == Decimal(200000)
    r = llamar(caja, "crm__capturar_lead", mundo, nombre="Luis", telefono="8295550000", interes="Bávaro")
    assert r.ok and r.data["nuevo"] and r.data["lead"]["id"] == "LEAD-001"


def test_consultar_y_actualizar(caja, mundo):
    assert llamar(caja, "crm__consultar_cliente", mundo, telefono="809-555-1234").data["lead"]["nombre"] == "Ana Pérez"
    assert llamar(caja, "crm__consultar_cliente", mundo, telefono="8290000000").data == {"encontrado": False}
    r = llamar(caja, "crm__actualizar_lead", mundo, id="LEAD-000", notas="Quiere ver el sábado")
    assert r.ok and mundo.leads[0].notas == "Quiere ver el sábado"
    assert not llamar(caja, "crm__actualizar_lead", mundo, id="LEAD-000").ok
    assert not llamar(caja, "crm__actualizar_lead", mundo, id="LEAD-404", notas="x").ok
    assert not llamar(caja, "crm__capturar_lead", mundo, nombre="Luis", telefono="555", interes="algo").ok


# ---------------------------------------------------------------- pedidos y pagos


def test_cotizar_y_crear_pedido(caja, mundo):
    lineas = [{"item_id": "PROP-001", "cantidad": 2}, {"item_id": "PROP-002"}]
    r = llamar(caja, "pedidos__cotizar", mundo, lineas=lineas)
    assert r.ok and r.data["total"] == "328000" and r.data["moneda"] == "USD"
    assert mundo.pedidos == []
    r = llamar(caja, "pedidos__crear_pedido", mundo, lineas=lineas, nombre="Luis", telefono="8295550000")
    assert r.ok and r.data["id"] == "PED-001" and mundo.pedidos[0].total == Decimal(328000)


def test_cotizar_errores(caja, mundo):
    assert "no existe" in llamar(caja, "pedidos__cotizar", mundo, lineas=[{"item_id": "X"}]).error
    assert "no está disponible" in llamar(caja, "pedidos__cotizar", mundo, lineas=[{"item_id": "PROP-004"}]).error
    assert not llamar(caja, "pedidos__cotizar", mundo, lineas=[]).ok
    assert not llamar(caja, "pedidos__cotizar", mundo, lineas=[{"item_id": "PROP-001", "cantidad": 0}]).ok


def test_enlace_pago(caja, mundo):
    r = llamar(caja, "pagos__enlace_pago", mundo, pedido_id="PED-001")
    assert not r.ok and "No existe el pedido" in r.error
    llamar(caja, "pedidos__crear_pedido", mundo, lineas=[{"item_id": "PROP-001"}], nombre="Luis", telefono="8295550000")
    r = llamar(caja, "pagos__enlace_pago", mundo, pedido_id="PED-001")
    assert r.ok and r.data["url"] == "https://pagos.ejemplo/PED-001" and mundo.enlaces_pago == [r.data["url"]]
    assert llamar(caja, "pagos__enlace_pago", mundo, item_id="PROP-002").data["url"] == "https://pagos.ejemplo/PROP-002"
    for args in ({}, {"pedido_id": "PED-001", "item_id": "PROP-001"}):
        r = llamar(caja, "pagos__enlace_pago", mundo, **args)
        assert not r.ok and "exactamente uno" in r.error


# ---------------------------------------------------------------- escalar y faq


def test_escalar(caja, mundo):
    assert not llamar(caja, "escalar__escalar_a_humano", mundo, motivo="ya").ok
    r = llamar(caja, "escalar__escalar_a_humano", mundo, motivo="Cliente pide hablar con un asesor")
    assert r.ok and r.data["ticket"] == "ESC-001" and mundo.escalados == ["Cliente pide hablar con un asesor"]
    assert llamar(caja, "escalar__escalar_a_humano", mundo, motivo="Otra queja seria").data["ticket"] == "ESC-002"


def test_faq(caja, mundo):
    r = llamar(caja, "faq__buscar_info", mundo, consulta="¿Puedo ponerlo en AIRBNB?")
    assert r.ok and r.data[0]["pregunta"] == "Se puede alquilar por Airbnb"
    assert llamar(caja, "faq__buscar_info", mundo, consulta="cuánto cuesta el mantenimiento").data[0]["respuesta"].startswith("Alrededor")
    assert llamar(caja, "faq__buscar_info", mundo, consulta="de la que con").data == []
    assert llamar(caja, "faq__buscar_info", mundo, consulta="mascotas").data == []


# ---------------------------------------------------------------- determinismo y aislamiento


def _guion(caja, mundo):
    return [
        llamar(caja, "catalogo__buscar", mundo, zona="Bávaro").model_dump(),
        llamar(caja, "calendario__agendar", mundo, inicio="2026-10-13T11:00", nombre="Luis", telefono="8295550000").model_dump(),
        llamar(caja, "crm__capturar_lead", mundo, nombre="Luis", telefono="8295550000", interes="Bávaro").model_dump(),
        llamar(caja, "pedidos__crear_pedido", mundo, lineas=[{"item_id": "PROP-001"}], nombre="Luis", telefono="8295550000").model_dump(),
        llamar(caja, "escalar__escalar_a_humano", mundo, motivo="Pide financiamiento").model_dump(),
    ]


def test_determinismo(caja, rubro, datos):
    m1, m2 = mundo_inicial(datos, rubro), mundo_inicial(datos, rubro)
    assert _guion(caja, m1) == _guion(caja, m2)
    assert m1 == m2


def test_aislamiento_entre_mundos(caja, rubro, datos):
    m1, m2 = mundo_inicial(datos, rubro), mundo_inicial(datos, rubro)
    _guion(caja, m1)
    assert m2.citas == m2.pedidos == m2.escalados == [] and len(m2.leads) == 1
    llamar(caja, "crm__actualizar_lead", m1, id="LEAD-000", notas="solo en m1")
    assert m2.leads[0].notas == "" and datos.clientes[0].notas == ""
    assert llamar(caja, "calendario__agendar", m2, inicio="2026-10-13T11:00", nombre="Ana", telefono="8095551234").data["id"] == "CITA-001"


def test_horario_con_zona_se_vuelve_local(rubro, datos):
    from fantasma.motor.esquemas import DatosRubro, Horario
    from fantasma.motor.herramientas import caja_para, mundo_inicial

    h = Horario.model_validate({"abre": "09:00:00Z", "cierra": "18:00:00Z", "ocupados": ["2026-10-13T10:00:00Z"]})
    d = DatosRubro(catalogo=datos.catalogo, horario=h)
    caja = caja_para(rubro, d)
    ll = caja.ejecutar("calendario__disponibilidad", '{"fecha": "2026-10-13"}', mundo_inicial(d, rubro))
    assert ll.resultado.ok and "10:00" not in str(ll.resultado.data)
