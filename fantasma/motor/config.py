"""config.yaml: a qué servidor va cada rol, presupuesto y metas."""

import os
from pathlib import Path
from typing import Literal

import yaml
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

RolNombre = Literal["agente", "simulador", "juez", "optimizador", "generador", "produccion"]


class Proveedor(BaseModel):
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    # llama-server acepta chat_template_kwargs; OpenAI y otros lo rechazan.
    local: bool = True

    def clave(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ[self.api_key_env]
        return "sin-clave"


class Rol(BaseModel):
    proveedor: str = "local"
    modelo: str
    temperatura: float = 0.7
    razonamiento: bool = False
    max_tokens: int = 1200


class Presupuesto(BaseModel):
    max_iterations: int = 30
    max_hours: float = 8.0
    plateau_patience: int = 6


class Metas(BaseModel):
    target_train_score: float = 0.90
    target_holdout_score: float = 0.85
    critical_pass_rate: float = 1.0
    umbral_paso_caso: float = 0.75


class Optimizacion(BaseModel):
    min_improvement: float = 0.02
    holdout_cada: int = 3
    sobreajuste: float = 0.05
    max_prompt_tokens: int = 2500
    banco_corto: int = 20
    muestra_fallos: int = 4
    medir_ruido: bool = True
    # Con una sola ronda, rechazar por un crítico roto deja la boleta sin "después" aunque la mejora neta sea clara.
    criticos_rotos_tolerados: int = 0


class Casos(BaseModel):
    cantidad: int = Field(default=80, ge=10, le=200)
    holdout: float = Field(default=0.3, gt=0, lt=1)


class Remoto(BaseModel):
    """El bot del usuario detrás de un webhook ("trae tu bot"). La validación anti-SSRF con DNS vive en fantasma/remoto.py."""

    url: str
    timeout_s: float = Field(default=20, gt=0, le=60)
    encabezados: dict[str, str] = {}


class Config(BaseModel):
    proveedores: dict[str, Proveedor]
    roles: dict[RolNombre, Rol]
    concurrency: int = 4
    repetitions_per_case: int = 2
    max_turns: int = 8
    timeout_s: float = 180
    reintentos: int = 4
    casos: Casos = Casos()
    presupuesto: Presupuesto = Presupuesto()
    metas: Metas = Metas()
    optimizacion: Optimizacion = Optimizacion()
    webhook_url: str | None = None
    remoto: Remoto | None = None
    # Solo desarrollo: permite http y hosts privados (un n8n en la LAN). Nunca lo controla el usuario de la app pública.
    remoto_inseguro: bool = False

    @model_validator(mode="after")
    def _url_remota(self) -> "Config":
        if self.remoto:
            u = urlsplit(self.remoto.url)
            esquemas = ("https", "http") if self.remoto_inseguro else ("https",)
            if u.scheme not in esquemas or not u.hostname:
                raise ValueError(f"remoto.url debe ser {' o '.join(esquemas)} con host: {self.remoto.url!r}")
            if u.username or u.password:
                raise ValueError("remoto.url no puede llevar usuario ni clave; usa remoto.encabezados")
        return self

    def proveedor_de(self, rol: RolNombre) -> Proveedor:
        return self.proveedores[self.roles[rol].proveedor]


def cargar(ruta: Path) -> Config:
    return Config.model_validate(yaml.safe_load(ruta.read_text()))
