from pydantic import BaseModel, model_validator

from fantasma.motor.esquemas import Mundo, ResultadoTool
from fantasma.motor.herramientas.base import Funcion, Herramienta
from fantasma.motor.herramientas.catalogo import buscar_item

URL_PAGOS = "https://pagos.ejemplo/"


class ArgsEnlace(BaseModel):
    pedido_id: str | None = None
    item_id: str | None = None

    @model_validator(mode="after")
    def _exactamente_uno(self) -> "ArgsEnlace":
        if (self.pedido_id is None) == (self.item_id is None):
            raise ValueError("indica exactamente uno: pedido_id o item_id")
        return self


class Pagos(Herramienta):
    nombre = "pagos"

    def funciones(self) -> list[Funcion]:
        return [
            Funcion(
                nombre="enlace_pago",
                descripcion="Genera el enlace de pago de un pedido creado (pedido_id) o de un solo item del catálogo (item_id). Solo uno de los dos.",
                args=ArgsEnlace,
                ejecutar=self.enlace_pago,
            )
        ]

    def enlace_pago(self, a: ArgsEnlace, mundo: Mundo) -> ResultadoTool:
        if a.pedido_id is not None:
            pedido = next((p for p in mundo.pedidos if p.id == a.pedido_id), None)
            if pedido is None:
                return ResultadoTool(ok=False, error=f"No existe el pedido {a.pedido_id}; créalo antes de pedir el enlace")
            id_, monto, moneda = pedido.id, pedido.total, pedido.moneda
        else:
            item = buscar_item(self.datos, a.item_id)
            if item is None:
                return ResultadoTool(ok=False, error=f"El item {a.item_id} no existe en el catálogo")
            if not item.disponible:
                return ResultadoTool(ok=False, error=f"El item {a.item_id} ({item.nombre}) no está disponible")
            id_, monto, moneda = item.id, item.precio, item.moneda
        url = URL_PAGOS + id_
        mundo.enlaces_pago.append(url)
        return ResultadoTool(ok=True, data={"url": url, "monto": str(monto), "moneda": moneda})
