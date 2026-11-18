"""Modelo de programación lineal entera mixta (PLEM) del proyecto, puesto en operación.

Implementa la formulación del informe (apartado 4.1) sobre el estado vivo del sistema:

  Conjuntos   M máquinas activas, J pedidos de la cartera, T días del horizonte, Mj compatibles.
  Variables   x[m,j] asignación, y[m,t] inicio de servicio, z[j] pedido no atendido (binarias)
              y H[m,t] horas acumuladas desde el último servicio (continua).
  Objetivo    (5)  min  Σ κm·ymt + Σ rj·zj + Σ (gj + π·fj)·xmj
              (14) E3: + Σ_{m∈Mv} 1,05·κm·(1 − Σt ymt)   (valor terminal de la deuda)
  Restricciones (6) cobertura, (7) exclusividad alquiler/taller, (8)-(9) horas con reinicio,
              (10) tope de horas, (11) tope de calendario, (12) capacidad del taller por día hábil,
              (13) un servicio por máquina; servicios completos dentro del horizonte y sin domingos.

Los escenarios E0 (práctica actual), E1 (asignación óptima sin control), E2 (modelo propuesto) y E3
(con valor terminal) se valoran con un evaluador único que recorre la solución día a día, como en el
informe (4.2.5). Se resuelve con PuLP + CBC (software libre).

Datos que toma del sistema: estado de mantenimiento de cada equipo a la fecha de inicio (último
preventivo y horómetro), parámetros por nivel (duración, costo, técnico-días), retorno estimado de los
equipos en obra, órdenes en ejecución, capacidad del taller y la cartera de pedidos registrada.
"""
import json
import math
import os
import random
import re
import tempfile
import threading
import time
import traceback
from datetime import date, timedelta

import logica as L

try:
    import pulp
except Exception:            # el sistema funciona sin el optimizador; la pantalla avisa
    pulp = None

PI_OK = 0.05                 # probabilidad de falla con el mantenimiento vigente (informe, tabla 4)
PI_VENC = 0.25               # probabilidad de falla con el mantenimiento vencido (caso base)
G = 10000                    # constante suficientemente grande (h)
PEN_TERMINAL = 1.05          # factor del valor terminal (ecuación 14)
OCUPACION = 71 / 85          # ocupación del registro: convierte la tasa de uso a horas por día de alquiler
COSTO_FALLA_BASE = 860       # S/ por atención de falla en obra, + 1,5 días de tarifa
COSTO_ZONA = {"Lima Norte": 180, "Lima Centro": 280, "Callao": 300, "Lima Sur/Este": 380}
SIN_DATOS_D0 = 10 ** 4       # equipo sin servicio documentado: debe pasar por taller antes de operar
SENS_PI = [0.10, 0.20, 0.25, 0.30, 0.40, 0.60, 0.80]
ESCENARIOS = {
    "E0": "Práctica actual",
    "E1": "Asignación óptima sin control de mantenimiento",
    "E2": "Modelo propuesto",
    "E3": "Modelo con valor terminal",
}


# ===================================================================== instancia
def costo_traslado_defecto(zona, categoria, rng=None):
    """Costo de ida y vuelta por zona (informe). Brazos: x1,6 con tope de S/ 1 400."""
    g = COSTO_ZONA.get(zona)
    if g is None:
        g = (rng or random).choice([900, 1100, 1400]) if rng else 1100
    if categoria.startswith("Brazo"):
        g = min(1400, g * 1.6)
    return float(g)


