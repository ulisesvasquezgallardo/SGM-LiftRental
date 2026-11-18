"""Lectura de los documentos de mantenimiento de cada carpeta de equipo (expediente).

Cada carpeta (TECL001, BECL003, …) trae, además de pólizas, fichas técnicas y check lists:

  * el formato «Plan de Procesos de Mantenimiento» (Mntto-PPMTE-2017): FECHA, HOROMETRO, ejecutor,
    supervisor y «PROXIMO MANTTO HR». Es el registro del servicio preventivo.
  * el CERTIFICADO DE MANTENIMIENTO: «según fecha dd-mm-aaaa» y la tabla del programa (fecha, horómetro,
    frecuencia, próximo). Suele llevar el formato PM anexado.
  * el CERTIFICADO DE OPERATIVIDAD: se emite al despachar el equipo (fecha y horómetro de esa inspección).
    No es un servicio preventivo: se registra como lectura de horómetro.

Se leen también las subcarpetas duplicadas y los .zip; un mismo archivo repetido cuenta una vez. Los .docx
son los editables de los mismos certificados y no se leen. El texto se extrae con pdftotext (Poppler, viene
con Git para Windows); el resultado queda en caché en datos/documentos.json y sólo se vuelve a leer un
archivo si cambia.

aplicar() es idempotente: agrega a la base los servicios que aún no tiene (orden Preventivo Completada,
origen «Documento de carpeta») y las lecturas de los certificados de operatividad, y devuelve un informe
con el último servicio de cada equipo y lo que no cuadra entre documentos.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import logica as L

APP_DIR = Path(__file__).resolve().parent
CACHE = APP_DIR / "datos" / "documentos.json"
DOCS_DIR = Path(os.environ.get("SGM_DOCS_DIR", APP_DIR.parent.parent))   # carpetas TECL001, BECL003, …
ORIGEN = "Documento de carpeta"
FUENTE_CO = "Certificado de operatividad"
FUENTE_PM = "Mantenimiento (documento)"
CARPETA = re.compile(r"(TECL|BECL|BCCL|UPCL)\d{3}")
NO_MANTTO = re.compile(r"poliza|seguro|check ?list|especificac|ficha|serie-|catalog|boom lift|^ha\d|18m\.|e450|"
                       r"1930es|4069le|compact\d|ds-", re.I)
CODIGO = re.compile(r"\b(TECL|BECL|BCCL|UPCL)-?(\d{3})\b")


def _pdftotext():
    for c in (shutil.which("pdftotext"), r"C:\Program Files\Git\mingw64\bin\pdftotext.exe",
              r"C:\Program Files (x86)\Git\mingw64\bin\pdftotext.exe"):
        if c and Path(c).exists():
            return c
    return None


def _texto_pdf(data, exe):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as t:
        t.write(data)
        p = t.name
    try:
        r = subprocess.run([exe, "-layout", "-enc", "UTF-8", p, "-"], capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace")
    finally:
        os.unlink(p)


def _f(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else "—"


def fecha(s):
    m = re.search(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{2,4})", s or "")
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    y += 2000 if y < 100 else 0
    if mo > 12 and d <= 12:          # 05.20.2026 (mes y día invertidos)
        d, mo = mo, d
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _num(s):
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def _plan(t):
    """Campos del formato Plan de Procesos de Mantenimiento."""
    o = {}
    m = re.search(r"FECHA:\s*([^\n]*)", t)
    o["fecha"] = fecha(m.group(1)) if m else None
    m = re.search(r"HOROMETRO:\s*([\d.,]+)", t)
    o["horometro"] = _num(m.group(1)) if m else None
    m = re.search(r"HR:\s*([\d.,]+)", t)
    o["proximo_h"] = _num(m.group(1)) if m else None
    m = re.search(r"cada\s*(\d+)\s*hrs\.\s*A\b", t)
    o["frecuencia"] = int(m.group(1)) if m else None
    m = re.search(r"EJECUTOR:\s*(.+?)(?:\s{2,}|SUPRV|\n)", t)
    o["ejecutor"] = m.group(1).strip(" /") if m else None
    m = re.search(r"SUPRV\.\s*/\s*JEFE:\s*(.+?)(?:\s{2,}|\n)", t)
    o["supervisor"] = m.group(1).strip() if m else None
    m = re.search(r"CODIGO\s*/\s*SERIE:\s*(\S+)\s*/\s*(\S+)", t)
    o["codigo_doc"], o["serie"] = (m.group(1), m.group(2)) if m else (None, None)
    return o


def leer_documento(nombre, data, exe):
    t = _texto_pdf(data, exe)
    cab = t.upper()[:400]
    if "CERTIFICADO DE MANTENIMIENTO" in cab:
        o = dict(tipo="CM")
        m = re.search(r"seg[uú]n\s*fecha\s*([\d/.\-\s]{8,12})", t)
        o["fecha_texto"] = fecha(m.group(1)) if m else None
        m = re.search(r"Mntto\.?\s*Prev\.?\s+(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\s+([\d.,]+)\s+(\d+)\s*Hrs\s+([\d.,]+)?", t)
        if m:
            o.update(fecha_tabla=fecha(m.group(1)), horometro=_num(m.group(2)), frecuencia=int(m.group(3)),
                     proximo_h=_num(m.group(4)))
        if "Plan de Procesos de Mantenimiento" in t:
            o["plan"] = _plan(t)
        o["fecha"] = o.get("fecha_tabla") or o.get("fecha_texto")
    elif "CERTIFICADO DE OPERATIVIDAD" in cab:
        o = dict(tipo="CO")
        i = t.find("Fecha")
        resto = t[i:] if i >= 0 else t
        fs = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", resto)
        o["fecha"] = fecha(fs[0]) if fs else None
        o["horometro"] = None
        for ln in resto.splitlines():
            m = re.match(r"^\s*(?:\d{1,2}/\d{1,2}/\d{4}\s+)?(\d{2,6}(?:\.\d{1,2})?)\s*(?:$|\s{2,})", ln)
            if m:
                o["horometro"] = _num(m.group(1))
                break
    elif "Plan de Procesos de Mantenimiento" in t:
        o = dict(tipo="PM", **_plan(t))
    else:
        o = dict(tipo="otro")
    m = CODIGO.search(t)
    o["codigo_doc"] = o.get("codigo_doc") or (f"{m.group(1)}-{m.group(2)}" if m else None)
    o["fecha_archivo"] = fecha(Path(nombre).name)
    return o


def leer_carpetas(docs_dir, cache=CACHE):
    """{codigo: [documento, …]} leyendo cada carpeta de equipo (con subcarpetas y .zip)."""
    exe = _pdftotext()
    previo = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    por_hash = {d["hash"]: d for docs in previo.get("equipos", {}).values() for d in docs}
    if not exe and not por_hash:
        raise RuntimeError("No se encontró pdftotext (Poppler). Instale Git para Windows o Poppler.")
    out, leidos = {}, 0
    for carpeta in sorted(Path(docs_dir).iterdir()):
        if not (carpeta.is_dir() and CARPETA.fullmatch(carpeta.name)):
            continue
        cod = f"{carpeta.name[:4]}-{carpeta.name[4:]}"
        docs = {}

        def util(rel):
            nombre = Path(rel.replace("\\", "/")).name
            return nombre.lower().endswith(".pdf") and not NO_MANTTO.search(nombre)

        def agregar(rel, data):
            nombre = Path(rel.replace("\\", "/")).name
            h = hashlib.md5(data).hexdigest()
            if h in docs:
                docs[h]["rutas"].append(rel)
                return
            nonlocal leidos
            if h in por_hash:
                d = dict(por_hash[h], rutas=[rel])
            elif exe:
                d = dict(leer_documento(nombre, data, exe), hash=h, rutas=[rel])
                leidos += 1
            else:
                return
            docs[h] = d

        for p in sorted(carpeta.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(carpeta))
            if p.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(p) as z:
                        for n in z.namelist():
                            if not n.endswith("/") and util(n):
                                agregar(f"{p.name}: {n}", z.read(n))
                except zipfile.BadZipFile:
                    pass
            elif util(rel):
                agregar(rel, p.read_bytes())
        out[cod] = sorted(docs.values(), key=lambda d: (d["tipo"], d.get("fecha") or "", d["rutas"][0]))
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(dict(leido=date.today().isoformat(), carpeta=str(docs_dir), equipos=out),
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return out, leidos


def consolidar(cod, docs):
    """Servicios preventivos (fecha → horómetro, fuentes), inspecciones e incoherencias de un equipo."""
    serv, insp, inc = {}, {}, []
    corto = lambda d: d["rutas"][0].split(": ")[-1].split("\\")[-1]

    def servicio(f, h, fuente, d, **extra):
        if not f:
            return
        s = serv.setdefault(f, dict(fecha=f, horometro=h, fuentes=[], archivo=None, ejecutor=None,
                                    supervisor=None, proximo_h=None, frecuencia=None))
        s["fuentes"].append(fuente)
        if h is not None and s["horometro"] is not None and abs(h - s["horometro"]) > 1:
            inc.append(f"{fuente} {corto(d)}: horómetro {h:g} para el servicio del {_f(f)}; "
                       f"el otro documento dice {s['horometro']:g}.")
        if fuente == "Formato PM" or s["archivo"] is None:
            s["archivo"] = corto(d)
        s["horometro"] = s["horometro"] if s["horometro"] is not None else h
        for k, v in extra.items():
            s[k] = s[k] or v

    fechas_pm = {d["fecha"] for d in docs if d["tipo"] == "PM" and d.get("fecha")}
    orden = {"PM": 0, "CM": 1, "CO": 2}
    for d in sorted(docs, key=lambda d: orden.get(d["tipo"], 3)):
        if d.get("codigo_doc") and d["codigo_doc"].replace("-", "") != cod.replace("-", ""):
            inc.append(f"{corto(d)} está en la carpeta {cod} pero el documento es del {d['codigo_doc']}.")
        if d["tipo"] == "PM":
            servicio(d["fecha"], d["horometro"], "Formato PM", d, ejecutor=d.get("ejecutor"),
                     supervisor=d.get("supervisor"), proximo_h=d.get("proximo_h"), frecuencia=d.get("frecuencia"))
            if d.get("fecha_archivo") and d["fecha"] and d["fecha_archivo"] != d["fecha"]:
                inc.append(f"El archivo «{corto(d)}» lleva en el nombre el {_f(d['fecha_archivo'])} "
                           f"pero el formato dice FECHA {_f(d['fecha'])}.")
            if not d["fecha"]:
                inc.append(f"El formato «{corto(d)}» no tiene fecha legible.")
        elif d["tipo"] == "CM":
            p = d.get("plan") or {}
            # Fecha del servicio: la que coincide con un formato PM de la carpeta; si no, la del formato
            # anexado, la de la tabla del programa o la del texto, en ese orden.
            cand = [x for x in (p.get("fecha"), d.get("fecha_tabla"), d.get("fecha_texto")) if x]
            f_cm = next((x for x in cand if x in fechas_pm), cand[0] if cand else None)
            servicio(f_cm, d.get("horometro"), "Certificado de mantenimiento", d,
                     proximo_h=d.get("proximo_h"), frecuencia=d.get("frecuencia"),
                     ejecutor=p.get("ejecutor"), supervisor=p.get("supervisor"))
            if d.get("fecha_texto") and d.get("fecha_tabla") and d["fecha_texto"] != d["fecha_tabla"]:
                inc.append(f"El certificado de mantenimiento dice «según fecha {_f(d['fecha_texto'])}» y la tabla del "
                           f"programa dice {_f(d['fecha_tabla'])}; se toma el {_f(f_cm)}.")
            if p.get("fecha") and d.get("fecha_tabla") and p["fecha"] != d["fecha_tabla"] and                     d.get("fecha_texto") != p["fecha"]:
                inc.append(f"El certificado de mantenimiento es del {_f(d['fecha_tabla'])} pero el formato PM anexado "
                           f"es del {_f(p['fecha'])} (h {p.get('horometro') or '—'}): anexo desactualizado.")
        elif d["tipo"] == "CO" and d.get("fecha"):
            prev = insp.get(d["fecha"])
            if prev is None or (prev["horometro"] is None and d.get("horometro") is not None):
                insp[d["fecha"]] = dict(fecha=d["fecha"], horometro=d.get("horometro"), archivo=corto(d))
            if d.get("horometro") is None:
                inc.append(f"Certificado de operatividad del {_f(d['fecha'])} sin horómetro legible.")

    servicios = sorted(serv.values(), key=lambda s: s["fecha"])
    inspecciones = sorted(insp.values(), key=lambda s: s["fecha"])
    for s in servicios:
        if s["proximo_h"] and s["horometro"] is not None and s["frecuencia"] and \
                abs((s["proximo_h"] - s["horometro"]) - s["frecuencia"]) > 1:
            inc.append(f"Servicio del {_f(s['fecha'])}: la leyenda indica cada {s['frecuencia']} h pero "
                       f"el próximo está a {s['proximo_h'] - s['horometro']:g} h ({s['proximo_h']:g}).")
    # Horómetro que baja con el tiempo (servicios e inspecciones juntos)
    linea = sorted([(s["fecha"], s["horometro"], "servicio") for s in servicios if s["horometro"] is not None] +
                   [(i["fecha"], i["horometro"], "certificado de operatividad") for i in inspecciones
                    if i["horometro"] is not None])
    for (f0, h0, t0), (f1, h1, t1) in zip(linea, linea[1:]):
        if h1 < h0 - 1:
            inc.append(f"El horómetro baja de {h0:g} ({t0} del {_f(f0)}) a {h1:g} ({t1} del "
                       f"{_f(f1)})" + (": posible cambio de horómetro o error de digitación." if h1 < h0 * 0.5 else "."))
    if not servicios:
        inc.append("La carpeta no tiene formato PM ni certificado de mantenimiento con fecha.")
    return dict(servicios=servicios, inspecciones=inspecciones, incoherencias=list(dict.fromkeys(inc)))


def aplicar(con, docs_por_equipo, hoy=None):
    """Registra en la base los servicios e inspecciones de los documentos que aún no tiene."""
    hoy = hoy or date.today()
    cfg = L.leer_config(con)
    eqs = {r["codigo"]: r for r in con.execute("SELECT * FROM equipos")}
    tec = {r["nombre"].upper(): r["id"] for r in con.execute("SELECT id, nombre FROM tecnicos")}
    r = con.execute("SELECT MAX(id) FROM corridas WHERE aplicada IS NOT NULL").fetchone()
    ultima_corrida = r[0] if r else None
    inf = dict(fecha=hoy.isoformat(), equipos=[], nuevos_servicios=0, nuevas_lecturas=0, incoherencias=0)
    for cod in sorted(set(eqs) | set(docs_por_equipo)):
        eq = eqs.get(cod)
        c = consolidar(cod, docs_por_equipo.get(cod, []))
        fila = dict(codigo=cod, en_sistema=bool(eq), activo=bool(eq and eq["activo"]), nuevos=[],
                    incoherencias=c["incoherencias"], servicios=c["servicios"], inspecciones=c["inspecciones"])
        if cod not in docs_por_equipo:
            fila["incoherencias"] = ["El equipo está en el sistema pero no hay carpeta de documentos."]
        if not eq:
            fila["incoherencias"].append("Hay carpeta de documentos pero el equipo no está en el sistema.")
            inf["equipos"].append(fila)
            continue
        antes = L.ultimo_pm(con, eq["id"])
        fila["antes"] = dict(fecha=antes["fecha"], horometro=antes["horometro"]) if antes else None
        for s in c["servicios"]:
            if L.a_fecha(s["fecha"]) > hoy:
                fila["incoherencias"].append(f"Servicio con fecha futura ({_f(s['fecha'])}): no se registra.")
                continue
            ya = con.execute("""SELECT id FROM ordenes WHERE equipo_id=? AND tipo='Preventivo' AND estado='Completada'
                                AND COALESCE(fecha_fin, fecha_inicio)=?""", (eq["id"], s["fecha"])).fetchone()
            if ya:
                continue
            freq = float(eq["frecuencia_h"] or 100)
            nivel = L.nivel_siguiente(s["horometro"] - freq, freq) if s["horometro"] is not None else "A"
            obs = f"Archivo: {s['archivo']} · Fuentes: {', '.join(dict.fromkeys(s['fuentes']))}"
            if s["proximo_h"]:
                obs += f" · Próximo mantto. según el documento: {s['proximo_h']:g} h"
            cur = con.execute(
                """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, fecha_programada, fecha_inicio, fecha_fin,
                   horometro, origen, descripcion, observaciones, supervisor, duracion_dias, tecnico_dias)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1)""",
                (eq["id"], "Preventivo", nivel, "Completada", s["fecha"], s["fecha"], s["fecha"], s["horometro"],
                 ORIGEN, f"Servicio {nivel} documentado en la carpeta del equipo", obs, s.get("supervisor")))
            t = tec.get((s.get("ejecutor") or "").upper()) or \
                (tec.get("TÉCNICO A") if "TÉCNICO A" in (s.get("ejecutor") or "").upper() else None)
            if t:
                con.execute("INSERT INTO orden_tecnicos VALUES (?,?)", (cur.lastrowid, t))
            if s["horometro"] is not None and not con.execute(
                    "SELECT 1 FROM lecturas WHERE equipo_id=? AND fecha=? AND ABS(horometro-?)<0.05",
                    (eq["id"], s["fecha"], s["horometro"])).fetchone():
                con.execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                            (eq["id"], s["fecha"], s["horometro"], FUENTE_PM))
                inf["nuevas_lecturas"] += 1
            fila["nuevos"].append(s["fecha"])
            inf["nuevos_servicios"] += 1
        for i in c["inspecciones"]:
            if i["horometro"] is None or L.a_fecha(i["fecha"]) > hoy:
                continue
            if not con.execute("SELECT 1 FROM lecturas WHERE equipo_id=? AND fecha=? AND ABS(horometro-?)<0.05",
                               (eq["id"], i["fecha"], i["horometro"])).fetchone():
                con.execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                            (eq["id"], i["fecha"], i["horometro"], FUENTE_CO))
                inf["nuevas_lecturas"] += 1
        fila["de_carpeta"] = [r[0] for r in con.execute(
            """SELECT COALESCE(fecha_fin, fecha_inicio) FROM ordenes WHERE equipo_id=? AND origen=? ORDER BY 1""",
            (eq["id"], ORIGEN))]
        ult = c["servicios"][-1] if c["servicios"] else None
        if ult and ult.get("frecuencia") and eq["frecuencia_h"] and abs(ult["frecuencia"] - float(eq["frecuencia_h"])) > 1:
            fila["incoherencias"].append(f"El último formato indica servicio cada {ult['frecuencia']} h; el sistema usa "
                                         f"{float(eq['frecuencia_h']):g} h para este equipo.")
        if fila["antes"] and ult and fila["antes"]["fecha"] > ult["fecha"]:
            fila["incoherencias"].append(f"El sistema tiene un servicio del {_f(fila['antes']['fecha'])} "
                                         f"que no está en la carpeta (último documentado: {_f(ult['fecha'])}).")
        # Horómetro de un servicio menor que una lectura anterior ya registrada en el sistema
        for s in c["servicios"]:
            if s["horometro"] is None:
                continue
            r = con.execute("""SELECT fecha, horometro, fuente FROM lecturas WHERE equipo_id=? AND fecha<?
                               ORDER BY horometro DESC LIMIT 1""", (eq["id"], s["fecha"])).fetchone()
            if r and r["horometro"] > s["horometro"] + 1:
                fila["incoherencias"].append(
                    f"El servicio del {_f(s['fecha'])} marca {s['horometro']:g} h, menos que la lectura del {_f(r['fecha'])} "
                    f"({r['horometro']:g} h, {r['fuente'].lower()})" +
                    (": posible cambio de horómetro." if s["horometro"] < r["horometro"] * 0.5 else
                     ": revise la fecha o el horómetro del formato."))
        if ult and ult["horometro"]:
            otro = con.execute("""SELECT COALESCE(fecha_fin, fecha_inicio) AS f FROM ordenes WHERE equipo_id=?
                                  AND tipo='Preventivo' AND estado='Completada' AND ABS(horometro-?)<0.05
                                  AND COALESCE(fecha_fin, fecha_inicio)<>? ORDER BY 1 LIMIT 1""",
                               (eq["id"], ult["horometro"], ult["fecha"])).fetchone()
            if otro:
                fila["incoherencias"].append(
                    f"El servicio del {_f(ult['fecha'])} tiene el mismo horómetro ({ult['horometro']:g}) que el del "
                    f"{_f(otro['f'])} que ya tenía el sistema: puede ser el mismo formato con la fecha corregida.")
        m = L.estado_equipo(con, eq, cfg, hoy)
        fila["estado"] = m
        for o in con.execute("""SELECT id, fecha_programada, origen, observaciones FROM ordenes WHERE equipo_id=?
                                AND tipo='Preventivo' AND estado IN ('Programada','Pendiente')""", (eq["id"],)):
            if ultima_corrida and f"corrida {ultima_corrida}" in f"{o['origen'] or ''} {o['observaciones'] or ''}".lower():
                continue        # lo decidió (o confirmó) la última corrida aplicada: adelanto intencional
            if m["estado"] == "Vigente" and ult and m["fecha_vence"] and o["fecha_programada"] and                     (L.a_fecha(m["fecha_vence"]) - L.a_fecha(o["fecha_programada"])).days > int(float(cfg.get("umbral_dias") or 15)):
                fila["incoherencias"].append(
                    f"Tiene la OT #{o['id']} programada para el {_f(o['fecha_programada'])}, pero el equipo está vigente "
                    f"hasta el {_f(m['fecha_vence'])} (preventivo del {_f(m['fecha_pm'])}" +
                    (f", certificado de operatividad del {_f(m['fecha_inspeccion'])}" if m.get("fecha_inspeccion") else "") +
                    "). Vuelva a optimizar.")
        fila["incoherencias"] = list(dict.fromkeys(fila["incoherencias"]))
        inf["incoherencias"] += len(fila["incoherencias"])
        inf["equipos"].append(fila)
    inf["total_servicios"] = con.execute("SELECT COUNT(*) FROM ordenes WHERE origen=?", (ORIGEN,)).fetchone()[0]
    inf["total_lecturas"] = con.execute("SELECT COUNT(*) FROM lecturas WHERE fuente IN (?,?) AND fecha>'2026-09-22'",
                                        (FUENTE_CO, FUENTE_PM)).fetchone()[0]
    con.execute("INSERT OR REPLACE INTO config(clave, valor) VALUES ('documentos_informe', ?)",
                (json.dumps(inf, ensure_ascii=False, default=str),))
    con.execute("INSERT OR REPLACE INTO config(clave, valor) VALUES ('documentos_fecha', ?)", (hoy.isoformat(),))
    return inf


def releer(con, docs_dir, hoy=None):
    docs, leidos = leer_carpetas(docs_dir)
    inf = aplicar(con, docs, hoy)
    inf["leidos"] = leidos
    inf["documentos"] = sum(len(v) for v in docs.values())
    inf["carpetas"] = len(docs)
    con.execute("UPDATE config SET valor=? WHERE clave='documentos_informe'", (json.dumps(inf, ensure_ascii=False, default=str),))
    return inf


if __name__ == "__main__":
    import sys
    import db as base
    sys.stdout.reconfigure(encoding="utf-8")
    con = base.conectar()
    r = releer(con, Path(sys.argv[1]) if len(sys.argv) > 1 else DOCS_DIR)
    con.commit()
    print(f"{r['documentos']} documentos ({r['leidos']} leídos ahora) · {r['nuevos_servicios']} servicios nuevos · "
          f"{r['nuevas_lecturas']} lecturas nuevas · {r['incoherencias']} observaciones")
