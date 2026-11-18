"""SGM Lift Rental — Sistema de Gestión de Mantenimiento y almacén virtual de equipos.

Ejecutar:  python app.py    →  http://127.0.0.1:5000
"""
import csv
from collections import Counter
import io
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

import db as base
import logica as L
import importar_estatus as IE
import importar_documentos as ID
import optimizador as O
import simulacion
from logica import a_fecha, iso

APP_DIR = Path(__file__).resolve().parent
# Carpeta con el expediente de cada equipo (BCCL001, TECL001, …). Por defecto, dos niveles arriba.
DOCS_DIR = Path(os.environ.get("SGM_DOCS_DIR", APP_DIR.parent.parent))
UPLOADS_DIR = APP_DIR / "datos" / "adjuntos"
ESTATUS_DIR = APP_DIR / "datos" / "estatus"     # copias de los libros de estatus cargados

app = Flask(__name__)
app.secret_key = os.environ.get("SGM_SECRET", "sgm-liftrental-local")
app.teardown_appcontext(base.cerrar_db)


# ================================================================ utilidades
def db():
    return base.get_db()


def cfg():
    return L.leer_config(db())


def num(v, defecto=None):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else defecto
    except ValueError:
        return defecto


def texto(v):
    v = (v or "").strip()
    return v or None


def equipo_o_404(codigo):
    eq = db().execute("SELECT * FROM equipos WHERE codigo=?", (codigo,)).fetchone()
    if not eq:
        abort(404)
    return eq


def orden_o_404(oid):
    o = db().execute(
        """SELECT o.*, e.codigo, e.categoria, e.situacion FROM ordenes o
           JOIN equipos e ON e.id=o.equipo_id WHERE o.id=?""", (oid,)).fetchone()
    if not o:
        abort(404)
    return o


def carpeta_expediente(codigo):
    return DOCS_DIR / codigo.replace("-", "")


def listar_documentos(codigo):
    docs = []
    for origen, carpeta in (("Expediente", carpeta_expediente(codigo)), ("Adjunto", UPLOADS_DIR / codigo)):
        if carpeta.is_dir():
            for p in sorted(carpeta.iterdir()):
                if p.is_file() and not p.name.startswith("~$"):
                    docs.append(dict(nombre=p.name, origen=origen, kb=round(p.stat().st_size / 1024),
                                     tipo=clasificar_doc(p.name)))
    return docs


def clasificar_doc(nombre):
    n = nombre.lower()
    if "certificado de mantenimiento" in n:
        return "Certificado de mantenimiento"
    if "operatividad" in n:
        return "Certificado de operatividad"
    if "check" in n:
        return "Check list del operador"
    if "poliza" in n or "póliza" in n:
        return "Póliza"
    if re.match(r"^[a-z]{4}\d{3}[\s_]", n):
        return "Formato de servicio (PM)"
    if "especificaciones" in n or "ficha" in n or re.search(r"\b(gs|ha|sj|z|e)\S*\.pdf$", n):
        return "Ficha técnica"
    return "Documento"


def ordenes_query(where="1=1", args=(), orden="o.id DESC", limite=None):
    q = f"""SELECT o.*, e.codigo, e.categoria, e.situacion,
               (SELECT group_concat(t.nombre, ', ') FROM orden_tecnicos ot
                 JOIN tecnicos t ON t.id=ot.tecnico_id WHERE ot.orden_id=o.id) AS tecnicos
            FROM ordenes o JOIN equipos e ON e.id=o.equipo_id WHERE {where} ORDER BY {orden}"""
    if limite:
        q += f" LIMIT {int(limite)}"
    return db().execute(q, args).fetchall()


@app.context_processor
def contexto():
    c = cfg()
    ref = L.fecha_ref(c)
    pend = db().execute(
        "SELECT COUNT(*) FROM ordenes WHERE estado IN ('Pendiente','Programada','En ejecución')").fetchone()[0]
    sim_hasta = a_fecha(c.get("simulacion_hasta"))
    SIM = dict(desde=a_fecha(c.get("simulacion_desde")), hasta=sim_hasta, semilla=c.get("simulacion_semilla"),
               resumen=c.get("simulacion_resumen"), atrasada=bool(sim_hasta and sim_hasta < date.today()
                                                                     and not c.get("fecha_referencia")))
    est_f = a_fecha(c.get("estatus_fecha"))
    EST = dict(fecha=est_f, archivo=c.get("estatus_archivo"), atrasado=bool(est_f and est_f < date.today()))
    return dict(CFG=c, REF=ref, REF_FIJA=bool(c.get("fecha_referencia")), HOY=date.today(), SIM=SIM, EST=EST,
                CORTO=L.CORTO,
                OT_ABIERTAS=pend, ZONAS=L.ZONAS, CATEGORIAS=L.CATEGORIAS, NIVELES=L.NIVELES,
                SITUACIONES=L.SITUACIONES, INDICADORES=INDICADORES, QUE_MIDE=QUE_MIDE, ruta=request.endpoint or "")


@app.template_filter("fecha")
def f_fecha(v, formato="%d/%m/%Y"):
    d = a_fecha(v) if v else None
    return d.strftime(formato) if d else "—"