def construir_instancia(db, cfg, T0=None, T=60):
    """Arma la instancia del modelo con el estado del sistema a la fecha T0 (por defecto, la de
    evaluación). Devuelve un dict serializable."""
    T0 = L.a_fecha(T0) or L.fecha_ref(cfg)
    T = int(T)
    fechas = [T0 + timedelta(days=t) for t in range(T)]
    domingos = [t for t, d in enumerate(fechas) if d.weekday() == 6]
    tecnicos = float(cfg.get("capacidad_tecnicos") or 3)

    # órdenes en ejecución (y correctivos programados): ocupan al equipo y consumen capacidad
    ocup_taller = [0.0] * T
    en_ejecucion = {}
    for o in db.execute("""SELECT * FROM ordenes WHERE estado='En ejecución'
                           OR (estado='Programada' AND tipo<>'Preventivo')""").fetchall():
        ds = L.dias_orden(o)
        if not ds:
            continue
        por_dia = float(o["tecnico_dias"] or 1) / len(ds)
        for d in ds:
            k = (d - T0).days
            if 0 <= k < T:
                ocup_taller[k] += por_dia
        if o["estado"] == "En ejecución":
            en_ejecucion[o["equipo_id"]] = dict(o=dict(o), ini=ds[0], fin=ds[-1])
    cap = [0.0 if t in domingos else max(0.0, tecnicos - ocup_taller[t]) for t in range(T)]

    maquinas = []
    for e in L.flota_con_estado(db, cfg, T0):
        m = e["mant"]
        U = float(e["frecuencia_h"] or 100)
        Dcal = L.intervalo_calendario(e["categoria"], cfg)
        sin_datos = m["estado"] == "Sin datos"
        h0 = 0.0 if sin_datos else max(0.0, float(m["horas_desde"] or 0))
        d0 = SIN_DATOS_D0 if sin_datos else int(m["dias_desde"] or 0)
        nivel = m["nivel"] or "A"
        ret, origen_ret = 0, "En patio"
        if e["situacion"] == "Fuera de servicio":
            ret, origen_ret = T, "Fuera de servicio"
        elif e["situacion"] == "En obra" and e["alquiler"]:
            f_ret, supuesto = L.retorno_estimado(e["alquiler"], T0)
            ret = min(T, max(1, (f_ret - T0).days + 1))
            origen_ret = f"En obra hasta el {f_ret:%d/%m}" + (" (prórroga supuesta)" if supuesto else "")
        ej = en_ejecucion.get(e["id"])
        if ej:
            ret = max(ret, min(T, (ej["fin"] - T0).days + 1))
            origen_ret = f"En taller hasta el {ej['fin']:%d/%m}"
            if ej["o"]["tipo"] == "Preventivo":       # el servicio en curso reinicia los contadores
                h0, d0, sin_datos = 0.0, max(0, (T0 - ej["ini"]).days), False
                nivel = L.nivel_siguiente(m["horometro_est"], U) if m["horometro_est"] else "A"
        p = L.parametros_servicio(db, e["categoria"], nivel)
        maquinas.append(dict(
            id=e["id"], codigo=e["codigo"], categoria=e["categoria"], situacion=e["situacion"],
            h_trab=float(e["altura_trabajo"] or 0), cap_kg=float(e["capacidad_kg"] or 227),
            U=U, Dcal=Dcal, h0=round(h0, 2), d0=d0, sin_datos=sin_datos, estado0=m["estado"],
            nivel=nivel, delta=max(1, int(p["duracion_dias"])), kappa=float(p["costo"]),
            tau=float(p["tecnico_dias"]), u=float(e["tasa_uso"] or 0) / OCUPACION,
            ret=ret, origen_ret=origen_ret, cliente=(e["alquiler"] or {}).get("cliente")))

    pedidos = []
    for r in db.execute("""SELECT * FROM pedidos WHERE estado IN ('Pendiente','Asignado')
                           AND fecha_fin >= ? AND fecha_inicio < ? ORDER BY fecha_inicio, id""",
                        (L.iso(T0), L.iso(T0 + timedelta(days=T)))).fetchall():
        fi, ff = L.a_fecha(r["fecha_inicio"]), L.a_fecha(r["fecha_fin"])
        dur = max(1, (ff - fi).days + 1)
        s = max(0, (fi - T0).days)
        e_ = min(T - 1, (ff - T0).days)
        if e_ < s:
            continue
        dias = e_ - s + 1
        frac = min(1.0, dias / dur)
        tar = float(r["tarifa_dia"] or L.tarifa_sugerida(dict(categoria=r["categoria"],
                                                               altura_trabajo=r["altura_req"] or 0)))
        g_total = float(r["costo_traslado"] if r["costo_traslado"] is not None
                        else costo_traslado_defecto(r["zona"], r["categoria"]))
        pedidos.append(dict(
            id=r["id"], cliente=r["cliente"], obra=r["obra"], zona=r["zona"], categoria=r["categoria"],
            hreq=float(r["altura_req"] or 0), cap=float(r["capacidad_req"] or 0), s=s, e=e_, dias=dias,
            dur_contrato=dur, frac=round(frac, 4), tarifa=tar, r=round(tar * dias, 2),
            g=round(g_total * frac, 2), g_total=g_total,
            f=round((COSTO_FALLA_BASE + 1.5 * tar) * frac, 2), fecha_inicio=r["fecha_inicio"],
            fecha_fin=r["fecha_fin"]))

    inst = dict(T0=L.iso(T0), T=T, domingos=domingos, cap=cap, tecnicos=tecnicos,
                ocup_taller=[round(v, 2) for v in ocup_taller], maquinas=maquinas, pedidos=pedidos)
    inst["pares"] = compatibles(inst)
    return inst


def compatibles(inst):
    """Mj: mismo tipo, altura y carga suficientes y libre en la fecha de inicio del pedido (4.1.1)."""
    pares = []
    for j, o in enumerate(inst["pedidos"]):
        for m, q in enumerate(inst["maquinas"]):
            if q["categoria"] != o["categoria"]:
                continue
            if q["h_trab"] + 1e-9 < o["hreq"] or q["cap_kg"] + 1e-9 < o["cap"]:
                continue
            if q["ret"] > o["s"]:
                continue
            pares.append((m, j))
    return pares


def inicio_valido(inst, t, d):
    T = inst["T"]
    dom = set(inst["domingos"])
    return t + d - 1 <= T - 1 and not any(k in dom for k in range(t, t + d))


def theta(q):
    """Último día operable sin pasar por taller (θm = D − d0m − 1)."""
    return int(q["Dcal"] - q["d0"] - 1)


def necesita_servicio(inst, q):
    """Mv: equipos cuyo intervalo vence dentro del horizonte (o ya vencido)."""
    return q["d0"] + inst["T"] >= q["Dcal"] or q["h0"] >= q["U"]


