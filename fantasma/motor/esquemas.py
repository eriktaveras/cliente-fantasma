"""Contratos de datos de la cuna. Todo lo que cruza entre módulos o se guarda en disco pasa por aquí."""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

NombreHerramienta = Literal["catalogo", "calendario", "crm", "pedidos", "pagos", "escalar", "faq"]
Moneda = Literal["USD", "DOP", "MXN", "EUR", "COP"]
Etiqueta = Literal["normal", "critical", "adversarial", "edge"]
Split = Literal["train", "holdout"]


# ---------------------------------------------------------------- entrada: rubros/<rubro>.yaml


class Regla(BaseModel):
    id: str
    texto: str
    critica: bool = False


class RubroSpec(BaseModel):
    rubro: str
    negocio: str
    descripcion: str
    idiomas: list[str] = ["es"]
    tono: str
    herramientas: list[NombreHerramienta]
    reglas: list[Regla] = []
    escalar_cuando: list[str] = []
    modelo_destino: str = "ornith-1.5"
    max_palabras_respuesta: int = 80
    # "Hoy" dentro de la simulación: fija el calendario para que las corridas sean comparables.
    hoy: datetime = datetime(2026, 10, 12, 9, 0)

    @field_validator("hoy")
    @classmethod
    def _hoy_local(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=None)
    # Lo que la cuna recibe de semilla para generar datos y casos (catálogo de ejemplo, personas previas...).
    semilla: dict[str, Any] = {}

    @field_validator("herramientas")
    @classmethod
    def _escalar_siempre(cls, v: list[str]) -> list[str]:
        return v if "escalar" in v else [*v, "escalar"]


# ---------------------------------------------------------------- datos del rubro (los genera la IA, los valida esto)


class Item(BaseModel):
    id: str
    nombre: str
    categoria: str
    precio: Decimal
    moneda: Moneda = "USD"
    disponible: bool = True
    descripcion: str = ""
    atributos: dict[str, str | int | float | bool] = {}


class Horario(BaseModel):
    zona: str = "America/Santo_Domingo"
    dias: list[int] = [0, 1, 2, 3, 4]  # 0 = lunes
    abre: time = time(9, 0)
    cierra: time = time(18, 0)
    duracion_min: int = 60
    ocupados: list[datetime] = []
    feriados: list[date] = []

    # Todo el calendario es hora local de `zona` sin tzinfo; el generador a veces agrega "Z" o un offset a la hora local.
    @field_validator("abre", "cierra")
    @classmethod
    def _hora_local(cls, v: time) -> time:
        return v.replace(tzinfo=None)

    @field_validator("ocupados")
    @classmethod
    def _fechas_locales(cls, v: list[datetime]) -> list[datetime]:
        return [d.replace(tzinfo=None) for d in v]


class Lead(BaseModel):
    id: str
    nombre: str
    telefono: str
    interes: str = ""
    presupuesto: Decimal | None = None
    notas: str = ""


class EntradaFAQ(BaseModel):
    pregunta: str
    respuesta: str
    claves: list[str] = []


class DatosRubro(BaseModel):
    catalogo: list[Item] = []
    horario: Horario | None = None
    clientes: list[Lead] = []
    faq: list[EntradaFAQ] = []
    moneda: Moneda = "USD"


class Hechos(BaseModel):
    """Lo que el agente puede afirmar. Un id, precio o nombre fuera de aquí (y de lo que devolvieron las herramientas) es invento."""

    ids: set[str] = set()
    precios: set[Decimal] = set()
    nombres: set[str] = set()

    def unir(self, otro: "Hechos") -> "Hechos":
        return Hechos(ids=self.ids | otro.ids, precios=self.precios | otro.precios, nombres=self.nombres | otro.nombres)


# ---------------------------------------------------------------- estado del mundo simulado (uno por conversación)


class Cita(BaseModel):
    id: str
    inicio: datetime
    nombre: str
    telefono: str
    item_id: str | None = None
    motivo: str = ""
    cancelada: bool = False


class LineaPedido(BaseModel):
    item_id: str
    cantidad: int = Field(ge=1)
    precio_unitario: Decimal


class Pedido(BaseModel):
    id: str
    lineas: list[LineaPedido]
    total: Decimal
    moneda: Moneda
    nombre: str
    telefono: str
    pagado: bool = False


class Mundo(BaseModel):
    reloj: datetime
    citas: list[Cita] = []
    leads: list[Lead] = []
    pedidos: list[Pedido] = []
    escalados: list[str] = []
    enlaces_pago: list[str] = []


