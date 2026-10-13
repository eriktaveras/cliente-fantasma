"""Optimizador y loop: aceptar, debe_parar, validar_propuesta, banco corto y una corrida completa con evaluación falsa."""

import importlib.util
import sys
import types
from types import SimpleNamespace

import pytest

# Mientras evaluar/simulador no existan, se sustituyen por stubs; los tests de abajo los monkeypatchean igual.
for _nombre, _attrs in {
    "fantasma.motor.simulador": ("render", "simular"),
    "fantasma.motor.evaluar": ("agregar", "consolidar", "evaluar_conjunto", "evaluar_rep", "puntuar"),
}.items():
    if importlib.util.find_spec(_nombre) is None:
        _mod = types.ModuleType(_nombre)
        for _a in _attrs:
            setattr(_mod, _a, lambda *a, **k: None)
        sys.modules[_nombre] = _mod

from fantasma.motor import loop, optimizador
from fantasma.motor.esquemas import Agregado, Caso, Corrida, EvaluacionRep, Persona, Propuesta, ResultadoCaso, VersionPrompt
from fantasma.motor.loop import aceptar, banco_corto, debe_parar
from fantasma.motor.optimizador import validar_propuesta
from fantasma.motor.storage import Store

REGLA = "Nunca inventes propiedades, precios ni disponibilidad."


def ag(puntaje: float, version: int = 0, conjunto: str = "train") -> Agregado:
    return Agregado(version=version, conjunto=conjunto, puntaje=puntaje, casos=2, pasan=2, criticos_total=1, criticos_pasan=1, inestables=0)


def res(caso_id: str, paso: bool, puntaje: float = 0.8, version: int = 0) -> ResultadoCaso:
    return ResultadoCaso(caso_id=caso_id, version=version, puntajes=[puntaje], puntaje=puntaje, paso=paso, inestable=False)


# ---------------------------------------------------------------- aceptar


def test_aceptar_sube_poco():
    ok, motivo = aceptar(ag(0.70), ag(0.71), [], [], 0.02, set())
    assert not ok and "mejora" in motivo


def test_aceptar_sube_bien():
    actual = [res("a", True), res("crit", True)]
    ok, _ = aceptar(ag(0.70), ag(0.75), actual, actual, 0.02, {"crit"})
    assert ok


def test_aceptar_rompe_critico():
    actual = [res("a", False), res("crit", True)]
    cand = [res("a", True), res("crit", False)]
    ok, motivo = aceptar(ag(0.60), ag(0.90), actual, cand, 0.02, {"crit"})
    assert not ok and "crit" in motivo


def test_aceptar_critico_que_ya_fallaba_no_bloquea():
    actual = [res("crit", False)]
    ok, _ = aceptar(ag(0.60), ag(0.70), actual, actual, 0.02, {"crit"})
    assert ok


# ---------------------------------------------------------------- debe_parar


@pytest.fixture
def cfg_parada(config):
    config.metas.target_train_score = 0.9
    config.metas.target_holdout_score = 0.85
    config.presupuesto.max_iterations = 10
    config.presupuesto.max_hours = 2.0
    config.presupuesto.plateau_patience = 3
    return config


def v(n=0, train=0.5, holdout=0.5, criticos_ok=True, estado="aceptada") -> VersionPrompt:
    return VersionPrompt(n=n, train=train, holdout=holdout, criticos_ok=criticos_ok, estado=estado)


def test_debe_parar_sigue(cfg_parada):
    assert debe_parar(Corrida(id="x", rubro="r", iteracion=2), v(), v(), cfg_parada, 0.5, 1) is None


def test_debe_parar_listo(cfg_parada):
    buena = v(train=0.92, holdout=0.88)
    assert debe_parar(Corrida(id="x", rubro="r"), buena, buena, cfg_parada, 0, 0) == "listo"


def test_debe_parar_no_listo_si_falla_critico_o_holdout(cfg_parada):
    corrida = Corrida(id="x", rubro="r")
    assert debe_parar(corrida, v(train=0.95, holdout=0.9, criticos_ok=False), v(), cfg_parada, 0, 0) is None
    assert debe_parar(corrida, v(train=0.95, holdout=0.80), v(), cfg_parada, 0, 0) is None
    assert debe_parar(corrida, v(train=0.95, holdout=None), v(), cfg_parada, 0, 0) is None


def test_debe_parar_meseta(cfg_parada):
    assert debe_parar(Corrida(id="x", rubro="r"), v(), v(), cfg_parada, 0, 3) == "meseta"


