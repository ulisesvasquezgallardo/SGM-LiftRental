"""Simulación de la operación de Lift Rental desde el registro de estatus (22/09/2026) hasta una fecha.

Parte del estado real —flota, último servicio documentado de cada equipo y contratos en obra al
22/09/2026— y recorre la operación día a día con la práctica actual descrita en el informe:

  * Retornos: cada contrato vuelve en su fecha de vencimiento; los que ya estaban vencidos al corte
    se renuevan por periodos de 30 días (supuesto del informe). Algunos contratos nuevos se prorrogan.
  * Taller reactivo: un equipo en patio con el mantenimiento vencido (o sin datos) entra a la cola del
    taller; los técnicos libres toman la cola en orden de llegada, de lunes a sábado, con la duración
    y los técnico-días de su nivel de servicio. Al terminar, el contador del equipo se reinicia.
  * Despacho: los pedidos llegan con la tasa observada (ley de Little sobre el registro) y se atienden
    con la primera máquina compatible libre en patio, sin verificar el mantenimiento —tal como muestra
    el registro—; si la máquina tenía su servicio en cola, la orden se cancela.
  * Horómetro: cada día en obra suma la tasa de uso del equipo dividida entre la ocupación (84 %).
  * Fallas en obra (supuestos del informe, tabla de parámetros): cada contrato falla con probabilidad
    25 % si el equipo opera con el mantenimiento vencido o sin datos y 5 % si está vigente, en
    proporción a los días del contrato. La falla abre un incidente y una OT correctiva; un técnico se
    desplaza a la obra (prioridad sobre el taller). Costo: S/ 860 + los días parados x tarifa.

Al llegar a la fecha final, el sistema programa los servicios pendientes en la capacidad libre del
taller, de modo que el almacén muestre qué equipos entran a mantenimiento, cuándo y con qué técnico.
Los datos generados (pedidos, clientes asignados, prórrogas) usan una semilla fija y se marcan como
«Simulación».
"""
import json
import math
import random
from datetime import date, timedelta

import logica as L

CORTE = date(2026, 9, 22)            # fecha del registro de estatus
T0 = date(2026, 10, 1)               # inicio del horizonte del informe (regla de prórroga)
OCUPACION = 71 / 85                   # equipos en obra / flota en el registro
SEMILLA = 20260922
P_PRORROGA = 0.42                     # prórroga de 30 días al vencer: calibrada para reproducir la
                                      # ocupación del registro (84 %); con 0.42 la simulación da 85 %
P_FALLA_VENCIDO = 0.25                # probabilidad de falla por pedido con mantenimiento vencido (informe)
P_FALLA_VIGENTE = 0.05                # probabilidad de falla con mantenimiento vigente (informe)
COSTO_FALLA = 860                     # S/ por atención de falla en obra, más días parados x tarifa
FALLAS = {
    "Tijera eléctrica": [
        "Fuga de aceite hidráulico en el cilindro de elevación",
        "Baterías descargadas: el cargador no completa la carga",
        "Alarma de inclinación activa: la plataforma no eleva",
        "Falla del joystick de la consola de plataforma",
        "Freno de estacionamiento no libera: el equipo no se traslada",
        "Sensor de sobrecarga bloquea la elevación"],
    "Brazo articulado eléctrico": [
        "Fuga hidráulica en manguera del brazo principal",
        "Baterías descargadas: el equipo no completa la jornada",
        "Falla del control proporcional del brazo (respuesta errática)",
        "Alarma de inclinación: no se puede extender la pluma"],
    "Brazo articulado combustión": [
        "El motor no arranca: falla en el sistema de combustible",
        "Sobrecalentamiento del motor diésel",
        "Fuga hidráulica en el cilindro de giro (jib)",
        "Falla del alternador: la batería no carga"],
    "Mástil vertical": [
        "Batería descargada: el mástil no eleva",
        "Falla del interruptor de límite de elevación"],
}
ESPERA_MAX = 3                        # días que un pedido espera una máquina antes de perderse
RETORNO_REPARACION = date(2026, 10, 11)   # TECL-018: reparación externa (día 10 del horizonte)
PREFERENCIA_TECNICOS = ["Técnico A", "Técnico B", "Técnico C"]


