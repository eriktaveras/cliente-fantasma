"""Regresiones de la revisión E2E en la app web: remoto_inseguro desde el formulario y SSE de corridas olvidadas."""

from tests.test_app import cliente, crear, lanzar, servicio  # noqa: F401
from tests.test_servicio import COLMADO


def test_remoto_inseguro_acepta_http_local_desde_la_web(cliente, servicio):
    r = cliente.post("/probar", data={"modo": "remoto", "texto": COLMADO, "url_bot": "http://127.0.0.1:8152/bot"}, follow_redirects=False)
    assert r.status_code == 400 and not servicio.indice.listar()

    servicio.config_base = servicio.config_base.model_copy(update={"remoto_inseguro": True})
    run_id = crear(cliente, modo="remoto", url_bot="http://127.0.0.1:8152/bot")
    assert 'value="http://127.0.0.1:8152/bot"' in cliente.get(f"/corrida/{run_id}/ficha").text


def test_sse_de_corrida_olvidada_no_queda_abierto(cliente, servicio):
    run_id = crear(cliente)
    lanzar(cliente, servicio, run_id)
    servicio.eventos.olvidar(run_id)
    assert cliente.get(f"/corrida/{run_id}/eventos").status_code == 204
    assert run_id not in servicio.eventos._recientes
