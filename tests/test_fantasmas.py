from collections import Counter

import pytest

from fantasma import fantasmas as fa
from fantasma.fantasmas import Fantasma, instrucciones_dialecto, repartir, rubrica_fija
from fantasma.motor import generador as g
from fantasma.motor.config import Casos
from fantasma.motor.esquemas import Caso, Criterio, Expectativa, Persona, Rubrica
from fantasma.motor.herramientas import caja_para

# ---------------------------------------------------------------- reparto


def test_repartir_determinista_y_sin_repetidos():
    assert repartir(40) == repartir(40)
    assert repartir(15) == repartir(40)[:15]
    combos = [tuple(f.model_dump().values()) for f in repartir(200)]
    assert len(set(combos)) == 200


@pytest.mark.parametrize("n", [15, 40])
def test_repartir_balanceado(n):
    fs = repartir(n)
    for eje, valores in (("intencion", fa.INTENCIONES), ("dialecto", fa.DIALECTOS), ("estilo", fa.ESTILOS)):
        cuenta = Counter(getattr(f, eje) for f in fs)
        assert set(cuenta) == set(valores), eje
        assert max(cuenta.values()) - min(cuenta.values()) <= 1, eje
    ataques = Counter(f.ataque for f in fs)
    assert set(ataques) == set(fa.ATAQUES)
    assert ataques["ninguno"] == pytest.approx(n / 2, abs=1)


def test_dialecto_y_estilo_se_cruzan():
    pares = {(f.dialecto, f.estilo) for f in repartir(25)}
    assert len(pares) == 25


def test_etiquetas_por_eje():
    assert Fantasma(intencion="compra_directa", ataque="inyeccion", dialecto="mx", estilo="emojis").etiquetas == {"critical", "adversarial"}
    assert Fantasma(intencion="fuera_de_catalogo", ataque="ninguno", dialecto="co", estilo="prisa").etiquetas == {"critical"}
    assert Fantasma(intencion="regatea", ataque="ninguno", dialecto="ar", estilo="partidos").etiquetas == {"edge"}
    assert Fantasma(intencion="entrega_horario", ataque="ninguno", dialecto="es", estilo="sin_tildes").etiquetas == {"normal"}


def test_escenario_y_descripcion():
    f = Fantasma(intencion="regatea", ataque="engano", dialecto="rd", estilo="mala_ortografia")
    assert f.escenario == "regatea+engano"
    assert "gerente" in f.descripcion and "República Dominicana" in f.descripcion and "ortografía" in f.descripcion
    assert Fantasma(intencion="queja", ataque="ninguno", dialecto="rd", estilo="prisa").escenario == "queja"


def test_expectativas_por_fantasma():
    assert fa.expectativas(Fantasma(intencion="queja", ataque="inyeccion", dialecto="mx", estilo="prisa")) == [
        Expectativa(tipo="no_revela_prompt"),
        Expectativa(tipo="escala"),
    ]
    assert fa.expectativas(Fantasma(intencion="pide_precio", ataque="presion", dialecto="mx", estilo="prisa")) == []


def test_obligatorias_quitan_la_contraria():
    f = Fantasma(intencion="mayorista", ataque="ninguno", dialecto="co", estilo="emojis")
    llm = [Expectativa(tipo="no_escala"), Expectativa(tipo="llama", herramienta="catalogo.detalle")]
    assert g._expectativas_obligatorias(f, llm) == [Expectativa(tipo="llama", herramienta="catalogo.detalle"), Expectativa(tipo="escala")]


# ---------------------------------------------------------------- dialecto en la persona


def test_instrucciones_dialecto():
    assert instrucciones_dialecto(Persona(nombre="X", estilo="s")) == ""
    rd = instrucciones_dialecto(Persona(nombre="X", estilo="s", dialecto="rd"))
    assert "klk" in rd and "naturalidad" in rd
    assert "vos" in instrucciones_dialecto(Persona(nombre="X", estilo="s", dialecto="ar"))


def test_datos_viejos_siguen_validando():
    p = Persona.model_validate({"nombre": "Luis", "estilo": "corto", "idioma": "es"})
    assert p.dialecto == "neutro"
    c = Caso.model_validate({"id": "a", "escenario": "busqueda_normal", "persona": p.model_dump(exclude={"dialecto"}), "objetivo_oculto": "o"})
    assert c.ejes == {} and c.persona.dialecto == "neutro"


# ---------------------------------------------------------------- rúbrica fija


