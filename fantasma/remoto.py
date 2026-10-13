"""Trae tu bot: el agente es un webhook del usuario y sustituye a turno_agente cuando rol_agente == "remoto".

Contrato: POST {"sesion", "mensaje", "historial": [{"rol": "cliente"|"bot", "texto"}]} con X-Cliente-Fantasma: true;
responde {"respuesta": "..."} o texto plano. La URL la escribe un desconocido en una app pública: de ahí el anti-SSRF.
"""

import asyncio
import ipaddress
import json
import socket
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

from fantasma.motor.config import Remoto
from fantasma.motor.esquemas import Turno
from fantasma.motor.llm import ErrorTecnico

ROL = "remoto"
ENCABEZADO = "X-Cliente-Fantasma"
MAX_BYTES = 16 * 1024
# Lo que entra al historial del simulador y al juez (Ultra cobra la entrada): un bot que contesta 60 KB por turno
# multiplicaría el costo de cada llamada. WhatsApp no da para más que esto.
MAX_TEXTO = 2000
# Lo que el usuario no puede pisar: rompería el anclaje de IP, el cuerpo o la marca de tráfico de prueba.
RESERVADOS = {"host", "content-type", "content-length", "transfer-encoding", "connection", ENCABEZADO.lower()}
REINTENTABLES = {408, 425, 429, 500, 502, 503, 504}

# Las pruebas lo cambian por un httpx.MockTransport.
TRANSPORTE: httpx.AsyncBaseTransport | None = None


class URLNoPermitida(ErrorTecnico):
    """La URL apunta a la red interna o no cumple el esquema. Es ErrorTecnico para que nunca se le cobre al bot."""


def modelo(remoto: Remoto) -> str:
    return f"remoto:{urlsplit(remoto.url).hostname}"


def nueva_sesion(caso_id: str, version: int, repeticion: int) -> str:
    # Única también entre reintentos de la misma conversación: el bot no debe arrastrar memoria de un intento fallido.
    return f"{caso_id}-v{version}-r{repeticion}-{uuid.uuid4().hex[:8]}"


def historial_de(turnos: list[Turno]) -> list[dict]:
    return [{"rol": "cliente" if t.rol == "cliente" else "bot", "texto": t.texto} for t in turnos]


def _permitida(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global ya excluye privadas, loopback, link-local (169.254.169.254, la metadata de las nubes), CGNAT/Tailscale y reservadas.
    return ip.is_global and not ip.is_multicast


async def resolver(host: str, puerto: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, puerto, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0] for i in infos))


async def destino(remoto: Remoto, inseguro: bool = False) -> tuple[str, str, str]:
    """(url con la IP ya validada, encabezado Host, nombre para SNI).

    Se conecta a la IP que se validó y no al nombre: si httpx resolviera otra vez, un DNS con TTL 0 (rebinding) podría
    contestar una IP interna en la segunda consulta.
    """
    u = urlsplit(remoto.url)
    if u.scheme not in (("https", "http") if inseguro else ("https",)) or not u.hostname:
        raise URLNoPermitida(f"remoto: solo se aceptan URLs https con host: {remoto.url!r}")
    try:
        ips = await resolver(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError as exc:
        raise ErrorTecnico(f"remoto: no resuelve {u.hostname}: {exc}") from exc
    if not ips:
        raise ErrorTecnico(f"remoto: {u.hostname} no tiene direcciones")
    if not inseguro:
        internas = [ip for ip in ips if not _permitida(ipaddress.ip_address(ip.split("%")[0]))]
        if internas:
            raise URLNoPermitida(f"remoto: {u.hostname} resuelve a una dirección no pública ({', '.join(internas)})")
    ip = ips[0]
    hostport = (f"[{ip}]" if ":" in ip else ip) + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, hostport, u.path or "/", u.query, "")), u.netloc, u.hostname


async def comprobar(remoto: Remoto, inseguro: bool = False) -> None:
    """Para validar la URL al crear la corrida, antes de gastar en el simulador."""
    await destino(remoto, inseguro)


def _respuesta(crudo: str) -> str:
    try:
        data = json.loads(crudo)
    except json.JSONDecodeError:
        return crudo
    # n8n con "All Incoming Items" devuelve la lista de un elemento.
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if isinstance(data, dict):
        if not isinstance(data.get("respuesta"), str):
            raise ErrorTecnico(f"remoto: el JSON no trae 'respuesta' de texto (claves: {', '.join(list(data)[:10])})")
        return data["respuesta"].strip()
    return data.strip() if isinstance(data, str) else crudo


async def _leer(r: httpx.Response) -> str:
    if int(r.headers.get("content-length") or 0) > MAX_BYTES:
        raise ErrorTecnico(f"remoto: respuesta de más de {MAX_BYTES} bytes")
    cuerpo = bytearray()
    async for trozo in r.aiter_bytes():
        cuerpo += trozo
        if len(cuerpo) > MAX_BYTES:
            raise ErrorTecnico(f"remoto: respuesta de más de {MAX_BYTES} bytes")
    return cuerpo.decode(r.encoding or "utf-8", errors="replace").strip()


class _Reintentable(ErrorTecnico):
    pass


async def _post(remoto: Remoto, cuerpo: dict, inseguro: bool) -> str:
    url, host, sni = await destino(remoto, inseguro)
    encabezados = {k: v for k, v in remoto.encabezados.items() if k.lower() not in RESERVADOS}
    encabezados |= {"Host": host, ENCABEZADO: "true"}
    # trust_env=False: un HTTP(S)_PROXY del entorno resolvería el nombre por su cuenta y se saltaría el anclaje.
    async with httpx.AsyncClient(transport=TRANSPORTE, timeout=remoto.timeout_s, follow_redirects=False, trust_env=False) as c:
        async with c.stream("POST", url, json=cuerpo, headers=encabezados, extensions={"sni_hostname": sni}) as r:
            if r.is_redirect:
                raise ErrorTecnico(f"remoto: HTTP {r.status_code}, no se siguen redirecciones")
            if r.status_code in REINTENTABLES:
                raise _Reintentable(f"remoto: HTTP {r.status_code}")
            if not r.is_success:
                raise ErrorTecnico(f"remoto: HTTP {r.status_code}")
            return _respuesta(await _leer(r))


async def turno_remoto(remoto: Remoto, sesion: str, mensaje: str, historial: list[dict], inseguro: bool = False, reintentos: int = 3) -> tuple[str, int]:
    """(texto del bot, ms). Errores de red, timeouts y 5xx se reintentan; si persisten, ErrorTecnico."""
    cuerpo = {"sesion": sesion, "mensaje": mensaje, "historial": historial}
    ultimo: Exception | None = None
    for intento in range(reintentos):
        if intento:
            await asyncio.sleep(min(10, 2**intento))
        t0 = time.monotonic()
        try:
            # Tope total además del de httpx, que es por operación: un bot que gotea bytes no debe colgar la conversación.
            async with asyncio.timeout(remoto.timeout_s):
                texto = await _post(remoto, cuerpo, inseguro)
        except (_Reintentable, httpx.TransportError, TimeoutError) as exc:
            ultimo = exc
            continue
        if len(texto) > MAX_TEXTO:
            texto = texto[:MAX_TEXTO] + "…"
        return texto, int((time.monotonic() - t0) * 1000)
    raise ErrorTecnico(f"remoto: sin respuesta tras {reintentos} intentos: {ultimo!r}")
