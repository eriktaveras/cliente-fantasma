"""Regresiones de reservas: concurrencia real, JSON reintentado y cancelación."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from fantasma import costos
from fantasma.indice import Indice
from fantasma.motor.llm import LLM
from fantasma.servicio import Servicio


class Dato(BaseModel):
    valor: int


def preparar(config, tmp_path, respuestas):
    config.proveedores["local"].precios["falso"] = (1, 1)
    config.roles["ficha"].max_tokens = 100
    indice = Indice(tmp_path / "indice.sqlite")
    indice.crear("prueba", "ip")
    llm = LLM(config)
    llamadas = []

    async def create(**kw):
        llamadas.append(kw)
        r = respuestas.pop(0)
        if callable(r):
            return await r()
        if isinstance(r, BaseException):
            raise r
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=r, tool_calls=[]))])

    llm._clientes["local"] = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    control = costos.Medido(llm, lambda _: None,
        reservar=lambda usd: indice.reservar("prueba", usd, 1, 1, 1),
        liquidar=lambda reserva, gasto: indice.liquidar("prueba", reserva, gasto))
    return llm, control, indice, llamadas


def test_desconocido_se_rechaza_y_local_no_significa_gratis(config, tmp_path):
    with pytest.raises(costos.PrecioDesconocido, match="Sin tarifa"):
        costos.tarifa(config, "ficha")
    config.proveedores["local"].precios["falso"] = (0, 0)
    assert costos.tarifa(config, "ficha") == (0, 0)


def test_estructurado_reserva_cada_subllamada_y_cuenta_json_invalido(config, tmp_path):
    llm, control, indice, llamadas = preparar(config, tmp_path, ["incorrecto", '{"valor": 2}'])
    r = asyncio.run(control.estructurado("ficha", [{"role": "user", "content": "dato"}], Dato))
    assert r.valor == 2 and len(llamadas) == 2
    assert control.gastado() == pytest.approx(0.00004)
    assert indice.obtener("prueba").costo_usd == pytest.approx(control.gastado())
    assert indice.db.execute("SELECT reserva_usd FROM corridas").fetchone()[0] == 0


def test_reservas_concurrentes_diarias_son_atomicas(tmp_path):
    indice = Indice(tmp_path / "indice.sqlite")
    indice.crear("a", "ip-a")
    indice.crear("b", "ip-b")
    indice.reservar("a", .7, 1, 1, 1)
    with pytest.raises(costos.PresupuestoAgotado, match="diario"):
        indice.reservar("b", .4, 1, 1, 1)
    indice.liquidar("a", .7, .1)
    indice.reservar("b", .4, 1, 1, 1)


def test_cancelacion_en_vuelo_cobra_reserva_y_en_cola_no(config, tmp_path):
    async def flujo():
        empezo = asyncio.Event()
        async def bloqueada():
            empezo.set()
            await asyncio.Event().wait()
        config.concurrency_global = 1
        _, control, indice, llamadas = preparar(config, tmp_path, [bloqueada])
        t1 = asyncio.create_task(control.chat("ficha", [{"role": "user", "content": "hola"}]))
        await empezo.wait()
        t2 = asyncio.create_task(control.chat("ficha", [{"role": "user", "content": "hola"}]))
        await asyncio.sleep(0)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        assert control.gastado() == 0
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert len(llamadas) == 1
        assert control.sin_confirmar > 0
        assert indice.obtener("prueba").costo_usd == control.sin_confirmar
        assert indice.db.execute("SELECT reserva_usd FROM corridas").fetchone()[0] == 0
    asyncio.run(flujo())


def test_reinicio_cobra_reserva_incierta(tmp_path):
    indice = Indice(tmp_path / "indice.sqlite")
    indice.crear("a", "ip")
    indice.reservar("a", .1, 1, 1, 1)
    indice.marcar_interrumpidas()
    assert indice.obtener("a").estado == "error"
    assert indice.obtener("a").costo_usd == .1


def test_ficha_no_llama_si_no_cabe_reserva(config, tmp_path, monkeypatch):
    from fantasma.servicio import Medida
    from tests.test_servicio import LLMGuionado, COLMADO
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config.proveedores["local"].precios["falso"] = (100, 100)
    creados = []
    def fabrica(c):
        llm = LLMGuionado(c)
        creados.append(llm)
        return llm
    s = Servicio(tmp_path, config=config, fabrica_llm=fabrica,
                 tamaños={"ligera": Medida(casos=15, iteraciones=1, tope_usd=.00001, medir_ruido=False)})
    with pytest.raises(costos.PresupuestoAgotado):
        asyncio.run(s.crear_ficha([], COLMADO, "ip"))
    assert not creados[0].llamadas
    assert s.indice.listar()[0].estado == "presupuesto"
    assert s.indice.listar()[0].costo_usd == 0


def test_retry_http_reserva_de_nuevo_y_corta_antes_de_enviar(config, tmp_path, monkeypatch):
    import httpx
    import openai
    error = openai.APITimeoutError(request=httpx.Request("POST", "https://ejemplo.invalid"))
    llm, control, indice, llamadas = preparar(config, tmp_path, [error, "ok"])
    config.reintentos = 2
    # Caben la primera reserva, pero no un segundo intento sin confirmar el primero.
    reservas = []
    def reservar(usd):
        reservas.append(usd)
        indice.reservar("prueba", usd, usd * 1.5, 1, 1)
    control._reservar = reservar
    async def sin_espera(*_):
        pass
    monkeypatch.setattr("fantasma.motor.llm.asyncio.sleep", sin_espera)
    with pytest.raises(costos.PresupuestoAgotado):
        asyncio.run(control.chat("ficha", [{"role": "user", "content": "hola"}]))
    assert len(llamadas) == 1 and len(reservas) == 2
    assert control.gastado() == pytest.approx(reservas[0])
    assert control.sin_confirmar == pytest.approx(reservas[0])


def test_dos_clientes_comparten_limite_global(config, tmp_path):
    async def flujo():
        config.concurrency_global = 1
        dentro = 0
        maximo = 0
        async def respuesta():
            nonlocal dentro, maximo
            dentro += 1
            maximo = max(maximo, dentro)
            await asyncio.sleep(.01)
            dentro -= 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))])
        _, c1, _, _ = preparar(config, tmp_path / "a", [respuesta])
        _, c2, _, _ = preparar(config, tmp_path / "b", [respuesta])
        await asyncio.gather(c1.chat("ficha", []), c2.chat("ficha", []))
        assert maximo == 1
    asyncio.run(flujo())



def test_json_invalido_agota_tres_intentos_y_persiste_todo(config, tmp_path):
    from fantasma.motor.llm import ErrorTecnico
    _, control, indice, llamadas = preparar(config, tmp_path, ["mal", "mal", "mal"])
    with pytest.raises(ErrorTecnico, match="JSON inválido"):
        asyncio.run(control.estructurado("ficha", [], Dato))
    assert len(llamadas) == 3
    assert control.gastado() == pytest.approx(.00006)
    assert indice.obtener("prueba").costo_usd == pytest.approx(.00006)
    assert indice.db.execute("SELECT reserva_usd FROM corridas").fetchone()[0] == 0


def test_precio_desconocido_cliente_real_no_envia(config, tmp_path):
    _, control, indice, llamadas = preparar(config, tmp_path, ["ok"])
    config.proveedores["local"].precios.clear()
    with pytest.raises(costos.PrecioDesconocido):
        asyncio.run(control.chat("ficha", []))
    assert not llamadas and control.gastado() == 0
    assert indice.obtener("prueba").costo_usd == 0


def test_migracion_gastos_no_duplica_y_reinicio_carga_deuda_hoy(tmp_path, monkeypatch):
    from fantasma import indice as modulo
    ruta = tmp_path / "indice.sqlite"
    hoy = ["2026-10-14"]
    monkeypatch.setattr(modulo, "_hoy", lambda: hoy[0])
    indice = Indice(ruta)
    indice.crear("a", "ip")
    indice.actualizar("a", costo_usd=.2)
    indice.db.execute("UPDATE corridas SET creado = '2026-10-14T00:00:00+00:00'")
    indice.db.execute("DROP TABLE gastos_diarios")
    indice.db.close()
    indice = Indice(ruta)
    assert indice.gasto_del_dia() == .2
    indice.db.close()
    indice = Indice(ruta)
    assert indice.gasto_del_dia() == .2
    indice.reservar("a", .3, 1, 1, 1)
    hoy[0] = "2026-10-15"
    indice.marcar_interrumpidas()
    assert indice.gasto_del_dia() == .3
    assert indice.gasto_del_dia_de("ip") == .3
    assert indice.obtener("a").costo_usd == .5
    indice.marcar_interrumpidas()
    assert indice.gasto_del_dia() == .3