@app.template_filter("fecha_archivo")
def f_fecha_archivo(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


@app.template_filter("num")
def f_num(v, dec=0):
    if v is None or v == "":
        return "—"
    s = f"{float(v):,.{dec}f}"
    return s


@app.template_filter("soles")
def f_soles(v):
    return "—" if v in (None, "") else f"S/ {float(v):,.0f}"


@app.template_filter("dias_hasta")
def f_dias_hasta(desde, hasta):
    a, b = a_fecha(desde), a_fecha(hasta)
    return (b - a).days if a and b else "—"


@app.template_filter("dia")
def f_dia(d):
    return ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"][a_fecha(d).weekday()]


@app.template_filter("sumar_dias")
def f_sumar_dias(d, n):
    return iso(a_fecha(d) + timedelta(days=int(n)))


@app.template_filter("fromjson")
def f_fromjson(v):
    try:
        return json.loads(v) if v else {}
    except ValueError:
        return {}


@app.template_filter("ot")
def f_ot(v):
    return f"OT-{int(v):04d}"


@app.template_filter("clase")
def f_clase(estado):
    return {"Vigente": "ok", "Por vencer": "warn", "Vencido": "bad", "Sin datos": "none",
            "Disponible": "ok", "En obra": "info", "En taller": "warn", "Fuera de servicio": "bad",
            "Pendiente": "warn", "Programada": "info", "En ejecución": "accent",
            "Completada": "ok", "Cancelada": "none", "Cerrado": "none",
            "apto": "ok", "advertencia": "warn", "bloqueado": "bad",
            "Alta": "bad", "Normal": "info", "Baja": "none",
            "Asignado": "info", "Despachado": "ok", "No atendido": "bad", "Cancelado": "none",
            "Terminada": "ok", "En curso": "accent", "Error": "bad"}.get(estado, "none")


# ================================================================ almacén en vivo
@app.route("/")
def almacen():
    c = cfg()
    ref = L.fecha_ref(c)
    a = L.almacen_vivo(db(), c, ref)
    supervisor = db().execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=0 ORDER BY id").fetchall()
    inc_mes = db().execute("""SELECT COUNT(*) n, SUM(mant_estado IN ('Vencido','Sin datos')) v FROM incidentes
                              WHERE fecha >= ?""", (iso(ref - timedelta(days=30)),)).fetchone()
    return render_template("almacen.html", a=a, supervisor=supervisor, inc_mes=inc_mes,
                           horizonte=14, limite=ref + timedelta(days=14), escena=escena_almacen(a, ref))


TIPO_SPRITE = {"Tijera eléctrica": "tij", "Brazo articulado eléctrico": "bel",
               "Brazo articulado combustión": "bdi", "Mástil vertical": "mas"}


def escena_almacen(a, ref):
    """Datos de la vista isométrica del almacén (se dibuja en static/almacen3d.js)."""
    corto = lambda n: (n.split()[0][:1] + ". " + " ".join(n.split()[1:])) if n and " " in n else (n or "")
    ing = {}
    for p in a["proximos"]:
        if p["codigo"] not in ing and p["fecha"]:
            ing[p["codigo"]] = dict(f=p["fecha"].strftime("%d/%m"), dia=f_dia(p["fecha"]),
                                    d=(p["fecha"] - ref).days, tec=p["tecnicos"], niv=p["nivel"],
                                    tipo=p["tipo"], fuente=p["fuente"])
    maqs = []
    for e in a["flota"]:
        al, m = e["alquiler"] or {}, e["mant"]
        ret = e.get("retorno")
        maqs.append(dict(
            c=e["codigo"], k=TIPO_SPRITE.get(e["categoria"], "tij"), cat=e["categoria"],
            mm=f"{e['marca'] or ''} {e['modelo'] or ''}".strip(), alt=e["altura_trabajo"],
            sit=e["situacion"], est=m["estado"], vence=f_fecha(m["fecha_vence"]) if m["fecha_vence"] else None,
            zona=al.get("zona") or "Sin zona", cli=al.get("cliente"), obra=al.get("obra"),
            ret=ret.strftime("%d/%m/%Y") if ret else None, sup=bool(e.get("retorno_supuesto")),
            ing=ing.get(e["codigo"]), url=url_for("equipo", codigo=e["codigo"])))
    tecs = []
    for p in a["personal"]:
        ots = [dict(c=o["codigo"], obra=bool(o.get("en_obra")), ot=f_ot(o["id"]), tipo=o["tipo"], niv=o["nivel"],
                    ejec=o["estado"] == "En ejecución",
                    hasta=f_fecha(o["fin_previsto"], "%d/%m") if o.get("fin_previsto") else None) for o in p["hoy"]]
        px = p["proxima"]
        tecs.append(dict(n=p["t"]["nombre"], nc=corto(p["t"]["nombre"]), estado=p["estado"], ots=ots,
                         prox=f"{f_dia(px['d'])} {px['d']:%d/%m} — {px['t']}" if px else None,
                         url=url_for("tecnico", tid=p["t"]["id"])))
    prox = [dict(f=f"{f_dia(p['fecha'])} {p['fecha']:%d/%m}", c=p["codigo"], niv=p["nivel"] or "",
                 tec=corto((p["tecnicos"] or "").split(", ")[0]) if p["tecnicos"] else "—")
            for p in a["proximos"][:6] if p["fecha"]]
    return dict(maqs=maqs, tecs=tecs, prox=prox, zonas=list(a["zonas"].keys()), ref=f"{ref:%d/%m/%Y}")


@app.route("/simulacion", methods=["POST"])
def resimular():
    """Reconstruye el estado desde el registro del 22/09/2026 y simula la operación hasta la fecha."""
    hasta = a_fecha(request.form.get("hasta")) or date.today()
    if hasta <= simulacion.CORTE:
        flash("La fecha debe ser posterior al registro de estatus del 22/09/2026.", "error")
        return redirect(url_for("configuracion"))
    semilla = int(num(request.form.get("semilla")) or simulacion.SEMILLA)
    base.cerrar_db()
    base.inicializar(forzar=True)
    con = base.conectar()
    try:
        st = simulacion.simular(con, hasta, semilla=semilla, log=lambda *_: None)
        con.commit()
    finally:
        con.close()
    flash(f"Operación simulada del 22/09/2026 al {hasta:%d/%m/%Y}: {st['despachos']} despachos, "
          f"{st['servicios']} servicios preventivos, {st['fallas']} fallas en obra atendidas.", "ok")
    return redirect(url_for("almacen"))


# ================================================================ panel
@app.route("/panel")
def panel():
    c = cfg()
    ref = L.fecha_ref(c)
    flota = L.flota_con_estado(db(), c, ref)
    cuenta = lambda f: sum(1 for e in flota if f(e))
    sit = {s: cuenta(lambda e, s=s: e["situacion"] == s) for s in L.SITUACIONES}
    mant = {s: cuenta(lambda e, s=s: e["mant"]["estado"] == s)
            for s in ("Vigente", "Por vencer", "Vencido", "Sin datos")}
    en_obra_venc = sorted([e for e in flota if e["situacion"] == "En obra" and e["mant"]["estado"] in ("Vencido", "Sin datos")],
                          key=lambda e: e["mant"]["dias_rest"] if e["mant"]["dias_rest"] is not None else 99999)
    disp_venc = [e for e in flota if e["situacion"] == "Disponible" and e["mant"]["estado"] in ("Vencido", "Sin datos")]
    contratos_venc = sorted([e for e in flota if e["alquiler"] and e["alquiler"].get("contrato_vencido")],
                            key=lambda e: -e["alquiler"]["dias_atraso"])
    proximos = sorted([e for e in flota if e["mant"]["estado"] in ("Vigente", "Por vencer")
                       and e["mant"]["fecha_vence"]
                       and a_fecha(e["mant"]["fecha_vence"]) <= ref + timedelta(days=30)],
                      key=lambda e: e["mant"]["fecha_vence"])
    disponibles_aptos = [e for e in flota if e["situacion"] == "Disponible" and e["mant"]["estado"] in ("Vigente", "Por vencer")]
    atrasadas = ordenes_query("o.estado='Programada' AND o.fecha_programada < ?", (iso(ref),),
                              "o.fecha_programada")
    en_ejec = ordenes_query("o.estado='En ejecución'", (), "o.fecha_inicio")
    pendientes = ordenes_query("o.estado='Pendiente'", (), "o.id")
    carga = L.carga_taller(db(), ref, 14)
    cap = float(c.get("capacidad_tecnicos") or 3)
    inc = db().execute(
        """SELECT COUNT(*) n, SUM(CASE WHEN mant_estado IN ('Vencido','Sin datos') THEN 1 ELSE 0 END) v,
                  SUM(costo) c FROM incidentes WHERE fecha >= ?""", (iso(ref - timedelta(days=90)),)).fetchone()
    excepciones = db().execute(
        """SELECT COUNT(*) FROM alquileres WHERE excepcion_motivo IS NOT NULL AND fecha_salida >= ?""",
        (iso(ref - timedelta(days=60)),)).fetchone()[0]
    por_categoria = {}
    for e in flota:
        d = por_categoria.setdefault(e["categoria"], dict(total=0, obra=0, venc=0))
        d["total"] += 1
        d["obra"] += e["situacion"] == "En obra"
        d["venc"] += e["mant"]["estado"] in ("Vencido", "Sin datos")
    return render_template("panel.html", flota=flota, sit=sit, mant=mant, total=len(flota),
                           en_obra_venc=en_obra_venc, disp_venc=disp_venc,
                           contratos_venc=contratos_venc, proximos=proximos,
                           disponibles_aptos=disponibles_aptos, atrasadas=atrasadas,
                           en_ejec=en_ejec, pendientes=pendientes, carga=carga, cap=cap, inc=inc,
                           excepciones=excepciones, por_categoria=por_categoria)


# ================================================================ almacén virtual: equipos
@app.route("/equipos")
def equipos():
    c = cfg()
    flota = L.flota_con_estado(db(), c, solo_activos=request.args.get("bajas") != "1")
    marcas = sorted({e["marca"] for e in flota if e["marca"]})
    return render_template("equipos.html", flota=flota, marcas=marcas)


@app.route("/equipos/<codigo>")
def equipo(codigo):
    eq = equipo_o_404(codigo)
    c = cfg()
    ref = L.fecha_ref(c)
    m = L.estado_equipo(db(), eq, c, ref)
    alqs = db().execute("SELECT * FROM alquileres WHERE equipo_id=? ORDER BY fecha_salida DESC",
                        (eq["id"],)).fetchall()
    ords = ordenes_query("o.equipo_id=?", (eq["id"],),
                         "COALESCE(o.fecha_fin, o.fecha_programada, o.creado) DESC")
    lects = db().execute("SELECT * FROM lecturas WHERE equipo_id=? ORDER BY fecha DESC, horometro DESC",
                         (eq["id"],)).fetchall()
    incs = db().execute("SELECT * FROM incidentes WHERE equipo_id=? ORDER BY fecha DESC", (eq["id"],)).fetchall()
    alq_activo = next((a for a in alqs if a["estado"] == "En obra"), None)
    orden_abierta = next((o for o in ords if o["estado"] in L.ESTADOS_ABIERTOS), None)
    dias_alquiler = sum(((a_fecha(a["fecha_retorno"]) or ref) - a_fecha(a["fecha_salida"])).days
                        for a in alqs if a["fecha_salida"])
    costo_mant = sum((o["costo_real"] or 0) for o in ords if o["estado"] == "Completada")
    serie = sorted(((l["fecha"], l["horometro"]) for l in lects), key=lambda x: x[0])
    return render_template("equipo.html", eq=eq, m=m, alqs=alqs, ords=ords, lects=lects, incs=incs,
                           alq_activo=alq_activo, orden_abierta=orden_abierta, docs=listar_documentos(codigo),
                           dias_alquiler=dias_alquiler, costo_mant=costo_mant, serie=serie,
                           tarifa=L.tarifa_sugerida(eq), tab=request.args.get("tab", "resumen"),
                           exp=L.expediente(codigo), formato=L.formato_plan(eq["categoria"]))


CAMPOS_EQ = ["codigo", "categoria", "marca", "modelo", "serie", "anio", "altura_plataforma",
             "altura_trabajo", "capacidad_kg", "peso_kg", "valor_usd", "frecuencia_h", "tasa_uso",
             "situacion", "ubicacion_detalle", "notas"]
NUMERICOS_EQ = {"anio", "altura_plataforma", "altura_trabajo", "capacidad_kg", "peso_kg", "valor_usd",
                "frecuencia_h", "tasa_uso"}


def leer_form_equipo():
    d = {}
    for k in CAMPOS_EQ:
        v = request.form.get(k)
        d[k] = num(v) if k in NUMERICOS_EQ else texto(v)
    d["codigo"] = (d["codigo"] or "").upper()
    if d["anio"]:
        d["anio"] = int(d["anio"])
    return d


@app.route("/equipos/nuevo", methods=["GET", "POST"])
def equipo_nuevo():
    if request.method == "POST":
        d = leer_form_equipo()
        if not re.match(r"^[A-Z]{4}-\d{3}$", d["codigo"] or ""):
            flash("El código debe tener el formato AAAA-000 (por ejemplo, TECL-077).", "error")
            return render_template("equipo_form.html", eq=d, nuevo=True)
        if db().execute("SELECT 1 FROM equipos WHERE codigo=?", (d["codigo"],)).fetchone():
            flash(f"Ya existe un equipo con el código {d['codigo']}.", "error")
            return render_template("equipo_form.html", eq=d, nuevo=True)
        d["frecuencia_h"] = d["frecuencia_h"] or (250 if d["categoria"].startswith("Brazo") else 100)
        d["tasa_uso"] = d["tasa_uso"] if d["tasa_uso"] is not None else 0.13
        d["situacion"] = d["situacion"] or "Disponible"
        cols = list(d.keys())
        cur = db().execute(f"INSERT INTO equipos({','.join(cols)}, con_expediente) VALUES "
                           f"({','.join('?' * len(cols))}, 0)", [d[k] for k in cols])
        f_pm, h_pm = texto(request.form.get("fecha_pm")), num(request.form.get("horometro_pm"))
        if f_pm and h_pm is not None:
            db().execute("""INSERT INTO ordenes(equipo_id, tipo, nivel, estado, fecha_programada, fecha_inicio,
                            fecha_fin, horometro, origen, descripcion) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (cur.lastrowid, "Preventivo", "A", "Completada", f_pm, f_pm, f_pm, h_pm,
                          "Registro inicial", "Último servicio informado al registrar el equipo"))
            db().execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                         (cur.lastrowid, f_pm, h_pm, "Registro inicial"))
        db().commit()
        flash(f"Equipo {d['codigo']} incorporado al almacén.", "ok")
        return redirect(url_for("equipo", codigo=d["codigo"]))
    return render_template("equipo_form.html", eq={"situacion": "Disponible"}, nuevo=True)


@app.route("/equipos/<codigo>/editar", methods=["GET", "POST"])
def equipo_editar(codigo):
    eq = equipo_o_404(codigo)
    if request.method == "POST":
        d = leer_form_equipo()
        d.pop("codigo")
        d["frecuencia_h"] = d["frecuencia_h"] or eq["frecuencia_h"]
        d["tasa_uso"] = d["tasa_uso"] if d["tasa_uso"] is not None else eq["tasa_uso"]
        d["situacion"] = d["situacion"] or eq["situacion"]
        d["activo"] = 0 if request.form.get("baja") else 1
        db().execute(f"UPDATE equipos SET {', '.join(k + '=?' for k in d)} WHERE id=?",
                     [*d.values(), eq["id"]])
        db().commit()
        flash("Datos del equipo actualizados.", "ok")
        return redirect(url_for("equipo", codigo=codigo))
    return render_template("equipo_form.html", eq=eq, nuevo=False)


@app.route("/equipos/<codigo>/lectura", methods=["POST"])
def equipo_lectura(codigo):
    eq = equipo_o_404(codigo)
    f, h = texto(request.form.get("fecha")), num(request.form.get("horometro"))
    if not f or h is None:
        flash("Indique la fecha y la lectura del horómetro.", "error")
    else:
        ult = L.ultima_lectura(db(), eq["id"])
        if ult and a_fecha(f) >= a_fecha(ult["fecha"]) and h < ult["horometro"]:
            flash(f"La lectura ({h:,.1f} h) es menor que la anterior ({ult['horometro']:,.1f} h del "
                  f"{a_fecha(ult['fecha']).strftime('%d/%m/%Y')}). Verifique el dato.", "error")
            return redirect(url_for("equipo", codigo=codigo, tab="horometro"))
        db().execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                     (eq["id"], f, h, texto(request.form.get("fuente")) or "Lectura manual"))
        if request.form.get("recalcular") and ult and a_fecha(f) > a_fecha(ult["fecha"]):
            pm = L.ultimo_pm(db(), eq["id"])
            base_f, base_h = (ult["fecha"], ult["horometro"])
            if pm and a_fecha(pm["fecha"]) > a_fecha(base_f):
                base_f, base_h = pm["fecha"], pm["horometro"]
            dias = (a_fecha(f) - a_fecha(base_f)).days
            if dias > 0 and h >= base_h:
                db().execute("UPDATE equipos SET tasa_uso=? WHERE id=?", (round((h - base_h) / dias, 4), eq["id"]))
        db().commit()
        flash("Lectura de horómetro registrada.", "ok")
    return redirect(url_for("equipo", codigo=codigo, tab="horometro"))


@app.route("/equipos/<codigo>/documentos", methods=["POST"])
def equipo_subir(codigo):
    equipo_o_404(codigo)
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Seleccione un archivo.", "error")
    else:
        destino = UPLOADS_DIR / codigo
        destino.mkdir(parents=True, exist_ok=True)
        nombre = secure_filename(archivo.filename) or "documento"
        archivo.save(destino / nombre)
        flash(f"Documento «{nombre}» agregado al expediente virtual.", "ok")
    return redirect(url_for("equipo", codigo=codigo, tab="documentos"))


@app.route("/equipos/<codigo>/doc/<origen>/<path:nombre>")
def equipo_doc(codigo, origen, nombre):
    equipo_o_404(codigo)
    carpeta = carpeta_expediente(codigo) if origen == "Expediente" else UPLOADS_DIR / codigo
    return send_from_directory(carpeta, nombre)


@app.route("/equipos/<codigo>/certificado")
def equipo_certificado(codigo):
    eq = equipo_o_404(codigo)
    c = cfg()
    m = L.estado_equipo(db(), eq, c)
    alq = db().execute("SELECT * FROM alquileres WHERE equipo_id=? AND estado='En obra'", (eq["id"],)).fetchone()
    return render_template("certificado.html", eq=eq, m=m, alq=alq)


# ================================================================ uso: alquileres y despacho
@app.route("/alquileres")
def alquileres():
    c = cfg()
    ref = L.fecha_ref(c)
    estado = request.args.get("estado", "En obra")
    filas = db().execute(
        """SELECT a.*, e.codigo, e.categoria, e.marca, e.modelo, e.altura_trabajo FROM alquileres a
           JOIN equipos e ON e.id=a.equipo_id WHERE a.estado=? ORDER BY a.fecha_salida DESC""",
        (estado,)).fetchall()
    flota = {e["id"]: e for e in L.flota_con_estado(db(), c, ref)}
    clientes = db().execute(
        """SELECT cliente, COUNT(*) n FROM alquileres WHERE estado='En obra' AND cliente IS NOT NULL
           GROUP BY cliente ORDER BY n DESC LIMIT 8""").fetchall()
    return render_template("alquileres.html", filas=filas, flota=flota, estado=estado, clientes=clientes)


@app.route("/alquileres/nuevo", methods=["GET", "POST"])
def despacho():
    c = cfg()
    ref = L.fecha_ref(c)
    p = request.values
    criterios = dict(categoria=p.get("categoria") or "Tijera eléctrica", altura=num(p.get("altura"), 0),
                     capacidad=num(p.get("capacidad"), 0), fecha_salida=p.get("fecha_salida") or iso(ref),
                     fecha_vencimiento=p.get("fecha_vencimiento") or iso(ref + timedelta(days=30)),
                     cliente=p.get("cliente", ""), obra=p.get("obra", ""), zona=p.get("zona", ""))
    bloquear = c.get("bloquear_vencidos", "1") == "1"

    if request.method == "POST" and p.get("equipo_id"):
        eq = db().execute("SELECT * FROM equipos WHERE id=?", (p.get("equipo_id"),)).fetchone()
        fs, fv = a_fecha(p.get("fecha_salida")), a_fecha(p.get("fecha_vencimiento"))
        errores = []
        if not eq:
            errores.append("Equipo no encontrado.")
        elif eq["situacion"] != "Disponible":
            errores.append(f"El {eq['codigo']} no está disponible (situación: {eq['situacion']}).")
        if not texto(p.get("cliente")) or not texto(p.get("obra")):
            errores.append("Indique el cliente y la obra.")
        if not fs or not fv or fv < fs:
            errores.append("Revise las fechas del contrato.")
        if eq and not errores:
            est = dict(eq)
            est["mant"] = L.estado_equipo(db(), eq, c, max(ref, fs))
            clase, motivo = L.evaluar_despacho(est, fs, fv)
            exc_m, exc_a = texto(p.get("excepcion_motivo")), texto(p.get("excepcion_autoriza"))
            if clase == "bloqueado" and bloquear and not (exc_m and exc_a):
                errores.append(f"{eq['codigo']}: {motivo} Para despacharlo debe registrar el motivo de la "
                               "excepción y quién la autoriza.")
            if clase == "advertencia" and not p.get("acepto"):
                errores.append(f"{eq['codigo']}: {motivo} Confirme que conoce la advertencia.")
        if errores:
            for e in errores:
                flash(e, "error")
        else:
            h = num(p.get("horometro_salida"))
            alq = db().execute(
                """INSERT INTO alquileres(equipo_id, cliente, obra, zona, fecha_salida, fecha_vencimiento,
                   horometro_salida, tarifa_dia, estado, mant_estado_salida, excepcion_motivo,
                   excepcion_autoriza, notas) VALUES (?,?,?,?,?,?,?,?,'En obra',?,?,?,?)""",
                (eq["id"], texto(p.get("cliente")), texto(p.get("obra")), texto(p.get("zona")), iso(fs), iso(fv),
                 h, num(p.get("tarifa_dia")), est["mant"]["estado"],
                 exc_m if clase == "bloqueado" else None, exc_a if clase == "bloqueado" else None,
                 texto(p.get("notas")))).lastrowid
            if num(p.get("pedido")):
                db().execute("UPDATE pedidos SET estado='Despachado', equipo_id=?, alquiler_id=? WHERE id=?",
                             (eq["id"], alq, int(num(p.get("pedido")))))
            if h is not None:
                db().execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                             (eq["id"], iso(fs), h, "Salida a obra"))
            db().execute("UPDATE equipos SET situacion='En obra', ubicacion_detalle=? WHERE id=?",
                         (texto(p.get("obra")), eq["id"]))
            db().commit()
            flash(f"{eq['codigo']} despachado a {p.get('obra')} ({p.get('cliente')}).", "ok")
            return redirect(url_for("alquileres"))

    candidatos = []
    buscar = p.get("buscar") or request.method == "POST"
    if buscar:
        fs = a_fecha(criterios["fecha_salida"])
        for e in L.flota_con_estado(db(), c, max(ref, fs)):
            if e["categoria"] != criterios["categoria"] or e["situacion"] != "Disponible" or e["alquiler"]:
                continue
            if criterios["altura"] and (e["altura_trabajo"] or 0) < criterios["altura"]:
                continue
            if criterios["capacidad"] and (e["capacidad_kg"] or 0) < criterios["capacidad"]:
                continue
            clase, motivo = L.evaluar_despacho(e, criterios["fecha_salida"], criterios["fecha_vencimiento"])
            e["clase"], e["motivo"], e["tarifa"] = clase, motivo, L.tarifa_sugerida(e)
            if e["orden"] and e["orden"]["fecha_programada"]:
                e["motivo"] += f" Tiene la {f_ot(e['orden']['id'])} programada el {f_fecha(e['orden']['fecha_programada'])}."
            candidatos.append(e)
        rango = {"apto": 0, "advertencia": 1, "bloqueado": 2}
        candidatos.sort(key=lambda e: (rango[e["clase"]], e["altura_trabajo"] or 99,
                                       -(e["mant"]["dias_rest"] or -999)))
    clientes = [r[0] for r in db().execute(
        "SELECT DISTINCT cliente FROM alquileres WHERE cliente IS NOT NULL ORDER BY cliente")]
    seleccionado = p.get("equipo_id")
    pedido = db().execute("SELECT * FROM pedidos WHERE id=?", (num(p.get("pedido"), 0),)).fetchone()
    return render_template("despacho.html", pedido=pedido, criterios=criterios, candidatos=candidatos, buscar=buscar,
                           clientes=clientes, bloquear=bloquear, seleccionado=seleccionado, form=p)


@app.route("/alquileres/<int:aid>/retorno", methods=["GET", "POST"])
def retorno(aid):
    a = db().execute("""SELECT a.*, e.codigo, e.categoria FROM alquileres a JOIN equipos e
                        ON e.id=a.equipo_id WHERE a.id=?""", (aid,)).fetchone()
    if not a or a["estado"] != "En obra":
        abort(404)
    c = cfg()
    ref = L.fecha_ref(c)
    if request.method == "POST":
        fr = a_fecha(request.form.get("fecha_retorno"))
        h = num(request.form.get("horometro_retorno"))
        if not fr:
            flash("Indique la fecha de retorno.", "error")
            return redirect(request.url)
        ult = L.ultima_lectura(db(), a["equipo_id"])
        if h is not None and ult and h < ult["horometro"]:
            flash(f"El horómetro de retorno ({h:,.1f} h) es menor que la última lectura "
                  f"({ult['horometro']:,.1f} h).", "error")
            return redirect(request.url)
        db().execute("""UPDATE alquileres SET estado='Cerrado', fecha_retorno=?, horometro_retorno=?,
                        notas=COALESCE(notas||' · ','')||COALESCE(?, '') WHERE id=?""",
                     (iso(fr), h, texto(request.form.get("notas")), aid))
        if h is not None:
            db().execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                         (a["equipo_id"], iso(fr), h, "Retorno de obra"))
        eq = db().execute("SELECT * FROM equipos WHERE id=?", (a["equipo_id"],)).fetchone()
        m = L.estado_equipo(db(), eq, c, max(ref, fr))
        situacion, msg = "Disponible", f"{a['codigo']} retornó al patio con el mantenimiento {m['estado'].lower()}."
        abierta = db().execute(f"SELECT id FROM ordenes WHERE equipo_id=? AND estado IN {L.ESTADOS_ABIERTOS}",
                               (a["equipo_id"],)).fetchone()
        if m["estado"] in ("Vencido", "Por vencer", "Sin datos") or request.form.get("falla"):
            nivel = m["nivel"] or "A"
            pp = L.parametros_servicio(db(), a["categoria"], nivel)
            if not abierta:
                tipo = "Correctivo" if request.form.get("falla") else "Preventivo"
                cur = db().execute(
                    """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, prioridad, duracion_dias, tecnico_dias,
                       costo_estimado, origen, descripcion) VALUES (?,?,?,'Pendiente',?,?,?,?,?,?)""",
                    (a["equipo_id"], tipo, nivel if tipo == "Preventivo" else None,
                     "Alta" if m["estado"] != "Por vencer" or tipo == "Correctivo" else "Normal",
                     pp["duracion_dias"], pp["tecnico_dias"], pp["costo"], "Retorno de obra",
                     texto(request.form.get("falla_detalle")) if tipo == "Correctivo"
                     else f"Servicio {nivel}: el equipo retornó con el mantenimiento {m['estado'].lower()}"))
                msg += f" Se generó la {f_ot(cur.lastrowid)}."
            if m["estado"] in ("Vencido", "Sin datos") or request.form.get("falla"):
                situacion = "En taller"
                msg += " Queda retenido en taller: no puede volver a salir sin pasar por mantenimiento."
        db().execute("UPDATE equipos SET situacion=?, ubicacion_detalle=NULL WHERE id=?", (situacion, a["equipo_id"]))
        db().commit()
        flash(msg, "ok" if situacion == "Disponible" else "warn")
        return redirect(url_for("equipo", codigo=a["codigo"]))
    eq = db().execute("SELECT * FROM equipos WHERE id=?", (a["equipo_id"],)).fetchone()
    m = L.estado_equipo(db(), eq, c, ref)
    return render_template("retorno.html", a=a, m=m, ult=L.ultima_lectura(db(), a["equipo_id"]))


@app.route("/alquileres/<int:aid>/prorroga", methods=["POST"])
def prorroga(aid):
    a = db().execute("SELECT a.*, e.codigo FROM alquileres a JOIN equipos e ON e.id=a.equipo_id WHERE a.id=?",
                     (aid,)).fetchone()
    nueva = a_fecha(request.form.get("fecha_vencimiento"))
    if not a or not nueva:
        flash("Indique la nueva fecha de vencimiento.", "error")
    else:
        nota = f"Prórroga registrada: {f_fecha(a['fecha_vencimiento'])} → {nueva.strftime('%d/%m/%Y')}"
        db().execute("UPDATE alquileres SET fecha_vencimiento=?, notas=COALESCE(notas||' · ','')||? WHERE id=?",
                     (iso(nueva), nota, aid))
        db().commit()
        flash(f"{a['codigo']}: {nota}.", "ok")
    return redirect(request.referrer or url_for("alquileres"))


# ================================================================ mantenimiento: órdenes de trabajo
@app.route("/mantenimiento")
def mantenimiento():
    estado = request.args.get("estado", "abiertas")
    if estado == "abiertas":
        filas = ordenes_query(f"o.estado IN {L.ESTADOS_ABIERTOS}", (),
                              "CASE o.estado WHEN 'En ejecución' THEN 0 WHEN 'Programada' THEN 1 ELSE 2 END, "
                              "COALESCE(o.fecha_programada,'9999'), o.id")
    else:
        filas = ordenes_query("o.estado=?", (estado,), "COALESCE(o.fecha_fin, o.fecha_programada) DESC", 400)
    conteo = {r[0]: r[1] for r in db().execute("SELECT estado, COUNT(*) FROM ordenes GROUP BY estado")}
    c = cfg()
    ref = L.fecha_ref(c)
    flota = L.flota_con_estado(db(), c, ref)
    sin_orden = [e for e in flota if e["mant"]["estado"] in ("Vencido", "Por vencer")
                 and e["situacion"] in ("Disponible", "En taller") and not e["orden"]]
    return render_template("mantenimiento.html", filas=filas, estado=estado, conteo=conteo, sin_orden=sin_orden)


def leer_form_orden():
    f = request.form
    return dict(tipo=f.get("tipo") or "Preventivo", nivel=texto(f.get("nivel")),
                prioridad=f.get("prioridad") or "Normal", fecha_programada=texto(f.get("fecha_programada")),
                duracion_dias=int(num(f.get("duracion_dias"), 1) or 1),
                tecnico_dias=num(f.get("tecnico_dias"), 1), costo_estimado=num(f.get("costo_estimado")),
                descripcion=texto(f.get("descripcion")), observaciones=texto(f.get("observaciones")))


@app.route("/mantenimiento/nueva", methods=["GET", "POST"])
def orden_nueva():
    c = cfg()
    tecnicos = db().execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY nombre").fetchall()
    equipos_l = db().execute("SELECT id, codigo, categoria, marca, modelo FROM equipos WHERE activo=1 ORDER BY codigo").fetchall()
    if request.method == "POST":
        eq = db().execute("SELECT * FROM equipos WHERE id=?", (request.form.get("equipo_id"),)).fetchone()
        if not eq:
            flash("Seleccione el equipo.", "error")
            return redirect(request.url)
        d = leer_form_orden()
        tids = request.form.getlist("tecnicos")
        d["estado"] = "Programada" if d["fecha_programada"] else "Pendiente"
        if d["fecha_programada"] and L.es_domingo(a_fecha(d["fecha_programada"])):
            d["fecha_programada"] = iso(a_fecha(d["fecha_programada"]) + timedelta(days=1))
        avisos = L.verificar_programacion(db(), c, d["fecha_programada"], d["duracion_dias"], d["tecnico_dias"], tids)
        cols = list(d.keys())
        cur = db().execute(f"INSERT INTO ordenes(equipo_id, origen, {','.join(cols)}) VALUES (?, 'Manual', "
                           f"{','.join('?' * len(cols))})", [eq["id"], *d.values()])
        for t in tids:
            db().execute("INSERT INTO orden_tecnicos VALUES (?,?)", (cur.lastrowid, int(t)))
        db().commit()
        flash(f"{f_ot(cur.lastrowid)} creada para {eq['codigo']}.", "ok")
        for a in avisos:
            flash(a, "warn")
        return redirect(url_for("orden", oid=cur.lastrowid))
    sugerencia = None
    codigo = request.args.get("equipo")
    sel = None
    if codigo:
        sel = db().execute("SELECT * FROM equipos WHERE codigo=?", (codigo,)).fetchone()
        if sel:
            m = L.estado_equipo(db(), sel, c)
            nivel = m["nivel"] or "A"
            sugerencia = dict(m=m, nivel=nivel, **L.parametros_servicio(db(), sel["categoria"], nivel))
    params = [dict(r) for r in db().execute("SELECT * FROM parametros_nivel")]
    return render_template("orden_form.html", o=None, sel=sel, sugerencia=sugerencia, tecnicos=tecnicos,
                           equipos=equipos_l, asignados=[], params=params,
                           tipo=request.args.get("tipo", "Preventivo"))


@app.route("/mantenimiento/<int:oid>")
def orden(oid):
    o = orden_o_404(oid)
    c = cfg()
    eq = db().execute("SELECT * FROM equipos WHERE id=?", (o["equipo_id"],)).fetchone()
    tecs = L.tecnicos_de_orden(db(), oid)
    avisos = []
    if o["estado"] == "Programada":
        avisos = L.verificar_programacion(db(), c, o["fecha_programada"], o["duracion_dias"], o["tecnico_dias"],
                                          [t["id"] for t in tecs], excluir_orden=oid)
    alq = db().execute("SELECT * FROM alquileres WHERE equipo_id=? AND estado='En obra'", (eq["id"],)).fetchone()
    try:
        hechas = set(json.loads(o["tareas"])) if o["tareas"] else None
    except ValueError:
        hechas = None
    plan = L.tareas_plan(eq["categoria"], o["nivel"]) if o["tipo"] == "Preventivo" else []
    return render_template("orden.html", o=o, eq=eq, tecs=tecs, m=L.estado_equipo(db(), eq, c), avisos=avisos,
                           plan=plan, hechas=hechas, formato=L.formato_plan(eq["categoria"]),
                           alq=alq, ult=L.ultima_lectura(db(), eq["id"]),
                           dias=L.dias_orden(o) if o["estado"] in ("Programada", "En ejecución") else [])


@app.route("/mantenimiento/<int:oid>/editar", methods=["GET", "POST"])
def orden_editar(oid):
    o = orden_o_404(oid)
    c = cfg()
    if request.method == "POST":
        d = leer_form_orden()
        tids = request.form.getlist("tecnicos")
        if o["estado"] in ("Pendiente", "Programada"):
            d["estado"] = "Programada" if d["fecha_programada"] else "Pendiente"
        db().execute(f"UPDATE ordenes SET {', '.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), oid])
        db().execute("DELETE FROM orden_tecnicos WHERE orden_id=?", (oid,))
        for t in tids:
            db().execute("INSERT INTO orden_tecnicos VALUES (?,?)", (oid, int(t)))
        db().commit()
        for a in L.verificar_programacion(db(), c, d["fecha_programada"], d["duracion_dias"], d["tecnico_dias"],
                                          tids, excluir_orden=oid):
            flash(a, "warn")
        flash("Orden actualizada.", "ok")
        return redirect(url_for("orden", oid=oid))
    tecnicos = db().execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY nombre").fetchall()
    sel = db().execute("SELECT * FROM equipos WHERE id=?", (o["equipo_id"],)).fetchone()
    params = [dict(r) for r in db().execute("SELECT * FROM parametros_nivel")]
    return render_template("orden_form.html", o=o, sel=sel, sugerencia=None, tecnicos=tecnicos, equipos=[],
                           asignados=[t["id"] for t in L.tecnicos_de_orden(db(), oid)], params=params, tipo=o["tipo"])


@app.route("/mantenimiento/<int:oid>/<accion>", methods=["POST"])
def orden_accion(oid, accion):
    o = orden_o_404(oid)
    c = cfg()
    ref = L.fecha_ref(c)
    f = request.form
    if accion == "iniciar" and o["estado"] in ("Pendiente", "Programada"):
        fi = texto(f.get("fecha_inicio")) or iso(ref)
        db().execute("UPDATE ordenes SET estado='En ejecución', fecha_inicio=? WHERE id=?", (fi, oid))
        if o["situacion"] != "En obra":
            db().execute("UPDATE equipos SET situacion='En taller' WHERE id=?", (o["equipo_id"],))
        flash(f"{f_ot(oid)} en ejecución.", "ok")
    elif accion == "completar" and o["estado"] in ("Pendiente", "Programada", "En ejecución"):
        ff = texto(f.get("fecha_fin")) or iso(ref)
        h = num(f.get("horometro"))
        if h is None:
            flash("Registre el horómetro al cierre del servicio: sin él, el próximo vencimiento por horas "
                  "no puede calcularse.", "error")
            return redirect(url_for("orden", oid=oid))
        ult = L.ultima_lectura(db(), o["equipo_id"])
        if ult and h < ult["horometro"] and a_fecha(ff) >= a_fecha(ult["fecha"]):
            flash(f"El horómetro ({h:,.1f} h) es menor que la última lectura ({ult['horometro']:,.1f} h).", "error")
            return redirect(url_for("orden", oid=oid))
        marcadas = f.getlist("tareas")
        if o["tipo"] == "Preventivo":
            plan = L.codigos_tareas(o["categoria"], texto(f.get("nivel")) or o["nivel"])
            faltan = [t for t in plan if t not in marcadas]
            if plan and faltan and not f.get("confirmar_incompleto"):
                flash(f"Faltan {len(faltan)} tareas de la lista del plan ({', '.join(faltan[:8])}"
                      f"{'…' if len(faltan) > 8 else ''}). Márquelas o confirme el cierre con tareas pendientes.", "error")
                return redirect(url_for("orden", oid=oid))
        db().execute("""UPDATE ordenes SET estado='Completada', fecha_inicio=COALESCE(fecha_inicio, ?), fecha_fin=?,
                        horometro=?, costo_real=?, observaciones=COALESCE(?, observaciones),
                        nivel=COALESCE(?, nivel), tareas=? WHERE id=?""",
                     (ff, ff, h, num(f.get("costo_real")), texto(f.get("observaciones")), texto(f.get("nivel")),
                      json.dumps(marcadas) if marcadas else None, oid))
        db().execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                     (o["equipo_id"], ff, h, f"Cierre {f_ot(oid)}"))
        otra = db().execute(f"""SELECT 1 FROM ordenes WHERE equipo_id=? AND id<>? AND estado='En ejecución'""",
                            (o["equipo_id"], oid)).fetchone()
        if o["situacion"] in ("En taller", "Fuera de servicio") and not otra:
            db().execute("UPDATE equipos SET situacion='Disponible', ubicacion_detalle=NULL WHERE id=?", (o["equipo_id"],))
        flash(f"{f_ot(oid)} completada. {o['codigo']} " +
              ("queda disponible para despacho." if o["situacion"] != "En obra" else "continúa en obra."), "ok")
    elif accion == "cancelar" and o["estado"] in L.ESTADOS_ABIERTOS:
        db().execute("UPDATE ordenes SET estado='Cancelada', observaciones=COALESCE(?, observaciones) WHERE id=?",
                     (texto(f.get("motivo")), oid))
        flash(f"{f_ot(oid)} cancelada.", "ok")
    else:
        flash("Acción no permitida para el estado actual de la orden.", "error")
    db().commit()
    return redirect(url_for("orden", oid=oid))


@app.route("/mantenimiento/programar", methods=["POST"])
def programar():
    creadas = L.programar_automatico(db(), cfg())
    db().commit()
    if creadas:
        flash(f"Se programaron {len(creadas)} servicios en la capacidad libre del taller: " +
              ", ".join(f"{x['codigo']} ({x['fecha'].strftime('%d/%m')})" for x in creadas[:12]) +
              ("…" if len(creadas) > 12 else ""), "ok")
    else:
        flash("No hay equipos en patio o taller con el mantenimiento vencido o por vencer sin orden programada.", "ok")
    return redirect(url_for("taller"))


@app.route("/taller")
def taller():
    c = cfg()
    ref = L.fecha_ref(c)
    desde = a_fecha(request.args.get("desde")) or (ref - timedelta(days=ref.weekday()))
    dias = int(request.args.get("dias", 21))
    rango = [desde + timedelta(days=i) for i in range(dias)]
    carga = L.carga_taller(db(), desde, dias)
    agenda = L.agenda_tecnicos(db(), desde, dias)
    tecnicos = db().execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY nombre").fetchall()
    sin_tecnico = ordenes_query(
        "o.estado IN ('Programada','En ejecución') AND NOT EXISTS (SELECT 1 FROM orden_tecnicos x WHERE x.orden_id=o.id)",
        (), "o.fecha_programada")
    pendientes = ordenes_query("o.estado='Pendiente'", (), "o.prioridad, o.id")
    cap = float(c.get("capacidad_tecnicos") or 3)
    habiles = [d for d in rango if not L.es_domingo(d)]
    usada = sum(carga[d]["carga"] for d in habiles)
    return render_template("taller.html", rango=rango, carga=carga, agenda=agenda, tecnicos=tecnicos, cap=cap,
                           desde=desde, dias=dias, sin_tecnico=sin_tecnico, pendientes=pendientes,
                           utilizacion=usada / (cap * len(habiles)) if habiles else 0, usada=usada,
                           disponible=cap * len(habiles))


# ================================================================ personal de mantenimiento
@app.route("/personal")
def personal():
    c = cfg()
    ref = L.fecha_ref(c)
    tecs = db().execute("SELECT * FROM tecnicos ORDER BY activo DESC, nombre").fetchall()
    agenda = L.agenda_tecnicos(db(), ref, 14)
    stats = {}
    for t in tecs:
        r = db().execute(
            """SELECT SUM(o.estado='Completada') comp, SUM(o.estado='En ejecución') ejec,
                      SUM(o.estado='Programada') prog, MAX(CASE WHEN o.estado='Completada' THEN o.fecha_fin END) ultimo,
                      SUM(CASE WHEN o.estado='Completada' AND o.fecha_fin >= ? THEN 1 ELSE 0 END) comp90
               FROM ordenes o JOIN orden_tecnicos ot ON ot.orden_id=o.id WHERE ot.tecnico_id=?""",
            (iso(ref - timedelta(days=90)), t["id"])).fetchone()
        dias_ocup = len(agenda.get(t["id"], {}))
        stats[t["id"]] = dict(r, dias_ocupados=dias_ocup, ocupacion=dias_ocup / 12)
    total_comp = sum((s["comp"] or 0) for s in stats.values()) or 1
    return render_template("personal.html", tecs=tecs, stats=stats, total_comp=total_comp)


CAMPOS_TEC = ["nombre", "cargo", "especialidad", "dni", "telefono", "correo", "fecha_ingreso", "notas"]


@app.route("/personal/nuevo", methods=["GET", "POST"])
@app.route("/personal/<int:tid>/editar", methods=["GET", "POST"])
def tecnico_form(tid=None):
    t = db().execute("SELECT * FROM tecnicos WHERE id=?", (tid,)).fetchone() if tid else None
    if tid and not t:
        abort(404)
    if request.method == "POST":
        d = {k: texto(request.form.get(k)) for k in CAMPOS_TEC}
        d["activo"] = 1 if request.form.get("activo", "1") == "1" else 0
        d["ejecuta"] = 1 if request.form.get("ejecuta", "1") == "1" else 0
        if not d["nombre"]:
            flash("El nombre es obligatorio.", "error")
            return render_template("tecnico_form.html", t=d)
        if t:
            db().execute(f"UPDATE tecnicos SET {', '.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), tid])
        else:
            tid = db().execute(f"INSERT INTO tecnicos({','.join(d)}) VALUES ({','.join('?' * len(d))})",
                               list(d.values())).lastrowid
        db().commit()
        flash("Datos del técnico guardados.", "ok")
        return redirect(url_for("tecnico", tid=tid))
    return render_template("tecnico_form.html", t=t or {"activo": 1, "ejecuta": 1, "cargo": "Técnico de mantenimiento"})


@app.route("/personal/<int:tid>")
def tecnico(tid):
    t = db().execute("SELECT * FROM tecnicos WHERE id=?", (tid,)).fetchone()
    if not t:
        abort(404)
    ords = ordenes_query("EXISTS (SELECT 1 FROM orden_tecnicos x WHERE x.orden_id=o.id AND x.tecnico_id=?)", (tid,),
                         "CASE WHEN o.estado IN ('En ejecución','Programada','Pendiente') THEN 0 ELSE 1 END, "
                         "COALESCE(o.fecha_fin, o.fecha_programada) DESC")
    por_cat = {}
    for o in ords:
        if o["estado"] == "Completada":
            por_cat[o["categoria"]] = por_cat.get(o["categoria"], 0) + 1
    return render_template("tecnico.html", t=t, ords=ords, por_cat=por_cat)


# ================================================================ incidentes en obra
@app.route("/incidentes")
def incidentes():
    filas = db().execute(
        """SELECT i.*, e.codigo, e.categoria, a.cliente, a.obra, t.nombre AS tecnico FROM incidentes i
           JOIN equipos e ON e.id=i.equipo_id LEFT JOIN alquileres a ON a.id=i.alquiler_id
           LEFT JOIN tecnicos t ON t.id=i.tecnico_id ORDER BY i.fecha DESC""").fetchall()
    n = len(filas)
    venc = sum(1 for f in filas if f["mant_estado"] in ("Vencido", "Sin datos"))
    return render_template("incidentes.html", filas=filas, n=n, venc=venc,
                           costo=sum((f["costo"] or 0) for f in filas),
                           dias=sum((f["dias_parado"] or 0) for f in filas))


@app.route("/incidentes/nuevo", methods=["GET", "POST"])
def incidente_nuevo():
    c = cfg()
    if request.method == "POST":
        eq = db().execute("SELECT * FROM equipos WHERE id=?", (request.form.get("equipo_id"),)).fetchone()
        fecha = a_fecha(request.form.get("fecha"))
        if not eq or not fecha or not texto(request.form.get("descripcion")):
            flash("Indique el equipo, la fecha y la descripción de la falla.", "error")
            return redirect(request.url)
        m = L.calcular_estado(eq, L.ultimo_pm(db(), eq["id"], fecha), L.ultima_lectura(db(), eq["id"], fecha), c, fecha)
        alq = db().execute("""SELECT id FROM alquileres WHERE equipo_id=? AND fecha_salida<=?
                              AND (fecha_retorno IS NULL OR fecha_retorno>=?) ORDER BY fecha_salida DESC""",
                           (eq["id"], iso(fecha), iso(fecha))).fetchone()
        h = num(request.form.get("horometro"))
        orden_id = None
        if request.form.get("crear_ot"):
            cur = db().execute(
                """INSERT INTO ordenes(equipo_id, tipo, estado, prioridad, origen, descripcion, duracion_dias,
                   tecnico_dias) VALUES (?, 'Correctivo', 'Pendiente', 'Alta', 'Incidente en obra', ?, 1, 1)""",
                (eq["id"], texto(request.form.get("descripcion"))))
            orden_id = cur.lastrowid
            if request.form.get("tecnico_id"):
                db().execute("INSERT INTO orden_tecnicos VALUES (?,?)", (orden_id, int(request.form.get("tecnico_id"))))
        db().execute(
            """INSERT INTO incidentes(equipo_id, alquiler_id, fecha, horometro, mant_estado, descripcion, tecnico_id,
               costo, dias_parado, orden_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (eq["id"], alq["id"] if alq else None, iso(fecha), h, m["estado"], texto(request.form.get("descripcion")),
             request.form.get("tecnico_id") or None, num(request.form.get("costo")),
             num(request.form.get("dias_parado")), orden_id))
        if h is not None:
            db().execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                         (eq["id"], iso(fecha), h, "Incidente en obra"))
        db().commit()
        flash(f"Incidente registrado para {eq['codigo']}. En la fecha de la falla el mantenimiento estaba "
              f"«{m['estado'].lower()}».", "warn" if m["estado"] in ("Vencido", "Sin datos") else "ok")
        return redirect(url_for("incidentes"))
    equipos_l = db().execute(
        "SELECT id, codigo, categoria, situacion FROM equipos WHERE activo=1 ORDER BY situacion<>'En obra', codigo").fetchall()
    tecnicos = db().execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY nombre").fetchall()
    return render_template("incidente_form.html", equipos=equipos_l, tecnicos=tecnicos,
                           sel=request.args.get("equipo"))


