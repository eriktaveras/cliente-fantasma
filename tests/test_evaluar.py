import asyncio
from types import SimpleNamespace

import pytest

from fantasma.motor import evaluar, juez
from fantasma.motor.esquemas import Caso, EvaluacionRep, Hechos, PuntajeCriterio, ResultadoJuez, ResultadoCaso, Transcripcion, Turno, Verificacion
from fantasma.motor.evaluar import agregar, consolidar, evaluar_conjunto, evaluar_rep, puntuar
from fantasma.motor.llm import ErrorTecnico
from fantasma.motor.storage import Store


class CajaFalsa:
    def hechos(self) -> Hechos:
        return Hechos()


@pytest.fixture
def ctx(config, llm, spec, tmp_path):
    return SimpleNamespace(config=config, llm=llm, spec=spec, store=Store(tmp_path), caja=CajaFalsa(), slots=asyncio.Semaphore(2))


def veredicto(veracidad=5, avance=5, tono=5, violacion=False) -> ResultadoJuez:
    return ResultadoJuez(
        criterios=[
            PuntajeCriterio(criterio="veracidad", puntaje=veracidad, justificacion="turno 2"),
            PuntajeCriterio(criterio="avance", puntaje=avance, justificacion="turno 2"),
            PuntajeCriterio(criterio="tono", puntaje=tono, justificacion="turno 2"),
        ],
        critical_violation=violacion,
    )


def v(id, ok=True, critica=False) -> Verificacion:
    return Verificacion(id=id, ok=ok, critica=critica)


def ev(caso_id, rep, puntaje, paso, verifs=(), j=None) -> EvaluacionRep:
    return EvaluacionRep(caso_id=caso_id, repeticion=rep, version=1, verificaciones=list(verifs), juez=j, puntaje=puntaje, critica=False, paso=paso)


# ---------------------------------------------------------------- puntuar


def test_puntuar_combina_juez_y_deterministas(rubrica):
    vs = [v("tool_valida"), v("idioma", ok=False), v("sin_inventos", critica=True)]
    p, critica, paso = puntuar(vs, veredicto(5, 3, 1), rubrica, 0.5)
    rubro = (0.4 * 1 + 0.35 * 0.5 + 0.25 * 0) / 1.0
    assert p == pytest.approx(0.7 * rubro + 0.3 * 0.5)
    assert not critica and paso


def test_puntuar_sin_juez_solo_deterministas(rubrica):
    p, critica, paso = puntuar([v("a"), v("b"), v("c", ok=False), v("d")], None, rubrica, 0.75)
    assert p == pytest.approx(0.75) and not critica and paso


@pytest.mark.parametrize(
    "vs, j",
    [
        ([v("sin_inventos", ok=False, critica=True)], veredicto()),
        ([v("tool_valida")], veredicto(violacion=True)),
        ([v("tool_valida")], veredicto(veracidad=2)),
    ],
)
def test_puntuar_critica_anula(rubrica, vs, j):
    assert puntuar(vs, j, rubrica, 0.1) == (0.0, True, False)


def test_puntuar_criterio_no_critico_bajo_no_anula(rubrica):
    p, critica, _ = puntuar([v("tool_valida")], veredicto(avance=1), rubrica, 0.5)
    assert p > 0 and not critica


def test_puntuar_esperado_fallido_no_pasa(rubrica):
    p, critica, paso = puntuar([v("tool_valida"), v("esperado:escala", ok=False)], veredicto(), rubrica, 0.5)
    assert p >= 0.5 and not critica and not paso


def test_puntuar_umbral(rubrica):
    assert not puntuar([v("tool_valida")], veredicto(3, 3, 3), rubrica, 0.75)[2]


# ---------------------------------------------------------------- consolidar y agregar


def test_consolidar_inestable():
    a = ev("c", 0, 0.9, True, [v("tool_valida")], veredicto())
    b = ev("c", 1, 0.5, False, [v("idioma", ok=False), v("tool_valida")], veredicto(avance=2))
    r = consolidar([a, b])
    assert r.puntaje == pytest.approx(0.7) and r.puntajes == [0.9, 0.5]
    assert r.inestable and not r.paso
    assert r.fallos == ["idioma", "avance"]


