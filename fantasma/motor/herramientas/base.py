"""Interfaz común de la biblioteca de herramientas mock.

Cada herramienta se construye con los datos del rubro, expone sus funciones como tool schemas de OpenAI con nombre
"<herramienta>__<funcion>" (los puntos no son válidos en nombres de funciones), y ejecuta de forma determinista contra
un `Mundo` que es una copia por conversación. Un argumento inválido devuelve ResultadoTool(ok=False), nunca excepción.
"""

import json
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, ValidationError

from fantasma.motor.esquemas import DatosRubro, Hechos, LlamadaTool, Mundo, ResultadoTool, RubroSpec

SEPARADOR = "__"


class Funcion(BaseModel):
    """Una función de una herramienta: descripción + modelo pydantic de argumentos + implementación."""

    model_config = {"arbitrary_types_allowed": True}

    nombre: str
    descripcion: str
    args: type[BaseModel]
    ejecutar: Callable[[Any, Mundo], ResultadoTool]


class Herramienta:
    nombre: ClassVar[str]

    def __init__(self, datos: DatosRubro, rubro: RubroSpec) -> None:
        self.datos = datos
        self.rubro = rubro

    def funciones(self) -> list[Funcion]:
        raise NotImplementedError

    def hechos(self) -> Hechos:
        return Hechos()

    def definiciones(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": f"{self.nombre}{SEPARADOR}{f.nombre}",
                    "description": f.descripcion,
                    "parameters": _parametros(f.args),
                },
            }
            for f in self.funciones()
        ]

    def ejecutar(self, funcion: str, args: dict, mundo: Mundo) -> ResultadoTool:
        f = next((f for f in self.funciones() if f.nombre == funcion), None)
        if f is None:
            return ResultadoTool(ok=False, error=f"La función {funcion} no existe en {self.nombre}")
        try:
            validos = f.args.model_validate(args)
        except ValidationError as exc:
            return ResultadoTool(ok=False, error="Argumentos inválidos: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
        return f.ejecutar(validos, mundo)


def _parametros(modelo: type[BaseModel]) -> dict:
    """JSON schema plano: sin $defs ni títulos, que a los modelos locales les cuesta seguir."""
    s = modelo.model_json_schema()
    defs = s.pop("$defs", {})

    def limpiar(nodo):
        if isinstance(nodo, dict):
            if "$ref" in nodo:
                return limpiar(defs[nodo["$ref"].rsplit("/", 1)[-1]])
            return {k: limpiar(v) for k, v in nodo.items() if k != "title"}
        if isinstance(nodo, list):
            return [limpiar(x) for x in nodo]
        return nodo

    return limpiar(s)


class Caja:
    """Todas las herramientas de un rubro juntas: lo que ve el agente y lo que ejecuta el runner."""

    def __init__(self, herramientas: list[Herramienta]) -> None:
        self.por_nombre = {h.nombre: h for h in herramientas}

    def definiciones(self) -> list[dict]:
        return [d for h in self.por_nombre.values() for d in h.definiciones()]

    def hechos(self) -> Hechos:
        total = Hechos()
        for h in self.por_nombre.values():
            total = total.unir(h.hechos())
        return total

    def ejecutar(self, nombre_completo: str, argumentos: str, mundo: Mundo) -> LlamadaTool:
        herramienta, _, funcion = nombre_completo.partition(SEPARADOR)
        nombre = f"{herramienta}.{funcion}" if funcion else f"desconocida.{nombre_completo}"
        try:
            args = json.loads(argumentos or "{}")
            if not isinstance(args, dict):
                raise ValueError("los argumentos deben ser un objeto")
        except (json.JSONDecodeError, ValueError) as exc:
            return LlamadaTool(nombre=nombre, args={"_crudo": argumentos}, resultado=ResultadoTool(ok=False, error=f"JSON inválido: {exc}"))
        h = self.por_nombre.get(herramienta)
        if h is None:
            return LlamadaTool(nombre=f"desconocida.{nombre_completo}", args=args, resultado=ResultadoTool(ok=False, error=f"No existe la herramienta {nombre_completo}"))
        return LlamadaTool(nombre=nombre, args=args, resultado=h.ejecutar(funcion, args, mundo))


def mundo_inicial(datos: DatosRubro, rubro: RubroSpec) -> Mundo:
    return Mundo(reloj=rubro.hoy, leads=[c.model_copy() for c in datos.clientes])