# ================================================================ configuración y exportación
@app.route("/configuracion", methods=["GET", "POST"])
def configuracion():
    if request.method == "POST":
        for k in base.CONFIG_DEFECTO:
            if k in ("bloquear_vencidos", "inspeccion_reinicia"):
                v = "1" if request.form.get(k) else "0"
            else:
                v = (request.form.get(k) or "").strip()
            db().execute("INSERT OR REPLACE INTO config(clave, valor) VALUES (?,?)", (k, v))
        for r in db().execute("SELECT categoria, nivel FROM parametros_nivel").fetchall():
            pref = f"p_{r['categoria']}_{r['nivel']}_"
            vals = [num(request.form.get(pref + x)) for x in ("dur", "costo", "td")]
            if all(v is not None for v in vals):
                db().execute("""UPDATE parametros_nivel SET duracion_dias=?, costo=?, tecnico_dias=?
                                WHERE categoria=? AND nivel=?""", (int(vals[0]), vals[1], vals[2], r["categoria"], r["nivel"]))
        db().commit()
        flash("Configuración guardada.", "ok")
        return redirect(url_for("configuracion"))
    params = db().execute("SELECT * FROM parametros_nivel ORDER BY categoria, nivel").fetchall()
    return render_template("configuracion.html", params=params, docs_dir=DOCS_DIR, db_path=base.DB_PATH)


