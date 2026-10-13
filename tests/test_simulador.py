import asyncio
import json

import pytest
from pydantic import BaseModel

from fantasma.motor.agente import fecha_es, turno_agente
from fantasma.motor.contexto import Contexto
from fantasma.motor.esquemas import LlamadaTool, Mundo, ResultadoTool, Transcripcion, Turno
from fantasma.motor.herramientas.base import Caja, Funcion, Herramienta
from fantasma.motor.llm import ErrorTecnico
from fantasma.motor.simulador import ARRANQUE, instrucciones_cliente, render, simular
from fantasma.motor.storage import Store
from tests.conftest import texto, tool


class ArgsNota(BaseModel):
    nota: str


class Eco(Herramienta):
    """Herramienta de prueba: deja una marca en el mundo para comprobar el aislamiento."""

    nombre = "eco"

    def funciones(self) -> list[Funcion]:
        def anotar(a: ArgsNota, mundo: Mundo) -> ResultadoTool:
            mundo.escalados.append(a.nota)
            return ResultadoTool(ok=True, data={"guardado": a.nota})

        return [Funcion(nombre="anotar", descripcion="Anota algo.", args=ArgsNota, ejecutar=anotar)]


@pytest.fixture
def ctx(config, llm, spec, tmp_path):
    c = Contexto(config, llm, spec, Store(tmp_path))
    c.caja = Caja([Eco(spec.datos, spec.rubro)])
    return c


def correr(coro):
    return asyncio.run(coro)


def test_fecha_en_espanol():
    from datetime import datetime

    assert fecha_es(datetime(2026, 10, 12, 9, 0)) == "lunes 12 de octubre de 2026, 09:00"


def test_bucle_de_tools_muta_historial(ctx, llm):
    llm.encolar("agente", tool("eco__anotar", '{"nota": "hola"}', id="a"), tool("eco__nada", "{}", id="b"), texto("Anotado."))
    mundo = Mundo(reloj=ctx.spec.rubro.hoy)
    historial = [{"role": "user", "content": "anota hola"}]
    txt, llamadas, tokens, ms = correr(turno_agente(ctx, "Eres X.", historial, mundo))

    assert txt == "Anotado."
    assert [ll.nombre for ll in llamadas] == ["eco.anotar", "eco.nada"]
    assert llamadas[0].resultado.ok and not llamadas[1].resultado.ok
    assert (tokens, ms) == (30, 15)
    assert mundo.escalados == ["hola"]
    assert [m["role"] for m in historial] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert historial[1]["tool_calls"][0]["function"]["name"] == "eco__anotar"
    assert historial[2]["tool_call_id"] == "a"
    assert json.loads(historial[2]["content"]) == {"ok": True, "data": {"guardado": "hola"}, "error": None}
    assert historial[-1] == {"role": "assistant", "content": "Anotado."}
    system = llm.llamadas[0][1][0]
    assert system["role"] == "system" and system["content"].endswith("Fecha y hora actual: lunes 12 de octubre de 2026, 09:00")
    # La segunda ronda ya ve el resultado de la primera.
    assert llm.llamadas[1][1][-1]["role"] == "tool"


def test_bucle_limitado_a_seis_rondas(ctx, llm):
    llm.encolar("agente", *[tool("eco__anotar", '{"nota": "x"}', t="pensando") for _ in range(10)])
    historial: list[dict] = []
    txt, llamadas, _, _ = correr(turno_agente(ctx, "P", historial, Mundo(reloj=ctx.spec.rubro.hoy)))
    assert txt == "pensando"
    assert len(llamadas) == 6
    assert historial[-1] == {"role": "assistant", "content": "pensando"}


def test_fin_objetivo(ctx, llm):
    llm.encolar("simulador", "Hola, busco apartamento", "Perfecto, gracias [FIN:objetivo]")
    llm.encolar("agente", tool("eco__anotar", '{"nota": "lead"}'), texto("Claro, ¿en qué zona?"))
    t = correr(simular(ctx, ctx.spec.casos[0], "Eres X.", 1, 0))

    assert t.fin == "objetivo" and t.error is None
    assert [(x.rol, x.texto) for x in t.turnos] == [
        ("cliente", "Hola, busco apartamento"),
        ("agente", "Claro, ¿en qué zona?"),
        ("cliente", "Perfecto, gracias"),
    ]
    assert t.turnos[1].tools[0].nombre == "eco.anotar"
    assert t.turnos[1].tokens_salida == 20
    assert t.mundo_final.escalados == ["lead"]
    assert (t.caso_id, t.version, t.repeticion, t.modelo_agente) == ("busqueda-normal", 1, 0, "falso")

    # La segunda llamada del simulador ve la conversación invertida y sin tools.
    vista = [ms for rol, ms in llm.llamadas if rol == "simulador"][1]
    assert vista[0]["role"] == "system" and "Luis" in vista[0]["content"]
    assert [(m["role"], m["content"]) for m in vista[1:]] == [
        ("user", ARRANQUE),
        ("assistant", "Hola, busco apartamento"),
        ("user", "Claro, ¿en qué zona?"),
    ]