def test_debe_parar_presupuesto_iteraciones_y_horas(cfg_parada):
    assert debe_parar(Corrida(id="x", rubro="r", iteracion=10), v(), v(), cfg_parada, 0, 0) == "presupuesto"
    assert debe_parar(Corrida(id="x", rubro="r", iteracion=1), v(), v(), cfg_parada, 2.0, 0) == "presupuesto"


# ---------------------------------------------------------------- validar_propuesta


def prop(prompt: str) -> Propuesta:
    return Propuesta(hipotesis="h", cambio="c", prompt=prompt)


def test_validar_identico(spec):
    assert "idéntico" in validar_propuesta(prop(spec.prompt_base.upper() + "  "), spec.prompt_base, spec, 2500)


def test_validar_largo(spec):
    assert "largo" in validar_propuesta(prop(spec.prompt_base + " x" * 200), spec.prompt_base, spec, 100)


def test_validar_fuga_nombre(spec):
    nuevo = spec.prompt_base + "\nSi el cliente se llama Kelvin, sé breve."
    assert "nombre" in validar_propuesta(prop(nuevo), spec.prompt_base, spec, 2500)


def test_validar_nombre_ya_presente_no_cuenta(spec):
    actual = spec.prompt_base + "\nEjemplo: Luis pregunta por precios."
    nuevo = actual + "\nConfirma la fecha antes de agendar."
    assert validar_propuesta(prop(nuevo), actual, spec, 2500) is None


def test_validar_fuga_5grama(spec):
    nuevo = spec.prompt_base + "\nSi alguien quiere agendar visita a un 2 recamaras, ofrece horario."
    assert "filtra datos" in validar_propuesta(prop(nuevo), spec.prompt_base, spec, 2500)


def test_validar_quita_regla_critica(spec):
    nuevo = "Eres el asistente de Inmobiliaria Prueba.\nSé amable y breve con cada cliente."
    assert "no_inventar" in validar_propuesta(prop(nuevo), spec.prompt_base, spec, 2500)


def test_validar_reformular_regla_critica_vale(spec):
    nuevo = "Eres el asistente de Inmobiliaria Prueba.\nNunca inventes precios, propiedades o disponibilidad: usa el catálogo."
    assert validar_propuesta(prop(nuevo), spec.prompt_base, spec, 2500) is None


def test_validar_propuesta_valida(spec):
    nuevo = spec.prompt_base + "\nAntes de agendar, confirma día y hora con el cliente."
    assert validar_propuesta(prop(nuevo), spec.prompt_base, spec, 2500) is None


# ---------------------------------------------------------------- banco corto


def _caso(i: str, critico: bool = False) -> Caso:
    return Caso(
        id=i, escenario="e", persona=Persona(nombre="X", estilo="s"), objetivo_oculto="o",
        etiquetas={"critical"} if critico else {"normal"},
    )


def test_banco_corto_determinista():
    casos = [_caso(i) for i in ("e", "d", "c", "b", "a")] + [_caso("z-crit", critico=True)]
    resultados = [res("d", False), res("a", True), res("b", True)]
    banco = banco_corto(casos, resultados, 4)
    assert [c.id for c in banco] == ["a", "b", "d", "z-crit"]
    assert [c.id for c in banco_corto(list(reversed(casos)), list(reversed(resultados)), 4)] == [c.id for c in banco]


def test_banco_corto_no_recorta_prioritarios():
    casos = [_caso("a", critico=True), _caso("b"), _caso("c")]
    banco = banco_corto(casos, [res("b", False), res("c", False)], 1)
    assert [c.id for c in banco] == ["a", "b", "c"]


# ---------------------------------------------------------------- loop completo


def prompt(n: int) -> str:
    return f"Eres el asistente de Inmobiliaria Prueba.\n{REGLA}\nCambio número {n}."


PUNTAJES = {"base": 0.50, prompt(1): 0.60, prompt(3): 0.55, prompt(4): 0.58}


class EvalFalsa:
    """Puntaje fijo por texto de prompt; registra cada llamada."""

    def __init__(self, puntajes: dict[str, float]):
        self.puntajes = puntajes
        self.llamadas: list[tuple[int, str, int]] = []

    def puntaje(self, texto: str) -> float:
        return self.puntajes.get(texto, self.puntajes["base"])

    async def conjunto(self, ctx, casos, prompt, version, conjunto, reps, rol_agente="agente", progreso=None):
        self.llamadas.append((version, conjunto, reps))
        p = self.puntaje(prompt)
        resultados = [ResultadoCaso(caso_id=c.id, version=version, puntajes=[p] * reps, puntaje=p, paso=True, inestable=False) for c in casos]
        criticos = sum(c.critico for c in casos)
        return Agregado(
            version=version, conjunto=conjunto, puntaje=p, casos=len(casos), pasan=len(casos),
            criticos_total=criticos, criticos_pasan=criticos, inestables=0,
        ), resultados

    async def rep(self, ctx, caso, prompt, version, repeticion, rol_agente="agente"):
        p = self.puntaje(prompt)
        return EvaluacionRep(caso_id=caso.id, repeticion=repeticion, version=version, verificaciones=[], juez=None, puntaje=p, critica=False, paso=True)


