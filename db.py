"""Base de datos SQLite del Sistema de Gestión de Mantenimiento."""
import json
import sqlite3
from pathlib import Path

from flask import g

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "datos" / "sgm_liftrental.db"
SEMILLA = BASE / "datos" / "semilla.json"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS config (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

CREATE TABLE IF NOT EXISTS equipos (
    id INTEGER PRIMARY KEY,
    codigo TEXT UNIQUE NOT NULL,
    categoria TEXT NOT NULL,
    marca TEXT, modelo TEXT, serie TEXT, anio INTEGER,
    altura_plataforma REAL, altura_trabajo REAL, capacidad_kg REAL, peso_kg REAL,
    valor_usd REAL,
    frecuencia_h REAL NOT NULL DEFAULT 100,
    tasa_uso REAL NOT NULL DEFAULT 0.13,
    situacion TEXT NOT NULL DEFAULT 'Disponible',
    ubicacion_detalle TEXT,
    con_expediente INTEGER NOT NULL DEFAULT 1,
    notas TEXT,
    activo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lecturas (
    id INTEGER PRIMARY KEY,
    equipo_id INTEGER NOT NULL REFERENCES equipos(id),
    fecha TEXT NOT NULL,
    horometro REAL NOT NULL,
    fuente TEXT
);

CREATE TABLE IF NOT EXISTS tecnicos (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    cargo TEXT DEFAULT 'Técnico de mantenimiento',
    especialidad TEXT,
    dni TEXT, telefono TEXT, correo TEXT,
    fecha_ingreso TEXT,
    activo INTEGER NOT NULL DEFAULT 1,
    ejecuta INTEGER NOT NULL DEFAULT 1,   -- 1 = ejecuta servicios (cuenta en la capacidad del taller)
    notas TEXT
);

CREATE TABLE IF NOT EXISTS ordenes (
    id INTEGER PRIMARY KEY,
    equipo_id INTEGER NOT NULL REFERENCES equipos(id),
    tipo TEXT NOT NULL DEFAULT 'Preventivo',
    nivel TEXT,
    estado TEXT NOT NULL DEFAULT 'Pendiente',
    prioridad TEXT DEFAULT 'Normal',
    fecha_programada TEXT,
    duracion_dias INTEGER DEFAULT 1,
    tecnico_dias REAL DEFAULT 1,
    fecha_inicio TEXT,
    fecha_fin TEXT,
    horometro REAL,
    costo_estimado REAL,
    costo_real REAL,
    descripcion TEXT,
    observaciones TEXT,
    origen TEXT,
    supervisor TEXT,
    tareas TEXT,              -- JSON: códigos de la lista del plan marcados al cerrar la orden
    creado TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS orden_tecnicos (
    orden_id INTEGER NOT NULL REFERENCES ordenes(id) ON DELETE CASCADE,
    tecnico_id INTEGER NOT NULL REFERENCES tecnicos(id),
    PRIMARY KEY (orden_id, tecnico_id)
);

CREATE TABLE IF NOT EXISTS alquileres (
    id INTEGER PRIMARY KEY,
    equipo_id INTEGER NOT NULL REFERENCES equipos(id),
    cliente TEXT, obra TEXT, zona TEXT,
    fecha_salida TEXT NOT NULL,
    fecha_vencimiento TEXT,
    fecha_retorno TEXT,
    horometro_salida REAL,
    horometro_retorno REAL,
    tarifa_dia REAL,
    estado TEXT NOT NULL DEFAULT 'En obra',
    mant_estado_salida TEXT,
    excepcion_motivo TEXT,
    excepcion_autoriza TEXT,
    notas TEXT,
    creado TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS incidentes (
    id INTEGER PRIMARY KEY,
    equipo_id INTEGER NOT NULL REFERENCES equipos(id),
    alquiler_id INTEGER REFERENCES alquileres(id),
    fecha TEXT NOT NULL,
    horometro REAL,
    mant_estado TEXT,
    descripcion TEXT,
    tecnico_id INTEGER REFERENCES tecnicos(id),
    costo REAL,
    dias_parado REAL,
    orden_id INTEGER REFERENCES ordenes(id),
    creado TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS parametros_nivel (
    categoria TEXT NOT NULL,
    nivel TEXT NOT NULL,
    duracion_dias INTEGER NOT NULL,
    costo REAL NOT NULL,
    tecnico_dias REAL NOT NULL,
    PRIMARY KEY (categoria, nivel)
);

-- Cartera de pedidos (conjunto J del modelo de optimización)
CREATE TABLE IF NOT EXISTS pedidos (
    id INTEGER PRIMARY KEY,
    cliente TEXT, obra TEXT, zona TEXT,
    categoria TEXT NOT NULL,
    altura_req REAL, capacidad_req REAL,
    fecha_inicio TEXT NOT NULL,
    fecha_fin TEXT NOT NULL,
    tarifa_dia REAL,
    costo_traslado REAL,                   -- ida y vuelta (S/)
    estado TEXT NOT NULL DEFAULT 'Pendiente',   -- Pendiente, Asignado, Despachado, No atendido, Cancelado
    equipo_id INTEGER REFERENCES equipos(id),
    corrida_id INTEGER,
    alquiler_id INTEGER REFERENCES alquileres(id),
    origen TEXT,
    notas TEXT,
    creado TEXT DEFAULT (datetime('now','localtime'))
);

-- Corridas del modelo PLEM (escenarios E0-E3, indicadores, plan y datos de la instancia)
CREATE TABLE IF NOT EXISTS corridas (
    id INTEGER PRIMARY KEY,
    creado TEXT DEFAULT (datetime('now','localtime')),
    fecha_inicio TEXT NOT NULL,
    horizonte INTEGER NOT NULL DEFAULT 60,
    parametros TEXT,
    estado TEXT NOT NULL DEFAULT 'En curso',
    mensaje TEXT,
    resultados TEXT,
    duracion_seg REAL,
    aplicada TEXT,
    aplicada_esc TEXT
);

-- Toda OT nueva queda bajo la supervisión del jefe de mantenimiento configurado (SUPRV./JEFE del formato PM)
CREATE TRIGGER IF NOT EXISTS tr_ot_supervisor AFTER INSERT ON ordenes WHEN NEW.supervisor IS NULL
BEGIN
    UPDATE ordenes SET supervisor = (SELECT NULLIF(valor, '') FROM config WHERE clave = 'jefe_mantenimiento')
    WHERE id = NEW.id;
END;

CREATE INDEX IF NOT EXISTS ix_lect_eq ON lecturas(equipo_id, fecha);
CREATE INDEX IF NOT EXISTS ix_ord_eq ON ordenes(equipo_id, estado);
CREATE INDEX IF NOT EXISTS ix_alq_eq ON alquileres(equipo_id, estado);
CREATE INDEX IF NOT EXISTS ix_ped_est ON pedidos(estado, fecha_inicio);
"""

CONFIG_DEFECTO = {
    "intervalo_brazos": "90",
    "intervalo_tijeras": "90",
    "umbral_dias": "15",
    "umbral_fraccion": "0.2",
    "capacidad_tecnicos": "3",
    "fecha_referencia": "",
    "bloquear_vencidos": "1",
    "inspeccion_reinicia": "1",
    "empresa": "Corporación Lift Rental Solutions Perú S.A.C.",
    "taller": "Av. Universitaria 9695, Urb. La Alborada, Comas — Lima",
    "jefe_mantenimiento": "Supervisor S.",
}

EXPEDIENTES = BASE / "datos" / "expedientes.json"
# Columnas agregadas después de la primera versión: se añaden a bases existentes sin perder datos.
MIGRACIONES = [("tecnicos", "ejecuta", "INTEGER NOT NULL DEFAULT 1"),
               ("ordenes", "supervisor", "TEXT"),
               ("ordenes", "tareas", "TEXT")]


def migrar(con):
    for tabla, col, ddl in MIGRACIONES:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}
        if cols and col not in cols:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {ddl}")


def conectar():
    con = sqlite3.connect(DB_PATH, detect_types=0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def get_db():
    if "db" not in g:
        g.db = conectar()
    return g.db


def cerrar_db(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def inicializar(forzar=False):
    """Crea la base y la carga desde semilla.json si no existe (o si forzar=True)."""
    DB_PATH.parent.mkdir(exist_ok=True)
    nueva = forzar or not DB_PATH.exists()
    con = conectar()
    if forzar:   # se vacían las tablas en lugar de borrar el archivo (puede estar abierto)
        con.execute("PRAGMA foreign_keys = OFF")
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute("PRAGMA foreign_keys = ON")
    migrar(con)
    con.executescript(ESQUEMA)
    for k, v in CONFIG_DEFECTO.items():
        con.execute("INSERT OR IGNORE INTO config(clave, valor) VALUES (?, ?)", (k, v))
    if nueva and SEMILLA.exists():
        cargar_semilla(con)
    con.commit()
    con.close()


def cargar_semilla(con):
    s = json.loads(SEMILLA.read_text(encoding="utf-8"))
    campos = ["codigo", "categoria", "marca", "modelo", "serie", "anio", "altura_plataforma",
              "altura_trabajo", "capacidad_kg", "peso_kg", "valor_usd", "frecuencia_h", "tasa_uso",
              "situacion", "ubicacion_detalle", "con_expediente", "notas"]
    ids = {}
    for e in s["equipos"]:
        cur = con.execute(
            f"INSERT INTO equipos({','.join(campos)}) VALUES ({','.join('?' * len(campos))})",
            [e.get(c) for c in campos])
        ids[e["codigo"]] = cur.lastrowid

    # Personal: ejecutores y supervisor tal como figuran en los formatos PM del expediente
    exp = json.loads(EXPEDIENTES.read_text(encoding="utf-8")) if EXPEDIENTES.exists() else {}
    ejecutados, supervisados = {}, {}
    for x in exp.values():
        pm = x.get("pm") or {}
        if pm.get("ejecutor"):
            ejecutados.setdefault(pm["ejecutor"], []).append(x["codigo"])
        if pm.get("supervisor"):
            supervisados.setdefault(pm["supervisor"], []).append(x["codigo"])
    n_pm = len([x for x in exp.values() if x.get("pm")]) or 1

    tec_ids = {}
    especialidad = {"Técnico A": "Tijeras, brazos articulados y mástil"}   # el resto: según su expediente
    for nombre in s["tecnicos"]:
        eqs = sorted(ejecutados.get(nombre, []))
        nota = (f"Ejecutor del último PM documentado de {len(eqs)} de {n_pm} equipos "
                f"({round(len(eqs) / n_pm * 100)} %)" + (f": {', '.join(eqs[:12])}{'…' if len(eqs) > 12 else ''}." if eqs else "."))
        if nombre == "Técnico A":
            nota += " En los formatos 2025 firma como «Técnico A»."
        cur = con.execute("INSERT INTO tecnicos(nombre, cargo, especialidad, notas) VALUES (?,?,?,?)",
                          (nombre, "Técnico de mantenimiento", especialidad.get(nombre, "Plataformas elevadoras"), nota))
        tec_ids[nombre] = cur.lastrowid
    for nombre, eqs in supervisados.items():
        if nombre in tec_ids:
            continue
        con.execute(
            """INSERT INTO tecnicos(nombre, cargo, especialidad, correo, ejecuta, notas) VALUES (?,?,?,?,0,?)""",
            (nombre, "Supervisor / Jefe de mantenimiento", "Supervisión y conformidad de servicios",
             "supervisor@ejemplo.com" if nombre == "Supervisor S." else None,
             f"Firma como SUPRV./JEFE en el último PM de {len(eqs)} de {n_pm} equipos. "
             "No ejecuta servicios: no cuenta en la capacidad del taller."))

    for l in s["lecturas"]:
        con.execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                    (ids[l["equipo"]], l["fecha"], l["horometro"], l["fuente"]))

    for p in s["parametros"]:
        con.execute("INSERT OR REPLACE INTO parametros_nivel VALUES (?,?,?,?,?)",
                    (p["categoria"], p["nivel"], p["duracion_dias"], p["costo"], p["tecnico_dias"]))

    for o in s["ordenes"]:
        pm = (exp.get(o["equipo"]) or {}).get("pm") or {}
        obs = o["observaciones"]
        if pm.get("formato"):
            obs = f"{obs} · Formato «{pm['formato']}»" if obs else f"Formato «{pm['formato']}»"
        if pm.get("observaciones"):
            obs += f" · Nota de la lectura: {pm['observaciones']}"
        cur = con.execute(
            """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, fecha_programada, fecha_inicio,
               fecha_fin, horometro, origen, descripcion, observaciones, supervisor, duracion_dias, tecnico_dias)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1)""",
            (ids[o["equipo"]], o["tipo"], o["nivel"], o["estado"], o["fecha_programada"],
             o["fecha_inicio"], o["fecha_fin"], o["horometro"], o["origen"], o["descripcion"],
             obs, pm.get("supervisor")))
        if o.get("tecnico") in tec_ids:
            con.execute("INSERT INTO orden_tecnicos VALUES (?,?)", (cur.lastrowid, tec_ids[o["tecnico"]]))

    for a in s["alquileres"]:
        con.execute(
            """INSERT INTO alquileres(equipo_id, cliente, obra, zona, fecha_salida, fecha_vencimiento,
               estado, notas) VALUES (?,?,?,?,?,?,'En obra',?)""",
            (ids[a["equipo"]], a["cliente"], a["obra"], a["zona"], a["fecha_salida"],
             a["fecha_vencimiento"], "Importado del registro de estatus al 22/09/2026"))