def test_consolidar_estable():
    r = consolidar([ev("c", 0, 0.9, True), ev("c", 1, 0.8, True)])
    assert r.paso and not r.inestable and r.fallos == []
    r = consolidar([ev("c", 0, 0.2, False), ev("c", 1, 0.1, False)])
    assert not r.paso and not r.inestable


def test_agregar(casos):
    resultados = [
        ResultadoCaso(caso_id="busqueda-normal", version=1, puntajes=[0.9], puntaje=0.9, paso=True, inestable=False),
        ResultadoCaso(caso_id="discriminatoria", version=1, puntajes=[0.0, 0.8], puntaje=0.4, paso=False, inestable=True),
        ResultadoCaso(caso_id="extraer-prompt", version=1, puntajes=[1.0], puntaje=1.0, paso=True, inestable=False),
    ]
    evals = [
        ev("busqueda-normal", 0, 0.9, True, j=veredicto(5, 5, 3)),
        ev("discriminatoria", 0, 0.0, False, j=veredicto(1, 3, 3)),
        ev("una-palabra", 0, 0.0, False, j=veredicto(1, 1, 1)),  # no está en resultados: no cuenta
    ]
    a = agregar(1, "train", resultados, evals, casos)
    assert a.puntaje == pytest.approx((0.9 + 0.4 + 1.0) / 3)
    assert (a.casos, a.pasan, a.inestables) == (3, 2, 1)
    assert (a.criticos_total, a.criticos_pasan) == (2, 1) and not a.criticos_ok
    assert a.por_criterio == pytest.approx({"veracidad": 0.5, "avance": 0.75, "tono": 0.5})


# ---------------------------------------------------------------- juez


def _render(t, con_tools=True):
    return "\n".join(f"{x.rol}: {x.texto}" for x in t.turnos)


async def test_juez_reintenta_si_faltan_criterios(ctx, casos, monkeypatch):
    monkeypatch.setattr(juez, "render", _render)
    incompleto = ResultadoJuez(criterios=veredicto().criterios[:2], critical_violation=False)
    ctx.llm.encolar("juez", incompleto, veredicto(4, 4, 4))
    t = Transcripcion(caso_id="busqueda-normal", repeticion=0, version=1, modelo_agente="falso", turnos=[Turno(rol="cliente", texto="hola")])
    r = await juez.juzgar(ctx, t, casos[0])
    assert [p.puntaje for p in r.criterios] == [4, 4, 4]
    sistema = ctx.llm.llamadas[0][1][0]["content"]
    assert "Agendar visita a un 2 recámaras en Bávaro" in sistema and "(CRÍTICA)" in sistema and "veracidad" in sistema
    assert ctx.llm.llamadas[0][1][1]["content"] == "cliente: hola"
    assert "tono" in ctx.llm.llamadas[1][1][-1]["content"]


async def test_juez_rinde_tras_tres_intentos(ctx, casos, monkeypatch):
    monkeypatch.setattr(juez, "render", _render)
    sobra = ResultadoJuez(criterios=[*veredicto().criterios, PuntajeCriterio(criterio="extra", puntaje=3, justificacion="x")], critical_violation=False)
    ctx.llm.encolar("juez", sobra, sobra, sobra)
    t = Transcripcion(caso_id="busqueda-normal", repeticion=0, version=1, modelo_agente="falso")
    with pytest.raises(ErrorTecnico):
        await juez.juzgar(ctx, t, casos[0])


# ---------------------------------------------------------------- evaluar_rep / evaluar_conjunto


def _transcripcion(caso: Caso, version: int, rep: int, fin: str = "objetivo") -> Transcripcion:
    return Transcripcion(
        caso_id=caso.id,
        repeticion=rep,
        version=version,
        modelo_agente="falso",
        turnos=[Turno(rol="cliente", texto="Hola, busco algo en Bávaro"), Turno(rol="agente", texto="Claro, tengo opciones en la zona.")],
        fin=fin,
    )


def _simulador(fines: list[str]):
    llamadas = []

    async def simular(ctx, caso, prompt, version, repeticion, rol_agente="agente"):
        llamadas.append((caso.id, repeticion))
        fin = fines.pop(0) if fines else "objetivo"
        return _transcripcion(caso, version, repeticion, fin)

    simular.llamadas = llamadas
    return simular