# ===================================================================== modelo PLEM
def resolver(inst, control=True, terminal=False, gap=0.01, tlim=300, inicial=None, threads=None, msg=False):
    if pulp is None:
        raise RuntimeError("Falta la librería PuLP (pip install pulp==2.9.0).")
    MQ, PD, T = inst["maquinas"], inst["pedidos"], inst["T"]
    M, J, TT = range(len(MQ)), range(len(PD)), range(T)
    pares = [tuple(p) for p in inst["pares"]]
    prob = pulp.LpProblem("LiftRental", pulp.LpMinimize)
    x = {(m, j): pulp.LpVariable(f"x_{m}_{j}", cat="Binary") for (m, j) in pares}
    y = {(m, t): pulp.LpVariable(f"y_{m}_{t}", cat="Binary")
         for m in M for t in TT if t >= MQ[m]["ret"] and inicio_valido(inst, t, MQ[m]["delta"])}
    z = {j: pulp.LpVariable(f"z_{j}", cat="Binary") for j in J}
    H = {(m, t): pulp.LpVariable(f"H_{m}_{t}", lowBound=0) for m in M for t in range(T + 1)}
    Y = lambda m, t: y.get((m, t), 0)

    # (5) función objetivo
    obj = (pulp.lpSum(MQ[m]["kappa"] * v for (m, t), v in y.items())
           + pulp.lpSum(PD[j]["r"] * z[j] for j in J)
           + pulp.lpSum((PD[j]["g"] + PI_OK * PD[j]["f"]) * v for (m, j), v in x.items()))
    if terminal:                                                         # (14)
        obj += pulp.lpSum(PEN_TERMINAL * MQ[m]["kappa"] * (1 - pulp.lpSum(Y(m, t) for t in TT))
                          for m in M if necesita_servicio(inst, MQ[m]))
    prob += obj

    por_pedido = {j: [] for j in J}
    activos = {(m, t): [] for m in M for t in TT}
    for (m, j) in pares:
        por_pedido[j].append(x[m, j])
        for t in range(PD[j]["s"], PD[j]["e"] + 1):
            activos[m, t].append(x[m, j])
    for j in J:                                                          # (6)
        prob += pulp.lpSum(por_pedido[j]) + z[j] == 1, f"cobertura_{j}"
    for m in M:
        q = MQ[m]
        d, u, th = q["delta"], q["u"], theta(q)
        prob += H[m, 0] == q["h0"], f"h0_{m}"                             # (8)
        for t in TT:
            occ = pulp.lpSum(activos[m, t])
            taller = [y[m, k] for k in range(t - d + 1, t + 1) if (m, k) in y]
            if activos[m, t] or len(taller) > 1:                         # (7)
                prob += occ + pulp.lpSum(taller) <= 1, f"excl_{m}_{t}"
            exo = u if t < q["ret"] else 0.0                             # horas en obra antes del retorno
            prob += H[m, t + 1] >= H[m, t] + u * occ + exo - G * Y(m, t), f"horas_{m}_{t}"   # (9)
            if control and activos[m, t]:
                prob += H[m, t + 1] <= q["U"] + G * (1 - occ), f"tope_h_{m}_{t}"           # (10)
                if t > th:                                                                   # (11)
                    prob += occ <= pulp.lpSum(y[m, k] for k in range(0, t - d + 1) if (m, k) in y), \
                        f"tope_cal_{m}_{t}"
        ys = [v for (mm, t), v in y.items() if mm == m]
        if len(ys) > 1:
            prob += pulp.lpSum(ys) <= 1, f"unico_{m}"                    # (13)
    for t in TT:                                                         # (12)
        carga = [(MQ[m]["tau"] / MQ[m]["delta"]) * y[m, k]
                 for m in M for k in range(t - MQ[m]["delta"] + 1, t + 1) if (m, k) in y]
        if carga:
            prob += pulp.lpSum(carga) <= inst["cap"][t], f"capacidad_{t}"
    if not control:
        for v in y.values():
            v.upBound = 0

    ws = inicial is not None
    if ws:
        a0, s0 = inicial
        for (m, j), v in x.items():
            v.setInitialValue(1 if a0.get(j) == m else 0)
        for j, v in z.items():
            v.setInitialValue(0 if j in a0 else 1)
        for (m, t), v in y.items():
            v.setInitialValue(1 if s0.get(m) == t else 0)

    log = os.path.join(tempfile.gettempdir(), f"sgm_cbc_{os.getpid()}_{threading.get_ident()}.log")
    t0 = time.time()
    intentos = 0
    while True:      # CBC en Windows puede colgarse al arrancar: el vigía lo detiene y se reintenta una vez
        intentos += 1
        try:
            with _Vigia(tlim + 30):
                st = prob.solve(pulp.PULP_CBC_CMD(msg=msg, gapRel=gap, timeLimit=tlim, warmStart=ws,
                                                  logPath=log, threads=threads or 1))
            break
        except pulp.PulpSolverError:
            if intentos >= 2:
                raise RuntimeError("El solver CBC no respondió (se detuvo por el tiempo límite dos veces).")
    seg = time.time() - t0
    asig = {j: m for (m, j), v in x.items() if (v.value() or 0) > 0.5}
    serv = {m: t for (m, t), v in y.items() if (v.value() or 0) > 0.5}
    info = dict(status=pulp.LpStatus[st], sol_status=prob.sol_status, seg=round(seg, 2),
                nvars=len(prob.variables()), ncons=len(prob.constraints), npares=len(pares),
                nbin=len(x) + len(y) + len(z), obj=pulp.value(prob.objective), **_log_cbc(log))
    info["optimo"] = info["status"] == "Optimal" and not info.get("parado")
    try:
        os.remove(log)
    except OSError:
        pass
    return asig, serv, info


