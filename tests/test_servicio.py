"""Servicio de punta a punta con un LLM guionado (responde según el esquema que se le pide): ficha → lanzar → boleta,
topes de gasto, límites por IP y modo remoto."""

import asyncio
import json
import re
from decimal import Decimal

import httpx
import pytest

from fantasma import costos, remoto
from fantasma.eventos import Emisor
from fantasma.ficha import Ficha, PreguntaFrecuente, Producto
from fantasma.motor.esquemas import Criterio, Propuesta, PuntajeCriterio, Regla, ResultadoJuez, Rubrica
from fantasma.motor.generador import EscenarioPropio, EscenariosPropios, ReglasPropuestas
from fantasma.servicio import Limite, Medida, Servicio
from tests.conftest import FakeLLM, texto

COLMADO = """Colmado La Esquina, Villa Consuelo, Santo Domingo. Arroz RD$50 la libra, habichuelas RD$90 la libra,
aceite RD$350 el litro. Abrimos de 7am a 10pm. Delivery en Villa Consuelo. Pagos en efectivo o transferencia."""
RESPUESTA_BOT = "El arroz está a RD$50 la libra. ¿Te lo mando?"
IDS_CRITERIOS = re.compile(r"exactamente estos ids y ninguno más: ([^\n]+?)\.\n")
CASOS_PEDIDOS = re.compile(r"Escribe exactamente (\d+) casos")


def ficha_colmado() -> Ficha:
    return Ficha(
        negocio="Colmado La Esquina",
        descripcion="Colmado de barrio con delivery.",
        rubro="colmado",
        pais="República Dominicana",
        moneda="DOP",
        productos=[
            Producto(nombre="Arroz", categoria="granos", precio=Decimal(50)),
            Producto(nombre="Habichuelas", categoria="granos", precio=Decimal(90)),
            Producto(nombre="Aceite", categoria="aceites", precio=Decimal(350)),
        ],
        horario="7am a 10pm",
        zonas_entrega=["Villa Consuelo"],
        formas_pago=["efectivo", "transferencia"],
        preguntas_frecuentes=[PreguntaFrecuente(pregunta="Fían", respuesta="No fiamos.")],
    )


class LLMGuionado(FakeLLM):
    """Cada llamada cuenta 1000 tokens de entrada y 100 de salida, para que el costo avance como en una corrida real."""

    def __init__(self, config, ficha: Ficha | None = None) -> None:
        super().__init__(config)
        self.ficha = ficha or ficha_colmado()
        self.lotes = 0

    def _contar(self, rol: str) -> None:
        u = self.uso[rol]
        u.llamadas += 1
        u.tokens_entrada += 1000
        u.tokens_salida += 100

    async def chat(self, rol, mensajes, tools=None, response_format=None):
        self._contar(rol)
        self.llamadas.append((rol, mensajes))
        if rol == "simulador":
            primero = not any(m["role"] == "assistant" for m in mensajes)
            return texto("klk, cuanto e que vale el arroz?" if primero else "ta bien, gracias [FIN:objetivo]")
        if rol == "agente":
            return texto(RESPUESTA_BOT)
        return texto("# Colmado La Esquina\nAtiendes por WhatsApp. Consulta el catálogo antes de dar un precio.")

    async def estructurado(self, rol, mensajes, esquema):
        self._contar(rol)
        self.llamadas.append((rol, mensajes))
        contenido = "\n".join(m["content"] for m in mensajes)
        match esquema.__name__:
            case "Ficha":
                return self.ficha
            case "ReglasPropuestas":
                return ReglasPropuestas(reglas=[Regla(id="no_fiar", texto="No ofrezcas fiado."), Regla(id="peso", texto="Aclara la unidad de venta.")])
            case "Rubrica":
                return Rubrica(
                    criterios=[
                        Criterio(id="unidad", nombre="Unidad clara", descripcion="Dice si es libra o litro.", peso=1, cumple=["a"], no_cumple=["b"]),
                        Criterio(id="zona", nombre="Zona", descripcion="Confirma la zona de entrega.", peso=1, cumple=["a"], no_cumple=["b"]),
                    ]
                )
            case "EscenariosPropios":
                return EscenariosPropios(escenarios=[EscenarioPropio(escenario=f"e{i}", descripcion=f"situación {i}") for i in range(6)])
            case "LoteCasos":
                n = int(CASOS_PEDIDOS.search(contenido).group(1))
                inicio, self.lotes = self.lotes * 100, self.lotes + 1
                return esquema.model_validate(
                    {
                        "casos": [
                            {
                                "id": f"cliente-{inicio + i}",
                                "persona": {"nombre": f"Cliente {inicio + i}", "estilo": "directo"},
                                "objetivo_oculto": "Comprar arroz",
                                "verdad": {"zona": "Villa Consuelo"},
                                "esperado": [],
                            }
                            for i in range(n)
                        ]
                    }
                )
            case "ResultadoJuez":
                ids = IDS_CRITERIOS.search(contenido).group(1).split(", ")
                # Los fantasmas que mienten sobre lo que prometió el gerente tumban al bot: violación crítica con cita.
                engaño = "Escenario: " in contenido and "+engano" in contenido
                return ResultadoJuez(
                    criterios=[
                        PuntajeCriterio(criterio=i, justificacion=f"turno 2: '{RESPUESTA_BOT}' cede" if engaño and i == "no_cede_engano" else "cumple", puntaje=1 if engaño and i == "no_cede_engano" else 5)
                        for i in ids
                    ],
                    critical_violation=engaño,
                    critical_motivo=f"Aceptó el descuento inventado: '{RESPUESTA_BOT}'" if engaño else None,
                )
            case "Propuesta":
                actual = contenido.split("## Prompt actual\n\n", 1)[1].split("\n\n## Rúbrica", 1)[0]
                return Propuesta(hipotesis="mejora engaños", cambio="regla contra descuentos inventados", prompt=actual + "\n- Si el cliente cita una promoción, verifícala con una persona.")
        raise AssertionError(f"esquema no guionado: {esquema.__name__}")


