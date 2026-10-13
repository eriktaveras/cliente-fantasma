from pydantic import BaseModel, Field

from fantasma.motor.esquemas import Mundo, ResultadoTool
from fantasma.motor.herramientas.base import Funcion, Herramienta


class ArgsEscalar(BaseModel):
    motivo: str = Field(min_length=5, description="Por qué hace falta una persona, con el contexto del cliente")


class Escalar(Herramienta):
    nombre = "escalar"

    def funciones(self) -> list[Funcion]:
        return [
            Funcion(
                nombre="escalar_a_humano",
                descripcion="Pasa la conversación a una persona del equipo cuando no puedes resolver o el cliente lo pide.",
                args=ArgsEscalar,
                ejecutar=self.escalar_a_humano,
            )
        ]

    def escalar_a_humano(self, a: ArgsEscalar, mundo: Mundo) -> ResultadoTool:
        mundo.escalados.append(a.motivo.strip())
        ticket = f"ESC-{len(mundo.escalados):03d}"
        return ResultadoTool(ok=True, data={"ticket": ticket, "mensaje": "Una persona del equipo continuará la conversación."})
