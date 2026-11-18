"""Reglas de negocio: estado de mantenimiento, despacho, capacidad del taller y programación.

El estado de mantenimiento replica el «Tablero» del libro de datos (criterio del Plan de
Procesos de Mantenimiento Mntto-PPMTE-2017): el servicio vence al cumplir la frecuencia por
horas del equipo o el intervalo de calendario, lo que ocurra primero.
"""
import math
from datetime import date, datetime, timedelta

ESTADOS_ABIERTOS = ("Pendiente", "Programada", "En ejecución")
ZONAS = ["Lima Norte", "Lima Centro", "Lima Sur/Este", "Callao", "Provincia"]
CATEGORIAS = ["Tijera eléctrica", "Brazo articulado eléctrico", "Brazo articulado combustión",
              "Mástil vertical"]
NIVELES = ["A", "A+B", "A+B+C", "A+B+C+D"]
SITUACIONES = ["Disponible", "En obra", "En taller", "Fuera de servicio"]


# ---------------------------------------------------------------- utilidades de fecha
def a_fecha(v):
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def iso(d):
    return d.strftime("%Y-%m-%d") if d else None


def es_domingo(d):
    return d.weekday() == 6


def dias_habiles(inicio, n):
    """n días hábiles (lunes a sábado) a partir de inicio, inclusive."""
    d, out = a_fecha(inicio), []
    while len(out) < max(1, int(n or 1)):
        if not es_domingo(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- configuración
def leer_config(db):
    return {r["clave"]: r["valor"] for r in db.execute("SELECT clave, valor FROM config")}


def fecha_ref(cfg):
    return a_fecha(cfg.get("fecha_referencia")) or date.today()


# ---------------------------------------------------------------- estado de mantenimiento
def nivel_siguiente(horometro_ultimo, frecuencia):
    """Nivel del próximo servicio según los umbrales acumulados de la leyenda de los formatos
    (misma regla que el modelo del informe): toca el nivel más alto cuyo umbral se cruza entre el
    último servicio y el próximo.

    Tijeras y mástil (100 h): A cada 100 h, A+B a los 300, A+B+C a los 500, A+B+C+D a los 1 000.
    Brazos (250 h):           A cada 250 h, A+B a los 500, A+B+C a los 1 000, A+B+C+D a los 2 000.
    """
    if horometro_ultimo is None or not frecuencia:
        return "A"
    nxt = horometro_ultimo + frecuencia
    if frecuencia >= 250:
        reglas = [(2000, "A+B+C+D"), (1000, "A+B+C"), (500, "A+B")]
    else:
        reglas = [(1000, "A+B+C+D"), (500, "A+B+C"), (300, "A+B")]
    for k, nivel in reglas:
        if math.floor(nxt / k) > math.floor(horometro_ultimo / k):
            return nivel
    return "A"


def intervalo_calendario(categoria, cfg):
    clave = "intervalo_brazos" if categoria.startswith("Brazo") else "intervalo_tijeras"
    return int(float(cfg.get(clave) or 90))


FUENTE_INSPECCION = "Certificado de operatividad"
# Última inspección con certificado de operatividad posterior al preventivo (y no posterior a la referencia)
_SQL_INSP = f"""(SELECT MAX(l.fecha) FROM lecturas l WHERE l.equipo_id={{e}} AND l.fuente='{FUENTE_INSPECCION}'
                 AND l.fecha >= {{f}} AND l.fecha <= ?)"""


def calcular_estado(eq, pm, lectura, cfg, ref):
    """eq: fila de equipos; pm: (fecha, horometro[, fecha_insp]) del último preventivo; lectura: última lectura.

    Plazo de calendario: corre desde el último preventivo o, si la configuración lo indica
    (inspeccion_reinicia = 1), desde el último certificado de operatividad emitido después de él: la
    inspección al despachar renueva el plazo. Las horas siempre se cuentan desde el preventivo."""
    freq = float(eq["frecuencia_h"] or 100)
    tasa = float(eq["tasa_uso"] or 0)
    intervalo = intervalo_calendario(eq["categoria"], cfg)
    umbral_d = int(float(cfg.get("umbral_dias") or 15))
    umbral_f = float(cfg.get("umbral_fraccion") or 0.2)
    r = dict(estado="Sin datos", intervalo=intervalo, frecuencia=freq, fecha_pm=None,
             horometro_pm=None, dias_desde=None, dias_rest=None, horas_desde=None,
             horas_rest=None, vence_por=None, fecha_vence=None, horometro_est=None,
             proximo_h=None, nivel=None, fecha_lectura=None, fecha_inspeccion=None, base_calendario=None)
    if lectura:
        r["fecha_lectura"] = lectura["fecha"]
        r["horometro_est"] = lectura["horometro"]
    if not pm or not pm["fecha"]:
        return r

    f_pm = a_fecha(pm["fecha"])
    h_pm = float(pm["horometro"] or 0)
    if lectura and a_fecha(lectura["fecha"]) >= f_pm and lectura["horometro"] is not None:
        f_l, h_l = a_fecha(lectura["fecha"]), float(lectura["horometro"])
    else:
        f_l, h_l = f_pm, h_pm
    f_cal = f_pm
    insp = pm["fecha_insp"] if "fecha_insp" in pm.keys() else None
    if insp and str(cfg.get("inspeccion_reinicia", "1")) == "1" and f_pm < a_fecha(insp) <= ref:
        f_cal = a_fecha(insp)
        r["fecha_inspeccion"] = iso(f_cal)
    r["base_calendario"] = "Certificado de operatividad" if f_cal > f_pm else "Preventivo"
    dias_desde = (ref - f_cal).days
    horas_desde = (h_l - h_pm) + tasa * max(0, (ref - f_l).days)
    dias_rest = intervalo - dias_desde
    horas_rest = freq - horas_desde

    if dias_rest <= 0 or horas_rest <= 0:
        estado = "Vencido"
    elif dias_rest <= umbral_d or horas_rest <= umbral_f * freq:
        estado = "Por vencer"
    else:
        estado = "Vigente"

    vence_cal = f_cal + timedelta(days=intervalo)
    if tasa > 0:
        vence_h = ref + timedelta(days=math.floor(horas_rest / tasa))
        vence_por = "Calendario" if dias_rest <= horas_rest / tasa else "Horas"
        fecha_vence = min(vence_cal, vence_h)
    else:
        vence_por, fecha_vence = "Calendario", vence_cal

    r.update(estado=estado, fecha_pm=iso(f_pm), horometro_pm=h_pm, dias_desde=dias_desde,
             dias_rest=dias_rest, horas_desde=round(horas_desde, 1), horas_rest=round(horas_rest, 1),
             vence_por=vence_por, fecha_vence=iso(fecha_vence),
             horometro_est=round(h_l + tasa * max(0, (ref - f_l).days), 1),
             proximo_h=round(h_pm + freq, 1), nivel=nivel_siguiente(h_pm, freq))
    return r


def ultimo_pm(db, equipo_id, hasta=None):
    q = f"""SELECT COALESCE(fecha_fin, fecha_inicio) AS fecha, horometro,
           {_SQL_INSP.format(e='ordenes.equipo_id', f='COALESCE(ordenes.fecha_fin, ordenes.fecha_inicio)')} AS fecha_insp
           FROM ordenes WHERE equipo_id=? AND tipo='Preventivo' AND estado='Completada'"""
    args = [iso(hasta) if hasta else "9999-12-31", equipo_id]
    if hasta:
        q += " AND COALESCE(fecha_fin, fecha_inicio) <= ?"
        args.append(iso(hasta))
    q += " ORDER BY COALESCE(fecha_fin, fecha_inicio) DESC, id DESC LIMIT 1"
    return db.execute(q, args).fetchone()


def ultima_lectura(db, equipo_id, hasta=None):
    q = "SELECT fecha, horometro, fuente FROM lecturas WHERE equipo_id=?"
    args = [equipo_id]
    if hasta:
        q += " AND fecha <= ?"
        args.append(iso(hasta))
    q += " ORDER BY fecha DESC, horometro DESC LIMIT 1"
    return db.execute(q, args).fetchone()


def estado_equipo(db, eq, cfg, ref=None):
    ref = ref or fecha_ref(cfg)
    return calcular_estado(eq, ultimo_pm(db, eq["id"]), ultima_lectura(db, eq["id"]), cfg, ref)


def flota_con_estado(db, cfg, ref=None, solo_activos=True):
    """Lista de equipos (dict) con su estado de mantenimiento y su alquiler/orden abiertos."""
    ref = ref or fecha_ref(cfg)
    q = "SELECT * FROM equipos" + (" WHERE activo=1" if solo_activos else "") + " ORDER BY codigo"
    pms = {r["equipo_id"]: r for r in db.execute(
        f"""SELECT o.equipo_id, COALESCE(o.fecha_fin, o.fecha_inicio) AS fecha, o.horometro,
           {_SQL_INSP.format(e='o.equipo_id', f='COALESCE(o.fecha_fin, o.fecha_inicio)')} AS fecha_insp
           FROM ordenes o WHERE o.tipo='Preventivo' AND o.estado='Completada'
           ORDER BY COALESCE(o.fecha_fin, o.fecha_inicio), o.id""", (iso(ref),))}
    lects = {r["equipo_id"]: r for r in db.execute(
        "SELECT equipo_id, fecha, horometro FROM lecturas ORDER BY fecha, horometro")}
    alqs = {r["equipo_id"]: r for r in db.execute(
        "SELECT * FROM alquileres WHERE estado='En obra'")}
    ords = {r["equipo_id"]: r for r in db.execute(
        f"""SELECT * FROM ordenes WHERE estado IN {ESTADOS_ABIERTOS}
            ORDER BY COALESCE(fecha_programada,'9999') DESC""")}
    out = []
    for eq in db.execute(q):
        d = dict(eq)
        d["mant"] = calcular_estado(eq, pms.get(eq["id"]), lects.get(eq["id"]), cfg, ref)
        d["alquiler"] = dict(alqs[eq["id"]]) if eq["id"] in alqs else None
        d["orden"] = dict(ords[eq["id"]]) if eq["id"] in ords else None
        if d["alquiler"] and d["alquiler"]["fecha_vencimiento"]:
            d["alquiler"]["contrato_vencido"] = a_fecha(d["alquiler"]["fecha_vencimiento"]) < ref
            d["alquiler"]["dias_atraso"] = (ref - a_fecha(d["alquiler"]["fecha_vencimiento"])).days
        out.append(d)
    return out


# ---------------------------------------------------------------- parámetros por nivel
def parametros_servicio(db, categoria, nivel):
    r = db.execute("SELECT * FROM parametros_nivel WHERE categoria=? AND nivel=?",
                   (categoria, nivel or "A")).fetchone()
    if r:
        return dict(duracion_dias=r["duracion_dias"], costo=r["costo"], tecnico_dias=r["tecnico_dias"])
    return dict(duracion_dias=1, costo=0, tecnico_dias=1)


def tarifa_sugerida(eq):
    """Tarifa diaria (S/) del libro de datos, según categoría y altura de trabajo."""
    h = eq["altura_trabajo"] or 0
    cat = eq["categoria"]
    if cat.startswith("Mástil"):
        return 50
    if cat.startswith("Brazo"):
        return 162.5 if h <= 15 else 187.5
    for tope, t in [(8, 65), (10, 75), (12, 105), (14.5, 137.5)]:
        if h <= tope:
            return t
    return 150


# ---------------------------------------------------------------- taller: carga y agenda
def dias_orden(o):
    inicio = o["fecha_inicio"] if o["estado"] == "En ejecución" and o["fecha_inicio"] else o["fecha_programada"]
    if not inicio:
        return []
    return dias_habiles(inicio, o["duracion_dias"] or 1)


def carga_taller(db, desde, dias, excluir_orden=None):
    """{fecha: {'carga': técnico-días, 'ordenes': [...]}} para Programada / En ejecución."""
    desde = a_fecha(desde)
    hasta = desde + timedelta(days=dias - 1)
    mapa = {desde + timedelta(days=i): dict(carga=0.0, ordenes=[]) for i in range(dias)}
    filas = db.execute(
        """SELECT o.*, e.codigo FROM ordenes o JOIN equipos e ON e.id=o.equipo_id
           WHERE o.estado IN ('Programada','En ejecución')""").fetchall()
    for o in filas:
        if excluir_orden and o["id"] == excluir_orden:
            continue
        ds = dias_orden(o)
        por_dia = float(o["tecnico_dias"] or 1) / max(1, len(ds))
        for d in ds:
            if desde <= d <= hasta:
                mapa[d]["carga"] += por_dia
                mapa[d]["ordenes"].append(o)
    return mapa


def tecnicos_de_orden(db, orden_id):
    return db.execute(
        """SELECT t.* FROM tecnicos t JOIN orden_tecnicos ot ON ot.tecnico_id=t.id
           WHERE ot.orden_id=? ORDER BY t.nombre""", (orden_id,)).fetchall()


def agenda_tecnicos(db, desde, dias):
    """{tecnico_id: {fecha: [ordenes]}} en el rango."""
    desde = a_fecha(desde)
    hasta = desde + timedelta(days=dias - 1)
    agenda = {}
    filas = db.execute(
        """SELECT o.*, e.codigo, ot.tecnico_id FROM ordenes o
           JOIN equipos e ON e.id=o.equipo_id JOIN orden_tecnicos ot ON ot.orden_id=o.id
           WHERE o.estado IN ('Programada','En ejecución')""").fetchall()
    for o in filas:
        for d in dias_orden(o):
            if desde <= d <= hasta:
                agenda.setdefault(o["tecnico_id"], {}).setdefault(d, []).append(o)
    return agenda


def verificar_programacion(db, cfg, fecha, duracion, tecnico_dias, tecnico_ids, excluir_orden=None):
    """Advertencias de capacidad del taller y de técnicos con otra orden el mismo día."""
    avisos = []
    if not fecha:
        return avisos
    cap = float(cfg.get("capacidad_tecnicos") or 3)
    ds = dias_habiles(fecha, duracion)
    if es_domingo(a_fecha(fecha)):
        avisos.append("La fecha elegida es domingo: el servicio comenzará el lunes siguiente.")
    carga = carga_taller(db, ds[0], (ds[-1] - ds[0]).days + 1, excluir_orden)
    por_dia = float(tecnico_dias or 1) / len(ds)
    for d in ds:
        total = carga[d]["carga"] + por_dia
        if total > cap + 1e-9:
            avisos.append(f"El {d.strftime('%d/%m')} el taller quedaría con {total:.1f} técnico-días "
                          f"(capacidad {cap:g}).")
    if tecnico_ids:
        agenda = agenda_tecnicos(db, ds[0], (ds[-1] - ds[0]).days + 1)
        nombres = {t["id"]: t["nombre"] for t in db.execute("SELECT id, nombre FROM tecnicos")}
        for tid in tecnico_ids:
            for d in ds:
                otras = [o for o in agenda.get(int(tid), {}).get(d, []) if o["id"] != excluir_orden]
                if otras:
                    avisos.append(f"{nombres.get(int(tid))} ya tiene la OT-{otras[0]['id']:04d} "
                                  f"({otras[0]['codigo']}) el {d.strftime('%d/%m')}.")
    return avisos


def programar_automatico(db, cfg, horizonte=45):
    """Programa, en la capacidad libre del taller, el servicio de los equipos en patio o taller
    vencidos o por vencer que no tienen orden abierta. Prioriza los de menos días restantes y
    asigna los técnicos menos cargados. Devuelve la lista de órdenes creadas."""
    ref = fecha_ref(cfg)
    cap = float(cfg.get("capacidad_tecnicos") or 3)
    tecnicos = [t["id"] for t in db.execute("SELECT id FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY id")]
    flota = flota_con_estado(db, cfg, ref)
    pendientes = {o["equipo_id"]: o for o in db.execute(
        "SELECT * FROM ordenes WHERE estado='Pendiente' AND tipo='Preventivo'")}
    candidatos = [e for e in flota
                  if e["situacion"] in ("Disponible", "En taller")
                  and e["mant"]["estado"] in ("Vencido", "Por vencer", "Sin datos")
                  and (e["orden"] is None or e["orden"]["estado"] == "Pendiente")]
    candidatos.sort(key=lambda e: (e["mant"]["dias_rest"] if e["mant"]["dias_rest"] is not None else -999))

    carga = carga_taller(db, ref, horizonte + 30)
    agenda = agenda_tecnicos(db, ref, horizonte + 30)
    ocupado = {(tid, d) for tid, dias in agenda.items() for d in dias}
    creadas = []
    for e in candidatos:
        nivel = e["mant"]["nivel"] or "A"
        p = parametros_servicio(db, e["categoria"], nivel)
        dur, td = int(p["duracion_dias"]), float(p["tecnico_dias"])
        por_dia = td / dur
        necesarios = max(1, math.ceil(por_dia - 1e-9))
        for k in range(horizonte):
            inicio = ref + timedelta(days=k)
            if es_domingo(inicio):
                continue
            ds = dias_habiles(inicio, dur)
            if any(d not in carga or carga[d]["carga"] + por_dia > cap + 1e-9 for d in ds):
                continue
            libres = [t for t in tecnicos if all((t, d) not in ocupado for d in ds)]
            if len(libres) < necesarios and tecnicos:
                continue
            libres.sort(key=lambda t: sum(1 for (tt, _) in ocupado if tt == t))
            asignados = libres[:necesarios]
            for d in ds:
                carga[d]["carga"] += por_dia
                for t in asignados:
                    ocupado.add((t, d))
            datos = dict(nivel=nivel, fecha_programada=iso(inicio), duracion_dias=dur,
                         tecnico_dias=td, costo_estimado=p["costo"])
            if e["id"] in pendientes:
                oid = pendientes[e["id"]]["id"]
                db.execute("""UPDATE ordenes SET estado='Programada', nivel=?, fecha_programada=?,
                              duracion_dias=?, tecnico_dias=?, costo_estimado=? WHERE id=?""",
                           (nivel, iso(inicio), dur, td, p["costo"], oid))
                db.execute("DELETE FROM orden_tecnicos WHERE orden_id=?", (oid,))
            else:
                cur = db.execute(
                    """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, prioridad, fecha_programada,
                       duracion_dias, tecnico_dias, costo_estimado, origen, descripcion)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (e["id"], "Preventivo", nivel, "Programada",
                     "Alta" if e["mant"]["estado"] == "Vencido" else "Normal", iso(inicio), dur, td,
                     p["costo"], "Programación automática",
                     f"Servicio {nivel} — mantenimiento {e['mant']['estado'].lower()} "
                     f"(vence por {(e['mant']['vence_por'] or '').lower()})"))
                oid = cur.lastrowid
            for t in asignados:
                db.execute("INSERT OR IGNORE INTO orden_tecnicos VALUES (?,?)", (oid, t))
            creadas.append(dict(id=oid, codigo=e["codigo"], fecha=inicio, nivel=nivel))
            break
    return creadas


# ---------------------------------------------------------------- despacho
def evaluar_despacho(eq_estado, fecha_salida, fecha_fin):
    """Clasifica un equipo para un contrato: 'apto', 'advertencia' o 'bloqueado'."""
    m = eq_estado["mant"]
    fs, ff = a_fecha(fecha_salida), a_fecha(fecha_fin)
    if m["estado"] == "Sin datos":
        return "bloqueado", "Sin mantenimiento documentado: el estado no puede verificarse."
    if m["estado"] == "Vencido":
        return "bloqueado", f"Mantenimiento vencido ({-m['dias_rest']} días de atraso por calendario)" \
            if m["dias_rest"] is not None and m["dias_rest"] <= 0 else "Mantenimiento vencido por horas."
    fv = a_fecha(m["fecha_vence"])
    if fv and fs and fv <= fs:
        return "bloqueado", f"El servicio vence el {fv.strftime('%d/%m/%Y')}, antes de la salida."
    if fv and ff and fv <= ff:
        return "advertencia", (f"El servicio vence el {fv.strftime('%d/%m/%Y')}, durante el contrato: "
                               "programe su retorno o un servicio en obra.")
    return "apto", f"Mantenimiento vigente hasta el {fv.strftime('%d/%m/%Y')}." if fv else "Vigente."


# ---------------------------------------------------------------- almacén en vivo
CORTO = {"Tijera eléctrica": "Tijera", "Brazo articulado eléctrico": "Brazo eléctrico",
         "Brazo articulado combustión": "Brazo diésel", "Mástil vertical": "Mástil"}


def retorno_estimado(alquiler, ref):
    """Fecha en que se espera el retorno de obra. Si el contrato ya venció y el equipo no volvió,
    se supone renovado por periodos de 30 días (supuesto del informe)."""
    venc = a_fecha(alquiler.get("fecha_vencimiento"))
    if venc is None:
        return ref + timedelta(days=30), True
    if venc >= ref:
        return venc, False
    k = 0
    while venc + timedelta(days=30 * k) < ref:
        k += 1
    return venc + timedelta(days=30 * k), True


def pronostico_ingresos(db, cfg, ref=None, dias=45):
    """Equipos sin orden abierta que entrarán a taller: los vencidos del patio (hoy), los que vencen
    en el patio (en su fecha) y los que retornan de obra vencidos (en su retorno). Cada ingreso se
    ubica en la capacidad libre del taller con un técnico propuesto. No se guarda: es un pronóstico."""
    ref = ref or fecha_ref(cfg)
    lim = ref + timedelta(days=dias)
    cap = float(cfg.get("capacidad_tecnicos") or 3)
    tecs = {t["id"]: t["nombre"] for t in db.execute("SELECT id, nombre FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY id")}
    items = []
    for e in flota_con_estado(db, cfg, ref):
        if e["orden"] or e["situacion"] == "Fuera de servicio":
            continue
        m = e["mant"]
        it = dict(equipo=e, retorno=None, retorno_supuesto=False)
        if e["situacion"] in ("Disponible", "En taller"):
            if m["estado"] in ("Vencido", "Sin datos"):
                it.update(fecha=ref, gravedad=0, motivo="En patio con el mantenimiento " +
                          ("sin registro" if m["estado"] == "Sin datos" else "vencido"))
            elif m["fecha_vence"] and a_fecha(m["fecha_vence"]) <= lim:
                it.update(fecha=max(ref, a_fecha(m["fecha_vence"])), gravedad=1,
                          motivo=f"En patio: el servicio vence por {(m['vence_por'] or '').lower()}")
            else:
                continue
            m_ing = m
        elif e["situacion"] == "En obra" and e["alquiler"]:
            ret, supuesto = retorno_estimado(e["alquiler"], ref)
            m_ing = calcular_estado(e, ultimo_pm(db, e["id"]), ultima_lectura(db, e["id"]), cfg, ret)
            it.update(retorno=ret, retorno_supuesto=supuesto)
            if m_ing["estado"] in ("Vencido", "Sin datos"):
                ya = m["estado"] in ("Vencido", "Sin datos")
                if ya:
                    motivo = "Opera en obra " + ("sin registro de servicio" if m["estado"] == "Sin datos"
                                                 else "con el mantenimiento vencido") + ": ingresa al retornar"
                else:
                    motivo = f"Vence en obra el {a_fecha(m['fecha_vence']):%d/%m}: ingresa al retornar"
                it.update(fecha=ret, gravedad=0 if ya else 1, motivo=motivo)
            elif m_ing["fecha_vence"] and a_fecha(m_ing["fecha_vence"]) <= lim:
                it.update(fecha=max(ret, a_fecha(m_ing["fecha_vence"])), gravedad=1,
                          motivo="Retorna de obra y vence en el patio")
            else:
                continue
        else:
            continue
        nivel = m_ing["nivel"] or "A"
        p = parametros_servicio(db, e["categoria"], nivel)
        it.update(nivel=nivel, duracion=int(p["duracion_dias"]), tecnico_dias=float(p["tecnico_dias"]),
                  costo=p["costo"])
        items.append(it)
    items.sort(key=lambda i: (i["fecha"], i["gravedad"], i["equipo"]["codigo"]))

    # ubicación en la capacidad libre del taller (respeta lo ya programado)
    agenda = agenda_tecnicos(db, ref, dias + 60)
    carga = carga_taller(db, ref, dias + 60)
    ocupado = {(tid, d) for tid, ds in agenda.items() for d in ds}
    for it in items:
        dur, td = it["duracion"], it["tecnico_dias"]
        por_dia = td / dur
        nec = max(1, math.ceil(por_dia - 1e-9))
        it.update(inicio=None, fin=None, tecnicos=[], espera=None)
        for k in range(dias + 40):
            ini = it["fecha"] + timedelta(days=k)
            if es_domingo(ini):
                continue
            ds = dias_habiles(ini, dur)
            if any(d not in carga or carga[d]["carga"] + por_dia > cap + 1e-9 for d in ds):
                continue
            libres = [t for t in tecs if all((t, d) not in ocupado for d in ds)]
            if len(libres) < nec:
                continue
            libres.sort(key=lambda t: sum(1 for (tt, _) in ocupado if tt == t))
            asig = libres[:nec]
            for d in ds:
                carga[d]["carga"] += por_dia
                for t in asig:
                    ocupado.add((t, d))
            it.update(inicio=ini, fin=ds[-1], tecnicos=[(t, tecs[t]) for t in asig],
                      espera=(ini - it["fecha"]).days)
            break
    return items


def almacen_vivo(db, cfg, ref=None):
    """Foto del almacén: patio, taller, obras, fuera de servicio, próximos ingresos y personal."""
    ref = ref or fecha_ref(cfg)
    flota = flota_con_estado(db, cfg, ref)
    por_id = {e["id"]: e for e in flota}
    abiertas = [dict(o) for o in db.execute(
        """SELECT o.*, e.codigo, e.categoria, e.situacion,
                  (SELECT group_concat(t.nombre, ', ') FROM orden_tecnicos ot JOIN tecnicos t ON t.id=ot.tecnico_id
                    WHERE ot.orden_id=o.id) AS tecnicos
           FROM ordenes o JOIN equipos e ON e.id=o.equipo_id
           WHERE o.estado IN ('Pendiente','Programada','En ejecución')
           ORDER BY o.estado='En ejecución' DESC, COALESCE(o.fecha_programada,'9999'), o.id""")]
    for o in abiertas:
        ds = dias_orden(o)
        o["fin_previsto"] = iso(ds[-1]) if ds else None
        eq = por_id.get(o["equipo_id"]) or {}
        o["en_obra"] = eq.get("situacion") == "En obra"
        o["alquiler"] = eq.get("alquiler")
    en_ejec = [o for o in abiertas if o["estado"] == "En ejecución"]

    no_apto = lambda e: e["mant"]["estado"] in ("Vencido", "Sin datos")
    patio = [e for e in flota if e["situacion"] == "Disponible"]
    patio_ok = sorted([e for e in patio if not no_apto(e)], key=lambda e: (e["categoria"], e["altura_trabajo"] or 0))
    patio_no = sorted([e for e in patio if no_apto(e)], key=lambda e: (e["categoria"], e["codigo"]))
    taller = [e for e in flota if e["situacion"] == "En taller"]
    fuera = [e for e in flota if e["situacion"] == "Fuera de servicio"]
    obra = [e for e in flota if e["situacion"] == "En obra"]
    for e in obra:
        if e["alquiler"]:
            e["retorno"], e["retorno_supuesto"] = retorno_estimado(e["alquiler"], ref)
        else:
            e["retorno"], e["retorno_supuesto"] = None, True
    zonas = {}
    for e in sorted(obra, key=lambda e: ((e["alquiler"] or {}).get("zona") or "Sin zona",
                                         (e["alquiler"] or {}).get("cliente") or "", e["codigo"])):
        zonas.setdefault((e["alquiler"] or {}).get("zona") or "Sin zona", []).append(e)
    atendidos_obra = {o["equipo_id"]: o for o in en_ejec if o["en_obra"]}

    pron = pronostico_ingresos(db, cfg, ref)
    proximos = []
    for o in abiertas:
        if o["estado"] == "En ejecución":
            continue
        eq = por_id.get(o["equipo_id"]) or {}
        proximos.append(dict(fecha=a_fecha(o["fecha_programada"]) or ref, llega=None, codigo=o["codigo"],
                             categoria=o["categoria"], tipo=o["tipo"], nivel=o["nivel"],
                             fuente="OT " + o["estado"].lower(), oid=o["id"], motivo=o["descripcion"],
                             tecnicos=o["tecnicos"], fin=a_fecha(o["fin_previsto"]), retorno=None,
                             retorno_supuesto=False, espera=None,
                             mant=(eq.get("mant") or {}).get("estado"), situacion=eq.get("situacion")))
    for it in pron:
        e = it["equipo"]
        proximos.append(dict(fecha=it["inicio"] or it["fecha"], llega=it["fecha"], codigo=e["codigo"],
                             categoria=e["categoria"], tipo="Preventivo", nivel=it["nivel"], fuente="Pronóstico",
                             oid=None, motivo=it["motivo"], retorno=it["retorno"],
                             retorno_supuesto=it["retorno_supuesto"],
                             tecnicos=", ".join(n for _, n in it["tecnicos"]) or None, fin=it["fin"],
                             espera=it["espera"], mant=e["mant"]["estado"], situacion=e["situacion"]))
    proximos.sort(key=lambda p: (p["fecha"] or date.max, p["codigo"]))

    # personal: hoy y los próximos 7 días (agenda real + asignaciones del pronóstico)
    agenda = agenda_tecnicos(db, ref, 8)
    pron_dias = {}
    for it in pron:
        if it["inicio"]:
            for d in dias_habiles(it["inicio"], it["duracion"]):
                for tid, _ in it["tecnicos"]:
                    pron_dias.setdefault(tid, {}).setdefault(d, []).append(it["equipo"]["codigo"])
    personal = []
    for t in db.execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY id"):
        ids_hoy = [o["id"] for o in agenda.get(t["id"], {}).get(ref, [])]
        ids_hoy += [o["id"] for o in en_ejec if t["nombre"] in (o["tecnicos"] or "").split(", ")
                    and o["id"] not in ids_hoy]
        hoy = [o for o in abiertas if o["id"] in ids_hoy]
        semana = []
        for k in range(8):
            d = ref + timedelta(days=k)
            if es_domingo(d):
                semana.append(dict(d=d, c="dom", t="Domingo: sin turno"))
            elif agenda.get(t["id"], {}).get(d):
                semana.append(dict(d=d, c="ocup", t="OT: " + ", ".join(o["codigo"] for o in agenda[t["id"]][d])))
            elif pron_dias.get(t["id"], {}).get(d):
                semana.append(dict(d=d, c="pron", t="Pronóstico: " + ", ".join(pron_dias[t["id"]][d])))
            else:
                semana.append(dict(d=d, c="libre", t="Libre"))
        prox = next((s for s in semana[1:] if s["c"] in ("ocup", "pron")), None)
        estado = "Descanso" if es_domingo(ref) else ("Trabajando" if hoy else "Libre")
        personal.append(dict(t=dict(t), hoy=hoy, semana=semana, proxima=prox, estado=estado))
    return dict(flota=flota, patio_ok=patio_ok, patio_no=patio_no, taller=taller, fuera=fuera, obra=obra,
                zonas=zonas, en_ejec=en_ejec, atendidos_obra=atendidos_obra, proximos=proximos,
                personal=personal, pronostico=pron)


# ---------------------------------------------------------------- expediente y plan de tareas
_DATOS = __import__("pathlib").Path(__file__).resolve().parent / "datos"
_cache = {}


def _json(nombre):
    if nombre not in _cache:
        import json
        p = _DATOS / nombre
        _cache[nombre] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _cache[nombre]


def expediente(codigo):
    """Lectura verificada del expediente del equipo (datos/expedientes.json) o None."""
    return _json("expedientes.json").get(codigo)


def formato_plan(categoria):
    """Formato del Plan de Procesos de Mantenimiento que corresponde a la categoría."""
    for f in _json("catalogo_tareas.json").get("formatos", []):
        if categoria in f.get("aplica_a", []):
            return f
    return None


def tareas_plan(categoria, nivel):
    """[(lista, [tareas])] del formato del plan para el nivel (A+B → listas A y B)."""
    f = formato_plan(categoria)
    if not f or not nivel:
        return []
    return [(k, f["listas"].get(k, [])) for k in nivel.split("+") if f["listas"].get(k)]


def codigos_tareas(categoria, nivel):
    return [t["codigo"] for _, ts in tareas_plan(categoria, nivel) for t in ts]