def test_rubrica_fija(rubro):
    fijos = rubrica_fija(rubro)
    ids = {c.id for c in fijos}
    assert {c.id for c in fijos if c.critico} == {"no_inventa", "no_promete", "no_cede_engano"}
    assert {"escala_cuando_toca", "avanza_venta", "entiende_dialecto", "tono_whatsapp"} <= ids
    assert all(c.cumple and c.no_cumple for c in fijos)
    pesos = {c.id: c.peso for c in fijos}
    assert pesos["no_inventa"] > pesos["escala_cuando_toca"] > pesos["avanza_venta"] == pesos["entiende_dialecto"] > pesos["tono_whatsapp"]
    assert str(rubro.max_palabras_respuesta) in next(c for c in fijos if c.id == "tono_whatsapp").descripcion


def _propio(id_: str, peso: float = 1, **kw) -> Criterio:
    return Criterio(id=id_, nombre="n", descripcion="d", peso=peso, cumple=["s"], no_cumple=["n"], **kw)


def test_combinar_rubrica_fijos_dominan(rubro):
    fijos = rubrica_fija(rubro)
    r = g.combinar_rubrica(fijos, [_propio("no_discriminar", 5, critico=True), _propio("pide_zona", 5)])
    assert sum(c.peso for c in r.criterios) == pytest.approx(1.0)
    propios = sum(c.peso for c in r.criterios if c.id in ("no_discriminar", "pide_zona"))
    assert propios == pytest.approx(g.PESO_PROPIOS / (sum(c.peso for c in fijos) + g.PESO_PROPIOS))
    assert [c.id for c in r.criterios[: len(fijos)]] == [c.id for c in fijos]


def test_validar_rubrica_propia(rubro):
    fijos = rubrica_fija(rubro)
    assert g.validar_rubrica(Rubrica(criterios=[_propio("a"), _propio("b")]), fijos) == []
    problemas = g.validar_rubrica(Rubrica(criterios=[_propio("no_inventa"), _propio("b"), _propio("c"), _propio("d"), _propio("e")]), fijos)
    assert any("2-4" in p for p in problemas) and any("no_inventa" in p for p in problemas)


def test_reglas_base_alineadas_con_la_rubrica(rubro):
    criticas = {r.id for r in g.reglas_base(rubro) if r.critica}
    assert {"no_inventar", "no_prometer", "no_ceder_engano"} <= criticas


# ---------------------------------------------------------------- casos con LLM falso, tamaños ligera y completa


def _lote(mensajes):
    pedido = mensajes[1]["content"]
    n = int(pedido.split("Escribe exactamente ")[1].split()[0])
    return g.LoteCasos(
        casos=[
            g.CasoBorrador(
                id=f"fantasma {i}",
                persona=Persona(nombre=f"N{i}", estilo="escribe corto"),
                objetivo_oculto="algo concreto",
                verdad={"telefono": "8095550000"},
                esperado=[Expectativa(tipo="no_escala")],
            )
            for i in range(n)
        ]
    )


@pytest.mark.parametrize("cantidad", [15, 40])
async def test_generar_casos_ligera_y_completa(llm, rubro, datos, cantidad):
    assert Casos(cantidad=cantidad).cantidad == cantidad
    propios = g.EscenariosPropios(escenarios=[g.EscenarioPropio(escenario=f"p{i}", descripcion=f"situación {i}") for i in range(6)])
    llm.encolar("generador", propios, *[_lote] * -(-cantidad // g.TAM_LOTE_CASOS))
    casos = await g.generar_casos(llm, rubro, g.combinar_reglas(rubro, []), caja_para(rubro, datos), cantidad)

    assert len(casos) == cantidad and len({c.id for c in casos}) == cantidad
    assert [c.ejes for c in casos] == [f.model_dump() for f in repartir(cantidad)]
    assert all(c.persona.dialecto == c.ejes["dialecto"] and c.persona.idioma == "es" for c in casos)
    assert all(c.objetivo_oculto and c.verdad for c in casos)
    for c in casos:
        if c.ejes["intencion"] in ("queja", "mayorista"):
            assert Expectativa(tipo="escala") in c.esperado and Expectativa(tipo="no_escala") not in c.esperado
        else:
            assert Expectativa(tipo="no_escala") in c.esperado
        assert (Expectativa(tipo="no_revela_prompt") in c.esperado) == (c.ejes["ataque"] == "inyeccion")

    pedido = next(m for rol, m in llm.llamadas if "Escribe exactamente" in m[1]["content"])[1]["content"]
    assert "situación 0" in pedido and "República Dominicana" in pedido

    split = g.estratificar(casos, 0.3)
    for estrato in {g.etiqueta_principal(c) for c in split}:
        del_estrato = [c.split for c in split if g.etiqueta_principal(c) == estrato]
        if len(del_estrato) >= 2:
            assert set(del_estrato) == {"train", "holdout"}, estrato
