from types import SimpleNamespace

import pytest
import yaml

from fantasma import boleta
from fantasma.motor import evaluar, generador, loop
from fantasma.motor.esquemas import EvaluacionRep, VersionPrompt
from fantasma.motor.llm import ErrorTecnico
from fantasma.motor.storage import Store


def evaluada(caso_id, rep=0, version=0):
    return EvaluacionRep(caso_id=caso_id, repeticion=rep, version=version, verificaciones=[], juez=None,
                         puntaje=1, critica=False, paso=True)


async def test_error_tecnico_critico_no_aprueba_ni_desaparece(casos, tmp_path, monkeypatch):
    store = Store(tmp_path)
    ctx = SimpleNamespace(store=store)

    async def medir(ctx, caso, prompt, version, rep, rol):
        return None if caso.critico else evaluada(caso.id, rep)

    monkeypatch.setattr(evaluar, "evaluar_rep", medir)
    ag, resultados = await evaluar.evaluar_conjunto(ctx, casos[:2], "prompt", 0, "train", 2)
    assert ag.puntaje == 1  # El error de infraestructura no se atribuye al bot.
    assert ag.criticos_total == 1 and ag.criticos_pasan == 0 and not ag.criticos_ok
    assert ag.cobertura.casos_solicitados == 2
    assert ag.cobertura.casos_excluidos == 1
    assert ag.cobertura.repeticiones_solicitadas == 4
    assert ag.cobertura.repeticiones_evaluadas == 2
    assert ag.cobertura.criticos_faltantes == [casos[1].id]
    assert not ag.cobertura.completa
    assert store.agregado(0, "train").cobertura == ag.cobertura
    assert len(resultados) == 1


async def test_repeticion_faltante_no_se_consolida_como_paso(casos, tmp_path, monkeypatch):
    async def medir(ctx, caso, prompt, version, rep, rol):
        return evaluada(caso.id) if rep == 0 else None

    monkeypatch.setattr(evaluar, "evaluar_rep", medir)
    ag, resultados = await evaluar.evaluar_conjunto(SimpleNamespace(store=Store(tmp_path)), casos[:1], "p", 0, "train", 2)
    assert not resultados[0].paso and not resultados[0].completo
    assert ag.pasan == 0 and ag.cobertura.casos_completos == 0
    assert ag.cobertura.repeticiones_excluidas == 1


def test_boleta_reconstruye_cobertura_historica_y_no_aprueba_8_de_15(spec, tmp_path):
    store = Store(tmp_path)
    store.guardar_spec(spec)
    store.guardar_version(VersionPrompt(n=0, estado="base"), spec.prompt_base)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"casos": {"cantidad": 15}, "repetitions_per_case": 2}))
    for caso in spec.casos:
        for rep in range(2):
            store.guardar_evaluacion(evaluada(caso.id, rep))
    n = boleta.nota_version(store, 0)
    assert n.nota == 100
    assert n.cobertura.casos_solicitados == 15
    assert n.cobertura.casos_generados == len(spec.casos)
    assert boleta.veredicto(n) == "incompleta"
    b = boleta.armar(store, run_id="r", modo="remoto", estado="terminada", costo_usd=0, modelos={})
    assert b.estado == "incompleta" and b.veredicto_texto == "Evaluación incompleta"


def test_boleta_ignora_repeticiones_de_ruido_y_exige_las_previstas(spec, tmp_path):
    store = Store(tmp_path)
    store.guardar_spec(spec)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"repetitions_per_case": 2}))
    for caso in spec.casos:
        store.guardar_evaluacion(evaluada(caso.id, 0))
        store.guardar_evaluacion(evaluada(caso.id, 2))
    n = boleta.nota_version(store, 0)
    assert n.cobertura.repeticiones_evaluadas == len(spec.casos)
    assert not n.cobertura.completa
    for caso in spec.casos:
        store.guardar_evaluacion(evaluada(caso.id, 1))
    assert boleta.veredicto(boleta.nota_version(store, 0)) == "listo"


def test_aceptacion_exige_mismos_casos_y_repeticiones(casos):
    actual = [evaluar.consolidar([evaluada(c.id, r) for r in range(2)], 2) for c in casos]
    candidato = [evaluar.consolidar([evaluada(c.id)], 1) for c in casos]
    ag = evaluar.agregar(0, "train", actual, [], casos)
    assert not loop.aceptar(ag, ag, actual, candidato, 0, {c.id for c in casos if c.critico})[0]
    assert not loop.aceptar(ag, ag, actual, actual[:1], 0, {c.id for c in casos if c.critico})[0]
    assert loop.aceptar(ag, ag, actual, actual, 0, {c.id for c in casos if c.critico})[0]


async def test_generador_rechaza_cobertura_parcial(llm, rubro, monkeypatch):
    async def propios(*a):
        return []

    async def lote(llm, rubro, reglas, funciones, lote, *a):
        if len(lote) < generador.TAM_LOTE_CASOS:
            raise ErrorTecnico("lote agotó reintentos")
        return [generador.CasoBorrador(id=f.escenario, persona={"nombre": "N", "estilo": "c"}, objetivo_oculto="o") for f in lote]

    monkeypatch.setattr(generador, "_escenarios_propios", propios)
    monkeypatch.setattr(generador, "_lote_casos", lote)
    monkeypatch.setattr(generador, "funciones_validas", lambda caja: {})
    solicitados = generador.TAM_LOTE_CASOS + 1
    with pytest.raises(ErrorTecnico, match=f"solo salieron .* de {solicitados}"):
        await generador.generar_casos(llm, rubro, [], None, solicitados)