# ================================================================ estatus diario de equipos
def libros_estatus():
    """Libros de estatus disponibles: los cargados (datos/estatus) y los de la carpeta de documentos."""
    out = []
    for carpeta in (DOCS_DIR, ESTATUS_DIR):
        if carpeta.exists():
            out += [p for p in carpeta.glob("*.xlsx") if "ESTATUS" in p.name.upper() and not p.name.startswith("~$")]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


@app.route("/estatus")
def estatus():
    c = cfg()
    inf = json.loads(c["estatus_informe"]) if c.get("estatus_informe") else None
    return render_template("estatus.html", inf=inf, libros=libros_estatus(), docs_dir=DOCS_DIR,
                           modo_sim=bool(c.get("simulacion_hasta")))


@app.route("/estatus/importar", methods=["POST"])
def estatus_importar():
    """Carga un libro «Estatus de los equipos». La primera vez (o con «reconstruir») parte del registro
    real del 22/09/2026 y descarta la operación simulada; después aplica sólo las hojas nuevas."""
    ESTATUS_DIR.mkdir(parents=True, exist_ok=True)
    f = request.files.get("archivo")
    if f and f.filename:
        if not f.filename.lower().endswith(".xlsx"):
            flash("El estatus debe ser un libro Excel (.xlsx).", "error")
            return redirect(url_for("estatus"))
        nombre = f"{date.today():%Y-%m-%d}_{secure_filename(f.filename)}"
        ruta = ESTATUS_DIR / nombre
        f.save(ruta)
        archivo = f.filename
    else:
        ruta = Path(request.form.get("ruta") or "")
        if ruta not in libros_estatus():
            flash("Elija un libro de estatus o adjunte el archivo.", "error")
            return redirect(url_for("estatus"))
        archivo = ruta.name
    c = cfg()
    reconstruir = bool(request.form.get("reconstruir")) or bool(c.get("simulacion_hasta")) or not c.get("estatus_fecha")
    try:
        if reconstruir:
            base.cerrar_db()
            r = IE.cargar_real(ruta, archivo=archivo)
        else:
            r = IE.importar(db(), ruta, archivo=archivo)
            db().commit()
    except (ValueError, KeyError) as e:
        flash(f"No se pudo leer el estatus: {e}", "error")
        return redirect(url_for("estatus"))
    k = r["cuenta"]
    flash(f"Estatus al {a_fecha(r['fecha']):%d/%m/%Y} cargado (hojas {', '.join(r['hojas'])}): "
          f"{r['flota']['obra']} equipos en obra, {r['flota']['patio']} en el patio; {k['nuevos']} contratos nuevos, "
          f"{k['cerrados']} devueltos, {k['actualizados']} actualizados, {k['vendidos']} equipos vendidos; "
          f"{k.get('coherencia', 0)} incoherencias entre días, {k.get('vencidos', 0)} contratos vencidos en obra y "
          f"{k.get('lagunas', 0)} lagunas para revisar.", "ok")
    return redirect(url_for("estatus"))


