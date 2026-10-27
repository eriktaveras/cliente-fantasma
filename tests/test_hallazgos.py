"""Regresiones de la revisión E2E de oct-2026: [FIN] en el saludo, peores fallas, inventos en remoto, carreras y topes."""

import asyncio

import httpx
import pytest

from fantasma import costos

from fantasma import boleta, remoto
from fantasma.ficha import Politicas, a_datos
from fantasma.motor.contexto import Contexto
from fantasma.motor.esquemas import Agregado, Caso, Persona, ResultadoCaso, Spec, Transcripcion, Turno, Verificacion, VersionPrompt
from fantasma.motor.herramientas import caja_para
from fantasma.motor.loop import aceptar
from fantasma.motor.simulador import simular
from fantasma.motor.storage import Store
from fantasma.motor.verificar import verificar
from fantasma.servicio import Limite, rubro_de
from tests.conftest import texto
from tests.test_boleta import conversacion, evaluacion, guardar, juez
from tests.test_servicio import COLMADO, URL, LLMGuionado, ficha_colmado, precios, servicio  # noqa: F401


def correr(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- simulador: [FIN] en el primer mensaje


@pytest.fixture
def ctx(config, llm, spec, tmp_path):
    return Contexto(config, llm, spec, Store(tmp_path))


def test_fin_en_el_saludo_no_corta_la_conversacion(ctx, llm):
    llm.encolar("simulador", "Hola, me regalan precio del arroz? [FIN]", "ok gracias [FIN:objetivo]")
    llm.encolar("agente", texto("El arroz está a RD$50."))
    t = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 0))
    assert t.fin == "objetivo"
    assert [(x.rol, x.texto) for x in t.turnos] == [
        ("cliente", "Hola, me regalan precio del arroz?"),
        ("agente", "El arroz está a RD$50."),
        ("cliente", "ok gracias"),
    ]


def test_saludo_vacio_es_error_tecnico_reintentable(ctx, llm):
    llm.encolar("simulador", "[FIN:objetivo]")
    t = correr(simular(ctx, ctx.spec.casos[0], "P", 0, 0))
    assert t.fin == "error_tecnico" and not t.turnos


# ---------------------------------------------------------------- boleta: peores fallas


@pytest.fixture
def store(tmp_path, spec):
    s = Store(tmp_path)
    s.guardar_spec(spec)
    s.guardar_version(VersionPrompt(n=0, estado="base", train=0.5, holdout=0.4), spec.prompt_base)
    return s


def test_peores_fallas_sin_respuesta_del_bot_no_entran_y_las_citas_van_primero(store, spec, rubrica):
    ids = [c.id for c in spec.casos]
    solo_cliente = Transcripcion(caso_id=ids[0], repeticion=0, version=0, modelo_agente="falso", turnos=[Turno(rol="cliente", texto="Hola, precio?")], fin="objetivo")
    no_llamo = Verificacion(id="esperado:llama:pedidos.crear_pedido", ok=False, critica=True, detalle="no se llamó")
    guardar(store, solo_cliente, evaluacion(ids[0], 0.0, critica=True, verificaciones=[no_llamo], j=juez(rubrica, {})))
    guardar(store, conversacion("quiero pedir", "Listo, anotado.", caso_id=ids[1]), evaluacion(ids[1], 0.0, critica=True, verificaciones=[no_llamo], j=juez(rubrica, {})))
    guardar(
        store,
        conversacion("precio?", "Sale en US$999.", caso_id=ids[2]),
        evaluacion(ids[2], 0.0, critica=True, verificaciones=[Verificacion(id="sin_inventos", ok=False, critica=True, detalle="monto 999")], j=juez(rubrica, {})),
    )

    fallas = boleta.peores_fallas(store, spec, 0)
    assert [f.caso_id for f in fallas] == [ids[2], ids[1]]
    assert fallas[0].cita == "Sale en US$999." and fallas[0].turno == 1
    # Una falla de herramienta no tiene frase que marcar como "AQUÍ FALLÓ", y su id interno sale en texto legible.
    assert fallas[1].cita == "" and fallas[1].turno is None
    assert fallas[1].que_fallo == "No usó la herramienta para crear el pedido cuando tocaba"