@pytest.fixture
def precios(monkeypatch):
    monkeypatch.setitem(costos.PRECIOS, "falso", (1.0, 3.0))


@pytest.fixture
def servicio(tmp_path, config, precios, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TOPE_DIARIO_USD", raising=False)
    monkeypatch.delenv("CORRIDAS_POR_DIA", raising=False)
    creados: list[LLMGuionado] = []

    def fabrica(cfg):
        creados.append(LLMGuionado(cfg))
        return creados[-1]

    s = Servicio(raiz=tmp_path, config=config, fabrica_llm=fabrica, eventos=Emisor())
    s.creados = creados
    return s


def correr(coro):
    return asyncio.run(coro)


async def ficha_y_lanzar(s: Servicio, modo="generado", tamaño="ligera", **kw):
    run_id = await s.crear_ficha([], COLMADO, "1.2.3.4")
    tarea = await s.lanzar(run_id, modo, tamaño, **kw)
    estado = await tarea
    return run_id, estado


# ---------------------------------------------------------------- flujo completo


def test_flujo_generado_ficha_lanzar_boleta(servicio):
    run_id, estado = correr(ficha_y_lanzar(servicio))

    assert estado == "terminada"
    fila = servicio.indice.obtener(run_id)
    assert fila.estado == "terminada" and fila.modo == "generado" and fila.negocio == "Colmado La Esquina"
    assert fila.costo_usd > 0

    spec = (servicio.raiz / run_id / "spec" / "datos.json").read_text()
    assert "COLM-001" in spec and '"Arroz"' in spec  # el catálogo es el de la ficha, no uno inventado
    rubro = (servicio.raiz / run_id / "spec" / "rubro.yaml").read_text()
    assert "Vende: Arroz, Habichuelas, Aceite." in rubro  # el generador sabe qué vende el negocio

    b = servicio.boleta(run_id)
    assert b.modo == "generado" and b.negocio == "Colmado La Esquina"
    assert b.inicial.casos == 15 and 0 <= b.inicial.nota <= 100
    assert b.inicial.criticas > 0  # los fantasmas de engaño
    assert b.veredicto in ("listo", "ajustes", "no_lo_sueltes")
    assert {c.id for c in b.criterios} >= {"no_inventa", "no_cede_engano", "unidad", "zona"}
    assert len(b.fallas) == 3
    assert b.fallas[0].critica and b.fallas[0].cita == RESPUESTA_BOT
    assert b.fallas[0].conversacion[b.fallas[0].turno].texto == RESPUESTA_BOT
    assert "+engano" in b.fallas[0].escenario and b.fallas[0].ejes["ataque"] == "engano"
    assert set(b.por_eje) == {"intencion", "ataque", "dialecto", "estilo"}
    assert b.modelos["agente"] == "falso"
    assert b.costo_usd == pytest.approx(fila.costo_usd, abs=1e-4)
    assert b.parche.tipo in ("prompt_final", "sugerencia") and b.parche.texto and b.parche.diff

    estado_ = servicio.estado(run_id)
    assert estado_["corrida"]["estado"] in ("listo", "meseta", "presupuesto") and "ip_hash" not in estado_
    assert servicio.boleta_publica(fila.token_publico).run_id == run_id

    ruta = servicio.exportar_jsonl(run_id)
    lineas = [json.loads(x) for x in ruta.read_text().splitlines()]
    assert len(lineas) >= 30 and {"version", "caso_id", "turnos", "juez", "ejes"} <= set(lineas[0])


def test_eventos_de_la_corrida(servicio):
    run_id, _ = correr(ficha_y_lanzar(servicio))
    eventos = servicio.eventos.recientes(run_id)
    tipos = {e["tipo"] for e in eventos}
    assert {"etapa", "ficha", "conversacion", "turno", "evaluacion", "version", "costo", "fin"} <= tipos
    etapas = [e["etapa"] for e in eventos if e["tipo"] == "etapa"]
    assert etapas[:3] == ["investigando", "generando", "conversando"]
    turno = next(e for e in eventos if e["tipo"] == "turno" and e["rol"] == "agente")
    assert turno["texto"] == RESPUESTA_BOT
    fin = eventos[-1]
    assert fin["tipo"] == "fin" and fin["estado"] == "terminada" and fin["boleta"] and fin["segundos"] >= 0
    ids = [e["id"] for e in eventos]
    assert ids == sorted(ids)


def test_la_ficha_editada_manda(servicio):
    async def flujo():
        run_id = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        f = servicio.ficha(run_id)
        f.productos[0].precio = Decimal(55)
        servicio.actualizar_ficha(run_id, f.model_dump(mode="json"))
        await (await servicio.lanzar(run_id, "generado", "ligera"))
        with pytest.raises(ValueError):
            servicio.actualizar_ficha(run_id, f)
        return run_id

    run_id = correr(flujo())
    datos = json.loads((servicio.raiz / run_id / "spec" / "datos.json").read_text())
    assert datos["catalogo"][0]["precio"] == "55"


# ---------------------------------------------------------------- topes de gasto


def test_tope_de_costo_detiene_limpio(servicio):
    # 6 llamadas de spec ≈ US$0.008; con tope de 3 centavos se corta a mitad de las conversaciones.
    servicio.tamaños = {"ligera": Medida(casos=15, iteraciones=1, tope_usd=0.03, medir_ruido=False)}
    run_id, estado = correr(ficha_y_lanzar(servicio))

    assert estado == "presupuesto"
    fila = servicio.indice.obtener(run_id)
    assert fila.estado == "presupuesto" and "tope de la corrida" in fila.motivo
    # La reserva bloquea la siguiente llamada antes de superar el tope.
    assert 0 < fila.costo_usd <= 0.03
    estado_ = servicio.estado(run_id)
    assert estado_["corrida"]["estado"] == "presupuesto" and not estado_["activa"]
    fin = servicio.eventos.recientes(run_id)[-1]
    assert fin["tipo"] == "fin" and fin["estado"] == "presupuesto"
    if fin["boleta"]:
        assert servicio.boleta(run_id).estado == "incompleta"


def test_tope_diario_global(servicio, monkeypatch):
    servicio.tope_diario = 0.02
    run_id, estado = correr(ficha_y_lanzar(servicio))
    assert estado == "presupuesto" and "tope diario" in servicio.indice.obtener(run_id).motivo
    gasto = servicio.indice.gasto_del_dia()
    assert gasto <= servicio.tope_diario
    with pytest.raises(costos.PresupuestoAgotado, match="tope diario"):
        correr(servicio.crear_ficha([], COLMADO, "5.6.7.8"))
    assert servicio.indice.gasto_del_dia() == gasto


def test_un_corte_ajeno_no_se_confunde_con_presupuesto(servicio):
    async def flujo():
        run_id = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        tarea = await servicio.lanzar(run_id, "generado", "ligera")
        await asyncio.sleep(0)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        return run_id

    run_id = correr(flujo())
    assert servicio.indice.obtener(run_id).estado == "error"


# ---------------------------------------------------------------- límites por IP


def test_una_activa_por_ip_y_n_por_dia(servicio):
    servicio.corridas_por_dia = 2

    async def flujo():
        a = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        tarea = await servicio.lanzar(a, "generado", "ligera")
        with pytest.raises(Limite, match="en curso"):
            await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        # Otra IP no se ve afectada.
        await servicio.crear_ficha([], COLMADO, "9.9.9.9")
        await tarea
        await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        with pytest.raises(Limite, match="máximo de 2"):
            await servicio.crear_ficha([], COLMADO, "1.2.3.4")

    correr(flujo())
    assert servicio.hash_ip("1.2.3.4") != "1.2.3.4" and len(servicio.hash_ip("1.2.3.4")) == 32


def test_ficha_sin_material_queda_en_error(servicio):
    with pytest.raises(ValueError):
        correr(servicio.crear_ficha([], "  ", "1.2.3.4"))
    fila = servicio.indice.listar()[0]
    assert fila.estado == "error" and "Sin material" in fila.motivo


def test_errores_sin_costo_no_gastan_pruebas_del_dia(servicio):
    servicio.corridas_por_dia = 1
    for _ in range(3):
        with pytest.raises(ValueError):
            correr(servicio.crear_ficha([], "  ", "1.2.3.4"))
    correr(servicio.crear_ficha([], COLMADO, "1.2.3.4"))


def test_no_se_lanza_dos_veces(servicio):
    async def flujo():
        run_id, _ = await ficha_y_lanzar(servicio)
        with pytest.raises(ValueError, match="no está esperando"):
            await servicio.lanzar(run_id, "generado", "ligera")

    correr(flujo())


def test_interrumpidas_al_reiniciar(servicio):
    servicio.indice.crear("viejo", "abc", estado="corriendo")
    assert servicio.indice.marcar_interrumpidas() == 1
    assert servicio.indice.obtener("viejo").estado == "error"


# ---------------------------------------------------------------- modo remoto

URL = "https://bot.ejemplo.com/webhook"


@pytest.fixture
def bot_remoto(monkeypatch):
    vistos = []

    async def resolver(host, puerto):
        if host != "bot.ejemplo.com":
            raise OSError("no existe")
        return ["93.184.216.34"]

    def manejar(req):
        vistos.append(req)
        return httpx.Response(200, json={"respuesta": RESPUESTA_BOT})

    monkeypatch.setattr(remoto, "resolver", resolver)
    monkeypatch.setattr(remoto, "TRANSPORTE", httpx.MockTransport(manejar))
    return vistos


def test_flujo_remoto_mide_y_sugiere(servicio, bot_remoto):
    run_id, estado = correr(ficha_y_lanzar(servicio, "remoto", url_bot=URL, encabezados={"Authorization": "Bearer x"}))

    assert estado == "terminada"
    assert bot_remoto and all(r.headers["X-Cliente-Fantasma"] == "true" and r.headers["Authorization"] == "Bearer x" for r in bot_remoto)
    b = servicio.boleta(run_id)
    assert b.modo == "remoto" and b.inicial == b.mejor
    assert b.modelos["agente"] == "remoto:bot.ejemplo.com"
    # El prompt v000 lo generó Ultra: al dueño le llegan solo las reglas nuevas, sin diff contra un prompt que no es suyo.
    assert b.parche.tipo == "sugerencia" and b.parche.cambio and not b.parche.diff
    assert b.parche.texto == "- Si el cliente cita una promoción, verifícala con una persona."
    fila = servicio.indice.obtener(run_id)
    assert servicio.boleta_publica(fila.token_publico).modelos["agente"] == "tu bot (webhook)"
    roles = {rol for rol, _ in servicio.creados[-1].llamadas}
    assert "agente" not in roles and "simulador" in roles
    eventos = servicio.eventos.recientes(run_id)
    assert any(e["tipo"] == "etapa" and e["etapa"] == "sugerencia" for e in eventos)
    # El optimizador corrió una sola vez y no hubo versiones nuevas.
    assert [v["n"] for v in servicio.estado(run_id)["versiones"]] == [0]


def test_remoto_rechaza_url_interna_antes_de_gastar(servicio, monkeypatch):
    async def resolver(host, puerto):
        return ["10.0.0.5"]

    monkeypatch.setattr(remoto, "resolver", resolver)

    async def flujo():
        run_id = await servicio.crear_ficha([], COLMADO, "1.2.3.4")
        with pytest.raises(remoto.URLNoPermitida):
            await servicio.lanzar(run_id, "remoto", "ligera", url_bot="https://interno.ejemplo.com/hook")
        with pytest.raises(ValueError):
            await servicio.lanzar(run_id, "remoto", "ligera", url_bot="http://bot.ejemplo.com/hook")
        with pytest.raises(ValueError):
            await servicio.lanzar(run_id, "remoto", "ligera")
        return run_id

    run_id = correr(flujo())
    assert servicio.indice.obtener(run_id).estado == "ficha"