# ================================================================ documentos de mantenimiento (carpetas)
@app.route("/documentos")
def documentos():
    c = cfg()
    inf = json.loads(c["documentos_informe"]) if c.get("documentos_informe") else None
    flota = {e["codigo"]: e for e in L.flota_con_estado(db(), c, solo_activos=False)}
    filtro = request.args.get("ver", "todos")
    filas = []
    for f in (inf or {}).get("equipos", []):
        e = flota.get(f["codigo"])
        f["mant"] = e["mant"] if e else None
        f["activo"] = bool(e and e["activo"])
        f["ultimo"] = f["servicios"][-1] if f["servicios"] else None
        f["ult_co"] = next((i for i in reversed(f["inspecciones"]) if i.get("horometro") is not None), None)
        if filtro == "obs" and not f["incoherencias"] or filtro == "nuevos" and not f.get("de_carpeta"):
            continue
        filas.append(f)
    est = Counter(f["mant"]["estado"] for f in (inf or {}).get("equipos", []) if f.get("mant") and f["activo"])
    solo_cal = sum(1 for f in (inf or {}).get("equipos", []) if f.get("mant") and f["activo"] and
                   f["mant"]["estado"] == "Vencido" and (f["mant"]["horas_rest"] or 0) > 0)
    return render_template("documentos.html", inf=inf, filas=filas, filtro=filtro, est=est, solo_cal=solo_cal,
                           docs_dir=ID.DOCS_DIR)


