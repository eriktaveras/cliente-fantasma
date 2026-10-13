from fantasma.motor.esquemas import DatosRubro, RubroSpec
from fantasma.motor.herramientas.base import SEPARADOR, Caja, Herramienta, mundo_inicial
from fantasma.motor.herramientas.calendario import Calendario
from fantasma.motor.herramientas.catalogo import Catalogo
from fantasma.motor.herramientas.crm import Crm
from fantasma.motor.herramientas.escalar import Escalar
from fantasma.motor.herramientas.faq import Faq
from fantasma.motor.herramientas.pagos import Pagos
from fantasma.motor.herramientas.pedidos import Pedidos

REGISTRO: dict[str, type[Herramienta]] = {h.nombre: h for h in (Catalogo, Calendario, Crm, Pedidos, Pagos, Escalar, Faq)}


def caja_para(rubro: RubroSpec, datos: DatosRubro) -> Caja:
    nombres = dict.fromkeys([*rubro.herramientas, "escalar"])
    return Caja([REGISTRO[n](datos, rubro) for n in nombres])


__all__ = ["REGISTRO", "SEPARADOR", "Caja", "Herramienta", "caja_para", "mundo_inicial"]