def agregar_falso(version, conjunto, resultados, evals, casos):
    p = sum(r.puntaje for r in resultados) / len(resultados)
    return Agregado(version=version, conjunto=conjunto, puntaje=p, casos=len(resultados), pasan=len(resultados), criticos_total=0, criticos_pasan=0, inestables=0)


def consolidar_falso(evals):
    p = sum(e.puntaje for e in evals) / len(evals)
    return ResultadoCaso(caso_id=evals[0].caso_id, version=evals[0].version, puntajes=[e.puntaje for e in evals], puntaje=p, paso=True, inestable=False)


@pytest.fixture
def entorno(monkeypatch, tmp_path, spec, config, llm):
    PUNTAJES["base"] = 0.50
    PUNTAJES[spec.prompt_base] = 0.50
    ev = EvalFalsa(PUNTAJES)
    monkeypatch.setattr(loop, "evaluar_conjunto", ev.conjunto)
    monkeypatch.setattr(loop, "evaluar_rep", ev.rep)
    monkeypatch.setattr(loop, "agregar", agregar_falso)
    monkeypatch.setattr(loop, "consolidar", consolidar_falso)
    config.metas.target_train_score = 0.99
    config.presupuesto.plateau_patience = 2
    config.presupuesto.max_iterations = 10
    config.optimizacion.holdout_cada = 1
    ctx = SimpleNamespace(config=config, llm=llm, spec=spec, store=Store(tmp_path / "corrida"))
    return ctx, ev


def cola_propuestas(monkeypatch, propuestas: list[Propuesta]):
    vistas = []

    async def proponer(ctx, prompt_actual, agregado, fallos, versiones, lecciones):
        vistas.append((prompt_actual, [v.estado for v in versiones], list(lecciones)))
        return propuestas.pop(0)

    monkeypatch.setattr(loop, "proponer", proponer)
    return vistas


async def test_loop_acepta_rechaza_y_para_por_meseta(monkeypatch, entorno):
    ctx, _ = entorno
    vistas = cola_propuestas(monkeypatch, [prop(prompt(1)), prop(prompt(1)), prop(prompt(3))])
    avisos: list[str] = []

    corrida = await loop.correr(ctx, avisos.append)

    store = ctx.store
    estados = {v.n: (v.estado, v.motivo) for v in store.versiones()}
    assert estados[0][0] == "base"
    assert estados[1][0] == "aceptada"
    assert estados[2] == ("rechazada", "prompt idéntico al actual")
    assert estados[3][0] == "rechazada" and estados[3][1].startswith("etapa A: 0.550 vs 0.600")
    assert corrida.estado == "meseta" == corrida.motivo_parada
    assert corrida.iteracion == 3
    assert corrida.mejor_version == 1
    assert corrida.min_improvement == pytest.approx(0.02)
    assert (store.dir / "final_prompt.md").read_text() == prompt(1)
    assert store.corrida().estado == "meseta"
    assert store.version(1).holdout == pytest.approx(0.60)
    # la tercera propuesta parte de v001 y ve el rechazo anotado
    assert vistas[2][0] == prompt(1)
    assert any("v002 rechazada" in l for l in vistas[2][2])
    # ruido medido con evaluar_rep sobre las reps desplazadas, no con evaluar_conjunto
    assert any("ruido" in a for a in avisos) and any("iteración" in a for a in avisos)


async def test_loop_rechaza_en_etapa_b_por_mejora_insuficiente(monkeypatch, entorno):
    ctx, _ = entorno
    PUNTAJES[prompt(5)] = 0.51
    PUNTAJES[prompt(6)] = 0.505
    cola_propuestas(monkeypatch, [prop(prompt(5)), prop(prompt(6))])

    corrida = await loop.correr(ctx)

    v1, v2 = ctx.store.version(1), ctx.store.version(2)
    assert v1.estado == "rechazada" and v1.motivo.startswith("etapa B: mejora +0.010")
    assert v2.estado == "rechazada"
    assert corrida.estado == "meseta" and corrida.mejor_version == 0
    assert (ctx.store.dir / "final_prompt.md").read_text() == ctx.spec.prompt_base