@app.route("/documentos/releer", methods=["POST"])
def documentos_releer():
    try:
        r = ID.releer(db(), ID.DOCS_DIR)
        db().commit()
    except (RuntimeError, OSError) as e:
        flash(f"No se pudieron leer las carpetas: {e}", "error")
        return redirect(url_for("documentos"))
    flash(f"Se revisaron {r['documentos']} documentos de {len(r['equipos'])} equipos ({r['leidos']} nuevos o cambiados): "
          f"{r['nuevos_servicios']} servicios y {r['nuevas_lecturas']} lecturas de horómetro agregados; "
          f"{r['incoherencias']} observaciones para revisar.", "ok")
    return redirect(url_for("documentos"))


# ================================================================ cartera de pedidos
ESTADOS_PEDIDO = ["Pendiente", "Asignado", "Despachado", "No atendido", "Cancelado"]


@app.route("/pedidos")
def pedidos():
    c = cfg()
    ref = L.fecha_ref(c)
    estado = request.args.get("estado", "abiertos")
    q = """SELECT p.*, e.codigo FROM pedidos p LEFT JOIN equipos e ON e.id=p.equipo_id"""
    if estado == "abiertos":
        filas = db().execute(q + " WHERE p.estado IN ('Pendiente','Asignado') ORDER BY p.fecha_inicio, p.id").fetchall()
    elif estado == "todos":
        filas = db().execute(q + " ORDER BY p.fecha_inicio DESC, p.id DESC").fetchall()
    else:
        filas = db().execute(q + " WHERE p.estado=? ORDER BY p.fecha_inicio DESC, p.id DESC", (estado,)).fetchall()
    cuenta = {r[0]: r[1] for r in db().execute("SELECT estado, COUNT(*) FROM pedidos GROUP BY estado")}
    clientes = [r[0] for r in db().execute(
        "SELECT DISTINCT cliente FROM alquileres WHERE cliente IS NOT NULL ORDER BY cliente")]
    return render_template("pedidos.html", filas=filas, estado=estado, cuenta=cuenta, clientes=clientes,
                           estados=ESTADOS_PEDIDO, ref=ref)


@app.route("/pedidos/nuevo", methods=["POST"])
def pedido_nuevo():
    p = request.form
    fi, ff = a_fecha(p.get("fecha_inicio")), a_fecha(p.get("fecha_fin"))
    if not fi or not ff or ff < fi or p.get("categoria") not in L.CATEGORIAS:
        flash("Revise la categoría y las fechas del pedido.", "error")
        return redirect(url_for("pedidos"))
    db().execute("""INSERT INTO pedidos(cliente, obra, zona, categoria, altura_req, capacidad_req, fecha_inicio,
                    fecha_fin, tarifa_dia, costo_traslado, estado, origen, notas)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'Pendiente','Registro comercial',?)""",
                 (texto(p.get("cliente")), texto(p.get("obra")), texto(p.get("zona")), p.get("categoria"),
                  num(p.get("altura_req")), num(p.get("capacidad_req")), iso(fi), iso(ff),
                  num(p.get("tarifa_dia")) or L.tarifa_sugerida(dict(categoria=p.get("categoria"),
                                                                       altura_trabajo=num(p.get("altura_req"), 0))),
                  num(p.get("costo_traslado")) if num(p.get("costo_traslado")) is not None
                  else O.costo_traslado_defecto(texto(p.get("zona")), p.get("categoria")),
                  texto(p.get("notas"))))
    db().commit()
    flash("Pedido registrado en la cartera. Entrará en la próxima corrida del modelo.", "ok")
    return redirect(url_for("pedidos"))


@app.route("/pedidos/generar", methods=["POST"])
def pedidos_generar():
    c = cfg()
    desde = a_fecha(request.form.get("desde")) or L.fecha_ref(c)
    T = int(num(request.form.get("horizonte"), 60))
    semilla = int(num(request.form.get("semilla"), 20260922))
    factor = float(num(request.form.get("factor"), 1.0))
    if request.form.get("reemplazar"):
        db().execute("DELETE FROM pedidos WHERE origen='Cartera calibrada' AND estado IN ('Pendiente','Asignado','No atendido')")
    try:
        n, lam = O.generar_cartera(db(), c, desde, T, semilla, factor)
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("pedidos"))
    db().commit()
    flash(f"Se generaron {n} pedidos del {desde:%d/%m/%Y} al {desde + timedelta(days=T - 1):%d/%m/%Y} "
          f"(tasa de llegada {lam:g} pedidos por día por la ley de Little, semilla {semilla}).", "ok")
    return redirect(url_for("pedidos"))


