"""Genera datos/semilla.json a partir de los libros de datos del proyecto.

Fuentes (carpeta superior «_Proyecto PPP - resultados»):
  - Libro_de_datos_flota_LiftRental.xlsx : flota, último mantenimiento, tablero,
    parámetros por nivel y estado inicial (registro de estatus al 22/09/2026).
  - Analisis_registro_estatus.xlsx       : los 12 equipos del registro sin expediente.

Uso:  python importar_excel.py      (requiere openpyxl)
La aplicación sólo lee semilla.json, de modo que openpyxl no es necesario para usarla.
"""
import json
from datetime import date, datetime
from pathlib import Path

import openpyxl

BASE = Path(__file__).resolve().parent
ORIGEN = BASE.parent
LIBRO = ORIGEN / "Libro_de_datos_flota_LiftRental.xlsx"
REGISTRO = ORIGEN / "Analisis_registro_estatus.xlsx"
SALIDA = BASE / "datos" / "semilla.json"


def f(v):
    """Fecha → 'AAAA-MM-DD'."""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return v


def filas(ws, desde):
    for r in ws.iter_rows(min_row=desde, values_only=True):
        if r[0] and str(r[0]).strip() and "-" in str(r[0]) and len(str(r[0])) <= 10:
            yield r


def categoria_por_codigo(cod):
    return {
        "TECL": "Tijera eléctrica",
        "BECL": "Brazo articulado eléctrico",
        "BCCL": "Brazo articulado combustión",
        "UPCL": "Mástil vertical",
    }[cod[:4]]


def main():
    wb = openpyxl.load_workbook(LIBRO, data_only=True)

    flota = {}
    for r in filas(wb["Flota"], 5):
        cat = r[1].replace("Mástil vertical unipersonal", "Mástil vertical")
        flota[r[0]] = dict(
            codigo=r[0], categoria=cat, marca=str(r[2]).title() if r[2] else None,
            modelo=r[3], serie=str(r[4]) if r[4] else None, anio=r[5],
            altura_plataforma=r[7], altura_trabajo=r[8], capacidad_kg=r[9],
            peso_kg=r[10], valor_usd=r[11], con_expediente=1,
        )

    mant = {}
    for r in filas(wb["Mantenimiento"], 5):
        mant[r[0]] = dict(
            fecha_pm=f(r[2]), horometro_pm=r[3], ejecutor=r[4], nivel=r[5] or "A",
            frecuencia_h=r[6], fecha_co=f(r[8]), horometro_co=r[9],
            archivo_pm=r[14], horometro_registro=r[15],
        )

    tablero = {}
    for r in filas(wb["Tablero"], 12):
        tablero[r[0]] = dict(fecha_lectura=f(r[5]), horometro_lectura=r[6], tasa=r[7])

    estado = {}
    for r in filas(wb["Estado inicial"], 5):
        estado[r[0]] = dict(
            situacion=r[1], cliente=r[2], obra=r[3], zona=r[4],
            fecha_salida=f(r[5]), fecha_vencimiento=f(r[6]),
        )

    parametros = []
    ws = wb["Parámetros"]
    for r in ws.iter_rows(min_row=5, max_row=20, values_only=True):
        if r[0] and r[1]:
            parametros.append(dict(
                nivel=r[0], categoria=r[1].replace("Mástil vertical unipersonal", "Mástil vertical"),
                duracion_dias=r[2], costo=r[3], tecnico_dias=r[4]))

    equipos, lecturas, ordenes, alquileres = [], [], [], []
    tecnicos = sorted({m["ejecutor"] for m in mant.values() if m["ejecutor"]})

    for cod, eq in flota.items():
        m, t, e = mant.get(cod, {}), tablero.get(cod, {}), estado.get(cod, {})
        sit = e.get("situacion")
        situacion, detalle = "Disponible", None
        if sit == "En obra":
            situacion = "En obra"
        elif sit == "Reparación externa":
            situacion, detalle = "Fuera de servicio", "Reparación externa (taller Dante)"
        elif sit == "Sin fila en el registro":
            detalle = "No figura en el registro de estatus; se supone en el taller"
        eq.update(frecuencia_h=m.get("frecuencia_h"), tasa_uso=round(t.get("tasa") or 0, 4),
                  situacion=situacion, ubicacion_detalle=detalle)
        if cod == "TECL-068":
            eq["notas"] = "El registro anota «se fue como la TECL069»: su contrato lo ocupa el TECL-069."
        equipos.append(eq)

        if m.get("fecha_pm"):
            ordenes.append(dict(
                equipo=cod, tipo="Preventivo", nivel=m["nivel"], estado="Completada",
                fecha_programada=m["fecha_pm"], fecha_inicio=m["fecha_pm"], fecha_fin=m["fecha_pm"],
                horometro=m["horometro_pm"], tecnico=m["ejecutor"], origen="Histórico (expediente)",
                descripcion=f"Servicio {m['nivel']} documentado en el formato Mntto-PPMTE-2017",
                observaciones=f"Archivo: {m['archivo_pm']}" if m.get("archivo_pm") else None,
            ))
            lecturas.append(dict(equipo=cod, fecha=m["fecha_pm"], horometro=m["horometro_pm"],
                                 fuente="Último mantenimiento"))
        if m.get("fecha_co") and m.get("horometro_co") is not None:
            lecturas.append(dict(equipo=cod, fecha=m["fecha_co"], horometro=m["horometro_co"],
                                 fuente="Certificado de operatividad"))
        if m.get("horometro_registro"):
            lecturas.append(dict(equipo=cod, fecha="2026-09-22", horometro=m["horometro_registro"],
                                 fuente="Registro de estatus"))
        if sit == "En obra":
            alquileres.append(dict(equipo=cod, cliente=e["cliente"], obra=e["obra"], zona=e["zona"],
                                   fecha_salida=e["fecha_salida"],
                                   fecha_vencimiento=e["fecha_vencimiento"]))

    # Equipos del registro sin expediente técnico
    wr = openpyxl.load_workbook(REGISTRO, data_only=True)["Cruce 85 equipos"]
    for r in wr.iter_rows(min_row=5, values_only=True):
        if not r[0] or r[3] != "No":
            continue
        cod = f"{r[0][:4]}-{r[0][4:]}"
        cat = categoria_por_codigo(cod)
        equipos.append(dict(
            codigo=cod, categoria=cat, marca=str(r[1]).title(), modelo=r[2], con_expediente=0,
            frecuencia_h=250 if cat.startswith("Brazo") else 100, tasa_uso=0.13,
            situacion="En obra" if r[4] == "En obra" else "Disponible",
            notas="Figura en el registro de estatus y en la póliza, pero no tiene carpeta de expediente: "
                  "su estado de mantenimiento no puede verificarse.",
        ))
        if r[4] == "En obra":
            alquileres.append(dict(equipo=cod, cliente=r[5], obra=r[6], zona=r[7],
                                   fecha_salida=f(r[8]), fecha_vencimiento=f(r[9])))

    semilla = dict(
        generado=datetime.now().strftime("%Y-%m-%d %H:%M"),
        equipos=equipos, lecturas=lecturas, ordenes=ordenes, alquileres=alquileres,
        tecnicos=tecnicos, parametros=parametros,
    )
    SALIDA.parent.mkdir(exist_ok=True)
    SALIDA.write_text(json.dumps(semilla, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(equipos)} equipos, {len(lecturas)} lecturas, {len(ordenes)} órdenes, "
          f"{len(alquileres)} alquileres, {len(tecnicos)} técnicos → {SALIDA}")


if __name__ == "__main__":
    main()
