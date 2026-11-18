"""Importa el «Estatus de los equipos» (libro Excel diario de operaciones) al sistema.

El libro trae una hoja por día («15-11», «16-11», «17-11», …) con una fila por equipo: cliente, obra,
fecha de salida, vencimiento, horómetro, accesorios y notas. Sólo se usan esas hojas diarias (la hoja
MOVILIZACIONES, si viene, no se lee). Se aplican en orden de fecha, a partir del último estatus cargado
(la primera vez, desde el registro del 22/09/2026 con el que se armó el sistema):

  * TALLER / DISPONIBLE   → el contrato abierto se cierra. Con estatus de días seguidos, el retorno es el
                             día del estatus que ya no lo muestra en obra; si hay un salto de varios días,
                             se estima con la regla del informe (renovación por periodos de 30 días)
                             dentro de esa ventana. «reparacion» → Fuera de servicio.
  * Cliente y obra        → si la salida coincide con el contrato abierto, se actualizan vencimiento y
                             obra (traslados); si no, se cierra el anterior y se abre el nuevo. Sin
                             vencimiento: salida + 30 días (supuesto del informe), o los días indicados.
  * VENDIDO               → el equipo sale de la flota activa (se conserva su historia).
  * Horómetro             → sólo se registra si supera la última lectura y no es un valor repetido en
                             varios equipos (copiado); si no, queda como observación.

Además se cruzan los estatus consecutivos entre sí (cruce_dias): equipos que una hoja muestra en el
taller cuando la siguiente dice que ya habían salido, contratos que desaparecen y vuelven, filas con datos
de un contrato anterior, columnas que cambian de un día a otro y datos que faltan en el último estatus.

Nada se inventa: lo que no figura en el libro (por ejemplo, los mantenimientos hechos desde el 22/09)
no se registra, y cada supuesto queda anotado en el contrato y en el informe de la importación.
"""
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta

import openpyxl

import logica as L

CORTE = date(2026, 9, 22)          # registro de estatus con el que se armó la base
COD = re.compile(r"\b(TECL|BECL|BCCL|UPCL)\s*-?\s*0*(\d{1,3})\b", re.I)
NOMBRE_HOJA = re.compile(r"^\s*(\d{1,2})[-_/. ](\d{1,2})(?:[-_/. ](\d{2,4}))?\s*$")
DIAS = re.compile(r"(\d{1,3})\s*d[ií]as?", re.I)
DURACION_SUPUESTA = 30

# Zona de la obra (palabras completas; el orden importa: provincia primero)
ZONAS = [
    ("Provincia", ["TRUJILLO", "CHICLAYO", "CHIMBOTE", "PUCALLPA", "ICA", "PISCO", "HUANUCO", "HUÁNUCO",
                   "AREQUIPA", "PIURA", "CUSCO", "CARTAVIO", "HUARAL", "HUACHO", "CAÑETE"]),
    ("Callao", ["CALLAO", "AEROPUERTO", "VENTANILLA", "PRODAC", "UNIMAR"]),
    ("Lima Norte", ["MEGA PLAZA", "MEGAPLAZA", "TRAPICHE", "CARABAYLLO", "COMAS", "INDEPENDENCIA",
                    "LOS OLIVOS", "PUENTE PIEDRA", "ANCON", "ANCÓN", "SAN MARTIN DE PORRES"]),
    ("Lima Sur/Este", ["LURIN", "LURÍN", "VILLA EL SALVADOR", "VES", "LIMA SUR", "SURCO", "JOCKEY", "ATE",
                       "SANTA ANITA", "HUACHIPA", "SANTA MATEO", "SAN MATEO", "SANTA CLARA", "KLO",
                       "CHORRILLOS", "PUNTA HERMOSA", "PUNTA NEGRA", "ÑAÑA", "CHOSICA", "LA MOLINA",
                       "SAN JUAN DE MIRAFLORES", "PACHACAMAC", "CARRETERA CENTRAL"]),
    ("Lima Centro", ["METRO", "CERCADO", "SAN LUIS", "MARINA", "WESTIN", "YUNGAY", "CENTRO CIVICO",
                     "CENTRO CÍVICO", "DONOFRIO", "D'ONOFRIO", "CLINICA INTERNACIONAL", "SAN ISIDRO",
                     "MIRAFLORES", "SAN BORJA", "LINCE", "JESUS MARIA", "BREÑA", "LA VICTORIA",
                     "SAN MIGUEL", "PUEBLO LIBRE", "MAGDALENA", "SURQUILLO"]),
]
_ZONA_RE = [(z, re.compile(r"\b(" + "|".join(re.escape(k) for k in ks) + r")\b")) for z, ks in ZONAS]
VACIAS = {"STOP", "SAC", "S.A.C", "S.A.C.", "EIRL", "E.I.R.L", "E.I.R.L.", "SRL", "S.R.L", "SA", "PERU",
          "PERÚ", "LIMA", "INGENIEROS", "INGENIERIA", "SERVICIOS", "GENERALES", "GRUPO", "CORPORACION",
          "CORPORACIÓN", "CONTRATISTAS", "PROYECTOS", "EQUIPAMIENTO", "ALMACEN", "ALMACÉN", "CENTRO",
          "COMERCIAL", "PLANTA", "MULTISERVICIOS", "SOLUCIONES"}


