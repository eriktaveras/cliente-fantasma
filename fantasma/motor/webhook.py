"""Aviso de fin de corrida a un webhook de n8n (que lo manda por Telegram o correo)."""

import httpx

from fantasma.motor.esquemas import Corrida
from fantasma.motor.storage import Store


def avisar(url: str, store: Store, corrida: Corrida, reporte: str) -> str:
    final = corrida.mejor_version
    train, holdout = store.agregado(final, "train"), store.agregado(final, "holdout")
    payload = {
        "corrida": corrida.id,
        "rubro": corrida.rubro,
        "estado": corrida.estado,
        "motivo": corrida.motivo_parada,
        "iteraciones": corrida.iteracion,
        "horas": round(corrida.horas, 2),
        "version_final": final,
        "train": round(train.puntaje, 4) if train else None,
        "holdout": round(holdout.puntaje, 4) if holdout else None,
        "criticos": f"{train.criticos_pasan}/{train.criticos_total}" if train else None,
        "reporte": reporte,
    }
    try:
        r = httpx.post(url, json=payload, timeout=20)
        return f"webhook {r.status_code}"
    except httpx.HTTPError as exc:
        return f"webhook falló: {exc!r}"