class ResultadoTool(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None


# ---------------------------------------------------------------- spec: rúbrica y casos


class Criterio(BaseModel):
    id: str
    nombre: str
    descripcion: str
    peso: float = Field(gt=0)
    critico: bool = False
    cumple: list[str] = []
    no_cumple: list[str] = []


class Rubrica(BaseModel):
    criterios: list[Criterio]


Dialecto = Literal["neutro", "rd", "mx", "co", "ar", "es"]


class Persona(BaseModel):
    nombre: str
    estilo: str
    idioma: Literal["es", "en", "spanglish"] = "es"
    dialecto: Dialecto = "neutro"
    paciencia: Literal["baja", "media", "alta"] = "media"
    detalle: Literal["escaso", "normal", "mucho"] = "normal"


class Expectativa(BaseModel):
    """Resultado esperado que se comprueba con código.

    llama / no_llama: `herramienta` es "<herramienta>.<funcion>" (p. ej. "calendario.agendar") y `args` exige valores:
      un literal, "@existe" (el id está en el catálogo), "@presente" (no vacío) o "@confirmada" (fecha futura en horario).
    escala / no_escala: se llamó o no a escalar.escalar_a_humano.
    idioma: todas las respuestas del agente en `valor`.
    no_revela_prompt: el agente no copia trozos de su system prompt.
    """

    tipo: Literal["llama", "no_llama", "escala", "no_escala", "idioma", "no_revela_prompt"]
    herramienta: str | None = None
    args: dict[str, Any] = {}
    valor: str | None = None


class Caso(BaseModel):
    id: str
    escenario: str
    persona: Persona
    objetivo_oculto: str
    verdad: dict[str, Any] = {}
    esperado: list[Expectativa] = []
    etiquetas: set[Etiqueta] = {"normal"}
    split: Split = "train"
    # Los ejes del fantasma (intencion, ataque, dialecto, estilo) para agregar fallas en la boleta; vacío en specs de cuna.
    ejes: dict[str, str] = {}

    @property
    def critico(self) -> bool:
        return "critical" in self.etiquetas


# ---------------------------------------------------------------- transcripciones


class LlamadaTool(BaseModel):
    nombre: str  # "<herramienta>.<funcion>"; "desconocida.<x>" si el modelo inventó la función
    args: dict[str, Any]
    resultado: ResultadoTool


class Turno(BaseModel):
    rol: Literal["cliente", "agente"]
    texto: str
    tools: list[LlamadaTool] = []
    tokens_salida: int = 0
    ms: int = 0


class Transcripcion(BaseModel):
    caso_id: str
    repeticion: int
    version: int
    modelo_agente: str
    turnos: list[Turno] = []
    fin: Literal["objetivo", "rendido", "max_turnos", "error_tecnico"] = "max_turnos"
    error: str | None = None
    mundo_final: Mundo | None = None

    def llamadas(self) -> list[LlamadaTool]:
        return [ll for t in self.turnos for ll in t.tools]


# ---------------------------------------------------------------- evaluación


class Verificacion(BaseModel):
    id: str
    ok: bool
    critica: bool = False
    detalle: str = ""


class PuntajeCriterio(BaseModel):
    # El orden importa: con salida por gramática el modelo escribe la justificación antes de decidir la nota.
    criterio: str
    justificacion: str
    puntaje: int = Field(ge=1, le=5)


class ResultadoJuez(BaseModel):
    criterios: list[PuntajeCriterio]
    critical_violation: bool
    critical_motivo: str | None = None


class EvaluacionRep(BaseModel):
    """Una repetición de un caso, evaluada."""

    caso_id: str
    repeticion: int
    version: int
    verificaciones: list[Verificacion]
    juez: ResultadoJuez | None
    puntaje: float  # 0-1; 0 si hubo violación crítica
    critica: bool
    paso: bool


class ResultadoCaso(BaseModel):
    caso_id: str
    version: int
    puntajes: list[float]
    puntaje: float
    paso: bool
    inestable: bool
    fallos: list[str] = []


class Agregado(BaseModel):
    """Puntaje de una versión sobre un conjunto de casos."""

    version: int
    conjunto: Literal["corto", "train", "holdout", "produccion"]
    puntaje: float
    casos: int
    pasan: int
    criticos_total: int
    criticos_pasan: int
    inestables: int
    por_criterio: dict[str, float] = {}

    @property
    def criticos_ok(self) -> bool:
        return self.criticos_pasan == self.criticos_total


# ---------------------------------------------------------------- optimización


class Propuesta(BaseModel):
    hipotesis: str
    cambio: str
    prompt: str


class VersionPrompt(BaseModel):
    n: int
    padre: int | None = None
    hipotesis: str | None = None
    cambio: str | None = None
    tokens: int = 0
    corto: float | None = None
    train: float | None = None
    holdout: float | None = None
    criticos_ok: bool = False
    estado: Literal["base", "candidata", "aceptada", "rechazada", "revertida"] = "candidata"
    motivo: str = ""
    creada: datetime = Field(default_factory=datetime.now)


EstadoCorrida = Literal["spec", "esperando_aprobacion", "corriendo", "listo", "meseta", "presupuesto", "error"]


class Corrida(BaseModel):
    id: str
    rubro: str
    estado: EstadoCorrida = "spec"
    iteracion: int = 0
    mejor_version: int = 0
    min_improvement: float | None = None  # el efectivo, tras medir el ruido de v000
    inicio: datetime = Field(default_factory=datetime.now)
    horas: float = 0.0
    motivo_parada: str = ""


# ---------------------------------------------------------------- spec completa de una corrida (runs/<id>/spec/)


class Spec(BaseModel):
    rubro: RubroSpec
    datos: DatosRubro
    rubrica: Rubrica
    casos: list[Caso]
    prompt_base: str

    def de(self, split: Split) -> list[Caso]:
        return [c for c in self.casos if c.split == split]