async def test_loop_revierte_por_sobreajuste(monkeypatch, entorno):
    ctx, ev = entorno
    texto = prompt(7)
    PUNTAJES[texto] = 0.80

    async def conjunto(ctx_, casos, prompt_, version, conjunto, reps, rol_agente="agente", progreso=None):
        a, r = await EvalFalsa.conjunto(ev, ctx_, casos, prompt_, version, conjunto, reps)
        if prompt_ == texto and conjunto == "holdout":
            a.puntaje = 0.30
        return a, r

    monkeypatch.setattr(loop, "evaluar_conjunto", conjunto)
    cola_propuestas(monkeypatch, [prop(texto), prop(prompt(3)), prop(prompt(3)), prop(prompt(3))])

    corrida = await loop.correr(ctx)

    v1 = ctx.store.version(1)
    assert v1.estado == "revertida" and "sobreajuste" in v1.motivo
    assert ctx.store.version(2).padre == 0
    assert any("sobreajuste" in l for l in ctx.store.historial())
    assert ctx.store.version(2).estado == "aceptada"
    assert corrida.estado == "meseta" and corrida.mejor_version == 2


async def test_loop_para_listo(monkeypatch, entorno):
    ctx, _ = entorno
    ctx.config.metas.target_train_score = 0.9
    ctx.config.metas.target_holdout_score = 0.85
    PUNTAJES[prompt(8)] = 0.95
    cola_propuestas(monkeypatch, [prop(prompt(8))])

    corrida = await loop.correr(ctx)

    assert corrida.estado == "listo" and corrida.mejor_version == 1


async def test_loop_retoma_candidata_pendiente(monkeypatch, entorno):
    ctx, _ = entorno
    store = ctx.store
    store.guardar_version(VersionPrompt(n=0, estado="base"), ctx.spec.prompt_base)
    store.guardar_version(VersionPrompt(n=1, padre=0, estado="candidata", cambio="pendiente"), prompt(1))
    store.guardar_corrida(Corrida(id="corrida", rubro="inmobiliaria", horas=1.5, min_improvement=0.02))
    cola_propuestas(monkeypatch, [prop(prompt(3)), prop(prompt(4))])

    corrida = await loop.correr(ctx)

    assert store.version(1).estado == "aceptada"
    assert corrida.horas >= 1.5
    assert corrida.mejor_version == 1


async def test_proponer_anonimiza_y_arma_mensaje(monkeypatch, spec, config, llm):
    from fantasma.motor.esquemas import LlamadaTool, PuntajeCriterio, ResultadoJuez, ResultadoTool, Transcripcion, Turno, Verificacion

    def render_falso(t, con_tools=True):
        return "\n".join(f"{tu.rol}: {tu.texto}" for tu in t.turnos)

    monkeypatch.setattr(optimizador, "render", render_falso)
    caso = spec.casos[0]
    t = Transcripcion(
        caso_id=caso.id, repeticion=0, version=0, modelo_agente="falso",
        turnos=[Turno(rol="cliente", texto="Hola, soy Luis"), Turno(rol="agente", texto="Hola Luis", tools=[LlamadaTool(nombre="faq.buscar", args={}, resultado=ResultadoTool(ok=True))])],
    )
    e = EvaluacionRep(
        caso_id=caso.id, repeticion=0, version=0, puntaje=0.4, critica=False, paso=False,
        verificaciones=[Verificacion(id="llama:calendario.agendar", ok=False, detalle="no agendó")],
        juez=ResultadoJuez(criterios=[PuntajeCriterio(criterio="avance", puntaje=2, justificacion="Luis se quedó sin cita")], critical_violation=False),
    )
    llm.encolar("optimizador", prop(prompt(1)))
    ctx = SimpleNamespace(config=config, llm=llm, spec=spec)
    versiones = [VersionPrompt(n=0, estado="base"), VersionPrompt(n=1, estado="rechazada", cambio="agregar saludo", motivo="etapa A")]

    p = await optimizador.proponer(ctx, spec.prompt_base, ag(0.5), [(caso, t, e)], versiones, ["no repetir saludos"])

    assert p.prompt == prompt(1)
    rol, mensajes = llm.llamadas[-1]
    cuerpo = mensajes[-1]["content"]
    assert rol == "optimizador"
    assert "Luis" not in cuerpo and "CLIENTE" in cuerpo
    assert "no agendó" in cuerpo and "CLIENTE se quedó sin cita" in cuerpo
    assert REGLA in cuerpo and "agregar saludo" in cuerpo and "no repetir saludos" in cuerpo
    assert "UN SOLO cambio" in mensajes[0]["content"]