def test_fin_rendido_sin_texto_previo(ctx, llm):
    llm.encolar("simulador", "hola", "[FIN:rendido]")
    llm.encolar("agente", texto(""))
    t = correr(simular(ctx, ctx.spec.casos[3], "P", 0, 1))
    assert t.fin == "rendido"
    assert [x.rol for x in t.turnos] == ["cliente", "agente"]
    vista = [ms for rol, ms in llm.llamadas if rol == "simulador"][1]
    assert vista[-1] == {"role": "user", "content": "(no respondió)"}


def test_fin_suelto_es_objetivo(ctx, llm):
    llm.encolar("simulador", "hola", "ok gracias [FIN]")
    llm.encolar("agente", texto("Hola, dime."))
    t = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 0))
    assert t.fin == "objetivo"
    assert [(x.rol, x.texto) for x in t.turnos] == [("cliente", "hola"), ("agente", "Hola, dime."), ("cliente", "ok gracias")]


def test_max_turnos(ctx, llm):
    llm.encolar("simulador", *[f"msg {i}" for i in range(10)])
    t = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 0))
    assert t.fin == "max_turnos"
    assert sum(x.rol == "cliente" for x in t.turnos) == ctx.config.max_turns
    assert len(t.turnos) == 2 * ctx.config.max_turns


def test_error_tecnico(ctx, llm):
    llm.encolar("simulador", "hola")
    llm.encolar("agente", ErrorTecnico("agente: caído"))
    t = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 0))
    assert t.fin == "error_tecnico"
    assert "caído" in t.error
    assert t.mundo_final is not None


def test_mundos_aislados(ctx, llm):
    llm.encolar("simulador", "anota", "[FIN:objetivo]", "hola", "[FIN:objetivo]")
    llm.encolar("agente", tool("eco__anotar", '{"nota": "solo-en-la-primera"}'), texto("Hecho."), texto("Hola."))
    t1 = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 0))
    t2 = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 1))
    assert t1.mundo_final.escalados == ["solo-en-la-primera"]
    assert t2.mundo_final.escalados == []
    assert [ll.nombre for ll in t2.llamadas()] == []


def test_instrucciones_cliente(spec):
    caso = spec.casos[3].model_copy(update={"persona": spec.casos[3].persona.model_copy(update={"idioma": "spanglish"})})
    ins = instrucciones_cliente(caso, spec.rubro)
    assert "Kelvin" in ins and "Inmobiliaria Prueba" in ins
    assert "spanglish" in ins and "poca paciencia" in ins
    assert caso.objetivo_oculto in ins
    assert "[FIN:objetivo]" in ins and "[FIN:rendido]" in ins


def test_render():
    largo = "x" * 1000
    t = Transcripcion(
        caso_id="c",
        repeticion=0,
        version=0,
        modelo_agente="m",
        turnos=[
            Turno(rol="cliente", texto="hola"),
            Turno(
                rol="agente",
                texto="Te agendé.",
                tools=[
                    LlamadaTool(nombre="calendario.agendar", args={"item_id": "PROP-001"}, resultado=ResultadoTool(ok=True, data={"id": largo})),
                    LlamadaTool(nombre="catalogo.buscar", args={}, resultado=ResultadoTool(ok=False, error="sin zona")),
                ],
            ),
            Turno(rol="agente", texto=""),
        ],
    )
    con = render(t)
    lineas = con.splitlines()
    assert lineas[0] == "CLIENTE: hola"
    assert lineas[1].startswith('  [tool calendario.agendar {"item_id": "PROP-001"} -> ok: {"id": "xxx')
    assert len(lineas[1]) < 500
    assert lineas[2] == "  [tool catalogo.buscar {} -> error: sin zona]"
    assert lineas[3] == "AGENTE: Te agendé."
    assert lineas[4] == "AGENTE: (no respondió)"
    assert render(t, con_tools=False) == "CLIENTE: hola\nAGENTE: Te agendé.\nAGENTE: (no respondió)"
