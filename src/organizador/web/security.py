"""Medidas de seguridad del servidor local.

* Solo 127.0.0.1.
* Testigo aleatorio por arranque, incrustado en la página inicial y enviado por
  el navegador en la cabecera ``X-Testigo`` (nunca en cookie: las cookies se
  comparten entre puertos del mismo host y cualquier otro servicio local las
  recibiría). Una cabecera personalizada además obliga a cualquier origen ajeno
  a pasar por un preflight CORS, que aquí se rechaza.
* ``Host`` debe ser 127.0.0.1/localhost con nuestro puerto (anti DNS rebinding).
* ``Origin``, si llega, debe ser exactamente el nuestro.
"""

from __future__ import annotations

import secrets

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

CABECERA_TESTIGO = "X-Testigo"
HOST = "127.0.0.1"

CABECERAS_SEGURIDAD = [
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


def generar_testigo() -> str:
    return secrets.token_urlsafe(32)


class Seguridad:
    def __init__(self, puerto: int, testigo: str | None = None) -> None:
        self.puerto = puerto
        self.testigo = testigo or generar_testigo()
        self.hosts = {f"127.0.0.1:{puerto}", f"localhost:{puerto}"}
        self.origenes = {f"http://127.0.0.1:{puerto}", f"http://localhost:{puerto}"}

    def host_valido(self, host: str | None) -> bool:
        return host is not None and host.lower() in self.hosts

    def origen_valido(self, origen: str | None) -> bool:
        return origen is None or origen.lower() in self.origenes

    def testigo_valido(self, valor: str | None) -> bool:
        return valor is not None and secrets.compare_digest(valor.encode(), self.testigo.encode())


class MiddlewareSeguridad:
    """Aplica Host/Origin a todo y el testigo a todo lo que cuelga de /api/."""

    def __init__(self, app: ASGIApp, seguridad: Seguridad) -> None:
        self.app = app
        self.seguridad = seguridad

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cabeceras = Headers(scope=scope)
        seg = self.seguridad
        motivo = None
        if not seg.host_valido(cabeceras.get("host")):
            motivo = "Host no permitido"
        elif not seg.origen_valido(cabeceras.get("origin")):
            motivo = "Origen no permitido"
        elif scope["path"].startswith("/api/") and not seg.testigo_valido(cabeceras.get(CABECERA_TESTIGO)):
            motivo = "Falta el testigo o no es válido"
        if motivo:
            respuesta = PlainTextResponse(motivo, status_code=403)
            await respuesta(scope, receive, send)
            return

        async def enviar(mensaje: dict) -> None:
            if mensaje["type"] == "http.response.start":
                mensaje.setdefault("headers", [])
                mensaje["headers"] = list(mensaje["headers"]) + CABECERAS_SEGURIDAD
            await send(mensaje)

        await self.app(scope, receive, enviar)
