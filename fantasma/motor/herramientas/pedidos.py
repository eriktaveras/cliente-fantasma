from pydantic import BaseModel, Field

from fantasma.motor.esquemas import LineaPedido, Mundo, Pedido, ResultadoTool
from fantasma.motor.herramientas.base import Funcion, Herramienta
from fantasma.motor.herramientas.catalogo import buscar_item
from fantasma.motor.herramientas.crm import Nombre, Telefono, siguiente_id


class Linea(BaseModel):
    item_id: str
    cantidad: int = Field(1, ge=1, le=1000)


class ArgsCotizar(BaseModel):
    lineas: list[Linea] = Field(min_length=1)


class ArgsCrearPedido(BaseModel):
    lineas: list[Linea] = Field(min_length=1)
    nombre: Nombre
    telefono: Telefono


class Pedidos(Herramienta):
    nombre = "pedidos"

    def funciones(self) -> list[Funcion]:
        return [
            Funcion(nombre="cotizar", descripcion="Calcula el total de una lista de items del catálogo con sus cantidades.", args=ArgsCotizar, ejecutar=self.cotizar),
            Funcion(
                nombre="crear_pedido",
                descripcion="Crea un pedido con los items y los datos del cliente. Devuelve el id para generar el enlace de pago.",
                args=ArgsCrearPedido,
                ejecutar=self.crear_pedido,
            ),
        ]

    def _lineas(self, lineas: list[Linea]) -> list[LineaPedido] | str:
        resultado = []
        for l in lineas:
            item = buscar_item(self.datos, l.item_id)
            if item is None:
                return f"El item {l.item_id} no existe en el catálogo"
            if not item.disponible:
                return f"El item {l.item_id} ({item.nombre}) no está disponible"
            resultado.append(LineaPedido(item_id=item.id, cantidad=l.cantidad, precio_unitario=item.precio))
        monedas = {buscar_item(self.datos, l.item_id).moneda for l in resultado}
        if len(monedas) > 1:
            return "No se pueden mezclar items en monedas distintas en un mismo pedido: " + ", ".join(sorted(monedas))
        return resultado

    def _cotizacion(self, lineas: list[LineaPedido]) -> dict:
        return {
            "lineas": [
                {
                    "item_id": l.item_id,
                    "nombre": buscar_item(self.datos, l.item_id).nombre,
                    "cantidad": l.cantidad,
                    "precio_unitario": str(l.precio_unitario),
                    "subtotal": str(l.precio_unitario * l.cantidad),
                }
                for l in lineas
            ],
            "total": str(sum((l.precio_unitario * l.cantidad for l in lineas))),
            "moneda": buscar_item(self.datos, lineas[0].item_id).moneda,
        }

    def cotizar(self, a: ArgsCotizar, mundo: Mundo) -> ResultadoTool:
        lineas = self._lineas(a.lineas)
        if isinstance(lineas, str):
            return ResultadoTool(ok=False, error=lineas)
        return ResultadoTool(ok=True, data=self._cotizacion(lineas))

    def crear_pedido(self, a: ArgsCrearPedido, mundo: Mundo) -> ResultadoTool:
        lineas = self._lineas(a.lineas)
        if isinstance(lineas, str):
            return ResultadoTool(ok=False, error=lineas)
        pedido = Pedido(
            id=siguiente_id("PED", [p.id for p in mundo.pedidos]),
            lineas=lineas,
            total=sum(l.precio_unitario * l.cantidad for l in lineas),
            moneda=buscar_item(self.datos, lineas[0].item_id).moneda,
            nombre=a.nombre,
            telefono=a.telefono,
        )
        mundo.pedidos.append(pedido)
        return ResultadoTool(ok=True, data=pedido.model_dump(mode="json"))
