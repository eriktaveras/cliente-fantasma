from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fantasma.motor.esquemas import Cita, DatosRubro, Horario, Mundo, ResultadoTool, RubroSpec
from fantasma.motor.herramientas.base import Funcion, Herramienta
from fantasma.motor.herramientas.catalogo import buscar_item
from fantasma.motor.herramientas.crm import Nombre, Telefono, siguiente_id

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class ArgsDisponibilidad(BaseModel):
    fecha: date = Field(description="Día a consultar, AAAA-MM-DD")


class ArgsAgendar(BaseModel):
    inicio: datetime = Field(description="Inicio de la cita, AAAA-MM-DDTHH:MM, uno de los huecos de disponibilidad")
    nombre: Nombre
    telefono: Telefono
    item_id: str | None = Field(None, description="Id del item del catálogo, si la cita es por uno")
    motivo: str = ""


class ArgsCancelar(BaseModel):
    cita_id: str


class Calendario(Herramienta):
    nombre = "calendario"

    def __init__(self, datos: DatosRubro, rubro: RubroSpec) -> None:
        super().__init__(datos, rubro)
        self.horario: Horario = datos.horario or Horario()
        self.duracion = timedelta(minutes=self.horario.duracion_min)

    def funciones(self) -> list[Funcion]:
        h = self.horario
        dias = ", ".join(DIAS[d] for d in sorted(h.dias))
        return [
            Funcion(
                nombre="disponibilidad",
                descripcion=f"Huecos libres de un día. Atendemos {dias} de {h.abre:%H:%M} a {h.cierra:%H:%M}, citas de {h.duracion_min} min.",
                args=ArgsDisponibilidad,
                ejecutar=self.disponibilidad,
            ),
            Funcion(nombre="agendar", descripcion="Agenda una cita en un hueco libre.", args=ArgsAgendar, ejecutar=self.agendar),
            Funcion(nombre="cancelar", descripcion="Cancela una cita por su id.", args=ArgsCancelar, ejecutar=self.cancelar),
        ]

    def _local(self, dt: datetime) -> datetime:
        # El mundo usa horas locales sin zona; si el modelo manda una con zona, se pasa a la del negocio.
        return dt.astimezone(ZoneInfo(self.horario.zona)).replace(tzinfo=None) if dt.tzinfo else dt

    def _dia_habil(self, d: date) -> bool:
        return d.weekday() in self.horario.dias and d not in self.horario.feriados

    def _huecos_del_dia(self, d: date) -> list[datetime]:
        if not self._dia_habil(d):
            return []
        actual, fin = datetime.combine(d, self.horario.abre), datetime.combine(d, self.horario.cierra)
        huecos = []
        while actual + self.duracion <= fin:
            huecos.append(actual)
            actual += self.duracion
        return huecos

    def _ocupado(self, inicio: datetime, mundo: Mundo) -> bool:
        tomados = [*self.horario.ocupados, *(c.inicio for c in mundo.citas if not c.cancelada)]
        return any(abs(t - inicio) < self.duracion for t in tomados)

    def disponibilidad(self, a: ArgsDisponibilidad, mundo: Mundo) -> ResultadoTool:
        libres = [h for h in self._huecos_del_dia(a.fecha) if h > mundo.reloj and not self._ocupado(h, mundo)]
        data: dict = {"fecha": a.fecha.isoformat(), "huecos": [h.strftime("%Y-%m-%dT%H:%M") for h in libres]}
        if not self._dia_habil(a.fecha):
            data["motivo"] = "feriado" if a.fecha in self.horario.feriados else f"no se atiende los {DIAS[a.fecha.weekday()]}"
        elif a.fecha < mundo.reloj.date():
            data["motivo"] = "fecha pasada"
        return ResultadoTool(ok=True, data=data)

    def agendar(self, a: ArgsAgendar, mundo: Mundo) -> ResultadoTool:
        inicio = self._local(a.inicio)
        if inicio <= mundo.reloj:
            return ResultadoTool(ok=False, error=f"{inicio:%Y-%m-%d %H:%M} ya pasó; hoy es {mundo.reloj:%Y-%m-%d %H:%M}")
        if not self._dia_habil(inicio.date()):
            return ResultadoTool(ok=False, error=f"El {inicio:%Y-%m-%d} no se atiende")
        if inicio not in self._huecos_del_dia(inicio.date()):
            h = self.horario
            return ResultadoTool(
                ok=False,
                error=f"{inicio:%H:%M} no es un hueco válido: horario {h.abre:%H:%M}-{h.cierra:%H:%M} en bloques de {h.duracion_min} min",
            )
        if self._ocupado(inicio, mundo):
            return ResultadoTool(ok=False, error=f"{inicio:%Y-%m-%d %H:%M} está ocupado; consulta disponibilidad")
        if a.item_id is not None and buscar_item(self.datos, a.item_id) is None:
            return ResultadoTool(ok=False, error=f"El item {a.item_id} no existe en el catálogo")
        cita = Cita(
            id=siguiente_id("CITA", [c.id for c in mundo.citas]),
            inicio=inicio,
            nombre=a.nombre,
            telefono=a.telefono,
            item_id=a.item_id,
            motivo=a.motivo,
        )
        mundo.citas.append(cita)
        return ResultadoTool(ok=True, data=cita.model_dump(mode="json"))

    def cancelar(self, a: ArgsCancelar, mundo: Mundo) -> ResultadoTool:
        cita = next((c for c in mundo.citas if c.id == a.cita_id), None)
        if cita is None:
            return ResultadoTool(ok=False, error=f"No existe la cita {a.cita_id}")
        if cita.cancelada:
            return ResultadoTool(ok=False, error=f"La cita {a.cita_id} ya estaba cancelada")
        cita.cancelada = True
        return ResultadoTool(ok=True, data=cita.model_dump(mode="json"))