@app.route("/pedidos/<int:pid>/<accion>", methods=["POST"])
def pedido_accion(pid, accion):
    nuevos = {"cancelar": "Cancelado", "reabrir": "Pendiente"}
    if accion == "eliminar":
        db().execute("DELETE FROM pedidos WHERE id=? AND estado<>'Despachado'", (pid,))
    elif accion in nuevos:
        db().execute("UPDATE pedidos SET estado=?, equipo_id=CASE WHEN ?='Pendiente' THEN NULL ELSE equipo_id END "
                     "WHERE id=? AND estado<>'Despachado'", (nuevos[accion], nuevos[accion], pid))
    else:
        abort(404)
    db().commit()
    return redirect(request.referrer or url_for("pedidos"))


# ================================================================ optimización (modelo PLEM)
def corrida_o_404(cid):
    c = db().execute("SELECT * FROM corridas WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    return c


def vista_corrida(c, esc):
    """Datos de la corrida listos para la plantilla."""
    if c["estado"] != "Terminada" or not c["resultados"]:
        return None
    res = json.loads(c["resultados"])
    inst = res["instancia"]
    T0 = a_fecha(inst["T0"])
    if esc not in res["escenarios"]:
        esc = "E3" if "E3" in res["escenarios"] else "E2"
    MQ, PD = inst["maquinas"], inst["pedidos"]
    plan = res["escenarios"][esc]["plan"]
    filas = {}
    for i, q in enumerate(MQ):
        barras = []
        if q["ret"] > 0:
            barras.append(dict(a=0, b=min(inst["T"], q["ret"]) - 1, k="obra" if q["situacion"] == "En obra" else "fuera",
                               t=q["origen_ret"]))
        filas[i] = dict(q=q, barras=barras)
    for a in plan["asig"]:
        o = PD[a["j"]]
        filas[a["m"]]["barras"].append(dict(a=a["s"], b=a["e"], k="venc" if a["dias_vencido"] else "alq",
                                            t=f"{o['cliente'] or 'Pedido'} · {o['obra'] or ''} ({a['s'] + 1}–{a['e'] + 1})"
                                              + (f" · {a['dias_vencido']} días con mantenimiento vencido" if a["dias_vencido"] else "")))
    for s in plan["serv"]:
        filas[s["m"]]["barras"].append(dict(a=s["t"], b=s["fin"], k="serv",
                                            t=f"Servicio {s['nivel']} · {s['td']:g} técnico-días · S/ {s['costo']:,.0f}"))
    gantt = sorted((f for f in filas.values() if f["barras"]),
                   key=lambda f: (L.CATEGORIAS.index(f["q"]["categoria"]) if f["q"]["categoria"] in L.CATEGORIAS else 9,
                                  f["q"]["codigo"]))
    dias = [T0 + timedelta(days=t) for t in range(inst["T"])]
    v = dict(res=res, inst=inst, esc=esc, plan=plan, gantt=gantt, dias=dias, T0=T0, PD=PD, MQ=MQ,
             E={k: v["ind"] for k, v in res["escenarios"].items()})
    v["X"] = explicar_corrida(c, v)
    return v


# Qué mide cada indicador y en qué sentido es mejor (para leer la tabla de escenarios sin el informe)
QUE_MIDE = {
    "atendidos": ("Pedidos de la cartera que se cubren con un equipo propio.", True),
    "no_atendidos": ("Pedidos que el plan no cubre con flota propia (se pueden subarrendar o reprogramar).", False),
    "ingreso": ("Alquiler que se cobra por los días de los pedidos atendidos dentro del periodo.", True),
    "dm_vencido": ("Suma de los días en que algún equipo trabaja en obra con el mantenimiento vencido.", False),
    "ped_riesgo": ("Pedidos atendidos en algún momento con un equipo que debería estar en taller.", False),
    "costo_falla": ("Costo que se espera pagar por fallas en obra según la probabilidad de falla.", False),
    "servicios": ("Mantenimientos preventivos que se hacen en el taller durante el periodo.", None),
    "costo_mant": ("Repuestos, insumos y mano de obra de esos servicios.", None),
    "tecdias": ("Días de trabajo de técnico que usan los servicios (1 técnico × 1 día = 1).", None),
    "venc_cierre": ("Equipos que terminan el periodo con el mantenimiento vencido.", False),
    "deuda": ("Lo que costará poner al día esos equipos: trabajo que se deja para después.", False),
    "traslado": ("Fletes de entrega y recojo de los equipos asignados.", False),
    "margen": ("Ingreso − traslado − mantenimiento − falla esperada.", True),
    "margen_deuda": ("Margen neto menos la deuda al cierre. Es la cifra para decidir entre escenarios.", True),
}


def explicar_corrida(c, v):
    """Lectura en lenguaje sencillo de una corrida: resumen, mejor valor de cada indicador, motivos de
    los pedidos sin atender, uso del taller y lo que se aplicó en la operación."""
    E, R, inst = v["E"], v["res"], v["inst"]
    P = "E3" if "E3" in E else "E2"
    rs = R.get("resumen", {})
    mejor = {}
    for clave, (_, alto) in QUE_MIDE.items():
        if alto is None or clave not in E[P]:
            continue
        vals = {k: E[k][clave] for k in E if k != "E1"}       # E1 sólo sirve de referencia
        top = max(vals.values()) if alto else min(vals.values())
        mejor[clave] = [k for k, x in vals.items() if abs(x - top) < 0.5]
    motivos = Counter(n["motivo"] for n in v["plan"]["no_atendidos"])
    taller = {}
    cap_total = rs.get("cap_total") or sum(inst["cap"])
    for k in E:
        carga = E[k].get("carga") or []
        dias_trab = sum(1 for x in carga if x > 0)
        llenos = sum(1 for t, x in enumerate(carga) if inst["cap"][t] and x >= inst["cap"][t] - 0.001)
        taller[k] = dict(dias=dias_trab, llenos=llenos, uso=(E[k]["tecdias"] / cap_total * 100) if cap_total else 0)
    S = R.get("solver", {})
    optimo = all(s.get("optimo") for s in S.values()) if S else False
    verif_ok = all(all(x == 0 for x in E[k]["verif"].values()) for k in E if k in ("E2", "E3"))
    aplicado = None
    if c["aplicada"]:
        ots = db().execute(
            """SELECT o.id, e.codigo, o.nivel, o.estado, o.fecha_programada,
                      (SELECT group_concat(t.nombre, ', ') FROM orden_tecnicos ot JOIN tecnicos t ON t.id=ot.tecnico_id
                       WHERE ot.orden_id=o.id) AS tecnicos
               FROM ordenes o JOIN equipos e ON e.id=o.equipo_id
               WHERE o.tipo='Preventivo' AND (o.origen LIKE ? OR LOWER(o.observaciones) LIKE ?)
               ORDER BY o.estado='Cancelada', o.fecha_programada IS NULL, o.fecha_programada, e.codigo""",
            (f"%corrida {c['id']} (%", f"%corrida {c['id']}:%")).fetchall()
        res = db().execute(
            """SELECT p.id, p.cliente, p.obra, p.fecha_inicio, p.estado, e.codigo FROM pedidos p
               LEFT JOIN equipos e ON e.id=p.equipo_id WHERE p.corrida_id=? ORDER BY p.fecha_inicio""",
            (c["id"],)).fetchall()
        aplicado = dict(ots=ots, pedidos=res,
                        programadas=[o for o in ots if o["estado"] in ("Programada", "En ejecución", "Completada")],
                        pendientes=[o for o in ots if o["estado"] == "Pendiente"],
                        canceladas=[o for o in ots if o["estado"] == "Cancelada"],
                        reservas=[p for p in res if p["estado"] in ("Asignado", "Despachado")],
                        no_atendidos=[p for p in res if p["estado"] == "No atendido"])
    return dict(P=P, mejor=mejor, motivos=motivos.most_common(), taller=taller, optimo=optimo,
                verif_ok=verif_ok, aplicado=aplicado, cap_total=cap_total,
                vencidos0=(rs.get("estados") or {}).get("Vencido", 0),
                sin_datos0=(rs.get("estados") or {}).get("Sin datos", 0),
                gana=E[P]["margen_deuda"] - E["E0"]["margen_deuda"],
                seg=sum(s.get("seg", 0) for s in S.values()))


@app.route("/optimizacion")
@app.route("/optimizacion/<int:cid>")
def optimizacion(cid=None):
    c = cfg()
    ref = L.fecha_ref(c)
    if cid is None:
        ult = db().execute("SELECT id FROM corridas ORDER BY id DESC LIMIT 1").fetchone()
        corrida = corrida_o_404(ult["id"]) if ult else None
    else:
        corrida = corrida_o_404(cid)
    if corrida and corrida["estado"] == "En curso" and not O.en_curso(corrida["id"]):
        db().execute("UPDATE corridas SET estado='Error', mensaje='Interrumpida. Vuelva a ejecutarla.' WHERE id=?",
                     (corrida["id"],))
        db().commit()
        corrida = corrida_o_404(corrida["id"])
    v = vista_corrida(corrida, request.args.get("esc", "")) if corrida else None
    historial = db().execute("SELECT id, creado, fecha_inicio, horizonte, estado, duracion_seg, aplicada, aplicada_esc, "
                             "mensaje FROM corridas ORDER BY id DESC LIMIT 15").fetchall()
    pend = db().execute("""SELECT COUNT(*) n, MIN(fecha_inicio) a, MAX(fecha_inicio) b FROM pedidos
                           WHERE estado IN ('Pendiente','Asignado') AND fecha_fin >= ?""", (iso(ref),)).fetchone()
    previa = None
    if not v and O.pulp is not None:
        try:
            previa = O.resumen_instancia(O.construir_instancia(db(), c, ref, 60))
        except Exception:
            previa = None
    return render_template("optimizacion.html", corrida=corrida, v=v, historial=historial, pend=pend,
                           previa=previa, ESC=O.ESCENARIOS, sin_pulp=O.pulp is None, PI_VENC=O.PI_VENC,
                           cap_tec=float(c.get("capacidad_tecnicos") or 3))


@app.route("/optimizacion/correr", methods=["POST"])
def optimizacion_correr():
    if O.pulp is None:
        flash("Falta la librería PuLP: instálela con «pip install pulp==2.9.0».", "error")
        return redirect(url_for("optimizacion"))
    if any(O.en_curso(r["id"]) for r in db().execute("SELECT id FROM corridas WHERE estado='En curso'")):
        flash("Ya hay una corrida en curso.", "error")
        return redirect(url_for("optimizacion"))
    c = cfg()
    desde = a_fecha(request.form.get("desde")) or L.fecha_ref(c)
    T = max(14, min(90, int(num(request.form.get("horizonte"), 60))))
    par = dict(gap=max(0.0, float(num(request.form.get("gap"), 1)) / 100),
               tlim=int(num(request.form.get("tlim"), 120)), tlim_e3=int(num(request.form.get("tlim_e3"), 180)),
               e3=bool(request.form.get("e3")), pi_v=float(num(request.form.get("pi_v"), 25)) / 100)
    n = db().execute("""SELECT COUNT(*) FROM pedidos WHERE estado IN ('Pendiente','Asignado') AND fecha_fin >= ?
                        AND fecha_inicio < ?""", (iso(desde), iso(desde + timedelta(days=T)))).fetchone()[0]
    if not n:
        flash("La cartera no tiene pedidos pendientes en el horizonte: registre los pedidos o genere una cartera calibrada.", "error")
        return redirect(url_for("pedidos"))
    cid = db().execute("INSERT INTO corridas(fecha_inicio, horizonte, parametros, estado, mensaje) VALUES (?,?,?,?,?)",
                       (iso(desde), T, json.dumps(par), "En curso", "En cola")).lastrowid
    db().commit()
    O.lanzar(base.conectar, cid)
    return redirect(url_for("optimizacion", cid=cid))


@app.route("/optimizacion/<int:cid>/estado.json")
def optimizacion_estado(cid):
    c = corrida_o_404(cid)
    return dict(estado=c["estado"], mensaje=c["mensaje"], vivo=O.en_curso(cid))


@app.route("/optimizacion/<int:cid>/aplicar", methods=["POST"])
def optimizacion_aplicar(cid):
    corrida = corrida_o_404(cid)
    esc = request.form.get("esc", "E3")
    ventana = max(1, min(30, int(num(request.form.get("ventana"), 7))))
    if corrida["estado"] != "Terminada" or esc not in ("E2", "E3"):
        abort(400)
    h = O.aplicar_plan(db(), cfg(), corrida, esc, ventana)
    db().commit()
    msg = (f"Plan {esc} aplicado a los primeros {ventana} días: {h['ot_nuevas']} OT preventivas nuevas, "
           f"{h['ot_actualizadas']} reprogramadas, {h['reservas']} equipos reservados para pedidos"
           + (f", {h['ot_pendientes']} OT preventivas devueltas a «Pendiente» con la fecha que propone el modelo"
              if h["ot_pendientes"] else "")
           + (f", {h['no_atendidos']} pedidos marcados como no atendidos con flota propia" if h["no_atendidos"] else "") + ".")
    if h["tecnicos_faltantes"]:
        msg += f" Faltaron {h['tecnicos_faltantes']} asignaciones de técnico: revíselas en el programa del taller."
    flash(msg, "ok")
    return redirect(url_for("optimizacion", cid=cid, esc=esc))


@app.route("/optimizacion/<int:cid>/exportar.csv")
def optimizacion_exportar(cid):
    v = vista_corrida(corrida_o_404(cid), request.args.get("esc", ""))
    if not v:
        abort(404)
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")
    E = v["E"]
    w.writerow([f"Corrida {cid}", f"Inicio {v['inst']['T0']}", f"Horizonte {v['inst']['T']} días"])
    w.writerow(["Indicador"] + [f"{k} {O.ESCENARIOS[k]}" for k in E])
    for clave, etq in INDICADORES:
        w.writerow([etq] + [E[k][clave] for k in E])
    w.writerow([])
    w.writerow(["Componente", "Transición", "Margen neto", "Margen descontada la deuda"])
    for d in v["res"]["descomposicion"]:
        w.writerow([d["componente"], f"{d['de']} -> {d['a']}", d["margen"], d["margen_deuda"]])
    w.writerow([])
    w.writerow(["Solver", "Estado", "Segundos", "Variables", "Restricciones", "Pares compatibles"])
    for k, s in v["res"]["solver"].items():
        w.writerow([k, s["status"], s["seg"], s["nvars"], s["ncons"], s["npares"]])
    for esc in E:
        p = v["res"]["escenarios"][esc]["plan"]
        w.writerow([])
        w.writerow([f"Plan {esc}: servicios", "Equipo", "Nivel", "Inicio", "Fin", "Técnico-días", "Costo"])
        for s in p["serv"]:
            w.writerow(["", s["codigo"], s["nivel"], iso(v["T0"] + timedelta(days=s["t"])),
                        iso(v["T0"] + timedelta(days=s["fin"])), s["td"], s["costo"]])
        w.writerow([f"Plan {esc}: asignaciones", "Pedido", "Cliente", "Obra", "Equipo", "Inicio", "Fin",
                    "Días con mantenimiento vencido"])
        for a in p["asig"]:
            o = v["PD"][a["j"]]
            w.writerow(["", a["pedido_id"], o["cliente"], o["obra"], a["codigo"], iso(v["T0"] + timedelta(days=a["s"])),
                        iso(v["T0"] + timedelta(days=a["e"])), a["dias_vencido"]])
        w.writerow([f"Plan {esc}: no atendidos", "Pedido", "Cliente", "Obra", "Motivo"])
        for n in p["no_atendidos"]:
            o = v["PD"][n["j"]]
            w.writerow(["", n["pedido_id"], o["cliente"], o["obra"], n["motivo"]])
    return Response("﻿" + salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=optimizacion_corrida_{cid}.csv"})


INDICADORES = [("atendidos", "Pedidos atendidos"), ("no_atendidos", "Pedidos no atendidos"),
               ("ingreso", "Ingreso por alquiler (S/)"), ("dm_vencido", "Días-máquina con mantenimiento vencido"),
               ("ped_riesgo", "Pedidos atendidos con equipo vencido"), ("costo_falla", "Costo esperado de falla en obra (S/)"),
               ("servicios", "Servicios ejecutados"), ("costo_mant", "Costo de mantenimiento (S/)"),
               ("venc_cierre", "Equipos vencidos al cierre"), ("deuda", "Deuda de mantenimiento al cierre (S/)"),
               ("traslado", "Costo de traslado (S/)"), ("margen", "Margen neto (S/)"),
               ("margen_deuda", "Margen neto descontada la deuda (S/)"), ("tecdias", "Técnico-días utilizados")]


@app.route("/exportar/<tabla>.csv")
def exportar(tabla):
    c = cfg()
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")
    if tabla == "equipos":
        w.writerow(["Código", "Categoría", "Marca", "Modelo", "Serie", "Año", "Altura trabajo (m)", "Capacidad (kg)",
                    "Situación", "Estado mantenimiento", "Último servicio", "Horómetro último servicio",
                    "Días restantes", "Horas restantes", "Vence", "Vence por", "Cliente", "Obra", "Vencimiento contrato"])
        for e in L.flota_con_estado(db(), c):
            m, a = e["mant"], e["alquiler"] or {}
            w.writerow([e["codigo"], e["categoria"], e["marca"], e["modelo"], e["serie"], e["anio"], e["altura_trabajo"],
                        e["capacidad_kg"], e["situacion"], m["estado"], m["fecha_pm"], m["horometro_pm"], m["dias_rest"],
                        m["horas_rest"], m["fecha_vence"], m["vence_por"], a.get("cliente"), a.get("obra"),
                        a.get("fecha_vencimiento")])
    elif tabla == "ordenes":
        w.writerow(["OT", "Equipo", "Tipo", "Nivel", "Estado", "Prioridad", "Programada", "Inicio", "Fin", "Horómetro",
                    "Técnicos", "Costo estimado", "Costo real", "Origen", "Descripción"])
        for o in ordenes_query():
            w.writerow([f_ot(o["id"]), o["codigo"], o["tipo"], o["nivel"], o["estado"], o["prioridad"],
                        o["fecha_programada"], o["fecha_inicio"], o["fecha_fin"], o["horometro"], o["tecnicos"],
                        o["costo_estimado"], o["costo_real"], o["origen"], o["descripcion"]])
    elif tabla == "alquileres":
        w.writerow(["Equipo", "Cliente", "Obra", "Zona", "Salida", "Vencimiento", "Retorno", "Horómetro salida",
                    "Horómetro retorno", "Tarifa S/ día", "Estado", "Mantenimiento a la salida", "Excepción", "Autoriza"])
        for a in db().execute("SELECT a.*, e.codigo FROM alquileres a JOIN equipos e ON e.id=a.equipo_id ORDER BY a.id"):
            w.writerow([a["codigo"], a["cliente"], a["obra"], a["zona"], a["fecha_salida"], a["fecha_vencimiento"],
                        a["fecha_retorno"], a["horometro_salida"], a["horometro_retorno"], a["tarifa_dia"], a["estado"],
                        a["mant_estado_salida"], a["excepcion_motivo"], a["excepcion_autoriza"]])
    elif tabla == "incidentes":
        w.writerow(["Fecha", "Equipo", "Horómetro", "Mantenimiento en la fecha", "Descripción", "Costo", "Días parado"])
        for i in db().execute("SELECT i.*, e.codigo FROM incidentes i JOIN equipos e ON e.id=i.equipo_id ORDER BY fecha"):
            w.writerow([i["fecha"], i["codigo"], i["horometro"], i["mant_estado"], i["descripcion"], i["costo"],
                        i["dias_parado"]])
    else:
        abort(404)
    return Response("﻿" + salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={tabla}_{date.today():%Y%m%d}.csv"})


@app.errorhandler(404)
def no_encontrado(_e):
    return render_template("error.html", mensaje="La página o el registro solicitado no existe."), 404


base.inicializar()
with base.conectar() as _con:     # base nueva: cargar el último estatus real; si no hay, simular hasta hoy
    _modo = _con.execute("SELECT 1 FROM config WHERE clave IN ('simulacion_hasta','estatus_fecha')").fetchone()
_con.close()
if not _modo:
    _libros = libros_estatus()
    if _libros:
        IE.cargar_real(_libros[0], archivo=_libros[0].name)
    else:
        with base.conectar() as _con:
            simulacion.simular(_con, date.today(), log=lambda *_: None)
            _con.commit()
        _con.close()
with base.conectar() as _con:
    if not _con.execute("SELECT 1 FROM config WHERE clave='documentos_fecha'").fetchone():
        try:   # primera vez: servicios y certificados de las carpetas de cada equipo
            ID.releer(_con, ID.DOCS_DIR)
        except (RuntimeError, OSError):
            pass
    O.marcar_interrumpidas(_con)
    _con.commit()
_con.close()

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    print(f"\n  SGM Lift Rental en http://127.0.0.1:{puerto}\n  Expediente de equipos: {DOCS_DIR}\n")
    app.run(host="127.0.0.1", port=puerto, debug=os.environ.get("SGM_DEBUG") == "1")
