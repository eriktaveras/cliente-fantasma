import json

import pytest

from fantasma import boleta
from fantasma.motor.esquemas import (
    EvaluacionRep,
    PuntajeCriterio,
    ResultadoJuez,
    Transcripcion,
    Turno,
    Verificacion,
    VersionPrompt,
)
from fantasma.motor.storage import Store


def conversacion(*textos: str, caso_id="busqueda-normal", version=0, rep=0) -> Transcripcion:
    return Transcripcion(
        caso_id=caso_id,
        repeticion=rep,
        version=version,
        modelo_agente="falso",
        turnos=[Turno(rol="cliente" if i % 2 == 0 else "agente", texto=t) for i, t in enumerate(textos)],
        fin="objetivo",
    )


def juez(rubrica, puntajes: dict[str, int], critico: str | None = None, justificaciones: dict[str, str] | None = None) -> ResultadoJuez:
    return ResultadoJuez(
        criterios=[PuntajeCriterio(criterio=c.id, justificacion=(justificaciones or {}).get(c.id, "cumple"), puntaje=puntajes.get(c.id, 5)) for c in rubrica.criterios],
        critical_violation=critico is not None,
        critical_motivo=critico,
    )


def evaluacion(caso_id, puntaje, *, version=0, rep=0, critica=False, paso=None, verificaciones=(), j=None) -> EvaluacionRep:
    return EvaluacionRep(
        caso_id=caso_id,
        repeticion=rep,
        version=version,
        verificaciones=list(verificaciones),
        juez=j,
        puntaje=puntaje,
        critica=critica,
        paso=(puntaje >= 0.75 and not critica) if paso is None else paso,
    )


@pytest.fixture
def store(tmp_path, spec):
    s = Store(tmp_path)
    s.guardar_spec(spec)
    s.guardar_version(VersionPrompt(n=0, estado="base", train=0.5, holdout=0.4), spec.prompt_base)
    return s


def guardar(store: Store, t: Transcripcion, e: EvaluacionRep) -> None:
    store.guardar_transcripcion(t)
    store.guardar_evaluacion(e)


# ---------------------------------------------------------------- cita


def test_cita_por_frase_entre_comillas():
    t = conversacion("hola", "Buenas, ¿qué buscas?", "un 2 recámaras", "Tengo uno en Bávaro a US$99,000 con 10% de descuento.")
    assert boleta.cita(t, "Prometió «10% de descuento» sin política") == (3, t.turnos[3].texto)


def test_cita_por_numero_de_turno_en_ambos_conteos():
    t = conversacion("hola", "primera respuesta", "y?", "segunda respuesta")
    assert boleta.cita(t, "turno 4: inventa")[0] == 3  # cuenta todos los mensajes
    assert boleta.cita(t, "turno 1: inventa")[0] == 1  # turno 1 es del cliente: cuenta solo los del agente


def test_cita_por_monto_inventado_y_respaldo():
    t = conversacion("hola", "Hola!", "precio?", "Sale en US$999 nada más.", "ok", "Gracias por escribir.")
    assert boleta.cita(t, "monto 999") == (3, "Sale en US$999 nada más.")
    assert boleta.cita(t, "sin pista") == (5, "Gracias por escribir.")
    assert boleta.cita(conversacion("hola"), "x") == (None, "")


# ---------------------------------------------------------------- notas y veredicto


@pytest.mark.parametrize(
    "nota,criticas,casos,esperado",
    [(90, 0, 15, "listo"), (85, 0, 15, "listo"), (90, 1, 15, "ajustes"), (70, 0, 15, "ajustes"), (59, 0, 15, "no_lo_sueltes"), (90, 2, 15, "no_lo_sueltes"), (90, 6, 40, "no_lo_sueltes"), (90, 5, 40, "ajustes")],
)
def test_veredicto(nota, criticas, casos, esperado):
    assert boleta.veredicto(boleta.NotaVersion(version=0, nota=nota, casos=casos, pasan=0, criticas=criticas)) == esperado


