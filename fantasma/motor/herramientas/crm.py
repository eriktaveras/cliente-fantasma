import re
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from fantasma.motor.esquemas import Lead, Mundo, ResultadoTool
from fantasma.motor.herramientas.base import Funcion, Herramienta


def _nombre(v: str) -> str:
    v = " ".join(v.split())
    if sum(c.isalpha() for c in v) < 2:
        raise ValueError("el nombre debe tener al menos 2 letras")
    return v


def digitos(telefono: str) -> str:
    return re.sub(r"\D", "", telefono)


def _telefono(v: str) -> str:
    if len(digitos(v)) < 7:
        raise ValueError("el teléfono debe tener al menos 7 dígitos")
    return digitos(v)


Nombre = Annotated[str, AfterValidator(_nombre)]
Telefono = Annotated[str, AfterValidator(_telefono)]


def mismo_telefono(a: str, b: str) -> bool:
    # Los últimos 10 dígitos ignoran el prefijo de país (+1 809... == 809...).
    return digitos(a)[-10:] == digitos(b)[-10:]


def siguiente_id(prefijo: str, ids: list[str]) -> str:
    usados = [int(m.group(1)) for i in ids if (m := re.fullmatch(rf"{prefijo}-(\d+)", i))]
    return f"{prefijo}-{max(usados, default=0) + 1:03d}"


class ArgsCapturar(BaseModel):
    nombre: Nombre
    telefono: Telefono
    interes: str = Field(min_length=2, description="Qué busca el cliente")
    presupuesto: Decimal | None = Field(None, ge=0)


class ArgsConsultar(BaseModel):
    telefono: Telefono


class ArgsActualizar(BaseModel):
    id: str
    interes: str | None = None
    presupuesto: Decimal | None = Field(None, ge=0)
    notas: str | None = None


class Crm(Herramienta):
    nombre = "crm"

    def funciones(self) -> list[Funcion]:
        return [
            Funcion(
                nombre="capturar_lead",
                descripcion="Registra un interesado. Si el teléfono ya existe, actualiza su ficha en vez de duplicarla.",
                args=ArgsCapturar,
                ejecutar=self.capturar_lead,
            ),
            Funcion(nombre="consultar_cliente", descripcion="Busca un cliente por teléfono.", args=ArgsConsultar, ejecutar=self.consultar_cliente),
            Funcion(nombre="actualizar_lead", descripcion="Actualiza interés, presupuesto o notas de un lead por su id.", args=ArgsActualizar, ejecutar=self.actualizar_lead),
        ]

    def capturar_lead(self, a: ArgsCapturar, mundo: Mundo) -> ResultadoTool:
        lead = next((l for l in mundo.leads if mismo_telefono(l.telefono, a.telefono)), None)
        if lead is None:
            lead = Lead(id=siguiente_id("LEAD", [l.id for l in mundo.leads]), nombre=a.nombre, telefono=a.telefono, interes=a.interes, presupuesto=a.presupuesto)
            mundo.leads.append(lead)
            return ResultadoTool(ok=True, data={"nuevo": True, "lead": lead.model_dump(mode="json")})
        lead.nombre, lead.interes = a.nombre, a.interes
        if a.presupuesto is not None:
            lead.presupuesto = a.presupuesto
        return ResultadoTool(ok=True, data={"nuevo": False, "lead": lead.model_dump(mode="json")})

    def consultar_cliente(self, a: ArgsConsultar, mundo: Mundo) -> ResultadoTool:
        lead = next((l for l in mundo.leads if mismo_telefono(l.telefono, a.telefono)), None)
        if lead is None:
            return ResultadoTool(ok=True, data={"encontrado": False})
        return ResultadoTool(ok=True, data={"encontrado": True, "lead": lead.model_dump(mode="json")})

    def actualizar_lead(self, a: ArgsActualizar, mundo: Mundo) -> ResultadoTool:
        lead = next((l for l in mundo.leads if l.id == a.id), None)
        if lead is None:
            return ResultadoTool(ok=False, error=f"No existe el lead {a.id}")
        if a.interes is None and a.presupuesto is None and a.notas is None:
            return ResultadoTool(ok=False, error="Indica al menos un campo a actualizar: interes, presupuesto o notas")
        if a.interes is not None:
            lead.interes = a.interes
        if a.presupuesto is not None:
            lead.presupuesto = a.presupuesto
        if a.notas is not None:
            lead.notas = a.notas
        return ResultadoTool(ok=True, data={"lead": lead.model_dump(mode="json")})