class _Vigia:
    """Detiene el proceso de CBC si supera el tiempo límite más un margen. PuLP espera al proceso sin
    plazo; aquí se intercepta su creación para poder terminarlo si queda colgado."""
    _lock = threading.Lock()

    def __init__(self, plazo):
        self.plazo, self.procs, self.timer = plazo, [], None

    def __enter__(self):
        import subprocess
        from pulp.apis import coin_api
        self._lock.acquire()
        real = subprocess.Popen

        def popen(*a, **k):
            p = real(*a, **k)
            self.procs.append(p)
            return p
        self._orig = coin_api.subprocess
        proxy = type("subprocess_vigilado", (), {})()
        for n in dir(subprocess):
            if not n.startswith("__"):
                setattr(proxy, n, getattr(subprocess, n))
        proxy.Popen = popen
        coin_api.subprocess = proxy
        self.timer = threading.Timer(self.plazo, self._matar)
        self.timer.daemon = True
        self.timer.start()
        return self

    def _matar(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()

    def __exit__(self, *exc):
        from pulp.apis import coin_api
        self.timer.cancel()
        coin_api.subprocess = self._orig
        self._lock.release()
        return False


def _log_cbc(ruta):
    try:
        txt = open(ruta, encoding="utf-8", errors="ignore").read()
    except OSError:
        return {}
    bp = re.findall(r"best possible ([0-9.\-e+]+)", txt)
    gap = re.findall(r"Gap:\s*([0-9.]+)", txt)
    return dict(cota=float(bp[-1]) if bp else None, gap=float(gap[-1]) if gap else None,
                parado="Stopped on time" in txt or "time limit" in txt.lower())


# ===================================================================== E0: práctica actual
def simular_E0(inst):
    """Despacho por orden de inicio con la primera máquina compatible libre (la de menor altura),
    sin mirar el mantenimiento; taller reactivo sólo con los vencidos que están en el patio."""
    MQ, PD, T, cap = inst["maquinas"], inst["pedidos"], inst["T"], inst["cap"]
    nM = len(MQ)
    ocup_hasta = [q["ret"] - 1 for q in MQ]
    H = [q["h0"] for q in MQ]
    dlast = [-q["d0"] for q in MQ]
    serv, asig, alq = {}, {}, {m: [] for m in range(nM)}
    carga = [0.0] * T
    orden = sorted(range(len(PD)), key=lambda j: (PD[j]["s"], j))
    for t in range(T):
        for j in [jj for jj in orden if PD[jj]["s"] == t]:
            o = PD[j]
            cand = [m for m in range(nM) if MQ[m]["categoria"] == o["categoria"]
                    and MQ[m]["h_trab"] + 1e-9 >= o["hreq"] and MQ[m]["cap_kg"] + 1e-9 >= o["cap"]
                    and ocup_hasta[m] < t and MQ[m]["ret"] <= t]
            if cand:
                m = min(cand, key=lambda k: (MQ[k]["h_trab"], k))
                asig[j] = m
                ocup_hasta[m] = o["e"]
                alq[m].append((o["s"], o["e"]))
        venc = []
        for m in range(nM):
            if m in serv or ocup_hasta[m] >= t:
                continue
            dias = t - dlast[m]
            if dias >= MQ[m]["Dcal"] or H[m] >= MQ[m]["U"]:
                if inicio_valido(inst, t, MQ[m]["delta"]):
                    venc.append((-dias, m))
        for _, m in sorted(venc):
            d = MQ[m]["delta"]
            load = MQ[m]["tau"] / d
            if all(carga[k] + load <= cap[k] + 1e-9 for k in range(t, t + d)):
                for k in range(t, t + d):
                    carga[k] += load
                serv[m] = t
                ocup_hasta[m] = t + d - 1
                H[m], dlast[m] = 0.0, t
        for m in range(nM):
            if any(s <= t <= e for (s, e) in alq[m]) or MQ[m]["ret"] > t:
                H[m] += MQ[m]["u"]
    return asig, serv


# ===================================================================== evaluador único
def evaluar(inst, asig, serv, pi_v=PI_VENC):
    """Recorre la solución día a día con el mismo procedimiento para todos los escenarios."""
    MQ, PD, T = inst["maquinas"], inst["pedidos"], inst["T"]
    nM = len(MQ)
    H = [q["h0"] for q in MQ]
    dlast = [-q["d0"] for q in MQ]
    rent = {m: [] for m in range(nM)}
    for j, m in asig.items():
        rent[m].append(j)
    dm_venc, riesgo, venc_ped = 0, set(), {}
    carga = [0.0] * T
    dom = set(inst["domingos"])
    serv_domingo = 0
    for m, t in serv.items():
        d = MQ[m]["delta"]
        for k in range(t, min(T, t + d)):
            carga[k] += MQ[m]["tau"] / d
            serv_domingo += k in dom
    solapes = 0
    for m in range(nM):
        dias_m = {}
        for j in rent[m]:
            for t in range(PD[j]["s"], PD[j]["e"] + 1):
                dias_m[t] = dias_m.get(t, 0) + 1
        if m in serv:
            for t in range(serv[m], serv[m] + MQ[m]["delta"]):
                dias_m[t] = dias_m.get(t, 0) + 1
        for t in range(MQ[m]["ret"]):
            if t in dias_m:
                dias_m[t] += 1
        solapes += sum(1 for v in dias_m.values() if v > 1)
    for t in range(T):
        for m in range(nM):
            if serv.get(m) == t:
                H[m], dlast[m] = 0.0, t
            js = [j for j in rent[m] if PD[j]["s"] <= t <= PD[j]["e"]]
            if js or MQ[m]["ret"] > t:
                H[m] += MQ[m]["u"]
            if js and ((t - dlast[m] >= MQ[m]["Dcal"]) or (H[m] > MQ[m]["U"] + 1e-6)):
                dm_venc += 1
                riesgo.update(js)
                for j in js:
                    venc_ped[j] = venc_ped.get(j, 0) + 1
    venc_cierre = [m for m in range(nM)
                   if (T - dlast[m] >= MQ[m]["Dcal"]) or H[m] >= MQ[m]["U"]]
    ingreso = sum(PD[j]["r"] for j in asig)
    trasl = sum(PD[j]["g"] for j in asig)
    cmant = sum(MQ[m]["kappa"] for m in serv)
    cfalla = sum(PD[j]["f"] * (pi_v if j in riesgo else PI_OK) for j in asig)
    deuda = sum(MQ[m]["kappa"] for m in venc_cierre)
    margen = ingreso - trasl - cmant - cfalla
    exceso = [t for t in range(T) if carga[t] > inst["cap"][t] + 1e-6]
    return dict(atendidos=len(asig), no_atendidos=len(PD) - len(asig), ingreso=round(ingreso, 2),
                dm_vencido=dm_venc, ped_riesgo=len(riesgo), costo_falla=round(cfalla, 2),
                servicios=len(serv), costo_mant=round(cmant, 2), venc_cierre=len(venc_cierre),
                deuda=round(deuda, 2), traslado=round(trasl, 2), margen=round(margen, 2),
                margen_deuda=round(margen - deuda, 2), carga=[round(c, 3) for c in carga],
                tecdias=round(sum(carga), 2), carga_max=round(max(carga) if carga else 0, 2),
                verif=dict(violaciones=dm_venc, solapes=solapes, serv_domingo=serv_domingo,
                           dias_exceso=len(exceso)),
                venc_ped=venc_ped, venc_cierre_ids=venc_cierre)


def completar_cierre(inst, asig, serv):
    """Fase de cierre: programa en la capacidad ociosa los servicios pendientes de los equipos que
    quedarían vencidos al cierre, sin modificar la asignación."""
    MQ, PD, T = inst["maquinas"], inst["pedidos"], inst["T"]
    serv = dict(serv)
    carga = [0.0] * T
    for m, t in serv.items():
        for k in range(t, t + MQ[m]["delta"]):
            carga[k] += MQ[m]["tau"] / MQ[m]["delta"]
    ocup = {m: set(range(0, q["ret"])) for m, q in enumerate(MQ)}
    for j, m in asig.items():
        ocup[m].update(range(PD[j]["s"], PD[j]["e"] + 1))
    for m, t in serv.items():
        ocup[m].update(range(t, t + MQ[m]["delta"]))
    pend = sorted((m for m in range(len(MQ)) if m not in serv), key=lambda m: -MQ[m]["kappa"])
    for m in pend:
        q = MQ[m]
        if not necesita_servicio(inst, q):
            continue
        d, load = q["delta"], q["tau"] / q["delta"]
        for t in range(q["ret"], T):
            if not inicio_valido(inst, t, d):
                continue
            rng_ = range(t, t + d)
            if any(k in ocup[m] for k in rng_):
                continue
            if any(PD[j]["s"] > t for j, mm in asig.items() if mm == m):
                continue
            if all(carga[k] + load <= inst["cap"][k] + 1e-9 for k in rng_):
                for k in rng_:
                    carga[k] += load
                serv[m] = t
                ocup[m].update(rng_)
                break
    return serv


# ===================================================================== corrida completa
def _plan(inst, asig, serv, ev):
    MQ, PD = inst["maquinas"], inst["pedidos"]
    pares = {}
    for (m, j) in inst["pares"]:
        pares.setdefault(j, []).append(m)
    asign = [dict(j=j, pedido_id=PD[j]["id"], m=m, equipo_id=MQ[m]["id"], codigo=MQ[m]["codigo"],
                  s=PD[j]["s"], e=PD[j]["e"], dias_vencido=ev["venc_ped"].get(j, 0))
             for j, m in sorted(asig.items(), key=lambda kv: (PD[kv[0]]["s"], kv[0]))]
    servs = [dict(m=m, equipo_id=MQ[m]["id"], codigo=MQ[m]["codigo"], t=t, fin=t + MQ[m]["delta"] - 1,
                  nivel=MQ[m]["nivel"], costo=MQ[m]["kappa"], td=MQ[m]["tau"], delta=MQ[m]["delta"])
             for m, t in sorted(serv.items(), key=lambda kv: (kv[1], MQ[kv[0]]["codigo"]))]
    no_at = []
    for j, o in enumerate(PD):
        if j in asig:
            continue
        cands = pares.get(j, [])
        if not cands:
            mot = "Ningún equipo compatible está libre en la fecha de inicio"
        elif o["r"] <= o["g"] + PI_OK * o["f"]:
            mot = "El ingreso del pedido no cubre el traslado y el riesgo de falla"
        else:
            mot = f"Los {len(cands)} equipos compatibles están ocupados o deben pasar por taller"
        no_at.append(dict(j=j, pedido_id=o["id"], motivo=mot, n_comp=len(cands)))
    return dict(asig=asign, serv=servs, no_atendidos=no_at)


def correr(inst, gap=0.01, tlim=120, tlim_e3=180, con_e3=True, pi_v=PI_VENC, avance=lambda *_: None):
    """Resuelve E0-E3 sobre la misma instancia y valora todo con el evaluador único."""
    res = dict(escenarios={}, solver={}, sensibilidad=[], descomposicion=[])
    sols = {}
    avance("Simulando la práctica actual (E0)")
    sols["E0"] = simular_E0(inst)
    avance("Resolviendo E1: asignación óptima sin control de mantenimiento")
    a, s, info = resolver(inst, control=False, gap=gap, tlim=tlim)
    sols["E1"], res["solver"]["E1"] = (a, s), info
    avance("Resolviendo E2: modelo propuesto (control de horas y calendario)")
    a, s, info = resolver(inst, control=True, gap=gap, tlim=tlim)
    sols["E2"], res["solver"]["E2"] = (a, s), info
    if con_e3:
        avance("Resolviendo E3: modelo con valor terminal (parte de la solución de E2)")
        a3, s3, info = resolver(inst, control=True, terminal=True, gap=gap, tlim=tlim_e3, inicial=sols["E2"])
        if not a3 and not s3 and sols["E2"][0]:          # sin solución en el tiempo: se conserva E2
            a3, s3 = sols["E2"]
            info["nota"] = "Sin solución mejor en el tiempo límite; se parte de E2."
        s3c = completar_cierre(inst, a3, s3)
        info["cierre"] = len(s3c) - len(s3)
        sols["E3"], res["solver"]["E3"] = (a3, s3c), info
    avance("Evaluando los escenarios con el evaluador único")
    for k, (a, s) in sols.items():
        ev = evaluar(inst, a, s, pi_v)
        res["escenarios"][k] = dict(nombre=ESCENARIOS[k], ind={x: v for x, v in ev.items()
                                                                if x not in ("venc_ped", "venc_cierre_ids")},
                                    plan=_plan(inst, a, s, ev), venc_cierre=[inst["maquinas"][m]["codigo"]
                                                                             for m in ev["venc_cierre_ids"]])
    E = {k: v["ind"] for k, v in res["escenarios"].items()}
    trans = [("Efecto de optimizar la asignación", "E0", "E1"),
             ("Efecto del control del mantenimiento", "E1", "E2"),
             ("Beneficio del modelo propuesto", "E0", "E2")]
    if "E3" in E:
        trans.append(("Beneficio del modelo con valor terminal", "E0", "E3"))
    res["descomposicion"] = [dict(componente=c, de=a, a=b, margen=round(E[b]["margen"] - E[a]["margen"], 2),
                                  margen_deuda=round(E[b]["margen_deuda"] - E[a]["margen_deuda"], 2))
                             for c, a, b in trans]
    for p in SENS_PI:
        fila = dict(pi=p)
        for k in ("E0", "E2", "E3"):
            if k in sols:
                ev = evaluar(inst, *sols[k], pi_v=p)
                fila[k] = dict(falla=ev["costo_falla"], margen_deuda=ev["margen_deuda"])
        fila["beneficio"] = round(fila["E2"]["margen_deuda"] - fila["E0"]["margen_deuda"], 2)
        if "E3" in fila:
            fila["beneficio_e3"] = round(fila["E3"]["margen_deuda"] - fila["E0"]["margen_deuda"], 2)
        res["sensibilidad"].append(fila)
    res["umbral_pi"] = _umbral(res["sensibilidad"], "beneficio")
    res["umbral_pi_e3"] = _umbral(res["sensibilidad"], "beneficio_e3")
    return res


def _umbral(filas, clave):
    """P(falla | vencido) a partir de la cual el modelo supera a la práctica actual (interpolación)."""
    pts = [(f["pi"], f[clave]) for f in filas if clave in f]
    for (p1, b1), (p2, b2) in zip(pts, pts[1:]):
        if b1 < 0 <= b2:
            return round(p1 + (p2 - p1) * (-b1) / (b2 - b1), 3)
    if pts and pts[0][1] >= 0:
        return 0.0
    return None


def resumen_instancia(inst):
    MQ, PD = inst["maquinas"], inst["pedidos"]
    cont = {}
    for q in MQ:
        cont[q["estado0"]] = cont.get(q["estado0"], 0) + 1
    return dict(maquinas=len(MQ), pedidos=len(PD), pares=len(inst["pares"]), T=inst["T"], T0=inst["T0"],
                en_obra=sum(1 for q in MQ if q["situacion"] == "En obra"),
                en_obra_vencidos=sum(1 for q in MQ if q["situacion"] == "En obra"
                                     and q["estado0"] in ("Vencido", "Sin datos")),
                estados=cont, cap_total=round(sum(inst["cap"]), 1), tecnicos=inst["tecnicos"],
                necesitan=sum(1 for q in MQ if necesita_servicio(inst, q)),
                tecdias_puesta=round(sum(q["tau"] for q in MQ if necesita_servicio(inst, q)), 1),
                mezcla={c: sum(1 for o in PD if o["categoria"] == c) for c in L.CATEGORIAS})


# ===================================================================== ejecución en segundo plano
_HILOS = {}


def lanzar(conectar, corrida_id):
    """Ejecuta la corrida en un hilo para que la página no quede esperando al solver."""
    h = threading.Thread(target=_trabajo, args=(conectar, corrida_id), daemon=True)
    _HILOS[corrida_id] = h
    h.start()


def en_curso(corrida_id):
    h = _HILOS.get(corrida_id)
    return bool(h and h.is_alive())


def _trabajo(conectar, cid):
    con = conectar()
    t0 = time.time()

    def avance(msg):
        con.execute("UPDATE corridas SET mensaje=? WHERE id=?", (msg, cid))
        con.commit()

    try:
        c = con.execute("SELECT * FROM corridas WHERE id=?", (cid,)).fetchone()
        p = json.loads(c["parametros"] or "{}")
        cfg = L.leer_config(con)
        avance("Construyendo la instancia con el estado actual de la flota")
        inst = construir_instancia(con, cfg, c["fecha_inicio"], c["horizonte"])
        if not inst["pedidos"]:
            raise RuntimeError("La cartera no tiene pedidos pendientes dentro del horizonte.")
        res = correr(inst, gap=p.get("gap", 0.01), tlim=p.get("tlim", 120), tlim_e3=p.get("tlim_e3", 180),
                     con_e3=p.get("e3", True), pi_v=p.get("pi_v", PI_VENC), avance=avance)
        res["instancia"] = inst
        res["resumen"] = resumen_instancia(inst)
        con.execute("""UPDATE corridas SET estado='Terminada', mensaje=?, resultados=?, duracion_seg=?
                       WHERE id=?""", ("Corrida terminada", json.dumps(res, ensure_ascii=False),
                                       round(time.time() - t0, 1), cid))
        con.commit()
    except Exception as e:
        con.execute("UPDATE corridas SET estado='Error', mensaje=?, duracion_seg=? WHERE id=?",
                    (f"{e}", round(time.time() - t0, 1), cid))
        con.commit()
        traceback.print_exc()
    finally:
        con.close()


def marcar_interrumpidas(con):
    """Al iniciar el sistema, las corridas que quedaron 'En curso' se marcan como interrumpidas."""
    con.execute("""UPDATE corridas SET estado='Error', mensaje='Interrumpida: el sistema se cerró durante
                   la corrida. Vuelva a ejecutarla.' WHERE estado='En curso'""")


# ===================================================================== cartera calibrada
def generar_cartera(db, cfg, T0, T=60, semilla=20260922, factor=1.0):
    """Cartera de pedidos calibrada con los contratos del sistema (como en el informe, 4.2.4):
    tasa de llegada por la ley de Little (equipos en obra ÷ duración media de los contratos),
    mezcla de tipos y zonas, duraciones empíricas, alturas de la flota. Llegadas uniformes en el
    horizonte. Devuelve la cantidad de pedidos creados."""
    rng = random.Random(semilla)
    T0 = L.a_fecha(T0)
    contratos = db.execute("""SELECT a.*, e.categoria FROM alquileres a JOIN equipos e ON e.id=a.equipo_id
                              WHERE a.fecha_vencimiento IS NOT NULL""").fetchall()
    dur = [d for d in ((L.a_fecha(a["fecha_vencimiento"]) - L.a_fecha(a["fecha_salida"])).days
                       for a in contratos) if d > 0]
    if not dur:
        raise RuntimeError("No hay contratos con fechas para calibrar la demanda.")
    en_obra = db.execute("SELECT COUNT(*) FROM alquileres WHERE estado='En obra'").fetchone()[0]
    lam = en_obra / (sum(dur) / len(dur))                          # ley de Little
    n = max(1, int(round(lam * T * factor)))
    mezcla = [a["categoria"] for a in contratos]
    zonas = [a["zona"] for a in contratos if a["zona"]] or L.ZONAS
    obras = [(a["cliente"], a["obra"]) for a in contratos if a["cliente"] and a["obra"]]
    alturas = {}
    for e in db.execute("SELECT categoria, altura_trabajo FROM equipos WHERE activo=1 AND altura_trabajo>0"):
        alturas.setdefault(e["categoria"], []).append(e["altura_trabajo"])
    for _ in range(n):
        cat = rng.choice(mezcla)
        href = rng.choice(alturas.get(cat) or [10])
        hreq = max(5.0, round(href - rng.choice([0, 0, 0.5, 1, 2]), 1))
        cap = 227 if rng.random() < 0.8 else rng.choice([300, 318, 350])
        if cat.startswith("Brazo"):
            cap = min(cap, 227)
        elif cat.startswith("Mástil"):
            cap = 136
        d = rng.choice(dur)
        s = rng.randrange(T)
        zona = rng.choice(zonas)
        cli, obra = rng.choice(obras) if obras else ("Cliente", "Obra")
        fi = T0 + timedelta(days=s)
        db.execute("""INSERT INTO pedidos(cliente, obra, zona, categoria, altura_req, capacidad_req,
                      fecha_inicio, fecha_fin, tarifa_dia, costo_traslado, estado, origen)
                      VALUES (?,?,?,?,?,?,?,?,?,?, 'Pendiente', 'Cartera calibrada')""",
                   (cli, obra, zona, cat, hreq, cap, L.iso(fi), L.iso(fi + timedelta(days=d - 1)),
                    L.tarifa_sugerida(dict(categoria=cat, altura_trabajo=hreq)),
                    costo_traslado_defecto(zona, cat, rng)))
    return n, round(lam, 3)


# ===================================================================== aplicar el plan (horizonte rodante)
def aplicar_plan(db, cfg, corrida, esc, ventana=7):
    """Ejecuta las decisiones de la primera semana del plan (horizonte rodante, informe 5.2.2):
    programa las OT preventivas que inician en la ventana, con técnicos libres, y reserva los equipos
    asignados a los pedidos que inician en la ventana. El resto se recalcula en la corrida siguiente."""
    res = json.loads(corrida["resultados"])
    inst = res["instancia"]
    T0 = L.a_fecha(inst["T0"])
    plan = res["escenarios"][esc]["plan"]
    tecs = [t["id"] for t in db.execute("SELECT id FROM tecnicos WHERE activo=1 AND ejecuta=1 ORDER BY id")]
    hechos = dict(ot_nuevas=0, ot_actualizadas=0, ot_pendientes=0, reservas=0, no_atendidos=0,
                  tecnicos_faltantes=0)
    origen = f"Optimización PLEM · corrida {corrida['id']} ({esc})"

    # El plan reemplaza la programación previa de los preventivos que aún no empiezan: los que el modelo
    # programa después de la ventana o no programa vuelven a «Pendiente» y liberan a sus técnicos.
    plan_m = {s["equipo_id"]: s for s in plan["serv"]}
    en_inst = {q["id"] for q in inst["maquinas"]}
    for o in db.execute("""SELECT id, equipo_id FROM ordenes WHERE tipo='Preventivo'
                           AND estado IN ('Pendiente','Programada')""").fetchall():
        if o["equipo_id"] not in en_inst:
            continue
        db.execute("DELETE FROM orden_tecnicos WHERE orden_id=?", (o["id"],))
        s = plan_m.get(o["equipo_id"])
        if s is None or s["t"] >= ventana:
            fecha = L.iso(T0 + timedelta(days=s["t"])) if s else None
            nota = (f"Corrida {corrida['id']}: el modelo propone iniciar el {T0 + timedelta(days=s['t']):%d/%m/%Y}; "
                    "se confirma en la próxima corrida") if s else \
                   f"Corrida {corrida['id']}: el modelo no programa este servicio en el horizonte"
            db.execute("""UPDATE ordenes SET estado='Pendiente', fecha_programada=?,
                          observaciones=COALESCE(observaciones||' · ','')||? WHERE id=?""", (fecha, nota, o["id"]))
            hechos["ot_pendientes"] += 1
    agenda = L.agenda_tecnicos(db, T0, inst["T"] + 10)
    ocupado = {(tid, d) for tid, ds in agenda.items() for d in ds}
    for s in plan["serv"]:
        if s["t"] >= ventana:
            continue
        ini = T0 + timedelta(days=s["t"])
        ds = [ini + timedelta(days=k) for k in range(s["delta"])]
        nec = max(1, math.ceil(s["td"] / s["delta"] - 1e-9))
        libres = sorted((t for t in tecs if all((t, d) not in ocupado for d in ds)),
                        key=lambda t: sum(1 for (tt, _) in ocupado if tt == t))
        asignados = libres[:nec]
        hechos["tecnicos_faltantes"] += nec - len(asignados)
        for t in asignados:
            for d in ds:
                ocupado.add((t, d))
        abierta = db.execute("""SELECT id FROM ordenes WHERE equipo_id=? AND tipo='Preventivo'
                                AND estado IN ('Pendiente','Programada') ORDER BY id DESC LIMIT 1""",
                             (s["equipo_id"],)).fetchone()
        datos = (s["nivel"], L.iso(ini), s["delta"], s["td"], s["costo"])
        if abierta:
            oid = abierta["id"]
            db.execute("""UPDATE ordenes SET estado='Programada', nivel=?, fecha_programada=?, duracion_dias=?,
                          tecnico_dias=?, costo_estimado=?, origen=? WHERE id=?""", datos + (origen, oid))
            db.execute("DELETE FROM orden_tecnicos WHERE orden_id=?", (oid,))
            hechos["ot_actualizadas"] += 1
        else:
            oid = db.execute(
                """INSERT INTO ordenes(equipo_id, tipo, nivel, estado, prioridad, fecha_programada, duracion_dias,
                   tecnico_dias, costo_estimado, origen, descripcion)
                   VALUES (?, 'Preventivo', ?, 'Programada', 'Alta', ?, ?, ?, ?, ?, ?)""",
                (s["equipo_id"], s["nivel"], L.iso(ini), s["delta"], s["td"], s["costo"], origen,
                 f"Servicio {s['nivel']} programado por el modelo de optimización")).lastrowid
            hechos["ot_nuevas"] += 1
        for t in asignados:
            db.execute("INSERT OR IGNORE INTO orden_tecnicos VALUES (?,?)", (oid, t))
    for a in plan["asig"]:
        if a["s"] >= ventana:
            continue
        db.execute("""UPDATE pedidos SET estado='Asignado', equipo_id=?, corrida_id=?
                      WHERE id=? AND estado IN ('Pendiente','Asignado')""",
                   (a["equipo_id"], corrida["id"], a["pedido_id"]))
        hechos["reservas"] += 1
    PD = {o["id"]: o for o in inst["pedidos"]}
    for n in plan["no_atendidos"]:
        if PD[n["pedido_id"]]["s"] >= ventana:
            continue
        db.execute("""UPDATE pedidos SET estado='No atendido', equipo_id=NULL, corrida_id=?,
                      notas=COALESCE(notas||' · ','')||? WHERE id=? AND estado IN ('Pendiente','Asignado')""",
                   (corrida["id"], f"Modelo: {n['motivo'].lower()}", n["pedido_id"]))
        hechos["no_atendidos"] += 1
    db.execute("UPDATE corridas SET aplicada=datetime('now','localtime'), aplicada_esc=? WHERE id=?",
               (esc, corrida["id"]))
    return hechos