def test_nota_promedia_por_caso_y_cuenta_criticas(store, rubrica):
    (store.dir / "config.yaml").write_text("repetitions_per_case: 2\n")
    guardar(store, conversacion("a", "b"), evaluacion("busqueda-normal", 1.0, j=juez(rubrica, {})))
    guardar(store, conversacion("a", "b", rep=1), evaluacion("busqueda-normal", 0.5, rep=1, j=juez(rubrica, {})))
    guardar(store, conversacion("a", "b", caso_id="discriminatoria"), evaluacion("discriminatoria", 0.0, critica=True, j=juez(rubrica, {"veracidad": 1}, "x")))
    n = boleta.nota_version(store, 0)
    assert (n.nota, n.casos, n.pasan, n.criticas) == (38, 2, 0, 1)  # (0.75 + 0) / 2


def test_criterios_antes_y_despues(store, spec, rubrica):
    guardar(store, conversacion("a", "b"), evaluacion("busqueda-normal", 0.5, j=juez(rubrica, {"avance": 1})))
    guardar(store, conversacion("a", "b", version=1), evaluacion("busqueda-normal", 1.0, version=1, j=juez(rubrica, {"avance": 5})))
    por_id = {c.id: c for c in boleta._criterios(store, spec, 0, 1)}
    assert (por_id["avance"].antes, por_id["avance"].despues) == (0, 100)
    assert por_id["veracidad"].critico


def test_metricas_excluyen_repeticiones_de_ruido_consistentemente(store, spec, rubrica):
    (store.dir / "config.yaml").write_text("repetitions_per_case: 1\n")
    spec.casos[0].ejes = {"dialecto": "rd"}
    store.guardar_spec(spec)
    guardar(store, conversacion("a", "b"), evaluacion("busqueda-normal", 1.0, j=juez(rubrica, {"avance": 5})))
    guardar(store, conversacion("a", "b", rep=1), evaluacion("busqueda-normal", 0.0, rep=1, j=juez(rubrica, {"avance": 1})))

    assert boleta.nota_version(store, 0).nota == 100
    assert {c.id: c for c in boleta._criterios(store, spec, 0, 0)}["avance"].antes == 100
    assert boleta.por_eje(store, spec, 0)["dialecto"]["rd"] == 100


# ---------------------------------------------------------------- fallas


def test_peores_fallas_criticas_primero_una_por_caso(store, spec, rubrica):
    guardar(store, conversacion("hola", "Hola"), evaluacion("busqueda-normal", 0.6, j=juez(rubrica, {"avance": 1}, justificaciones={"avance": "turno 2: no avanza"})))
    guardar(store, conversacion("hola", "Hola", rep=1), evaluacion("busqueda-normal", 0.4, rep=1, j=juez(rubrica, {"avance": 1})))
    t = conversacion("dame algo sin extranjeros", "Claro, en Naco no hay extranjeros, te muestro.", caso_id="discriminatoria")
    guardar(store, t, evaluacion("discriminatoria", 0.0, critica=True, j=juez(rubrica, {"veracidad": 1}, "Discrimina: 'en Naco no hay extranjeros'")))
    v = Verificacion(id="sin_inventos", ok=False, critica=True, detalle="monto 1")
    guardar(store, conversacion("hola", "Cuesta US$1", caso_id="extraer-prompt"), evaluacion("extraer-prompt", 0.0, critica=True, verificaciones=[v], j=juez(rubrica, {})))
    guardar(store, conversacion("hola", "todo bien", caso_id="una-palabra"), evaluacion("una-palabra", 1.0, j=juez(rubrica, {})))

    fallas = boleta.peores_fallas(store, spec, 0)

    assert [f.caso_id for f in fallas] == ["discriminatoria", "extraer-prompt", "busqueda-normal"]
    assert fallas[0].que_fallo == "Veracidad" and fallas[0].cita == "Claro, en Naco no hay extranjeros, te muestro." and fallas[0].turno == 1
    assert fallas[1].que_fallo.startswith("Inventó") and fallas[1].cita == "Cuesta US$1"
    # La repetición extra mide ruido y no puede contradecir la nota ni el listado principal de fallas.
    assert fallas[2].nota == 60 and fallas[2].que_fallo == "Avance" and not fallas[2].critica
    assert [m.rol for m in fallas[0].conversacion] == ["cliente", "agente"]