def test_version_historica_sin_cobertura_no_puede_declararse_lista(config):
    from fantasma.motor.esquemas import Corrida

    buena = VersionPrompt(n=0, train=1, holdout=1, criticos_ok=True)
    assert loop.debe_parar(Corrida(id="r", rubro="x"), buena, buena, config, 0, 0) is None
    buena.cobertura_train = buena.cobertura_holdout = True
    assert loop.debe_parar(Corrida(id="r", rubro="x"), buena, buena, config, 0, 0) == "listo"


async def test_loop_no_optimiza_linea_base_incompleta(spec, config, tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.guardar_spec(spec)
    ctx = SimpleNamespace(store=store, config=config, spec=spec)

    async def medir(ctx, casos, prompt, version, conjunto, reps, progreso=None):
        evals = [evaluada(c.id, 0) for c in casos]
        resultados = [evaluar.consolidar([e], reps) for e in evals]
        return evaluar.agregar(version, conjunto, resultados, evals, casos, reps), resultados

    async def no_proponer(*args):
        pytest.fail("No se debe optimizar sobre evidencia incompleta")

    monkeypatch.setattr(loop, "evaluar_conjunto", medir)
    monkeypatch.setattr(loop, "proponer", no_proponer)
    corrida = await loop.correr(ctx)
    assert corrida.estado == "error" and "incompleta" in corrida.motivo_parada
    assert store.version(0).cobertura_train is False
    assert store.version(0).cobertura_holdout is False


@pytest.mark.parametrize("estado", ["presupuesto", "error", "corriendo"])
def test_corrida_interrumpida_no_declara_listo_aunque_base_este_completa(spec, tmp_path, estado):
    store = Store(tmp_path)
    store.guardar_spec(spec)
    store.guardar_version(VersionPrompt(n=0, estado="base"), spec.prompt_base)
    for caso in spec.casos:
        store.guardar_evaluacion(evaluada(caso.id))
    b = boleta.armar(store, run_id="r", modo="generado", estado=estado, costo_usd=0, modelos={})
    assert b.mejor.cobertura.completa and b.mejor.nota == 100
    assert b.veredicto == "incompleta"


@pytest.mark.parametrize("limite_diario,limite_ip,patron", [(1, 10, "diario"), (10, 1, "visitante")])
def test_fichas_antiguas_cuentan_gasto_en_el_dia_actual(tmp_path, limite_diario, limite_ip, patron):
    from fantasma import costos
    from fantasma.indice import Indice

    indice = Indice(tmp_path / "indice.sqlite")
    indice.crear("a", "visitante")
    indice.crear("b", "visitante")
    indice.db.execute("UPDATE corridas SET creado = '2026-01-01T00:00:00+00:00', estado = 'ficha'")
    indice.reservar("a", .7, 1, limite_diario, limite_ip)
    with pytest.raises(costos.PresupuestoAgotado, match=patron):
        indice.reservar("b", .7, 1, limite_diario, limite_ip)
    indice.liquidar("a", .7, .7)
    assert indice.gasto_del_dia() == pytest.approx(.7)
    assert indice.gasto_del_dia_de("visitante") == pytest.approx(.7)
    with pytest.raises(costos.PresupuestoAgotado, match=patron):
        indice.reservar("b", .4, 1, limite_diario, limite_ip)


def test_reserva_cruza_medianoche_sin_perder_limite_ni_gasto(tmp_path, monkeypatch):
    from fantasma import costos
    from fantasma import indice as modulo

    hoy = ["2026-10-14"]
    monkeypatch.setattr(modulo, "_hoy", lambda: hoy[0])
    indice = modulo.Indice(tmp_path / "indice.sqlite")
    indice.crear("a", "visitante")
    indice.crear("b", "visitante")
    indice.reservar("a", .3, 2, 2, 2)
    indice.liquidar("a", .3, .3)
    indice.reservar("a", .7, 2, 2, 2)
    hoy[0] = "2026-10-15"
    # El trabajo enviado ayer sigue siendo una deuda en vuelo hoy.
    with pytest.raises(costos.PresupuestoAgotado, match="diario"):
        indice.reservar("b", .4, 2, 1, 2)
    with pytest.raises(costos.PresupuestoAgotado, match="visitante"):
        indice.reservar("b", .4, 2, 2, 1)
    indice.liquidar("a", .7, .2)
    assert indice.gasto_del_dia() == pytest.approx(.2)
    assert indice.gasto_del_dia_de("visitante") == pytest.approx(.2)
    indice.reservar("b", .7, 2, 1, 1)
    indice.liquidar("b", .7, .7)
    assert indice.gasto_del_dia() == pytest.approx(.9)
    assert indice.gasto_del_dia_de("visitante") == pytest.approx(.9)
    assert indice.obtener("a").costo_usd == pytest.approx(.5)
    hoy[0] = "2026-10-14"
    assert indice.gasto_del_dia() == pytest.approx(.3)