def zona(obra, cliente=""):
    s = f"{obra or ''} {cliente or ''}".upper()
    for z, rx in _ZONA_RE:
        if rx.search(s):
            return z
    return "Lima Centro"


def codigo(v):
    m = COD.search(str(v or ""))
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}" if m else None


def a_fecha(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None


def a_num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def texto(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def palabras(s):
    return {w for w in re.findall(r"[A-ZÁÉÍÓÚÑ0-9&]{3,}", (s or "").upper()) if w not in VACIAS}


def fecha_hoja(nombre, hoy):
    m = NOMBRE_HOJA.match(nombre)
    if not m:
        return None
    d, mes = int(m.group(1)), int(m.group(2))
    anio = int(m.group(3)) if m.group(3) else hoy.year
    if anio < 100:
        anio += 2000
    try:
        f = date(anio, mes, d)
    except ValueError:
        return None
    if not m.group(3) and f > hoy + timedelta(days=7):
        f = date(anio - 1, mes, d)
    return f


# ---------------------------------------------------------------- lectura del libro
def leer_hoja(ws):
    """Filas de una hoja de estatus: dict por equipo. Detecta las columnas por su encabezado."""
    filas = list(ws.iter_rows(values_only=True))
    cab_i = next((i for i, r in enumerate(filas[:10]) if any(texto(c).upper() == "CODIGO" for c in r)), None)
    if cab_i is None:
        return None
    cab = [texto(c).upper() for c in filas[cab_i]]

    def col(*nombres):
        for n in nombres:
            for i, c in enumerate(cab):
                if c.startswith(n):
                    return i
        return None

    c_cod, c_obra = col("CODIGO"), col("OBRA")
    c = dict(marca=col("MARCA"), modelo=col("MODELO"), serie=col("SERIE"), salida=col("FECHA SALIDA", "SALIDA"),
             venc=col("VENCIMIENTO"), horo=col("HOROMETRO"), acc=col("ACCESORIO"))
    c_cli = c_obra - 1 if c_obra else None
    sin_titulo = [i for i in range(1, c_cod or 0) if not cab[i] and i != c_cli]   # columnas sin encabezado
    out, terceros, dup = {}, [], []
    for r in filas[cab_i + 1:]:
        g = lambda i: r[i] if i is not None and i < len(r) else None
        cod = codigo(g(c_cod))
        marca = texto(g(c["marca"]))
        if not cod and marca.upper() != "TERCERO":
            continue
        nota_i = (c["acc"] + 1) if c["acc"] is not None else None
        f = dict(codigo=cod, id_celda=texto(g(c_cod)), marca=marca, modelo=texto(g(c["modelo"])), serie=texto(g(c["serie"])),
                 cliente=texto(g(c_cli)), obra=texto(g(c_obra)), salida_raw=g(c["salida"]), venc_raw=g(c["venc"]),
                 horo_raw=g(c["horo"]), accesorios=texto(g(c["acc"])), nota=texto(g(nota_i)),
                 extra=[g(i) for i in sin_titulo])
        f["salida"], f["venc"] = a_fecha(f["salida_raw"]), a_fecha(f["venc_raw"])
        obs = [texto(x) for x in (f["salida_raw"], f["venc_raw"], f["horo_raw"])
               if isinstance(x, str) and texto(x) not in ("", "--", "---", "----")
               and not a_fecha(x) and a_num(x) is None]
        if f["nota"]:
            obs.append(f["nota"])
        f["obs"] = [o for o in obs if o.upper() not in ("VENDIDO",)]
        f["horo"] = a_num(f["horo_raw"])
        m = next((DIAS.search(o) for o in f["obs"] if DIAS.search(o)), None)
        f["duracion"] = int(m.group(1)) if m else None
        cli, obra = f["cliente"].upper(), f["obra"].upper()
        if "VENDIDO" in cli or "VENDIDO" in obra:
            f["estado"] = "Vendido"
        elif not cli or cli.startswith("TALLER") or obra == "DISPONIBLE" or cli.startswith("SE RECOGIO"):
            f["estado"] = "Taller"
        else:
            f["estado"] = "Obra"
        f["reparacion"] = any("REPARAC" in o.upper() for o in f["obs"])
        if marca.upper() == "TERCERO":              # equipo subarrendado a un tercero, no es flota propia
            terceros.append(f)
            continue
        if cod in out:
            dup.append(cod)
        out[cod] = f
    return dict(filas=out, terceros=terceros, duplicados=dup, columnas_sin_titulo=len(sin_titulo))


def leer_libro(ruta, hoy=None):
    hoy = hoy or date.today()
    wb = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
    hojas, ignoradas = [], []
    for ws in wb.worksheets:
        f = fecha_hoja(ws.title, hoy)
        if not f:                       # MOVILIZACIONES, hojas de terceros, resúmenes: no se usan
            ignoradas.append(ws.title)
            continue
        h = leer_hoja(ws)
        if h:
            hojas.append((f, ws.title, h))
    wb.close()
    hojas.sort(key=lambda x: x[0])
    return dict(hojas=hojas, ignoradas=ignoradas)


# ---------------------------------------------------------------- aplicación al sistema
class Informe:
    def __init__(self):
        self.d = {k: [] for k in ("nuevos", "cerrados", "actualizados", "reabiertos", "transferidos", "vendidos",
                                  "situacion", "lecturas", "alertas", "supuestos", "coherencia", "lagunas", "vencidos")}

    def add(self, k, cod, msg, fecha=None, **extra):
        self.d[k].append(dict(equipo=cod, texto=msg, fecha=L.iso(fecha) if fecha else None, **extra))


def _nota(con, alq_id, msg):
    con.execute("UPDATE alquileres SET notas=COALESCE(notas||' · ','')||? WHERE id=?", (msg, alq_id))


def _estado_mant(con, eq, cfg, d):
    return L.calcular_estado(eq, L.ultimo_pm(con, eq["id"], d), L.ultima_lectura(con, eq["id"], d), cfg, d)["estado"]


def _fecha_retorno(a, lo, hi):
    """Retorno de un contrato que ya no figura en obra, dentro de la ventana [lo, hi] entre dos estatus
    (con estatus de días seguidos la ventana es un solo día). Si la ventana es larga, regla del informe:
    el contrato se renueva por periodos de 30 días."""
    if lo >= hi:
        return hi, "exacta"
    venc = L.a_fecha(a["fecha_vencimiento"])
    if venc is None:
        return hi, "estimada"
    r = venc
    while r < lo:
        r += timedelta(days=30)
    return min(r, hi), "estimada"


def aplicar_hoja(con, cfg, fecha, nombre, hoja, previa, inf, ultima=True):
    """Aplica el estatus de un día. previa: fecha del estatus anterior (ventana de los retornos)."""
    eqs = {r["codigo"]: dict(r) for r in con.execute("SELECT * FROM equipos")}
    por_id = {e["id"]: e for e in eqs.values()}
    abiertos = {r["equipo_id"]: dict(r) for r in con.execute("SELECT * FROM alquileres WHERE estado='En obra'")}
    filas = hoja["filas"]
    fuente = f"Estatus {fecha:%d/%m/%Y}"
    cerrados_aqui = getattr(inf, "cerrados_ids", {})
    inf.cerrados_ids = cerrados_aqui

    def cerrar(e, a, hi, motivo):
        lo = max(previa + timedelta(days=1), L.a_fecha(a["fecha_salida"]))
        if hi < lo:      # el contrato nuevo salió antes de la ventana: se cierra el día de esa salida
            lo = hi
        ret, como = _fecha_retorno(a, lo, hi)
        ret = max(ret, L.a_fecha(a["fecha_salida"]))
        con.execute("UPDATE alquileres SET estado='Cerrado', fecha_retorno=? WHERE id=?", (L.iso(ret), a["id"]))
        txt = (f"Retorno el {ret:%d/%m/%Y}: el {fuente.lower()} ya no lo muestra en obra" if como == "exacta" else
               f"Retorno estimado el {ret:%d/%m/%Y}: el {fuente.lower()} ya no lo muestra en obra y entre "
               f"los estatus del {lo - timedelta(days=1):%d/%m} y del {fecha:%d/%m} no hay registro del día exacto")
        _nota(con, a["id"], f"{txt} ({motivo})")
        cerrados_aqui[a["id"]] = (e["id"], a["fecha_salida"], a["cliente"])
        inf.add("cerrados", e["codigo"], f"{a['cliente']} · {a['obra']} (salió el {L.a_fecha(a['fecha_salida']):%d/%m/%Y}): "
                + txt.lower()[0] + txt[1:], ret, alquiler=a["id"])
        abiertos.pop(e["id"], None)

    def situacion(e, nueva, detalle=None, motivo=""):
        if e["situacion"] != nueva or (detalle and e.get("ubicacion_detalle") != detalle):
            if e["situacion"] != nueva:
                inf.add("situacion", e["codigo"], f"{e['situacion']} → {nueva}{(' · ' + motivo) if motivo else ''}", fecha)
            con.execute("UPDATE equipos SET situacion=?, ubicacion_detalle=? WHERE id=?", (nueva, detalle, e["id"]))
            e["situacion"], e["ubicacion_detalle"] = nueva, detalle

    # Equipos que «salieron como» otro código: el contrato pasa al equipo que está físicamente en obra
    for cod, f in filas.items():
        for o in f["obs"]:
            m = re.search(r"COMO\s+(?:LA|EL)?\s*([A-Z]{4}\s*-?\s*\d{1,3})", o.upper())
            otro = codigo(m.group(1)) if m else None
            e, x = eqs.get(cod), eqs.get(otro)
            if not (e and x and f["estado"] == "Obra") or e["id"] in abiertos:
                continue
            a = abiertos.get(x["id"])
            if a and (not f["salida"] or L.a_fecha(a["fecha_salida"]) == f["salida"]):
                con.execute("UPDATE alquileres SET equipo_id=? WHERE id=?", (e["id"], a["id"]))
                _nota(con, a["id"], f"Figuraba como {otro}; el {fuente.lower()} aclara que el equipo en obra es "
                                    f"la {cod} («{o}»)")
                abiertos[e["id"]] = dict(a, equipo_id=e["id"])
                abiertos.pop(x["id"], None)
                if x["situacion"] == "En obra":
                    situacion(x, "Disponible", motivo=f"su contrato pasó a la {cod}")
                inf.add("transferidos", cod, f"Contrato {a['cliente']} · {a['obra']} pasa de la {otro} a la {cod} "
                                             f"(«{o}»)", fecha)

    rep = Counter(f["horo"] for f in filas.values() if f["horo"] is not None)
    for cod, f in filas.items():
        e = eqs.get(cod)
        if not e:
            if f["estado"] != "Vendido":
                inf.add("alertas", cod, f"Figura en el estatus ({f['marca']} {f['modelo']}, {f['cliente'] or 'sin cliente'}) "
                                        "pero no está registrado en el sistema: créelo en Equipos si es flota propia.", fecha)
            continue
        a = abiertos.get(e["id"])

        if f["estado"] == "Vendido":
            if a:
                cerrar(e, a, fecha, "equipo vendido")
            if e["activo"]:
                con.execute("""UPDATE equipos SET activo=0, situacion='Disponible', ubicacion_detalle=NULL,
                               notas=COALESCE(notas||' · ','')||? WHERE id=?""",
                            (f"Vendido según el {fuente.lower()}{(' (' + f['obra'] + ')') if f['obra'].upper() != 'VENDIDO' else ''}", e["id"]))
                e["activo"] = 0
                inf.add("vendidos", cod, f"Vendido{(' · ' + f['obra']) if f['obra'].upper() != 'VENDIDO' else ''}: "
                                         "sale de la flota activa (se conserva su historia)", fecha)
            continue

        if not e["activo"]:
            con.execute("UPDATE equipos SET activo=1 WHERE id=?", (e["id"],))
            e["activo"] = 1
            inf.add("alertas", cod, "Estaba dado de baja en el sistema y el estatus lo vuelve a listar: se reactivó.", fecha)

        if f["estado"] == "Taller":
            if a:
                cerrar(e, a, fecha, "devuelto al taller")
            if f["reparacion"]:
                situacion(e, "Fuera de servicio", f"En reparación ({fuente.lower()})", "en reparación")
            elif e["situacion"] in ("En obra", "Fuera de servicio"):
                situacion(e, "Disponible", motivo="en el patio del taller")
        else:   # en obra
            salida = f["salida"]
            dur = f["duracion"]
            supuestos = []
            if salida is None:
                salida = (f["venc"] - timedelta(days=dur or DURACION_SUPUESTA)) if f["venc"] else fecha
                supuestos.append(f"fecha de salida no indicada: se toma el {salida:%d/%m/%Y}"
                                 + (f" (vencimiento − {dur or DURACION_SUPUESTA} días)" if f["venc"] else " (fecha del estatus)"))
            venc = f["venc"]
            if venc is None or venc < salida:
                venc = salida + timedelta(days=dur or DURACION_SUPUESTA)
                supuestos.append(f"vencimiento no indicado: salida + {dur or DURACION_SUPUESTA} días"
                                 + ("" if dur else " (supuesto del informe)"))
            z = zona(f["obra"], f["cliente"])
            mismo = a and (L.a_fecha(a["fecha_salida"]) == salida)
            if mismo:
                cambios = []
                if L.a_fecha(a["fecha_vencimiento"]) != venc and f["venc"]:
                    cambios.append(f"vencimiento {L.a_fecha(a['fecha_vencimiento']):%d/%m/%Y} → {venc:%d/%m/%Y}"
                                   if a["fecha_vencimiento"] else f"vencimiento {venc:%d/%m/%Y}")
                    con.execute("UPDATE alquileres SET fecha_vencimiento=? WHERE id=?", (L.iso(venc), a["id"]))
                if not (palabras(a["obra"]) & palabras(f["obra"])) and f["obra"]:
                    traslado = next((o for o in f["obs"] if "TRASLADO" in o.upper()), None)
                    cambios.append(f"obra {a['obra']} → {f['obra']}" + (f" («{traslado}»)" if traslado else ""))
                    con.execute("UPDATE alquileres SET obra=?, zona=? WHERE id=?", (f["obra"], z, a["id"]))
                if cambios:
                    _nota(con, a["id"], f"{fuente}: " + "; ".join(cambios))
                    inf.add("actualizados", cod, f"{a['cliente']} · " + "; ".join(cambios), fecha)
                alq_id = a["id"]
            else:
                if a:
                    cerrar(e, a, min(salida, fecha), f"el equipo salió a {f['cliente']}")
                re_id = next((i for i, (eid, sal, cli) in cerrados_aqui.items()
                              if eid == e["id"] and L.a_fecha(sal) == salida and palabras(cli) & palabras(f["cliente"])), None)
                if re_id:
                    con.execute("UPDATE alquileres SET estado='En obra', fecha_retorno=NULL WHERE id=?", (re_id,))
                    _nota(con, re_id, f"Reabierto: el {fuente.lower()} lo vuelve a mostrar en obra")
                    cerrados_aqui.pop(re_id)
                    inf.d["cerrados"] = [c for c in inf.d["cerrados"] if c.get("alquiler") != re_id]
                    inf.add("reabiertos", cod, f"{f['cliente']} · {f['obra']}: un estatus anterior lo mostraba en el taller; "
                                               f"el {fuente.lower()} lo vuelve a mostrar en obra (se toma como error de digitación).", fecha)
                    alq_id = re_id
                else:
                    notas = [f"Importado del {fuente.lower()}"]
                    if f["accesorios"]:
                        notas.append(f"Accesorios: {f['accesorios']}")
                    notas += [f"Nota del estatus: {o}" for o in f["obs"] if not DIAS.fullmatch(o.strip())]
                    notas += [s[0].upper() + s[1:] for s in supuestos]
                    mant = _estado_mant(con, e, cfg, salida)
                    cur = con.execute(
                        """INSERT INTO alquileres(equipo_id, cliente, obra, zona, fecha_salida, fecha_vencimiento,
                           estado, mant_estado_salida, notas) VALUES (?,?,?,?,?,?,'En obra',?,?)""",
                        (e["id"], f["cliente"], f["obra"], z, L.iso(salida), L.iso(venc), mant, " · ".join(notas)))
                    alq_id = cur.lastrowid
                    inf.add("nuevos", cod, f"{f['cliente']} · {f['obra']} ({z}) del {salida:%d/%m/%Y} al {venc:%d/%m/%Y}"
                                           f" · mantenimiento {mant.lower()} a la salida", salida)
                    for s in supuestos:
                        inf.add("supuestos", cod, s[0].upper() + s[1:], fecha)
                abiertos[e["id"]] = dict(id=alq_id)
            situacion(e, "En obra", f["obra"])
            for o in (f["obs"] if ultima else []):
                if not DIAS.fullmatch(o.strip()) and "COMO" not in o.upper() and "TRASLADO" not in o.upper():
                    inf.add("alertas", cod, f"Nota del estatus: «{o}» ({f['cliente']} · {f['obra']}).", fecha)
            atraso = (fecha - venc).days
            if atraso > 0 and ultima:
                inf.add("vencidos", cod, f"{f['cliente']} · {f['obra']}: el contrato venció el {venc:%d/%m/%Y} "
                                         f"(hace {atraso} días) y el equipo sigue en obra. Confirmar prórroga o recojo.",
                        fecha, dias=atraso)

        # Horómetro informado
        h = f["horo"] if ultima else None
        if h is not None:
            ult = L.ultima_lectura(con, e["id"])
            if rep[h] >= 3:
                inf.add("lecturas", cod, f"No registrado: {h:g} h se repite en {rep[h]} equipos de la hoja (valor copiado).", fecha)
            elif ult and h <= float(ult["horometro"]):
                inf.add("lecturas", cod, f"No registrado: {h:g} h es menor o igual que la última lectura "
                                         f"({float(ult['horometro']):g} h del {L.a_fecha(ult['fecha']):%d/%m/%Y}, {ult['fuente']}).", fecha)
            elif ult and (h - float(ult["horometro"])) > 12 * max(1, (fecha - L.a_fecha(ult["fecha"])).days):
                inf.add("lecturas", cod, f"No registrado: {h:g} h implica más de 12 h de uso por día desde la última lectura.", fecha)
            else:
                con.execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                            (e["id"], L.iso(fecha), h, fuente))
                inf.add("lecturas", cod, f"Registrado: {h:g} h al {fecha:%d/%m/%Y}.", fecha)

        # Serie distinta a la del expediente (sólo si la del estatus parece un número de serie)
        s1 = re.sub(r"[^0-9A-Z]", "", f["serie"].upper()).lstrip("0")
        s2 = re.sub(r"[^0-9A-Z]", "", str(e.get("serie") or "").upper()).lstrip("0")
        if (ultima and len(s1) >= 5 and re.search(r"\d{4}", s1) and not re.search(r"\d\s*MT?$|^OK$", f["serie"].upper())
                and s2 and not (s1.endswith(s2) or s2.endswith(s1))):
            inf.add("alertas", cod, f"Serie en el estatus «{f['serie']}» distinta a la del sistema «{e['serie']}».", fecha)

    # Equipos activos que el estatus no lista
    for e in (por_id.values() if ultima else []):
        if e["activo"] and e["codigo"] not in filas:
            inf.add("alertas", e["codigo"], "Activo en el sistema pero no figura en el estatus.", fecha)
    for cod in hoja["duplicados"]:
        inf.add("alertas", cod, f"Aparece más de una vez en la hoja {nombre}; se tomó la última fila.", fecha)


SERIE_ALTURA = re.compile(r"\d\s*MT?$|^OK$|^\d+\s*M$")


def _serie(x):
    return re.findall(r"\d{4,}", x.get("id_celda") or "") or [x.get("id_celda") or "?"]


def cruce_dias(con, todas, desde, inf):
    """Incoherencias entre estatus consecutivos y lagunas de información del último.
    todas: hojas del libro hasta la última aplicada [(fecha, nombre, hoja)]; sólo se informa lo que
    involucra hojas posteriores a «desde»."""
    eqs = {r["codigo"]: dict(r) for r in con.execute("SELECT * FROM equipos")}
    nuevas = [x for x in todas if x[0] > desde]
    if not nuevas:
        return
    # 1. Obra → taller → obra con la misma salida (error de digitación en la hoja del medio)
    digitacion = set()
    for (fa, na, ha), (fb, nb, hb), (fc, nc, hc) in zip(todas, todas[1:], todas[2:]):
        if fc <= desde:
            continue
        for cod, b in hb["filas"].items():
            a, c = ha["filas"].get(cod), hc["filas"].get(cod)
            if a and c and b["estado"] == "Taller" and a["estado"] == c["estado"] == "Obra" and a["salida"] == c["salida"]:
                digitacion.add(cod)
                inf.add("coherencia", cod, f"La hoja {nb} lo muestra en el taller entre la {na} y la {nc}, que lo muestran "
                                           f"en {c['cliente']} · {c['obra']} con la misma salida ({c['salida']:%d/%m/%Y}): "
                                           f"error de digitación en la hoja {nb}; se mantuvo el contrato.", fc)
    # 2. Despachos que las hojas anteriores no reflejan: en taller en hojas posteriores a la salida
    atrasadas = {}
    for j, (f2, n2, h2) in enumerate(todas):
        if f2 <= desde:
            continue
        for cod, b in h2["filas"].items():
            if b["estado"] != "Obra" or not b["salida"] or cod in digitacion:
                continue
            ns = [n1 for f1, n1, h1 in todas[:j]
                  if f1 > b["salida"] and (h1["filas"].get(cod) or {}).get("estado") == "Taller"]
            if ns:
                atrasadas[(cod, b["salida"])] = (ns, n2, b)
    for (cod, sal), (ns, n2, b) in sorted(atrasadas.items()):
        varias = len(ns) > 1
        inf.add("coherencia", cod, f"La{'s' if varias else ''} hoja{'s' if varias else ''} {' y '.join(ns)} lo "
                                   f"muestra{'n' if varias else ''} en el taller, pero la {n2} indica que salió a "
                                   f"{b['cliente']} · {b['obra']} el {sal:%d/%m/%Y}: "
                                   f"{'esas hojas no estaban actualizadas' if varias else 'esa hoja no estaba actualizada'}.",
                todas[-1][0])
    # 3. Días consecutivos: equipos que faltan, contratos vencidos que desaparecen, cambios en un mismo contrato
    for (f1, n1, h1), (f2, n2, h2) in zip(todas, todas[1:]):
        if f2 <= desde:
            continue
        A, B = h1["filas"], h2["filas"]
        for cod in sorted(set(A) | set(B)):
            a, b = A.get(cod), B.get(cod)
            if not a or not b:
                inf.add("coherencia", cod, f"Figura en la hoja {n1 if a else n2} pero no en la {n2 if a else n1}.", f2)
                continue
            if cod in digitacion:
                continue
            if a["estado"] == "Obra" and (b["estado"] != "Obra" or b["salida"] != a["salida"]):
                ret = min(f2, b["salida"]) if b["estado"] == "Obra" and b["salida"] else f2
                if a["venc"] and a["venc"] < f1:
                    inf.add("lagunas", cod, f"{a['cliente']} · {a['obra']} siguió en la hoja {n1} con el contrato "
                                            f"vencido el {a['venc']:%d/%m/%Y} y desaparece en la {n2}: no se sabe qué "
                                            f"día volvió realmente (se registró el {ret:%d/%m/%Y}).", f2)
            if a["estado"] == b["estado"] == "Obra" and a["salida"] and a["salida"] == b["salida"]:
                if a["venc"] != b["venc"]:
                    inf.add("coherencia", cod, f"Mismo contrato (salida {a['salida']:%d/%m/%Y}) con vencimiento "
                                               f"{a['venc'] or 'vacío'} en la hoja {n1} y {b['venc'] or 'vacío'} en la {n2}.", f2)
                pa, pb = palabras(a["cliente"]), palabras(b["cliente"])
                if pa and pb and not (pa & pb):
                    inf.add("coherencia", cod, f"Mismo contrato (salida {a['salida']:%d/%m/%Y}) con cliente "
                                               f"«{a['cliente']}» en la hoja {n1} y «{b['cliente']}» en la {n2}.", f2)
    # 3. Filas con datos sobrantes (agrupadas por equipo)
    resto = {}
    for f, n, h in nuevas:
        for cod, x in h["filas"].items():
            if x["estado"] == "Taller" and (x["salida"] or x["venc"]):
                fechas = " → ".join(d.strftime("%d/%m/%Y") if d else "—" for d in (x["salida"], x["venc"]))
                resto.setdefault((cod, f"Está en el taller pero la fila conserva fechas de contrato ({fechas}): "
                                       "datos del contrato anterior sin limpiar"), []).append(n)
            if x["estado"] == "Vendido" and x["horo"] is not None:
                resto.setdefault((cod, f"Vendido, pero la fila trae horómetro {x['horo']:g}"), []).append(n)
    for (cod, msg), ns in resto.items():
        inf.add("coherencia", cod, f"{msg} (hoja{'s' if len(ns) > 1 else ''} {', '.join(ns)}).", nuevas[-1][0])
    # 4. Columnas que aparecen o desaparecen
    con_col = [(f, n, h) for f, n, h in nuevas if h.get("columnas_sin_titulo")]
    sin_col = [n for f, n, h in nuevas if not h.get("columnas_sin_titulo")]
    if con_col and sin_col:
        f, n, h = con_col[-1]
        anios = [(cod, x["extra"][0]) for cod, x in h["filas"].items() if x.get("extra") and x["extra"][0] is not None]
        es_anio = [(c, int(v)) for c, v in anios if isinstance(v, (int, float)) and 1980 <= v <= 2030]
        con_exp = [(c, v) for c, v in es_anio if (eqs.get(c) or {}).get("anio")]
        igual = sum(1 for c, v in con_exp if int(eqs[c]["anio"]) == v)
        raros = sorted({int(v) for c, v in anios if isinstance(v, (int, float)) and not 1980 <= v <= 2030})
        inf.add("coherencia", None, f"Las hojas {', '.join(n for _, n, _ in con_col)} traen una columna sin título "
                                    f"(entre MODELOS y CODIGO) que la hoja {', '.join(sin_col)} ya no tiene. Parece el año "
                                    f"de fabricación, pero sólo coincide con el expediente en {igual} de {len(con_exp)} "
                                    f"equipos; {len(anios) - len(es_anio)} valores no son años "
                                    f"({', '.join(map(str, raros))}) y {len(h['filas']) - len(anios)} equipos la dejan vacía. "
                                    "No se cargó.", f)
    # 5. Equipos de terceros (terceros)
    for i, (f, n, h) in enumerate(nuevas):
        T = h["terceros"]
        if not T:
            continue
        despues = [m for _, m, hh in nuevas[i + 1:] if not hh["terceros"]]
        inf.add("coherencia", None, f"La hoja {n} trae {len(T)} filas de equipos subarrendados a un tercero"
                                    + (f" que ya no aparecen en la{'s' if len(despues) > 1 else ''} {', '.join(despues)}" if despues else "")
                                    + ". No son flota propia y no se cargan.", f)
        grupos = {}
        for t in T:
            grupos.setdefault(_serie(t)[0], []).append(t)
        for sid, ts in grupos.items():
            nombre = ts[0]["id_celda"]
            if len(ts) > 1:
                inf.add("coherencia", None, f"El equipo de terceros «{nombre}» aparece {len(ts)} veces en la hoja {n}: "
                                            + "; ".join(f"{t['cliente']} · {t['obra']}" for t in ts) + ".", f)
            for t in ts:
                if t["codigo"]:
                    inf.add("coherencia", t["codigo"], f"Una fila de un equipo de terceros usa el código {t['codigo']} en la "
                                                       f"hoja {n} ({t['cliente']} · {t['obra']}), y ese código es flota propia.", f)
                for o in t["obs"]:
                    if not DIAS.fullmatch(o.strip()):
                        inf.add("coherencia", None, f"Equipo de terceros «{t['id_celda']}» ({t['cliente']} · {t['obra']}), "
                                                    f"hoja {n}: «{o}».", f)
                if t["salida"] and not t["cliente"].upper().startswith("SE RECOGIO"):
                    for cod, x in h["filas"].items():
                        if x["salida"] == t["salida"] and x["venc"] == t["venc"] and palabras(x["obra"]) & palabras(t["obra"]):
                            inf.add("coherencia", cod, f"El equipo de terceros «{t['id_celda']}» figura en la hoja {n} con el "
                                                       f"mismo contrato que la {cod} ({x['cliente']} · {x['obra']}, "
                                                       f"{x['salida']:%d/%m/%Y} → {x['venc']:%d/%m/%Y}).", f)
    # 6. Lagunas del último estatus
    f, n, h = todas[-1]
    for cod, x in sorted(h["filas"].items()):
        e = eqs.get(cod) or {}
        falta = []
        if x["estado"] == "Obra":
            if not x["salida"]:
                falta.append("fecha de salida")
            if not x["venc"]:
                falta.append("vencimiento" + (f" (dice «{texto(x['venc_raw'])}»)" if texto(x["venc_raw"]) else ""))
            if not x["accesorios"]:
                falta.append("accesorios entregados")
        if x["estado"] != "Vendido":
            if not x["serie"]:
                falta.append("serie" + (f" (el expediente dice {e['serie']})" if e.get("serie") else ""))
            elif SERIE_ALTURA.search(x["serie"].upper()):
                falta.append(f"serie (dice «{x['serie']}», que no es un número de serie"
                             + (f"; el expediente dice {e['serie']}" if e.get("serie") else "") + ")")
        if falta:
            inf.add("lagunas", cod, "Falta: " + ", ".join(falta) + ".", f)
        if e and x["marca"] and e.get("marca") and x["marca"].upper().split()[0] not in str(e["marca"]).upper():
            inf.add("coherencia", cod, f"Marca «{x['marca']}» en el estatus; el expediente dice «{e['marca']}».", f)
    con_h = sum(1 for x in h["filas"].values() if x["horo"] is not None)
    activos = sum(1 for x in h["filas"].values() if x["estado"] != "Vendido")
    inf.add("lagunas", None, f"Sólo {con_h} de {activos} equipos traen horómetro en la hoja {n}, y ninguno de esos valores "
                             "sirve (ver «Horómetros del libro»). Sin lecturas nuevas, las horas de uso se estiman con la "
                             "tasa de cada equipo.", f)


def importar(con, ruta, desde=None, hoy=None, archivo=None):
    """Aplica al sistema las hojas de estatus posteriores a «desde». Devuelve el informe (dict)."""
    hoy = hoy or date.today()
    libro = leer_libro(ruta, hoy)
    cfg = L.leer_config(con)
    desde = L.a_fecha(desde or cfg.get("estatus_fecha")) or CORTE
    hojas = [(f, n, h) for f, n, h in libro["hojas"] if desde < f <= hoy]
    if not hojas:
        raise ValueError(f"El libro no trae hojas de estatus posteriores al {desde:%d/%m/%Y} "
                         f"(hojas encontradas: {', '.join(n for _, n, _ in libro['hojas']) or 'ninguna'}).")
    inf = Informe()
    previa = desde
    for i, (f, nombre, h) in enumerate(hojas):
        aplicar_hoja(con, cfg, f, nombre, h, previa, inf, ultima=i == len(hojas) - 1)
        previa = f
    ultima = hojas[-1]
    cruce_dias(con, [x for x in libro["hojas"] if x[0] <= ultima[0]], desde, inf)
    terceros = [t for _, _, h in hojas for t in h["terceros"]]
    resumen = dict(
        archivo=archivo or str(ruta), fecha=L.iso(ultima[0]), desde=L.iso(desde), hojas=[n for _, n, _ in hojas],
        ignoradas=libro["ignoradas"], terceros=len(terceros),
        cuenta={k: len(v) for k, v in inf.d.items()}, detalle=inf.d,
        flota=dict(con.execute("""SELECT COUNT(*) total, SUM(situacion='En obra') obra, SUM(situacion='Disponible') patio,
                                  SUM(situacion='En taller') taller, SUM(situacion='Fuera de servicio') fuera
                                  FROM equipos WHERE activo=1""").fetchone()))
    for k, v in (("estatus_fecha", resumen["fecha"]), ("estatus_archivo", resumen["archivo"]),
                 ("estatus_informe", json.dumps(resumen, ensure_ascii=False, default=str))):
        con.execute("INSERT OR REPLACE INTO config(clave, valor) VALUES (?,?)", (k, v))
    return resumen


def cargar_real(ruta, archivo=None, hoy=None, semilla=20260922):
    """Primera carga (o recarga completa): reconstruye la base desde el registro real del 22/09/2026
    —sin la operación simulada— y aplica encima las hojas de estatus del libro. Conserva la
    configuración, el historial de corridas y los pedidos registrados a mano; regenera la cartera
    calibrada con los contratos reales."""
    import db as base
    import optimizador as O
    con = base.conectar()
    try:
        cfg_prev = {r["clave"]: r["valor"] for r in con.execute("SELECT * FROM config")}
        corridas = [dict(r) for r in con.execute("SELECT * FROM corridas")]
        pedidos = [dict(r) for r in con.execute("SELECT * FROM pedidos WHERE COALESCE(origen,'')<>'Cartera calibrada'")]
    finally:
        con.close()
    base.inicializar(forzar=True)
    con = base.conectar()
    try:
        for k, v in cfg_prev.items():
            if not (k.startswith("simulacion_") or k.startswith("estatus_") or k == "fecha_referencia"):
                con.execute("INSERT OR REPLACE INTO config(clave, valor) VALUES (?,?)", (k, v))
        for c in corridas:
            if c.get("estado") == "Terminada" and "operación simulada" not in (c.get("mensaje") or ""):
                c["mensaje"] = ((c.get("mensaje") or "") + " · Corrida sobre la operación simulada (antes de cargar el estatus real)").lstrip(" ·")
            con.execute(f"INSERT INTO corridas({','.join(c)}) VALUES ({','.join('?' * len(c))})", list(c.values()))
        for p in pedidos:
            p.update(equipo_id=None, corrida_id=None, alquiler_id=None,
                     estado="Pendiente" if p["estado"] in ("Asignado", "Despachado", "No atendido") else p["estado"])
            con.execute(f"INSERT INTO pedidos({','.join(p)}) VALUES ({','.join('?' * len(p))})", list(p.values()))
        r = importar(con, ruta, desde=CORTE, hoy=hoy, archivo=archivo)
        # Servicios y certificados posteriores al registro del 22/09 que están en las carpetas de cada equipo
        import importar_documentos as ID
        try:
            d = ID.releer(con, ID.DOCS_DIR, hoy)
            r["documentos"] = dict(servicios=d["nuevos_servicios"], lecturas=d["nuevas_lecturas"],
                                   observaciones=d["incoherencias"])
        except (RuntimeError, OSError):
            pass
        cfg = L.leer_config(con)
        try:
            n, lam = O.generar_cartera(con, cfg, L.a_fecha(r["fecha"]), 60, semilla, 1.0)
            r["cartera"] = dict(pedidos=n, tasa=lam)
            con.execute("UPDATE config SET valor=? WHERE clave='estatus_informe'", (json.dumps(r, ensure_ascii=False, default=str),))
        except RuntimeError:
            pass
        con.commit()
        return r
    finally:
        con.close()
