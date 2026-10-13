"""Persistencia de una corrida: SQLite para lo consultable, archivos para lo legible (prompts y transcripciones).

runs/<id>/
  spec/{rubro.yaml, datos.json, rubrica.yaml, casos.yaml, prompt_v000.md}
  prompts/vNNN.md + vNNN.json
  transcripts/vNNN/<caso>__r<n>.json
  results.sqlite
Una evaluación se identifica por (versión, caso, repetición): la del banco corto se reusa en el train completo.
"""

import json
import sqlite3
from pathlib import Path

import yaml

from fantasma.motor.esquemas import Agregado, Caso, Corrida, DatosRubro, EvaluacionRep, ResultadoCaso, Rubrica, RubroSpec, Spec, Transcripcion, VersionPrompt

ESQUEMA = """
CREATE TABLE IF NOT EXISTS corrida (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS version (n INTEGER PRIMARY KEY, estado TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluacion (
  version INTEGER, caso_id TEXT, repeticion INTEGER, puntaje REAL, paso INTEGER, critica INTEGER,
  transcripcion TEXT, data TEXT NOT NULL, PRIMARY KEY (version, caso_id, repeticion));
CREATE TABLE IF NOT EXISTS resultado (
  version INTEGER, conjunto TEXT, caso_id TEXT, puntaje REAL, paso INTEGER, inestable INTEGER, data TEXT NOT NULL,
  PRIMARY KEY (version, conjunto, caso_id));
CREATE TABLE IF NOT EXISTS agregado (version INTEGER, conjunto TEXT, puntaje REAL, data TEXT NOT NULL, PRIMARY KEY (version, conjunto));
CREATE TABLE IF NOT EXISTS historial (id INTEGER PRIMARY KEY AUTOINCREMENT, cuando TEXT DEFAULT CURRENT_TIMESTAMP, texto TEXT NOT NULL);
"""


