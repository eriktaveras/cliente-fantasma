import re

from pydantic import BaseModel, Field

from fantasma.motor.esquemas import Mundo, ResultadoTool
from fantasma.motor.herramientas.base import Funcion, Herramienta
from fantasma.motor.herramientas.catalogo import normalizar

MAX_RESULTADOS = 3

STOPWORDS = set(
    "a al algo como con cual cuales cuando de del el ella en es esta este esto hay la las le les lo los mas me mi mis muy no o "
    "para pero por que se si sin su sus te tu tus un una unas uno unos y ya yo puedo puede pueden quiero tienen tiene tienes "
    "the is are do does can i you a an of to in for and or what how".split()
)


def palabras(texto: str) -> set[str]:
    # Quitar la "s" final empareja plurales simples (precios/precio) sin depender de un stemmer.
    return {p[:-1] if len(p) > 3 and p.endswith("s") else p for p in re.findall(r"[a-z0-9]+", normalizar(texto)) if p not in STOPWORDS}


class ArgsBuscarInfo(BaseModel):
    consulta: str = Field(min_length=2, description="La pregunta del cliente en palabras clave")


class Faq(Herramienta):
    nombre = "faq"

    def funciones(self) -> list[Funcion]:
        return [
            Funcion(
                nombre="buscar_info",
                descripcion=f"Busca en las preguntas frecuentes de {self.rubro.negocio} (políticas, horarios, condiciones). Devuelve hasta {MAX_RESULTADOS}.",
                args=ArgsBuscarInfo,
                ejecutar=self.buscar_info,
            )
        ]

    def buscar_info(self, a: ArgsBuscarInfo, mundo: Mundo) -> ResultadoTool:
        consulta = palabras(a.consulta)
        puntuadas = []
        for i, e in enumerate(self.datos.faq):
            solape = len(consulta & palabras(" ".join([e.pregunta, *e.claves])))
            if solape:
                puntuadas.append((-solape, i, e))
        puntuadas.sort(key=lambda t: t[:2])
        return ResultadoTool(ok=True, data=[{"pregunta": e.pregunta, "respuesta": e.respuesta} for _, _, e in puntuadas[:MAX_RESULTADOS]])