def test_que_fallo_expectativa_no_critica_con_juez_perfecto(spec, rubrica):
    e = evaluacion(
        spec.casos[0].id, 0.925, paso=False,
        verificaciones=[Verificacion(id="esperado:llama:catalogo.buscar", ok=False, critica=False, detalle="no se llamó"), Verificacion(id="idioma", ok=True)],
        j=juez(rubrica, {}),
    )
    assert boleta._que_fallo(e, spec) == ("No usó la herramienta para consultar el catálogo cuando tocaba", "no se llamó", False, e.verificaciones[0].id)
    # Si el juez sí vio un criterio malo, manda el juez.
    c = rubrica.criterios[0]
    e.juez = juez(rubrica, {c.id: 2}, justificaciones={c.id: "turno 2: mal"})
    assert boleta._que_fallo(e, spec) == (c.nombre, "turno 2: mal", True, c.id)


def test_parche_de_una_aceptada_sin_holdout_no_dice_que_se_rechazo(store, spec):
    store.guardar_version(VersionPrompt(n=1, padre=0, estado="aceptada", cambio="regla", motivo="mejora +0.150 (0.65 vs 0.50)"), spec.prompt_base + "\nBusca siempre.")
    p = boleta.parche(store, "generado", boleta.mejor_version(store.versiones()))
    assert p.tipo == "sugerencia" and p.motivo == "" and "+Busca siempre." in p.diff


def test_lineas_nuevas():
    assert boleta.lineas_nuevas("a\nb\n", "a\nregla nueva\nb\notra\n") == "regla nueva\notra"


# ---------------------------------------------------------------- verificar: montos de la ficha y totales


def _spec_colmado(spec) -> Spec:
    ficha = ficha_colmado()
    ficha.politicas = Politicas(delivery="Envío RD$100; pedido mínimo RD$500")
    rubro = rubro_de(ficha)
    return Spec(rubro=rubro, datos=a_datos(ficha, rubro), rubrica=spec.rubrica, casos=spec.casos, prompt_base="P")


@pytest.mark.parametrize("modelo", ["remoto:bot.ejemplo.com", "falso"])
def test_total_calculado_y_envio_de_la_ficha_no_son_inventos(spec, modelo):
    s = _spec_colmado(spec)
    caso = Caso(id="c", escenario="x", persona=Persona(nombre="Luis", estilo="corto"), objetivo_oculto="x")
    t = Transcripcion(
        caso_id="c", repeticion=0, version=0, modelo_agente=modelo,
        turnos=[
            Turno(rol="cliente", texto="dame 2 libras de arroz y un aceite"),
            Turno(rol="agente", texto="Serían RD$450 en total (arroz RD$100 + aceite RD$350). El envío es RD$100 y el pedido mínimo RD$500."),
        ],
    )
    v = {x.id: x for x in verificar(t, caso, s, caja_para(s.rubro, s.datos), "P")}
    assert v["sin_inventos"].ok, v["sin_inventos"].detalle
    t.turnos[1].texto = "El arroz te sale en RD$38 la libra y el envío es gratis."
    v = {x.id: x for x in verificar(t, caso, s, caja_para(s.rubro, s.datos), "P")}
    assert not v["sin_inventos"].ok and "38" in v["sin_inventos"].detalle


# ---------------------------------------------------------------- optimizador: mejora con riesgo


def _ag(p: float) -> Agregado:
    return Agregado(version=0, conjunto="train", puntaje=p, casos=2, pasan=1, criticos_total=1, criticos_pasan=1, inestables=0)


def test_aceptar_tolera_un_critico_roto_si_se_pide():
    antes = [ResultadoCaso(caso_id="crit", version=0, puntajes=[1.0], puntaje=1.0, paso=True, inestable=False)]
    despues = [ResultadoCaso(caso_id="crit", version=1, puntajes=[0.0], puntaje=0.0, paso=False, inestable=False)]
    assert not aceptar(_ag(0.17), _ag(0.26), antes, despues, 0.02, {"crit"})[0]
    ok, motivo = aceptar(_ag(0.17), _ag(0.26), antes, despues, 0.02, {"crit"}, tolerados=1)
    assert ok and "con riesgo: rompe crit" in motivo