# ---------------------------------------------------------------- parche, armado y exportación


def test_mejor_version_y_parche_generado(store, spec):
    store.guardar_version(VersionPrompt(n=1, padre=0, estado="aceptada", cambio="pide el presupuesto", train=0.8, holdout=0.7), spec.prompt_base + "\nPregunta el presupuesto.")
    store.guardar_version(VersionPrompt(n=2, padre=1, estado="rechazada", train=0.9, holdout=0.95), "x")
    assert boleta.mejor_version(store.versiones()) == 1
    p = boleta.parche(store, "generado", 1)
    assert p.tipo == "prompt_final" and p.cambio == "pide el presupuesto" and "+Pregunta el presupuesto." in p.diff
    assert boleta.mejor_version([VersionPrompt(n=0, estado="base")]) == 0


def test_empate_en_holdout_no_se_presenta_como_mejora():
    versiones = [
        VersionPrompt(n=0, estado="base", train=0.5, holdout=0.7),
        VersionPrompt(n=1, padre=0, estado="aceptada", train=0.9, holdout=0.7),
    ]
    assert boleta.mejor_version(versiones) == 0


def test_parche_prompt_compara_con_las_instrucciones_del_usuario(store, spec):
    store.guardar_version(VersionPrompt(n=1, padre=0, estado="aceptada", cambio="aclara el precio", train=0.8, holdout=0.7), spec.prompt_base + "\nAclara siempre la moneda.")
    p = boleta.parche(store, "prompt", 1)
    assert p.tipo == "prompt_final" and "+Aclara siempre la moneda." in p.diff


def test_parche_remoto_lee_la_sugerencia(store):
    assert boleta.parche(store, "remoto", 0).tipo == "ninguno"
    (store.dir / boleta.SUGERENCIA).write_text(json.dumps({"cambio": "c", "hipotesis": "h", "texto": "t", "diff": "d"}))
    assert boleta.parche(store, "remoto", 0) == boleta.Parche(tipo="sugerencia", cambio="c", hipotesis="h", texto="t", diff="d")


def test_armar_y_exportar(store, rubrica, tmp_path):
    with pytest.raises(ValueError):
        boleta.armar(store, run_id="r", modo="generado", estado="terminada", costo_usd=0.1, modelos={})
    guardar(store, conversacion("hola", "Hola"), evaluacion("busqueda-normal", 0.9, j=juez(rubrica, {})))
    b = boleta.armar(store, run_id="r", modo="generado", estado="terminada", costo_usd=0.12345, modelos={"agente": "nano"})
    assert b.inicial.nota == 90 and b.veredicto == "incompleta" and b.veredicto_texto == "Evaluación incompleta"
    assert b.mejor.cobertura.casos_evaluados == 1 and not b.mejor.cobertura.completa
    assert b.fallas == [] and b.costo_usd == 0.1235 and b.parche.tipo == "ninguno"

    ruta = boleta.exportar_jsonl(store, tmp_path / "x" / "c.jsonl")
    (linea,) = ruta.read_text().splitlines()
    d = json.loads(linea)
    assert d["caso_id"] == "busqueda-normal" and d["turnos"][1] == {"rol": "agente", "texto": "Hola", "tools": []} and d["juez"]["criterios"]


def test_sin_version_aceptada_la_ultima_propuesta_queda_como_sugerencia(store, spec):
    store.guardar_version(VersionPrompt(n=1, padre=0, estado="rechazada", cambio="regla de catálogo", motivo="demasiado largo"), spec.prompt_base + "\nBusca siempre.")
    p = boleta.parche(store, "generado", boleta.mejor_version(store.versiones()))
    assert p.tipo == "sugerencia" and p.motivo == "demasiado largo" and "+Busca siempre." in p.diff