def fecha_retorno_inicial(venc):
    """Regla del informe: si el contrato ya venció, se renueva por periodos de 30 días."""
    if venc is None:
        return T0 + timedelta(days=30)
    if venc >= T0:
        return venc
    k = 0
    while venc + timedelta(days=30 * k) < T0:
        k += 1
    return venc + timedelta(days=30 * k)


def simular(con, hasta, semilla=SEMILLA, programar=True, log=print):
    rng = random.Random(semilla)
    rng_f = random.Random(semilla + 1)      # fallas: flujo aparte para no alterar la demanda
    cfg = L.leer_config(con)
    hasta = L.a_fecha(hasta)
    cap = int(float(cfg.get("capacidad_tecnicos") or 3))

    eqs = {r["id"]: dict(r) for r in con.execute("SELECT * FROM equipos WHERE activo=1")}
    tecs = [dict(r) for r in con.execute("SELECT * FROM tecnicos WHERE activo=1 AND ejecuta=1")]
    orden_pref = {n: i for i, n in enumerate(PREFERENCIA_TECNICOS)}
    tecs.sort(key=lambda t: (orden_pref.get(t["nombre"], 99), t["id"]))
    tecs = tecs[:max(cap, 1)]

    # ---------------- estado de cada equipo
    def u_dia(e):
        return float(e["tasa_uso"] or 0) / OCUPACION

    def lectura(eid, d, h, fuente):
        con.execute("INSERT INTO lecturas(equipo_id, fecha, horometro, fuente) VALUES (?,?,?,?)",
                    (eid, L.iso(d), round(h, 1), fuente))

    def horometro(eid, d):
        ult = L.ultima_lectura(con, eid, d)
        return float(ult["horometro"]) if ult else 0.0

    def horometro_obra(eid, d):
        """Horómetro estimado de un equipo en obra al día d (última lectura + uso en obra)."""
        ult = L.ultima_lectura(con, eid, d)
        a = activos.get(eid)
        if not a:
            return horometro(eid, d)
        desde = max(a["salida"], L.a_fecha(ult["fecha"])) if ult else a["salida"]
        return (float(ult["horometro"]) if ult else 0.0) + u_dia(eqs[eid]) * max(0, (d - desde).days)

    def sortear_falla(eid, ini, fin, alq_id):
        """Sorteo de una falla durante [ini, fin): 25 % por pedido vencido, 5 % vigente,
        en proporción a los días sobre la duración media de los contratos."""
        dias = (fin - ini).days
        if dias <= 0:
            return
        m = estado(eid, ini)
        if m["estado"] in ("Vencido", "Sin datos"):
            d_ok = 0
        else:
            fv = L.a_fecha(m["fecha_vence"]) or fin
            d_ok = max(0, min(dias, (fv - ini).days))
        p = (P_FALLA_VIGENTE * d_ok + P_FALLA_VENCIDO * (dias - d_ok)) / dur_media
        if rng_f.random() < min(p, 0.9):
            dia = ini + timedelta(days=rng_f.randrange(dias))
            fallas.append(dict(equipo=eid, dia=dia, alquiler=alq_id))

    def estado(eid, d):
        e = eqs[eid]
        return L.calcular_estado(e, L.ultimo_pm(con, eid, d), L.ultima_lectura(con, eid, d), cfg, d)

    # Alquileres vigentes al corte: fecha de retorno según la regla del informe
    activos = {}
    for a in con.execute("SELECT * FROM alquileres WHERE estado='En obra'"):
        ret = fecha_retorno_inicial(L.a_fecha(a["fecha_vencimiento"]))
        activos[a["equipo_id"]] = dict(id=a["id"], retorno=max(ret, CORTE + timedelta(days=1)),
                                       salida=L.a_fecha(a["fecha_salida"]), inicial=True)
        if ret != L.a_fecha(a["fecha_vencimiento"]):
            con.execute("UPDATE alquileres SET notas=COALESCE(notas||' · ','')||? WHERE id=?",
                        (f"Contrato vencido al corte: se supone prorrogado hasta el {ret:%d/%m/%Y}", a["id"]))

    fallas = []               # fallas sorteadas pendientes de ocurrir
    fuera = {e["id"]: RETORNO_REPARACION for e in eqs.values() if e["situacion"] == "Fuera de servicio"}

    # ---------------- calibración de la demanda con el registro
    contratos = con.execute(
        "SELECT a.*, e.categoria FROM alquileres a JOIN equipos e ON e.id=a.equipo_id").fetchall()
    duraciones = [d for d in ((L.a_fecha(a["fecha_vencimiento"]) - L.a_fecha(a["fecha_salida"])).days
                              for a in contratos if a["fecha_vencimiento"] and a["fecha_salida"]) if d > 0]
    dur_media = sum(duraciones) / len(duraciones)
    obras = [(a["cliente"], a["obra"], a["zona"]) for a in contratos if a["cliente"]]
    for eid, a in activos.items():             # contratos vigentes al corte: lo que les queda en obra
        sortear_falla(eid, CORTE + timedelta(days=1), a["retorno"], a["id"])
    mezcla = [a["categoria"] for a in contratos]
    lam = len(contratos) / (sum(duraciones) / len(duraciones)) * len(eqs) / 85   # ley de Little
    alturas = {}
    for e in eqs.values():
        if e["altura_trabajo"]:
            alturas.setdefault(e["categoria"], []).append(e["altura_trabajo"])

    pedidos, d = [], CORTE + timedelta(days=1)
    while d <= hasta:
        n = poisson(rng, lam)
        for _ in range(n):
            cat = rng.choice(mezcla)
            h_ref = rng.choice(alturas.get(cat) or [10])
            pedidos.append(dict(dia=d, categoria=cat,
                                altura=max(5.0, round(h_ref - rng.choice([0, 0, 0.5, 1, 2]), 1)),
                                duracion=rng.choice(duraciones), obra=rng.choice(obras)))
        d += timedelta(days=1)

    # ---------------- recorrido día a día
    cola = []                 # [equipo_id, orden_id, fecha_ingreso]
    cola_corr = []            # correctivos: [equipo_id, orden_id, fecha_falla, incidente_id]
    en_taller = {}            # orden_id -> dict(equipo, fin, tecnicos)
    ocupado_hasta = {t["id"]: CORTE for t in tecs}
    stats = dict(despachos=0, despachos_vencidos=0, perdidos=0, servicios=0, cancelados=0, prorrogas=0,
                 fallas=0, fallas_vencidos=0, correctivos=0)
    espera = []
    d = CORTE + timedelta(days=1)
    while d <= hasta:
        # 1. Retornos de obra y de reparación externa
        for eid, a in list(activos.items()):
            if a["retorno"] != d:
                continue
            if rng.random() < P_PRORROGA:
                a["prorrogas"] = a.get("prorrogas", 0) + 1
                a["retorno"] = d + timedelta(days=30)
                stats["prorrogas"] += 1
                sortear_falla(eid, d, a["retorno"], a["id"])
                con.execute("""UPDATE alquileres SET fecha_vencimiento=?, notas=COALESCE(notas||' · ','')||?
                               WHERE id=?""", (L.iso(a["retorno"]), f"Prórroga de 30 días registrada el {d:%d/%m/%Y}", a["id"]))
                continue
            ult = L.ultima_lectura(con, eid, d)
            desde = max(a["salida"], L.a_fecha(ult["fecha"])) if ult else a["salida"]
            h = (float(ult["horometro"]) if ult else 0.0) + u_dia(eqs[eid]) * max(0, (d - desde).days)
            lectura(eid, d, h, "Retorno de obra")
            con.execute("""UPDATE alquileres SET estado='Cerrado', fecha_retorno=?, horometro_retorno=? WHERE id=?""",
                        (L.iso(d), round(h, 1), a["id"]))
            del activos[eid]
            eqs[eid]["situacion"] = "Disponible"
            fallas[:] = [f for f in fallas if f["alquiler"] != a["id"]]
        for eid, f in list(fuera.items()):
            if f == d:
                eqs[eid]["situacion"] = "Disponible"
                eqs[eid]["ubicacion_detalle"] = None
                del fuera[eid]

        # 1b. Fallas en obra del día: incidente + OT correctiva (técnico a la obra)
        for f in [f for f in fallas if f["dia"] == d]:
            fallas.remove(f)
            eid = f["equipo"]
            if eid not in activos or activos[eid]["id"] != f["alquiler"]:
                continue
            e = eqs[eid]
            m = estado(eid, d)
            h = horometro_obra(eid, d)
            desc = rng_f.choice(FALLAS.get(e["categoria"]) or FALLAS["Tijera eléctrica"])
            obra = con.execute("SELECT cliente, obra FROM alquileres WHERE id=?", (f["alquiler"],)).fetchone()
            dur = 2 if e["categoria"] == "Brazo articulado combustión" else 1
            cur = con.execute(
                """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, prioridad, duracion_dias, tecnico_dias,
                   costo_estimado, origen, descripcion, creado) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, "Correctivo", None, "Pendiente", "Alta", dur, dur, COSTO_FALLA,
                 "Simulación: falla en obra",
                 f"Atención en obra ({obra['cliente'] or 'cliente'} · {obra['obra'] or ''}): {desc}",
                 L.iso(d) + " 07:30"))
            oid = cur.lastrowid
            ci = con.execute(
                """INSERT INTO incidentes(equipo_id, alquiler_id, fecha, horometro, mant_estado, descripcion,
                   orden_id) VALUES (?,?,?,?,?,?,?)""",
                (eid, f["alquiler"], L.iso(d), round(h, 1), m["estado"], desc + " (simulación)", oid))
            cola_corr.append([eid, oid, d, ci.lastrowid])
            stats["fallas"] += 1
            stats["fallas_vencidos"] += m["estado"] in ("Vencido", "Sin datos")

        # 2. Equipos en patio vencidos entran a la cola del taller
        en_cola = {c[0] for c in cola} | {v["equipo"] for v in en_taller.values()}
        for eid, e in eqs.items():
            if e["situacion"] != "Disponible" or eid in en_cola:
                continue
            m = estado(eid, d)
            if m["estado"] in ("Vencido", "Sin datos"):
                nivel = m["nivel"] or "A"
                p = L.parametros_servicio(con, e["categoria"], nivel)
                cur = con.execute(
                    """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, prioridad, duracion_dias, tecnico_dias,
                       costo_estimado, origen, descripcion, creado) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, "Preventivo", nivel, "Pendiente", "Alta", p["duracion_dias"], p["tecnico_dias"], p["costo"],
                     "Simulación: taller reactivo",
                     f"Servicio {nivel}: equipo en patio con mantenimiento "
                     f"{'sin registro' if m['estado'] == 'Sin datos' else 'vencido'}", L.iso(d) + " 08:00"))
                cola.append([eid, cur.lastrowid, d])

        # 3. Taller: cierres y nuevos inicios (lunes a sábado)
        for oid, t in list(en_taller.items()):
            if t["fin"] < d:          # terminó al cierre de su último día hábil
                eid = t["equipo"]
                if t.get("incidente"):
                    en_obra = eid in activos
                    h = horometro_obra(eid, t["fin"])
                    parado = (t["fin"] - t["falla"]).days + 1
                    costo = COSTO_FALLA + parado * L.tarifa_sugerida(eqs[eid])
                    con.execute("""UPDATE ordenes SET estado='Completada', fecha_fin=?, horometro=?, costo_real=?,
                                   observaciones=? WHERE id=?""",
                                (L.iso(t["fin"]), round(h, 1), costo,
                                 "Falla atendida en obra (simulación)" if en_obra
                                 else "Falla atendida en taller (simulación)", oid))
                    con.execute("UPDATE incidentes SET tecnico_id=?, costo=?, dias_parado=? WHERE id=?",
                                (t["tecnicos"][0], costo, parado, t["incidente"]))
                    lectura(eid, t["fin"], h, f"Atención OT-{oid:04d}")
                    if not en_obra:
                        eqs[eid]["situacion"] = "Disponible"
                    stats["correctivos"] += 1
                else:
                    h = horometro(eid, t["fin"])
                    niv = con.execute("SELECT nivel FROM ordenes WHERE id=?", (oid,)).fetchone()["nivel"]
                    con.execute("""UPDATE ordenes SET estado='Completada', fecha_fin=?, horometro=?, costo_real=costo_estimado,
                                   tareas=?, observaciones=? WHERE id=?""",
                                (L.iso(t["fin"]), round(h, 1),
                                 json.dumps(L.codigos_tareas(eqs[eid]["categoria"], niv)),
                                 f"Servicio {niv} completado según el formato «"
                                 f"{(L.formato_plan(eqs[eid]['categoria']) or {}).get('formato', 'del plan')}» (simulación)",
                                 oid))
                    lectura(eid, t["fin"], h, f"Cierre OT-{oid:04d}")
                    eqs[eid]["situacion"] = "Disponible"
                    stats["servicios"] += 1
                del en_taller[oid]
        if not L.es_domingo(d):
            for item in list(cola_corr):        # primero las fallas en obra: el cliente está parado
                eid, oid, dia_falla, inc = item
                libres = [t for t in tecs if ocupado_hasta[t["id"]] < d]
                if not libres:
                    break
                o = con.execute("SELECT duracion_dias FROM ordenes WHERE id=?", (oid,)).fetchone()
                dias = L.dias_habiles(d, int(o["duracion_dias"] or 1))
                t = libres[0]
                ocupado_hasta[t["id"]] = dias[-1]
                con.execute("INSERT OR IGNORE INTO orden_tecnicos VALUES (?,?)", (oid, t["id"]))
                con.execute("UPDATE ordenes SET estado='En ejecución', fecha_programada=?, fecha_inicio=? WHERE id=?",
                            (L.iso(d), L.iso(d), oid))
                if eid not in activos and eqs[eid]["situacion"] == "Disponible":
                    eqs[eid]["situacion"] = "En taller"
                en_taller[oid] = dict(equipo=eid, fin=dias[-1], incidente=inc, falla=dia_falla, tecnicos=[t["id"]])
                cola_corr.remove(item)
            for item in list(cola):
                eid, oid, _ = item
                o = con.execute("SELECT * FROM ordenes WHERE id=?", (oid,)).fetchone()
                dur = int(o["duracion_dias"] or 1)
                necesarios = max(1, math.ceil(float(o["tecnico_dias"] or 1) / dur - 1e-9))
                libres = [t for t in tecs if ocupado_hasta[t["id"]] < d]
                if len(libres) < necesarios:
                    continue
                dias = L.dias_habiles(d, dur)
                asignados = libres[:necesarios]
                for t in asignados:
                    ocupado_hasta[t["id"]] = dias[-1]
                    con.execute("INSERT OR IGNORE INTO orden_tecnicos VALUES (?,?)", (oid, t["id"]))
                con.execute("UPDATE ordenes SET estado='En ejecución', fecha_programada=?, fecha_inicio=? WHERE id=?",
                            (L.iso(d), L.iso(d), oid))
                eqs[eid]["situacion"] = "En taller"
                en_taller[oid] = dict(equipo=eid, fin=dias[-1], tecnicos=[t["id"] for t in asignados])
                cola.remove(item)

        # 4. Despacho de los pedidos del día (y de los que esperan)
        espera += [p for p in pedidos if p["dia"] == d]
        for p in list(espera):
            if (d - p["dia"]).days > ESPERA_MAX:
                espera.remove(p)
                stats["perdidos"] += 1
                continue
            libres = [e for e in eqs.values() if e["situacion"] == "Disponible" and e["categoria"] == p["categoria"]
                      and (e["altura_trabajo"] or 0) >= p["altura"] and e["id"] not in activos
                      and e["id"] not in {c[0] for c in cola_corr}]
            if not libres:
                continue
            libres.sort(key=lambda e: (e["altura_trabajo"] or 0, e["codigo"]))
            e = libres[0]
            m = estado(e["id"], d)
            h = horometro(e["id"], d)      # en patio el horómetro no avanza
            fv = d + timedelta(days=int(p["duracion"]))
            cliente, obra, zona = p["obra"]
            cur = con.execute(
                """INSERT INTO alquileres(equipo_id, cliente, obra, zona, fecha_salida, fecha_vencimiento, horometro_salida,
                   tarifa_dia, estado, mant_estado_salida, notas, creado) VALUES (?,?,?,?,?,?,?,?,'En obra',?,?,?)""",
                (e["id"], cliente, obra, zona, L.iso(d), L.iso(fv), round(h, 1), L.tarifa_sugerida(e), m["estado"],
                 "Simulación: despacho por disponibilidad física (práctica actual)", L.iso(d) + " 09:00"))
            lectura(e["id"], d, h, "Salida a obra")
            for item in [c for c in cola if c[0] == e["id"]]:
                con.execute("""UPDATE ordenes SET estado='Cancelada', observaciones=? WHERE id=?""",
                            (f"Equipo despachado a {obra} el {d:%d/%m/%Y} antes de su servicio", item[1]))
                cola.remove(item)
                stats["cancelados"] += 1
            activos[e["id"]] = dict(id=cur.lastrowid, retorno=fv, salida=d, inicial=False)
            sortear_falla(e["id"], d, fv, cur.lastrowid)
            e["situacion"] = "En obra"
            e["ubicacion_detalle"] = obra
            espera.remove(p)
            stats["despachos"] += 1
            stats["despachos_vencidos"] += m["estado"] in ("Vencido", "Sin datos")
        d += timedelta(days=1)

    # ---------------- estado final al día «hasta»
    for eid, e in eqs.items():
        con.execute("UPDATE equipos SET situacion=?, ubicacion_detalle=? WHERE id=?",
                    (e["situacion"], e.get("ubicacion_detalle") if e["situacion"] in ("En obra", "Fuera de servicio") else None, eid))
    # Las órdenes en cola quedan pendientes (sin fecha); el sistema las programa a partir de hoy
    for k, v in (("simulacion_desde", L.iso(CORTE)), ("simulacion_hasta", L.iso(hasta)),
                 ("simulacion_semilla", str(semilla)), ("fecha_referencia", "" if hasta == date.today() else L.iso(hasta))):
        con.execute("INSERT OR REPLACE INTO config(clave, valor) VALUES (?,?)", (k, v))
    stats["pedidos"] = len(pedidos)
    stats["en_cola"] = len(cola)
    stats["correctivos_pendientes"] = len(cola_corr)
    if programar:
        cfg2 = L.leer_config(con)
        stats["programadas"] = len(L.programar_automatico(con, cfg2))
    resumen = ", ".join(f"{k}={v}" for k, v in stats.items())
    con.execute("INSERT OR REPLACE INTO config(clave, valor) VALUES ('simulacion_resumen', ?)", (resumen,))
    log(f"Simulación {CORTE:%d/%m/%Y} → {hasta:%d/%m/%Y}: {resumen}")
    return stats


def poisson(rng, lam):
    """Número de llegadas en un día (Knuth)."""
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


if __name__ == "__main__":
    import sys
    import db as base
    hasta = L.a_fecha(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    base.inicializar(forzar=True)
    con = base.conectar()
    simular(con, hasta)
    con.commit()