# ---------------------------------------------------------------- remoto: respuestas enormes


def test_respuesta_remota_se_recorta(monkeypatch):
    async def resolver(host, puerto):
        return ["93.184.216.34"]

    monkeypatch.setattr(remoto, "resolver", resolver)
    monkeypatch.setattr(remoto, "TRANSPORTE", httpx.MockTransport(lambda req: httpx.Response(200, json={"respuesta": "x" * 10000})))
    txt, _ = correr(remoto.turno_remoto(remoto.Remoto(url=URL), "s", "hola", []))
    assert len(txt) == remoto.MAX_TEXTO + 1 and txt.endswith("…")


# ---------------------------------------------------------------- servicio: carreras, IP y topes


@pytest.fixture
def dns_lento(monkeypatch):
    async def resolver(host, puerto):
        await asyncio.sleep(0.05)
        return ["93.184.216.34"]

    monkeypatch.setattr(remoto, "resolver", resolver)
    monkeypatch.setattr(remoto, "TRANSPORTE", httpx.MockTransport(lambda req: httpx.Response(200, json={"respuesta": "Hola"})))


def test_lanzar_en_paralelo_reserva_una_sola_corrida(servicio, dns_lento):
    async def flujo():
        a = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        r = await asyncio.gather(*(servicio.lanzar(a, "remoto", "ligera", url_bot=URL) for _ in range(3)), return_exceptions=True)
        tareas = [x for x in r if isinstance(x, asyncio.Task)]
        assert len(tareas) == 1 and all(isinstance(x, ValueError) for x in r if x not in tareas)
        await tareas[0]

    correr(flujo())
    assert len(servicio.creados) == 2  # la ficha y una sola corrida


def test_dos_fichas_de_la_misma_ip_no_corren_a_la_vez(servicio, dns_lento):
    async def flujo():
        a = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        b = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        r = await asyncio.gather(
            servicio.lanzar(a, "remoto", "ligera", url_bot=URL), servicio.lanzar(b, "remoto", "ligera", url_bot=URL), return_exceptions=True
        )
        assert sum(isinstance(x, asyncio.Task) for x in r) == 1 and sum(isinstance(x, Limite) for x in r) == 1
        await next(x for x in r if isinstance(x, asyncio.Task))

    correr(flujo())


def test_url_rechazada_devuelve_la_corrida_a_ficha(servicio, monkeypatch):
    async def resolver(host, puerto):
        return ["10.0.0.5"]

    monkeypatch.setattr(remoto, "resolver", resolver)

    async def flujo():
        a = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        with pytest.raises(remoto.URLNoPermitida):
            await servicio.lanzar(a, "remoto", "ligera", url_bot=URL)
        return a

    assert servicio.indice.obtener(correr(flujo())).estado == "ficha"


def test_ipv6_cuenta_por_64(servicio):
    assert servicio.hash_ip("2001:db8:1:2::1") == servicio.hash_ip("2001:db8:1:2:ffff:abcd:1:9")
    assert servicio.hash_ip("2001:db8:1:2::1") != servicio.hash_ip("2001:db8:1:3::1")
    assert servicio.hash_ip("1.2.3.4") != servicio.hash_ip("1.2.3.5")
    assert servicio.hash_ip("desconocida")


def test_tope_de_gasto_por_visitante(servicio):
    servicio.tope_ip = 0.05

    async def flujo():
        run_id = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        estado = await (await servicio.lanzar(run_id, "generado", "ligera"))
        return run_id, estado

    run_id, estado = correr(flujo())
    assert estado == "presupuesto" and "por visitante" in servicio.indice.obtener(run_id).motivo
    with pytest.raises(costos.PresupuestoAgotado, match="por visitante"):
        correr(servicio.crear_ficha([], COLMADO, "1.2.3.4"))
    correr(servicio.crear_ficha([], COLMADO, "5.6.7.8"))


