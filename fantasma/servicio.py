"""La capa que usa la app web: ficha → corrida en segundo plano → boleta, con topes de gasto y de uso por IP.

Cada corrida vive en runs/<id>/ (ficha.json, config.yaml, el Store del motor, costos.json) y tiene su fila en runs/indice.sqlite.
Los eventos para la vista en vivo salen por fantasma.eventos.
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from fantasma import boleta as boletas
from fantasma import costos, remoto
from fantasma.eventos import Emisor, emisor
from fantasma.ficha import Ficha, a_datos, a_rubro, investigar
from fantasma.indice import Indice, Modo, Tamaño
from fantasma.motor.config import Config, cargar
from fantasma.motor.contexto import Contexto
from fantasma.motor.esquemas import Agregado, Corrida, EvaluacionRep, RubroSpec, Transcripcion, VersionPrompt
from fantasma.motor.evaluar import evaluar_conjunto, reunir
from fantasma.motor.generador import generar_spec
from fantasma.motor.llm import LLM
from fantasma.motor.loop import _fallos, correr
from fantasma.motor.optimizador import proponer, tokens
from fantasma.motor.storage import Store


class Medida(BaseModel):
    casos: int
    iteraciones: int
    tope_usd: float
    # Medir el ruido de v000 repite todo el train: en la ligera se come medio presupuesto y con 1 iteración aporta poco.
    medir_ruido: bool
    criticos_rotos_tolerados: int = 0


TAMAÑOS: dict[str, Medida] = {
    # Medido en oct-2026: una ligera real cuesta US$0.22-0.70 (más cuando el bot contesta largo); el margen cubre la
    # varianza y el holdout de una v001 aceptada.
    "ligera": Medida(casos=15, iteraciones=1, tope_usd=1.00, medir_ruido=False, criticos_rotos_tolerados=1),
    "completa": Medida(casos=40, iteraciones=3, tope_usd=3.0, medir_ruido=True),
}


def rubro_de(ficha: Ficha) -> RubroSpec:
    """El RubroSpec de la ficha con los productos nombrados en la descripción: el generador solo ve la descripción y, sin ellos,
    Ultra adivinó qué vende un "colmado" (salieron casos de colchones)."""
    rubro = a_rubro(ficha)
    if ficha.productos:
        rubro.descripcion += " Vende: " + ", ".join(p.nombre for p in ficha.productos) + "."
    return rubro


LECCION_REMOTO = (
    "El bot real es un webhook externo con su propio prompt, que no conoces, y sin estas herramientas: "
    "escribe la regla nueva para que se entienda sola, sin nombrar herramientas ni funciones."
)
MOTIVOS = {"presupuesto": "rondas completadas", "meseta": "sin más mejoras", "listo": "metas alcanzadas"}


class Limite(Exception):
    """Un tope de uso o de gasto: la app lo muestra como 429 con el texto `clave` de textos.json en el idioma del visitante."""

    def __init__(self, clave: str, mensaje: str, **datos):
        super().__init__(mensaje)
        self.clave, self.datos = clave, datos


class StoreObservado(Store):
    """El Store del motor, que además publica cada evaluación, versión y agregado que se guarda."""

    def __init__(self, dir: Path, publicar: Callable[..., Any]) -> None:
        super().__init__(dir)
        self._publicar = publicar

    def guardar_evaluacion(self, e: EvaluacionRep) -> None:
        super().guardar_evaluacion(e)
        self._publicar("evaluacion", version=e.version, caso_id=e.caso_id, repeticion=e.repeticion, puntaje=round(e.puntaje, 3), paso=e.paso, critica=e.critica)

    def guardar_version(self, v: VersionPrompt, texto: str | None = None) -> None:
        super().guardar_version(v, texto)
        self._publicar("version", n=v.n, estado=v.estado, cambio=v.cambio, motivo=v.motivo, train=v.train, holdout=v.holdout)

    def guardar_agregado(self, a: Agregado) -> None:
        super().guardar_agregado(a)
        self._publicar("agregado", version=a.version, conjunto=a.conjunto, puntaje=round(a.puntaje, 3), casos=a.casos, pasan=a.pasan)


class Servicio:
    def __init__(
        self,
        raiz: Path = Path("runs"),
        config: Config | None = None,
        fabrica_llm: Callable[[Config], Any] = LLM,
        eventos: Emisor = emisor,
        tamaños: dict[str, Medida] = TAMAÑOS,
    ) -> None:
        self.raiz = raiz
        self.config_base = config or cargar(Path("config.yaml"))
        self.fabrica_llm = fabrica_llm
        self.eventos = eventos
        self.tamaños = tamaños
        self.indice = Indice(raiz / "indice.sqlite")
        self.tareas: dict[str, asyncio.Task] = {}
        self.tope_diario = float(os.environ.get("TOPE_DIARIO_USD", "10"))
        self.corridas_por_dia = int(os.environ.get("CORRIDAS_POR_DIA", "5"))
        # Por debajo del tope diario: un solo visitante no puede dejar la demo sin presupuesto para los demás.
        self.tope_ip = float(os.environ.get("TOPE_IP_USD", "4"))
        # Tavily no reporta costo por llamada: se carga una estimación fija por ficha para que entre en los topes.
        self.tavily_usd = float(os.environ.get("TAVILY_USD_FICHA", "0.02"))
        self._sal = os.environ.get("FANTASMA_SAL_IP", "cliente-fantasma")

    # ------------------------------------------------------------ utilidades

    def hash_ip(self, ip: str) -> str:
        try:
            dir = ipaddress.ip_address(ip)
        except ValueError:
            clave = ip
        else:
            # Quien tiene IPv6 controla su /64 entero: contar cada dirección aparte le daría corridas sin límite.
            clave = str(ipaddress.ip_network(f"{dir}/64", strict=False).network_address) if dir.version == 6 else str(dir)
        return hashlib.sha256(f"{self._sal}:{clave}".encode()).hexdigest()[:32]

    def _dir(self, run_id: str) -> Path:
        return self.raiz / run_id

    def _publicar(self, run_id: str, tipo: str, **datos: Any) -> None:
        self.eventos.publicar(run_id, tipo, **datos)

    def _fila(self, run_id: str):
        fila = self.indice.obtener(run_id)
        if fila is None:
            raise KeyError(f"No existe la corrida {run_id}")
        return fila

    def _verificar_limites(self, ip_hash: str) -> None:
        if self.indice.activas_de(ip_hash):
            raise Limite("err_limite_activa", "Ya tienes una prueba en curso: espera a que termine.")
        if self.indice.del_dia(ip_hash) >= self.corridas_por_dia:
            raise Limite("err_limite_dia", f"Llegaste al máximo de {self.corridas_por_dia} pruebas por día.", n=self.corridas_por_dia)
        if self.indice.gasto_del_dia() >= self.tope_diario:
            raise Limite("err_tope_demo", "La demo llegó a su tope de gasto de hoy. Vuelve mañana.")
        if self.indice.gasto_del_dia_de(ip_hash) >= self.tope_ip:
            raise Limite("err_tope_ip", "Llegaste al tope de gasto por visitante de hoy. Vuelve mañana.")

    def _config(self, modo: Modo, tamaño: Tamaño, url_bot: str | None, encabezados: dict[str, str] | None) -> Config:
        t = self.tamaños[tamaño]
        d = self.config_base.model_dump(mode="json")
        d["casos"] = {**d["casos"], "cantidad": t.casos}
        d["presupuesto"] = {**d["presupuesto"], "max_iterations": t.iteraciones}
        # Que un solo caso cambie de resultado mueve el puntaje ~1/train: una mejora menor que eso es ruido.
        train = max(1, round(t.casos * (1 - d["casos"]["holdout"])))
        d["optimizacion"] = {
            **d["optimizacion"], "medir_ruido": t.medir_ruido, "criticos_rotos_tolerados": t.criticos_rotos_tolerados,
            "min_improvement": max(d["optimizacion"]["min_improvement"], round(1 / train, 3)),
        }
        if modo == "remoto":
            if not url_bot:
                raise ValueError("El modo remoto necesita la URL del webhook de tu bot")
            d["remoto"] = {"url": url_bot, "encabezados": encabezados or {}}
        else:
            d["remoto"] = None
        # model_validate y no model_copy: así corre el validador de la URL remota.
        return Config.model_validate(d)

    # ------------------------------------------------------------ 1. ficha

    async def crear_ficha(self, urls: list[str], texto: str, ip: str, nombre: str = "", *, al_crear: Callable[[str], None] | None = None) -> str:
        ip_hash = self.hash_ip(ip)
        self._verificar_limites(ip_hash)
        run_id = f"{datetime.now():%Y%m%d}-{secrets.token_hex(4)}"
        self.indice.crear(run_id, ip_hash, negocio=nombre)
        if al_crear:
            al_crear(run_id)
        self._publicar(run_id, "etapa", etapa="investigando")
        tavily_key = os.environ.get("TAVILY_API_KEY") or None
        previo = self.tavily_usd if tavily_key and (urls or nombre) else 0.0

        def al_gastar(usd: float) -> None:
            self.indice.actualizar(run_id, costo_usd=previo + usd)
            self._publicar(run_id, "costo", usd=round(previo + usd, 4))

        llm = costos.Medido(
            self.fabrica_llm(self.config_base), al_gastar,
            reservar=lambda usd: self.indice.reservar(run_id, usd, self.tamaños["ligera"].tope_usd, self.tope_diario, self.tope_ip),
            liquidar=lambda reserva, gasto: self.indice.liquidar(run_id, reserva, gasto),
        )
        try:
            # La investigación externa también consume presupuesto antes de salir.
            self.indice.reservar(run_id, previo, self.tamaños["ligera"].tope_usd, self.tope_diario, self.tope_ip)
            self.indice.liquidar(run_id, previo, previo)
            ficha = await investigar(
                llm,
                urls=urls,
                texto=texto,
                tavily_key=tavily_key,
                nombre=nombre,
                progreso=lambda m: self._publicar(run_id, "log", texto=m),
            )
        except BaseException as exc:
            estado = "presupuesto" if isinstance(exc, costos.PresupuestoAgotado) else "error"
            self.indice.actualizar(run_id, estado=estado, motivo=str(exc)[:500] or "cancelada", costo_usd=self._fila(run_id).costo_usd)
            self._publicar(run_id, "error", mensaje=str(exc))
            raise
        d = self._dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        (d / "ficha.json").write_text(ficha.model_dump_json(indent=2))
        self.indice.actualizar(run_id, estado="ficha", negocio=ficha.negocio, costo_usd=previo + llm.gastado())
        self._publicar(run_id, "ficha", ficha=ficha.model_dump(mode="json"))
        return run_id

    def ficha(self, run_id: str) -> Ficha:
        return Ficha.model_validate_json((self._dir(run_id) / "ficha.json").read_text())

    def actualizar_ficha(self, run_id: str, ficha: Ficha | dict) -> Ficha:
        if self._fila(run_id).estado != "ficha":
            raise ValueError("La ficha ya no se puede editar: la prueba está lanzada")
        ficha = Ficha.model_validate(ficha)
        (self._dir(run_id) / "ficha.json").write_text(ficha.model_dump_json(indent=2))
        self.indice.actualizar(run_id, negocio=ficha.negocio)
        return ficha

    def guardar_seleccion(self, run_id: str, modo: Modo, prompt: str = "", url_bot: str = "") -> None:
        if modo not in ("generado", "prompt", "remoto"):
            raise ValueError(f"Modo de prueba desconocido: {modo}")
        ruta = self._dir(run_id) / "seleccion.json"
        seleccion = {
            "modo": modo,
            "prompt": prompt if modo == "prompt" else "",
            "url_bot": url_bot if modo == "remoto" else "",
        }
        ruta.write_text(json.dumps(seleccion, ensure_ascii=False))
        os.chmod(ruta, 0o600)

    def seleccion(self, run_id: str) -> dict[str, str]:
        ruta = self._dir(run_id) / "seleccion.json"
        if not ruta.exists():
            return {"modo": "generado", "prompt": "", "url_bot": ""}
        return json.loads(ruta.read_text())

    # ------------------------------------------------------------ 2. corrida

    async def lanzar(
        self,
        run_id: str,
        modo: Modo,
        tamaño: Tamaño,
        url_bot: str | None = None,
        encabezados: dict[str, str] | None = None,
        prompt_usuario: str | None = None,
    ) -> asyncio.Task:
        if modo not in ("generado", "prompt", "remoto"):
            raise ValueError(f"Modo de prueba desconocido: {modo}")
        fila = self._fila(run_id)
        if fila.estado != "ficha":
            raise ValueError(f"La corrida {run_id} no está esperando lanzamiento (estado {fila.estado})")
        if self.indice.activas_de(fila.ip_hash):
            raise Limite("err_limite_activa", "Ya tienes una prueba en curso: espera a que termine.")
        if self.indice.gasto_del_dia() >= self.tope_diario:
            raise Limite("err_tope_demo", "La demo llegó a su tope de gasto de hoy. Vuelve mañana.")
        if self.indice.gasto_del_dia_de(fila.ip_hash) >= self.tope_ip:
            raise Limite("err_tope_ip", "Llegaste al tope de gasto por visitante de hoy. Vuelve mañana.")
        if modo == "prompt" and not (prompt_usuario or "").strip():
            raise ValueError("El modo prompt necesita las instrucciones que quieres probar")
        if modo == "prompt" and tokens(prompt_usuario or "") > self.config_base.optimizacion.max_prompt_tokens:
            raise ValueError(f"Las instrucciones superan el límite de {self.config_base.optimizacion.max_prompt_tokens} tokens")
        config = self._config(modo, tamaño, url_bot, encabezados)
        # Se reserva antes del primer await: dos POST simultáneos verían los dos 'ficha' y lanzarían dos corridas.
        self.indice.actualizar(run_id, modo=modo, tamaño=tamaño, estado="generando")
        if config.remoto:
            # Antes de gastar en generar casos: una URL interna o que no resuelve se rechaza ya.
            try:
                await remoto.comprobar(config.remoto, config.remoto_inseguro)
            except Exception:
                self.indice.actualizar(run_id, estado="ficha")
                raise
        tarea = asyncio.create_task(self._correr(run_id, config, modo, tamaño, fila.costo_usd, prompt_usuario))
        self.tareas[run_id] = tarea
        tarea.add_done_callback(lambda _: self.tareas.pop(run_id, None))
        return tarea

    async def _correr(
        self,
        run_id: str,
        config: Config,
        modo: Modo,
        tamaño: Tamaño,
        costo_previo: float,
        prompt_usuario: str | None = None,
    ) -> str:
        t0 = time.monotonic()
        d = self._dir(run_id)
        publicar = lambda tipo, **datos: self._publicar(run_id, tipo, **datos)
        store = StoreObservado(d, publicar)
        (d / "config.yaml").write_text(yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True, sort_keys=False))
        ficha = self.ficha(run_id)
        store.guardar_corrida(Corrida(id=run_id, rubro=ficha.rubro, estado="spec"))

        tope = self.tamaños[tamaño].tope_usd
        ip_hash = self._fila(run_id).ip_hash
        def al_gastar(usd: float) -> None:
            total = costo_previo + usd
            self.indice.actualizar(run_id, costo_usd=total)
            publicar("costo", usd=round(total, 4))
            # Defensa adicional ante respuestas que superen la reserva estimada.
            if total > tope:
                raise costos.PresupuestoAgotado(f"tope de la corrida (US${tope:.2f})")
            if self.indice.gasto_del_dia() > self.tope_diario:
                raise costos.PresupuestoAgotado(f"tope diario de la demo (US${self.tope_diario:.2f})")
            if self.indice.gasto_del_dia_de(ip_hash) > self.tope_ip:
                raise costos.PresupuestoAgotado(f"tope diario por visitante (US${self.tope_ip:.2f})")

        llm = costos.Medido(
            self.fabrica_llm(config), al_gastar,
            reservar=lambda usd: self.indice.reservar(run_id, usd, tope, self.tope_diario, self.tope_ip),
            liquidar=lambda reserva, gasto: self.indice.liquidar(run_id, reserva, gasto),
        )
        progreso = lambda m: publicar("log", texto=m)
        estado, motivo = "terminada", ""
        try:
            publicar("etapa", etapa="generando")
            rubro = rubro_de(ficha)
            spec = await generar_spec(
                llm,
                config,
                rubro,
                progreso,
                datos=a_datos(ficha, rubro),
                prompt_base=prompt_usuario if modo == "prompt" else None,
            )
            store.guardar_spec(spec)
            self.indice.actualizar(run_id, estado="corriendo")
            publicar("etapa", etapa="conversando", casos=len(spec.casos))
            ctx = Contexto(config, llm, spec, store, al_turno=self._al_turno(run_id, spec))
            if modo == "remoto":
                await self._medir_remoto(ctx, progreso)
            else:
                corrida = await correr(ctx, progreso)
                # "presupuesto" del bucle es haber hecho todas las rondas pedidas, no el tope de gasto.
                motivo = MOTIVOS.get(corrida.motivo_parada, corrida.motivo_parada)
        except costos.PresupuestoAgotado as exc:
            estado, motivo = "presupuesto", str(exc)
        except asyncio.CancelledError:
            self._cerrar(run_id, store, "error", "cancelada", costo_previo + llm.gastado(), t0)
            (d / "costos.json").write_text(json.dumps({"previo": costo_previo, "por_rol": costos.por_rol(llm, config), "sin_confirmar_usd": llm.sin_confirmar}, indent=2))
            raise
        except Exception as exc:  # noqa: BLE001
            # Nadie espera esta tarea de fondo: el error queda en el índice y en los eventos para la app.
            estado, motivo = "error", f"{type(exc).__name__}: {exc}"[:500]
        self._cerrar(run_id, store, estado, motivo, costo_previo + llm.gastado(), t0)
        (d / "costos.json").write_text(json.dumps({"previo": costo_previo, "por_rol": costos.por_rol(llm, config), "sin_confirmar_usd": llm.sin_confirmar}, indent=2))
        return estado

    def _cerrar(self, run_id: str, store: Store, estado: str, motivo: str, costo: float, t0: float) -> None:
        c = store.corrida()
        if estado != "terminada":
            c.estado = "presupuesto" if estado == "presupuesto" else "error"
            c.motivo_parada = motivo
            store.guardar_corrida(c)
        self.indice.actualizar(run_id, estado=estado, motivo=motivo, costo_usd=costo)
        segundos = round(time.monotonic() - t0, 1)
        if estado == "error":
            self._publicar(run_id, "error", mensaje=motivo)
        self._publicar(run_id, "fin", estado=estado, motivo=motivo, costo_usd=round(costo, 4), segundos=segundos, boleta=bool(store.evaluaciones(0)))

    def _al_turno(self, run_id: str, spec) -> Callable[[Transcripcion], None]:
        casos = {c.id: c for c in spec.casos}

        def al_turno(t: Transcripcion) -> None:
            if len(t.turnos) == 1:
                caso = casos[t.caso_id]
                self._publicar(
                    run_id, "conversacion", caso_id=t.caso_id, version=t.version, repeticion=t.repeticion,
                    escenario=caso.escenario, ejes=caso.ejes, persona=caso.persona.nombre,
                )
            x = t.turnos[-1]
            self._publicar(
                run_id, "turno", caso_id=t.caso_id, version=t.version, repeticion=t.repeticion, n=len(t.turnos) - 1,
                rol=x.rol, texto=x.texto, tools=[ll.nombre for ll in x.tools],
            )

        return al_turno

    async def _medir_remoto(self, ctx: Contexto, progreso: Callable[[str], None]) -> None:
        """Al bot remoto no se le puede cambiar el prompt: se mide una vez y el optimizador solo deja una sugerencia."""
        spec, store, reps = ctx.spec, ctx.store, ctx.config.repetitions_per_case
        prompt = spec.prompt_base
        store.guardar_version(VersionPrompt(n=0, estado="base", tokens=tokens(prompt)), prompt)
        (ag_t, res_t), (ag_h, _) = await reunir(
            evaluar_conjunto(ctx, spec.de("train"), prompt, 0, "train", reps, remoto.ROL, progreso),
            evaluar_conjunto(ctx, spec.de("holdout"), prompt, 0, "holdout", reps, remoto.ROL, progreso),
        )
        v = store.version(0)
        v.train, v.criticos_ok, v.holdout = ag_t.puntaje, ag_t.criticos_ok, ag_h.puntaje
        store.guardar_version(v)
        fallos = _fallos(store, spec.de("train"), res_t, 0, reps, 4)
        if fallos:
            self._publicar(store.dir.name, "etapa", etapa="sugerencia")
            p = await proponer(ctx, prompt, ag_t, fallos, store.versiones(), [LECCION_REMOTO])
            # El prompt de arriba lo generó Ultra, no el dueño: un diff contra él no se puede aplicar. Se entregan solo las reglas nuevas.
            sugerencia = {"cambio": p.cambio, "hipotesis": p.hipotesis, "texto": boletas.lineas_nuevas(prompt, p.prompt)}
            (store.dir / boletas.SUGERENCIA).write_text(json.dumps(sugerencia, ensure_ascii=False, indent=2))
        c = store.corrida()
        c.estado, c.motivo_parada = "listo", "remoto: una medición"
        store.guardar_corrida(c)

    # ------------------------------------------------------------ 3. consulta

    def estado(self, run_id: str) -> dict:
        fila = self._fila(run_id)
        out = {**fila.model_dump(mode="json", exclude={"ip_hash"}), "activa": run_id in self.tareas, "corrida": None, "versiones": []}
        if (self._dir(run_id) / "results.sqlite").exists():
            store = Store(self._dir(run_id))
            corrida = store.corrida()
            out["corrida"] = corrida.model_dump(mode="json") if corrida else None
            out["versiones"] = [v.model_dump(mode="json", include={"n", "estado", "cambio", "motivo", "train", "holdout"}) for v in store.versiones()]
            out["evaluadas"] = len(store.evaluaciones(0))
            store.db.close()
        return out

    def boleta(self, run_id: str, publica: bool = False) -> boletas.Boleta:
        fila = self._fila(run_id)
        d = self._dir(run_id)
        config = cargar(d / "config.yaml")
        modelos = {rol: r.modelo for rol, r in config.roles.items()}
        if fila.modo == "remoto":
            # El host del webhook es del dueño: el enlace público lo puede abrir cualquiera.
            modelos["agente"] = "tu bot (webhook)" if publica else remoto.modelo(config.remoto)
        store = Store(d)
        try:
            return boletas.armar(store, run_id=run_id, modo=fila.modo, estado=fila.estado, costo_usd=fila.costo_usd, modelos=modelos)
        finally:
            store.db.close()

    def boleta_publica(self, token: str) -> boletas.Boleta:
        fila = self.indice.por_token(token)
        if fila is None:
            raise KeyError("Enlace no válido")
        return self.boleta(fila.id, publica=True)

    def exportar_jsonl(self, run_id: str) -> Path:
        self._fila(run_id)
        store = Store(self._dir(run_id))
        try:
            return boletas.exportar_jsonl(store, self._dir(run_id) / "export" / "conversaciones.jsonl")
        finally:
            store.db.close()
