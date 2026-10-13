import unicodedata
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, create_model

from fantasma.motor.esquemas import DatosRubro, Hechos, Item, Mundo, ResultadoTool, RubroSpec
from fantasma.motor.herramientas.base import Funcion, Herramienta

MAX_RESULTADOS = 5


def normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.lower().split())


def item_publico(item: Item) -> dict:
    return item.model_dump(mode="json", include={"id", "nombre", "categoria", "precio", "moneda", "disponible", "atributos"})


def buscar_item(datos: DatosRubro, item_id: str) -> Item | None:
    return next((i for i in datos.catalogo if i.id == item_id), None)


def _tipos_atributos(catalogo: list[Item]) -> dict[str, type]:
    """Un atributo con algún valor de texto se filtra como texto; si todos son bool, como bool; si no, como número."""
    valores: dict[str, list] = {}
    for item in catalogo:
        for clave, valor in item.atributos.items():
            valores.setdefault(clave, []).append(valor)
    tipos: dict[str, type] = {}
    for clave, vs in valores.items():
        if any(isinstance(v, str) for v in vs):
            tipos[clave] = str
        elif all(isinstance(v, bool) for v in vs):
            tipos[clave] = bool
        else:
            tipos[clave] = float
    return tipos


class ArgsDetalle(BaseModel):
    id: str = Field(description="Id del item, p. ej. el que devolvió buscar")


class Catalogo(Herramienta):
    nombre = "catalogo"

    def __init__(self, datos: DatosRubro, rubro: RubroSpec) -> None:
        super().__init__(datos, rubro)
        fijos = {"texto", "categoria", "precio_min", "precio_max"}
        # Las claves vienen de datos generados por IA: solo las que sirven como nombre de argumento.
        self.tipos = {
            k: t for k, t in _tipos_atributos(datos.catalogo).items() if k.isidentifier() and k not in fijos and f"{k}_min" not in fijos
        }
        campos: dict[str, Any] = {
            "texto": (str | None, Field(None, description="Palabras a buscar en nombre, descripción y atributos")),
            "categoria": (str | None, Field(None, description="Categoría exacta")),
            "precio_min": (Decimal | None, Field(None, ge=0)),
            "precio_max": (Decimal | None, Field(None, ge=0)),
        }
        for clave, tipo in self.tipos.items():
            if tipo is float:
                campos[f"{clave}_min"] = (float | None, Field(None, description=f"Valor mínimo de {clave}"))
            else:
                campos[clave] = (tipo | None, Field(None))
        self.args_buscar = create_model("ArgsBuscar", **campos)

    def _descripcion_buscar(self) -> str:
        categorias = sorted({i.categoria for i in self.datos.catalogo})
        partes = [f"Busca en el catálogo de {self.rubro.negocio}. Devuelve hasta {MAX_RESULTADOS} items y el total de coincidencias."]
        if categorias:
            partes.append("Categorías válidas: " + ", ".join(categorias) + ".")
        for clave, tipo in self.tipos.items():
            if tipo is str:
                valores = sorted({str(i.atributos[clave]) for i in self.datos.catalogo if clave in i.atributos})
                partes.append(f"Valores de {clave}: " + ", ".join(valores) + ".")
        return " ".join(partes)

    def funciones(self) -> list[Funcion]:
        return [
            Funcion(nombre="buscar", descripcion=self._descripcion_buscar(), args=self.args_buscar, ejecutar=self.buscar),
            Funcion(nombre="detalle", descripcion="Ficha completa de un item por su id.", args=ArgsDetalle, ejecutar=self.detalle),
        ]

    def hechos(self) -> Hechos:
        return Hechos(
            ids={i.id for i in self.datos.catalogo},
            precios={i.precio for i in self.datos.catalogo},
            nombres={i.nombre for i in self.datos.catalogo},
        )

    def _coincide(self, item: Item, a: BaseModel) -> bool:
        if a.categoria is not None and normalizar(item.categoria) != normalizar(a.categoria):
            return False
        if a.precio_min is not None and item.precio < a.precio_min:
            return False
        if a.precio_max is not None and item.precio > a.precio_max:
            return False
        for clave, tipo in self.tipos.items():
            if tipo is float:
                minimo = getattr(a, f"{clave}_min")
                valor = item.atributos.get(clave)
                if minimo is not None and (not isinstance(valor, (int, float)) or valor < minimo):
                    return False
                continue
            buscado = getattr(a, clave)
            if buscado is None:
                continue
            valor = item.atributos.get(clave)
            if tipo is str and (valor is None or normalizar(str(valor)) != normalizar(buscado)):
                return False
            if tipo is bool and valor is not buscado:
                return False
        if a.texto:
            pajar = normalizar(" ".join([item.nombre, item.categoria, item.descripcion, *map(str, item.atributos.values())]))
            if not all(p in pajar for p in normalizar(a.texto).split()):
                return False
        return True

    def buscar(self, a: BaseModel, mundo: Mundo) -> ResultadoTool:
        encontrados = [i for i in self.datos.catalogo if self._coincide(i, a)]
        return ResultadoTool(ok=True, data={"total": len(encontrados), "items": [item_publico(i) for i in encontrados[:MAX_RESULTADOS]]})

    def detalle(self, a: ArgsDetalle, mundo: Mundo) -> ResultadoTool:
        item = buscar_item(self.datos, a.id)
        if item is None:
            return ResultadoTool(ok=False, error=f"El item {a.id} no existe en el catálogo")
        return ResultadoTool(ok=True, data=item.model_dump(mode="json"))