def test_tavily_se_cobra_en_la_ficha(servicio, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "x")

    async def sin_red(http, clave, urls, nombre, progreso):
        return []

    monkeypatch.setattr("fantasma.ficha.buscar_en_web", sin_red)
    run_id = correr(servicio.crear_ficha(["https://colmado.example"], COLMADO, "1.2.3.4"))
    sin_tavily = correr(servicio.crear_ficha([], COLMADO, "5.6.7.8"))
    diferencia = servicio.indice.obtener(run_id).costo_usd - servicio.indice.obtener(sin_tavily).costo_usd
    assert diferencia == pytest.approx(servicio.tavily_usd)


def test_ligera_normal_no_dice_presupuesto(servicio):
    async def flujo():
        run_id = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        return run_id, await (await servicio.lanzar(run_id, "generado", "ligera"))

    run_id, estado = correr(flujo())
    fila = servicio.indice.obtener(run_id)
    assert estado == "terminada" and fila.motivo in ("rondas completadas", "sin más mejoras", "metas alcanzadas")


class JuezQueRevienta(LLMGuionado):
    """El 2.º juicio lanza un error que no es ErrorTecnico (como un 400 o 402 de Nebius)."""

    juicios = 0

    async def chat(self, rol, mensajes, tools=None, response_format=None):
        await asyncio.sleep(0)
        return await super().chat(rol, mensajes, tools, response_format)

    async def estructurado(self, rol, mensajes, esquema):
        await asyncio.sleep(0)
        if esquema.__name__ == "ResultadoJuez":
            JuezQueRevienta.juicios += 1
            if JuezQueRevienta.juicios == 2:
                raise RuntimeError("402 sin saldo")
        return await super().estructurado(rol, mensajes, esquema)


def test_un_error_en_una_conversacion_frena_a_las_demas(servicio):
    creados = []

    def fabrica(cfg):
        creados.append(JuezQueRevienta(cfg))
        return creados[-1]

    servicio.fabrica_llm = fabrica

    async def flujo():
        run_id = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        estado = await (await servicio.lanzar(run_id, "generado", "ligera"))
        llamadas = len(creados[-1].llamadas)
        eventos = len(servicio.eventos.recientes(run_id))
        await asyncio.sleep(0.05)
        return run_id, estado, llamadas, eventos

    run_id, estado, llamadas, eventos = correr(flujo())
    assert estado == "error" and "402" in servicio.indice.obtener(run_id).motivo
    assert len(creados[-1].llamadas) == llamadas
    assert len(servicio.eventos.recientes(run_id)) == eventos and servicio.eventos.recientes(run_id)[-1]["tipo"] == "fin"


def test_motivo_legible_para_el_dueno():
    from fantasma.boleta import motivo_legible

    assert motivo_legible("monto 149, monto 110, COLM-099") == "Dio cifras que no están en la ficha: 149, 110, COLM-099."
    assert motivo_legible("no se llamó") == "No la llamó."
    assert motivo_legible("Inventó el envío gratis") == "Inventó el envío gratis"


def test_producto_gratis_entra_al_catalogo():
    from decimal import Decimal

    from fantasma.ficha import Ficha, Producto, a_datos, a_rubro

    ficha = Ficha(negocio="DjangoBlaze", descripcion="d", rubro="software", productos=[Producto(nombre="Completa", precio=Decimal(99)), Producto(nombre="Gratis", precio=Decimal(0))])
    datos = a_datos(ficha, a_rubro(ficha))
    assert sorted(i.precio for i in datos.catalogo) == [Decimal(0), Decimal(99)]


# ---------------------------------------------------------------- optimizador: contexto copiado en la propuesta


def test_propuesta_no_arrastra_el_contexto_del_optimizador():
    from fantasma.motor.optimizador import limpiar_prompt

    base = "Eres el asistente de Bluebird Store.\nResponde corto y escala las quejas."
    copiado = base + "\n\n## Rúbrica\n\n- no_inventa: no cumple: 'a 900'\n\n## Reglas críticas (no se pueden quitar)\n\n- No inventes."
    assert limpiar_prompt(copiado) == base
    assert limpiar_prompt("<prompt_actual>\n" + base + "\n</prompt_actual>") == base
    assert limpiar_prompt("## Prompt actual\n\n" + base + "\n\n## Lecciones\n\n- algo") == base
    # Un encabezado propio del prompt que no es de contexto se respeta.
    propio = base + "\n\n## Horarios\n\nLunes a sábado."
    assert limpiar_prompt(propio) == propio
