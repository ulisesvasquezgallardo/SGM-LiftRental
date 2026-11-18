"""Consolida la lectura de los expedientes de cada máquina en datos/expedientes.json y
datos/catalogo_tareas.json.

La lectura se hizo carpeta por carpeta sobre el texto de cada documento (formato PM del Plan de
Procesos de Mantenimiento, certificado de mantenimiento, certificado de operatividad, ficha técnica y
check list) y cada dato se verificó contra el documento por un segundo lector. Este script toma ese
resultado (un JSON con «lotes» y «catalogo») y deja sólo lo que el sistema usa:

  * expedientes.json: por equipo, el último PM documentado (ejecutor, supervisor, formato, archivo),
    el certificado de operatividad (fecha, horómetro, observación, recomendación), los documentos,
    las inconsistencias encontradas y las pistas sobre su estado actual.
  * catalogo_tareas.json: las listas de tareas A, B, C y D de cada formato del plan y a qué
    categorías de equipo se aplica.

Uso:  python importar_expedientes.py <lectura_expedientes.json>
"""
import json
import sys
from pathlib import Path

DATOS = Path(__file__).resolve().parent / "datos"


def nombre_propio(s):
    """«TÉCNICO A» y «TÉCNICO C» → nombre registrado en el sistema."""
    if not s:
        return None
    t = " ".join(s.split()).title()
    return {"Técnico A": "Técnico A", "Técnico A": "Técnico A"}.get(t, t)


def consolidar(lectura):
    maquinas = {m["codigo"]: m for l in lectura["lotes"] for m in (l.get("extraccion") or {}).get("maquinas", [])}
    verif = {v["codigo"]: v for l in lectura["lotes"] for v in ((l.get("verificacion") or {}).get("maquinas") or [])}
    salida = {}
    for cod, m in sorted(maquinas.items()):
        v = verif.get(cod) or {}
        pm = m.get("ultimo_pm") or {}
        co = m.get("cert_operatividad") or {}
        cm = m.get("cert_mantenimiento") or {}
        incons = [dict(campo=i.get("campo"), detalle=i.get("detalle"), fuente="Lectura")
                  for i in (m.get("inconsistencias") or [])]
        for c in v.get("correcciones") or []:
            incons.append(dict(campo=c.get("campo"), detalle=f"Verificación: {c.get('valor_correcto')}",
                               fuente="Verificación", archivo=c.get("archivo")))
        for d in v.get("discrepancias_vs_libro") or []:
            incons.append(dict(campo=d.get("campo"), detalle=d.get("comentario"), fuente="Contraste con el sistema",
                               archivo=d.get("archivo"), cita=d.get("cita")))
        salida[cod] = dict(
            codigo=cod,
            confianza=v.get("confianza"),
            pm=dict(archivo=pm.get("archivo"), fecha=pm.get("fecha"), horometro=pm.get("horometro"),
                    proximo_h=pm.get("proximo_hr"), ejecutor=nombre_propio(pm.get("ejecutor")),
                    supervisor=nombre_propio(pm.get("supervisor")), formato=pm.get("formato"),
                    observaciones=pm.get("observaciones")) if pm else None,
            cert_mantenimiento=dict(archivo=cm.get("archivo"), programa=cm.get("programa"),
                                    detalle=cm.get("detalle"), contacto=cm.get("contacto")) if cm else None,
            cert_operatividad=dict(archivo=co.get("archivo"), fecha=co.get("fecha"), horometro=co.get("horometro"),
                                   observaciones=co.get("observaciones"), recomendaciones=co.get("recomendaciones"),
                                   firmante=co.get("firmante")) if co else None,
            tecnico=m.get("tecnico"),
            check_list=m.get("check_list"),
            documentos=m.get("documentos") or [],
            inconsistencias=incons,
            pistas=m.get("pistas_estado_actual") or [],
        )
    formatos = []
    for f in (lectura.get("catalogo") or {}).get("formatos", []):
        formatos.append(dict(formato=f["formato"], aplica_a=f.get("aplica_a") or [],
                             leyenda=f.get("leyenda") or [], notas=f.get("notas"),
                             listas={k: [dict(codigo=t["codigo"], tarea=t["tarea"]) for t in v]
                                     for k, v in (f.get("listas") or {}).items()}))
    return salida, dict(formatos=formatos)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    origen = Path(sys.argv[1])
    lectura = json.loads(origen.read_text(encoding="utf-8"))
    lectura = lectura.get("result", lectura)
    exp, cat = consolidar(lectura)
    DATOS.mkdir(exist_ok=True)
    (DATOS / "expedientes.json").write_text(json.dumps(exp, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATOS / "catalogo_tareas.json").write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(exp)} expedientes y {len(cat['formatos'])} formatos del plan escritos en {DATOS}")