class Store:
    def __init__(self, dir: Path) -> None:
        self.dir = dir
        for sub in ("spec", "prompts", "transcripts"):
            (dir / sub).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(dir / "results.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(ESQUEMA)

    # ------------------------------------------------------------ spec

    def guardar_spec(self, spec: Spec) -> None:
        d = self.dir / "spec"
        (d / "rubro.yaml").write_text(_yaml(spec.rubro.model_dump(mode="json")))
        (d / "datos.json").write_text(spec.datos.model_dump_json(indent=2))
        (d / "rubrica.yaml").write_text(_yaml(spec.rubrica.model_dump(mode="json")))
        (d / "casos.yaml").write_text(_yaml([c.model_dump(mode="json") for c in spec.casos]))
        (d / "prompt_v000.md").write_text(spec.prompt_base)

    def cargar_spec(self) -> Spec:
        """Lee la spec desde disco: si Erik la editó a mano, manda lo editado."""
        d = self.dir / "spec"
        return Spec(
            rubro=RubroSpec.model_validate(yaml.safe_load((d / "rubro.yaml").read_text())),
            datos=DatosRubro.model_validate_json((d / "datos.json").read_text()),
            rubrica=Rubrica.model_validate(yaml.safe_load((d / "rubrica.yaml").read_text())),
            casos=[Caso.model_validate(c) for c in yaml.safe_load((d / "casos.yaml").read_text())],
            prompt_base=(d / "prompt_v000.md").read_text(),
        )

    # ------------------------------------------------------------ corrida

    def guardar_corrida(self, c: Corrida) -> None:
        self.db.execute("INSERT OR REPLACE INTO corrida VALUES (?, ?)", (c.id, c.model_dump_json()))

    def corrida(self) -> Corrida | None:
        fila = self.db.execute("SELECT data FROM corrida LIMIT 1").fetchone()
        return Corrida.model_validate_json(fila[0]) if fila else None

    # ------------------------------------------------------------ versiones

    def guardar_version(self, v: VersionPrompt, texto: str | None = None) -> None:
        if texto is not None:
            (self.dir / "prompts" / f"v{v.n:03d}.md").write_text(texto)
        (self.dir / "prompts" / f"v{v.n:03d}.json").write_text(v.model_dump_json(indent=2))
        self.db.execute("INSERT OR REPLACE INTO version VALUES (?, ?, ?)", (v.n, v.estado, v.model_dump_json()))

    def versiones(self) -> list[VersionPrompt]:
        return [VersionPrompt.model_validate_json(r[0]) for r in self.db.execute("SELECT data FROM version ORDER BY n")]

    def version(self, n: int) -> VersionPrompt | None:
        fila = self.db.execute("SELECT data FROM version WHERE n = ?", (n,)).fetchone()
        return VersionPrompt.model_validate_json(fila[0]) if fila else None

    def texto(self, n: int) -> str:
        return (self.dir / "prompts" / f"v{n:03d}.md").read_text()

    # ------------------------------------------------------------ transcripciones y evaluaciones

    def ruta_transcripcion(self, version: int, caso_id: str, rep: int) -> Path:
        return self.dir / "transcripts" / f"v{version:03d}" / f"{caso_id}__r{rep}.json"

    def guardar_transcripcion(self, t: Transcripcion) -> Path:
        ruta = self.ruta_transcripcion(t.version, t.caso_id, t.repeticion)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(t.model_dump_json(indent=2))
        return ruta

    def transcripcion(self, version: int, caso_id: str, rep: int) -> Transcripcion | None:
        ruta = self.ruta_transcripcion(version, caso_id, rep)
        return Transcripcion.model_validate_json(ruta.read_text()) if ruta.exists() else None

    def guardar_evaluacion(self, e: EvaluacionRep) -> None:
        ruta = self.ruta_transcripcion(e.version, e.caso_id, e.repeticion)
        self.db.execute(
            "INSERT OR REPLACE INTO evaluacion VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (e.version, e.caso_id, e.repeticion, e.puntaje, int(e.paso), int(e.critica), str(ruta), e.model_dump_json()),
        )

    def evaluacion(self, version: int, caso_id: str, rep: int) -> EvaluacionRep | None:
        """Checkpoint por caso: si ya existe, no se vuelve a simular."""
        fila = self.db.execute("SELECT data FROM evaluacion WHERE version = ? AND caso_id = ? AND repeticion = ?", (version, caso_id, rep)).fetchone()
        return EvaluacionRep.model_validate_json(fila[0]) if fila else None

    def evaluaciones(self, version: int) -> list[EvaluacionRep]:
        return [EvaluacionRep.model_validate_json(r[0]) for r in self.db.execute("SELECT data FROM evaluacion WHERE version = ?", (version,))]

    # ------------------------------------------------------------ resultados y agregados

    def guardar_resultados(self, conjunto: str, resultados: list[ResultadoCaso]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO resultado VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r.version, conjunto, r.caso_id, r.puntaje, int(r.paso), int(r.inestable), r.model_dump_json()) for r in resultados],
        )

    def resultados(self, version: int, conjunto: str) -> list[ResultadoCaso]:
        q = "SELECT data FROM resultado WHERE version = ? AND conjunto = ?"
        return [ResultadoCaso.model_validate_json(r[0]) for r in self.db.execute(q, (version, conjunto))]

    def guardar_agregado(self, a: Agregado) -> None:
        self.db.execute("INSERT OR REPLACE INTO agregado VALUES (?, ?, ?, ?)", (a.version, a.conjunto, a.puntaje, a.model_dump_json()))

    def agregado(self, version: int, conjunto: str) -> Agregado | None:
        fila = self.db.execute("SELECT data FROM agregado WHERE version = ? AND conjunto = ?", (version, conjunto)).fetchone()
        return Agregado.model_validate_json(fila[0]) if fila else None

    def agregados(self) -> list[Agregado]:
        return [Agregado.model_validate_json(r[0]) for r in self.db.execute("SELECT data FROM agregado ORDER BY version, conjunto")]

    # ------------------------------------------------------------ historial de lecciones (lo lee el optimizador)

    def anotar(self, texto: str) -> None:
        self.db.execute("INSERT INTO historial (texto) VALUES (?)", (texto,))

    def historial(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT texto FROM historial ORDER BY id")]


def _yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def guardar_json(ruta: Path, data) -> None:
    ruta.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