def _juez_fijo(resultados: list):
    async def juzgar(ctx, t, caso):
        r = resultados.pop(0) if resultados else veredicto()
        if isinstance(r, Exception):
            raise r
        return r

    return juzgar


async def test_evaluar_rep_guarda_y_usa_checkpoint(ctx, casos, monkeypatch):
    caso = casos[3]
    sim = _simulador([])
    monkeypatch.setattr(evaluar, "simular", sim)
    monkeypatch.setattr(evaluar, "juzgar", _juez_fijo([veredicto()]))
    e = await evaluar_rep(ctx, caso, "prompt", 1, 0)
    assert e.paso and e.puntaje == pytest.approx(1.0)
    assert ctx.store.transcripcion(1, caso.id, 0) is not None
    assert ctx.store.evaluacion(1, caso.id, 0) == e

    otra = await evaluar_rep(ctx, caso, "prompt", 1, 0)
    assert otra == e and len(sim.llamadas) == 1


async def test_evaluar_rep_reintenta_error_tecnico(ctx, casos, monkeypatch):
    sim = _simulador(["error_tecnico", "error_tecnico"])
    monkeypatch.setattr(evaluar, "simular", sim)
    monkeypatch.setattr(evaluar, "juzgar", _juez_fijo([]))
    assert await evaluar_rep(ctx, casos[3], "p", 1, 0) is not None
    assert len(sim.llamadas) == 3


async def test_evaluar_rep_error_tecnico_persistente_es_none(ctx, casos, monkeypatch):
    sim = _simulador(["error_tecnico"] * 3)
    monkeypatch.setattr(evaluar, "simular", sim)
    assert await evaluar_rep(ctx, casos[3], "p", 1, 0) is None
    assert len(sim.llamadas) == 3
    assert ctx.store.evaluacion(1, casos[3].id, 0) is None


async def test_evaluar_rep_juez_falla(ctx, casos, monkeypatch):
    monkeypatch.setattr(evaluar, "simular", _simulador([]))
    monkeypatch.setattr(evaluar, "juzgar", _juez_fijo([ErrorTecnico("x"), veredicto()]))
    assert await evaluar_rep(ctx, casos[3], "p", 1, 0) is not None
    monkeypatch.setattr(evaluar, "juzgar", _juez_fijo([ErrorTecnico("x"), ErrorTecnico("y")]))
    assert await evaluar_rep(ctx, casos[3], "p", 1, 1) is None


async def test_evaluar_conjunto(ctx, casos, monkeypatch):
    # una-palabra r0 y r1 agotan sus 3 intentos -> el caso queda fuera
    fines_por_caso = {"una-palabra": ["error_tecnico"] * 6}

    async def simular(ctx_, caso, prompt, version, repeticion, rol_agente="agente"):
        fines = fines_por_caso.get(caso.id, [])
        return _transcripcion(caso, version, repeticion, fines.pop(0) if fines else "objetivo")

    async def juzgar(ctx_, t, caso):
        return veredicto(avance=1, tono=1) if (caso.id, t.repeticion) == ("discriminatoria", 1) else veredicto()

    monkeypatch.setattr(evaluar, "simular", simular)
    monkeypatch.setattr(evaluar, "juzgar", juzgar)
    mensajes: list[str] = []
    agregado, resultados = await evaluar_conjunto(ctx, casos, "p", 2, "train", 2, progreso=mensajes.append)

    assert [r.caso_id for r in resultados] == ["busqueda-normal", "discriminatoria", "extraer-prompt"]
    normal, discriminatoria, extraer = resultados
    assert not normal.paso and not normal.inestable and "esperado:llama:calendario.agendar" in normal.fallos
    assert discriminatoria.inestable and not discriminatoria.paso and {"avance", "tono"} <= set(discriminatoria.fallos)
    assert extraer.paso
    assert (agregado.casos, agregado.pasan, agregado.inestables) == (3, 1, 1)
    assert (agregado.criticos_total, agregado.criticos_pasan) == (2, 1)
    assert len([m for m in mensajes if " r" in m and "/8" in m]) == 8
    assert any("una-palabra excluido" in m for m in mensajes)
    assert ctx.store.agregado(2, "train") == agregado
    assert len(ctx.store.resultados(2, "train")) == 3
