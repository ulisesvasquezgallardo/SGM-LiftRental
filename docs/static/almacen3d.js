// Vista isométrica del almacén de máquinas: patio, taller, técnicos y obras.
// Se dibuja en SVG a partir de los datos que entrega la ruta «/» (escena_almacen).
(function () {
  const cont = document.getElementById('escena');
  const fuente = document.getElementById('escena-datos');
  if (!cont || !fuente) return;
  const D = JSON.parse(fuente.textContent);

  // ---------------------------------------------------------------- proyección y primitivas
  const A = 28;                                   // píxeles por unidad de piso
  const lim = { x0: 1e9, x1: -1e9, y0: 1e9, y1: -1e9 };
  function P(x, y, z) {
    const px = (x - y) * A, py = (x + y) * A / 2 - (z || 0) * A;
    if (px < lim.x0) lim.x0 = px; if (px > lim.x1) lim.x1 = px;
    if (py < lim.y0) lim.y0 = py; if (py > lim.y1) lim.y1 = py;
    return [px, py];
  }
  const f1 = (v) => v.toFixed(1);
  const pts = (arr) => arr.map((p) => P(p[0], p[1], p[2]).map(f1).join(',')).join(' ');
  const poly = (arr, fill, extra) => `<polygon points="${pts(arr)}" fill="${fill}" ${extra || ''}/>`;
  const linea = (a, b, stroke, w, extra) => {
    const p = P(a[0], a[1], a[2]), q = P(b[0], b[1], b[2]);
    return `<line x1="${f1(p[0])}" y1="${f1(p[1])}" x2="${f1(q[0])}" y2="${f1(q[1])}" stroke="${stroke}" stroke-width="${w}" stroke-linecap="round" ${extra || ''}/>`;
  };
  function tono(hex, f) {
    const n = parseInt(hex.slice(1), 16);
    const c = [n >> 16, (n >> 8) & 255, n & 255].map((v) => Math.max(0, Math.min(255, Math.round(v * f))));
    return '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('');
  }
  const caras = (c) => [c, tono(c, 0.84), tono(c, 0.68)];
  function caja(x, y, z, dx, dy, dz, c) {            // c = [techo, cara izquierda, cara derecha]
    if (typeof c === 'string') c = caras(c);
    return poly([[x, y + dy, z], [x + dx, y + dy, z], [x + dx, y + dy, z + dz], [x, y + dy, z + dz]], c[1]) +
      poly([[x + dx, y, z], [x + dx, y + dy, z], [x + dx, y + dy, z + dz], [x + dx, y, z + dz]], c[2]) +
      poly([[x, y, z + dz], [x + dx, y, z + dz], [x + dx, y + dy, z + dz], [x, y + dy, z + dz]], c[0]);
  }
  function baranda(x, y, z, dx, dy, h, col) {
    let s = '';
    [[x, y], [x + dx, y], [x + dx, y + dy], [x, y + dy]].forEach((e) => { s += linea([e[0], e[1], z], [e[0], e[1], z + h], col, 1); });
    return s + poly([[x, y, z + h], [x + dx, y, z + h], [x + dx, y + dy, z + h], [x, y + dy, z + h]], 'none', `stroke="${col}" stroke-width="1.2"`);
  }
  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  // texto «pintado» sobre el piso o sobre un muro (eje = 'x' sigue el eje gx; 'y' sigue el eje gy)
  function rotulo(t, x, y, z, eje, attrs) {
    const p = P(x, y, z);
    const m = eje === 'y' ? `matrix(1,-0.5,0,1,${f1(p[0])},${f1(p[1])})` : `matrix(1,0.5,0,1,${f1(p[0])},${f1(p[1])})`;
    return `<text transform="${m}" ${attrs}>${esc(t)}</text>`;
  }

  const COL = { tij: '#f2b200', bel: '#3b82d6', bdi: '#e8772e', mas: '#8b5fd0' };
  const EST = { 'Vigente': '#15803d', 'Por vencer': '#e59a1b', 'Vencido': '#c62828', 'Sin datos': '#9aa1ad' };
  const GRIS = ['#e9edf2', '#c5ccd6', '#aab2be'], NEGRO = ['#3a404b', '#2b303a', '#20242c'];

  // ---------------------------------------------------------------- máquinas
  function tijera(cx, cy, h, col) {
    const z0 = 0.24, zp = z0 + h, yn = cy + 0.26, yl = cy - 0.26;
    let s = caja(cx - 0.4, cy - 0.24, 0, 0.8, 0.48, 0.08, NEGRO) + caja(cx - 0.42, cy - 0.26, 0.08, 0.84, 0.52, 0.16, col);
    if (h > 0.2) {
      const n = h > 0.7 ? 2 : 1, dz = h / n;
      for (let i = 0; i < n; i++) {
        const a = z0 + i * dz, b = a + dz;
        s += linea([cx - 0.36, yl, a], [cx + 0.36, yl, b], '#9aa1ad', 1.2) + linea([cx + 0.36, yl, a], [cx - 0.36, yl, b], '#9aa1ad', 1.2);
        s += linea([cx - 0.36, yn, a], [cx + 0.36, yn, b], '#4b5563', 1.7) + linea([cx + 0.36, yn, a], [cx - 0.36, yn, b], '#4b5563', 1.7);
      }
    }
    return s + caja(cx - 0.44, cy - 0.27, zp, 0.88, 0.54, 0.06, GRIS) + baranda(cx - 0.44, cy - 0.27, zp + 0.06, 0.88, 0.54, 0.28, tono(col, 0.72));
  }
  function brazo(cx, cy, alto, col) {
    const osc = tono(col, 0.7);
    const tr = alto ? [[cx - 0.08, cy, 0.42], [cx - 0.4, cy, 1.05 + alto * 0.3], [cx + 0.44, cy, 1.35 + alto * 0.55]]
      : [[cx - 0.08, cy, 0.42], [cx - 0.42, cy, 0.66], [cx + 0.4, cy, 0.74]];
    const fin = tr[2];
    let s = caja(cx - 0.4, cy - 0.23, 0, 0.8, 0.46, 0.08, NEGRO) + caja(cx - 0.42, cy - 0.25, 0.08, 0.84, 0.5, 0.18, col) +
      caja(cx - 0.24, cy - 0.16, 0.26, 0.36, 0.32, 0.16, caras(osc));
    s += `<polyline points="${pts(tr)}" fill="none" stroke="${osc}" stroke-width="3.6" stroke-linejoin="round" stroke-linecap="round"/>`;
    s += `<polyline points="${pts(tr)}" fill="none" stroke="${tono(col, 1.12)}" stroke-width="1.1" stroke-linejoin="round" stroke-linecap="round"/>`;
    return s + caja(fin[0], cy - 0.17, fin[2] - 0.04, 0.3, 0.34, 0.05, GRIS) + baranda(fin[0], cy - 0.17, fin[2] + 0.01, 0.3, 0.34, 0.24, osc);
  }
  function mastil(cx, cy, h, col) {
    return caja(cx - 0.28, cy - 0.23, 0, 0.56, 0.46, 0.2, col) + caja(cx - 0.07, cy - 0.06, 0.2, 0.13, 0.12, h, GRIS) +
      caja(cx - 0.2, cy - 0.19, 0.2 + h, 0.38, 0.38, 0.05, GRIS) + baranda(cx - 0.2, cy - 0.19, 0.25 + h, 0.38, 0.38, 0.24, tono(col, 0.72));
  }
  const corto = (c) => c.replace('TECL-', 'T-').replace('BECL-', 'BE-').replace('BCCL-', 'BC-').replace('UPCL-', 'UP-');

  const capas = [], encima = [];                    // capas: se ordenan por profundidad; encima: etiquetas
  function maquina(i, cx, cy, enObra, rotuloFrente) {
    const m = D.maqs[i], col = COL[m.k] || COL.tij, e = EST[m.est] || EST['Sin datos'];
    const alt = Math.min(Number(m.alt) || 8, 18) / 18;
    let cuerpo, tope;
    if (m.k === 'tij') { const h = enObra ? 0.45 + alt * 0.75 : 0.1; cuerpo = tijera(cx, cy, h, col); tope = 0.6 + h; }
    else if (m.k === 'mas') { const h = enObra ? 0.9 + alt * 0.6 : 0.55; cuerpo = mastil(cx, cy, h, col); tope = 0.5 + h; }
    else { cuerpo = brazo(cx, cy, enObra ? alt : 0, col); tope = enObra ? 1.7 + alt * 0.55 : 1.0; }
    const piso = poly([[cx - 0.5, cy - 0.45, 0], [cx + 0.5, cy - 0.45, 0], [cx + 0.5, cy + 0.45, 0], [cx - 0.5, cy + 0.45, 0]],
      e, `fill-opacity="0.3" stroke="${e}" stroke-width="1.6"`);
    const texto = (m.c + ' ' + m.mm + ' ' + (m.cli || '')).toLowerCase();
    const attrs = `href="${m.url}" data-i="${i}" data-cat="${esc(m.cat)}" data-texto="${esc(texto)}"`;
    capas.push({ k: cx + cy, s: `<a ${attrs} class="maq">${piso}${cuerpo}</a>` });
    const et = corto(m.c), we = et.length * 4.5 + 6, q = P(cx, cy, tope + 0.02);
    encima.push(`<a ${attrs} class="maq esc-tag"><rect x="${f1(q[0] - we / 2)}" y="${f1(q[1] - 9)}" width="${f1(we)}" height="9.5" rx="2.5" stroke="${e}"/>` +
      `<text x="${f1(q[0])}" y="${f1(q[1] - 1.8)}">${et}</text></a>`);
    if (m.ing && m.ing.d <= 7 && m.sit !== 'En taller') {
      const p = P(cx, cy, tope + 0.62);
      encima.push(`<g class="esc-aviso" data-cat="${esc(m.cat)}"><rect x="${f1(p[0] - 15)}" y="${f1(p[1] - 6)}" width="30" height="11" rx="5.5"/>` +
        `<text x="${f1(p[0])}" y="${f1(p[1] + 2.4)}">${m.ing.f}</text></g>`);
    }
  }

  // ---------------------------------------------------------------- técnicos
  function tecnico(j, x, y, lugar) {
    const t = D.tecs[j];
    const c = t.estado === 'Trabajando' ? '#f2b200' : (t.estado === 'Libre' ? '#22a35a' : '#9aa1ad');
    const b = P(x, y, 0), h = P(x, y, 0.75);
    const s = `<ellipse cx="${f1(b[0])}" cy="${f1(b[1])}" rx="10" ry="5" fill="${c}" fill-opacity="0.45" stroke="${c}"/>` +
      caja(x - 0.07, y - 0.06, 0, 0.14, 0.12, 0.32, ['#2d4f8f', '#27457d', '#1f3866']) +
      caja(x - 0.1, y - 0.08, 0.32, 0.2, 0.16, 0.3, ['#3f72c9', '#3563b3', '#2b5196']) +
      `<circle cx="${f1(h[0])}" cy="${f1(h[1])}" r="3.5" fill="#f1c9a5"/>` +
      `<path d="M${f1(h[0] - 4.2)},${f1(h[1] - 0.6)} a4.2,4.2 0 0 1 8.4,0 z" fill="#f2b200" stroke="#b98200" stroke-width="0.5"/>`;
    capas.push({ k: x + y + 0.02, s: `<a href="${t.url}" class="tec" data-j="${j}">${s}</a>` });
    const ot = t.ots[0];
    const txt = t.nc + ' · ' + (t.estado === 'Trabajando' && ot ? ot.c : t.estado.toLowerCase()) + (lugar ? ' (' + lugar + ')' : '');
    const w = txt.length * 4.6 + 12, p = P(x, y, 1.2);
    encima.push(`<a href="${t.url}" class="tec esc-nombre" data-j="${j}"><rect x="${f1(p[0] - w / 2)}" y="${f1(p[1] - 8)}" width="${f1(w)}" height="13" rx="6.5" fill="${c}"/>` +
      `<text x="${f1(p[0])}" y="${f1(p[1] + 1.6)}" fill="${t.estado === 'Trabajando' ? '#3a2c00' : '#fff'}">${esc(txt)}</text></a>`);
  }

  // ---------------------------------------------------------------- distribución
  const idx = D.maqs.map((_, i) => i);
  const noApto = (m) => m.est === 'Vencido' || m.est === 'Sin datos';
  const libresOk = idx.filter((i) => D.maqs[i].sit === 'Disponible' && !noApto(D.maqs[i]));
  const libresNo = idx.filter((i) => D.maqs[i].sit === 'Disponible' && noApto(D.maqs[i]));
  const fuera = idx.filter((i) => D.maqs[i].sit === 'Fuera de servicio');
  const enTaller = idx.filter((i) => D.maqs[i].sit === 'En taller');
  const SX = 1.3, SY = 1.5;
  const resto = libresNo.concat(fuera);
  const nMax = Math.max(libresOk.length, resto.length);
  const COLS = nMax <= 12 ? 4 : (nMax <= 20 ? 5 : (nMax <= 30 ? 6 : 7));
  const filasOk = Math.max(1, Math.ceil(libresOk.length / COLS));
  const filasNo = Math.ceil(resto.length / COLS);
  const TX = 1.3 + COLS * SX + 0.3, W = TX + 6.7, ALTO = 3.3;
  const DEP = Math.max(1.9 + Math.max(D.tecs.length, 3) * 2.6, 1.4 + (filasOk + filasNo) * SY + (filasNo ? 1.6 : 0) + 0.9);

  let fondo = '';
  // losa y muros (corte del edificio, como una maqueta)
  fondo += poly([[0, DEP, -0.3], [W, DEP, -0.3], [W, DEP, 0], [0, DEP, 0]], '#9dbcc9') + poly([[W, 0, -0.3], [W, DEP, -0.3], [W, DEP, 0], [W, 0, 0]], '#86a8b6');
  fondo += poly([[0, 0, 0], [W, 0, 0], [W, DEP, 0], [0, DEP, 0]], '#d7e7ed');
  fondo += poly([[0, 0, 0], [0, DEP, 0], [0, DEP, ALTO], [0, 0, ALTO]], '#4f95ab') + poly([[0, 0, 0], [W, 0, 0], [W, 0, ALTO], [0, 0, ALTO]], '#63a9bf');
  fondo += poly([[0, 0, ALTO], [W, 0, ALTO], [W, -0.25, ALTO], [0, -0.25, ALTO]], '#3f7f94') + poly([[0, 0, ALTO], [0, DEP, ALTO], [-0.25, DEP, ALTO], [-0.25, 0, ALTO]], '#3a768a');
  // ventanas y portón
  for (let v = 0; v < 4; v++) fondo += poly([[0.9 + v * 2.3, 0, 2.2], [2.8 + v * 2.3, 0, 2.2], [2.8 + v * 2.3, 0, 2.9], [0.9 + v * 2.3, 0, 2.9]], '#a9d6e4', 'stroke="#e8f6fa" stroke-width="1"');
  fondo += poly([[0, DEP - 5.2, 0], [0, DEP - 1.6, 0], [0, DEP - 1.6, 2.0], [0, DEP - 5.2, 2.0]], '#e6ebef', 'stroke="#b9c3cc"');
  for (let r = 1; r < 6; r++) fondo += linea([0, DEP - 5.2, r * 0.33], [0, DEP - 1.6, r * 0.33], '#c3ccd4', 0.8);
  fondo += rotulo('LIFT RENTAL · ALMACÉN COMAS', 0, DEP - 0.5, 2.45, 'y', 'class="esc-muro"');

  // patio
  fondo += rotulo('PATIO · LIBRES: ' + libresOk.length, 0.6, 0, 1.45, 'x', 'class="esc-muro" style="font-size:12px"');
  libresOk.forEach((i, n) => maquina(i, 1.3 + (n % COLS) * SX, 1.6 + Math.floor(n / COLS) * SY, false));
  if (!libresOk.length) fondo += rotulo('Sin máquinas aptas en el patio', 1.2, 2.2, 0, 'x', 'class="esc-vacio"');
  if (resto.length) {
    const y0 = 1.6 + filasOk * SY + 0.45, y1 = y0 + (filasNo - 1) * SY;
    fondo += poly([[0.5, y0 - 0.75, 0], [COLS * SX + 0.7, y0 - 0.75, 0], [COLS * SX + 0.7, y1 + 1.5, 0], [0.5, y1 + 1.5, 0]],
      '#fbe3e3', 'stroke="#c62828" stroke-dasharray="5 4" stroke-width="1.2"');
    fondo += rotulo('NO DESPACHABLES: ' + resto.length, 0.7, y1 + 1.36, 0, 'x', 'class="esc-zona" fill="#c62828"');
    resto.forEach((i, n) => maquina(i, 1.3 + (n % COLS) * SX, y0 + Math.floor(n / COLS) * SY, false));
  }

  // taller: una bahía por técnico
  const nT = Math.max(D.tecs.length, 1), paso = Math.min(3.3, (DEP - 1.9) / nT);
  fondo += poly([[TX, 0.45, 0], [W - 0.4, 0.45, 0], [W - 0.4, DEP - 0.5, 0], [TX, DEP - 0.5, 0]], '#e7edf1', 'stroke="#f2b200" stroke-width="2" stroke-dasharray="9 6"');
  fondo += rotulo('TALLER — MANTENIMIENTO', TX + 0.3, DEP - 0.62, 0, 'x', 'class="esc-zona"');
  const usados = new Set(), enBahia = {};
  D.tecs.forEach((t, j) => {
    const y0 = 1.0 + j * paso, yc = y0 + Math.min(paso, 2.4) / 2;
    fondo += poly([[TX + 0.5, y0, 0], [TX + 3.2, y0, 0], [TX + 3.2, y0 + Math.min(paso - 0.4, 2.2), 0], [TX + 0.5, y0 + Math.min(paso - 0.4, 2.2), 0]],
      '#f6f8fa', 'stroke="#9aa9b3" stroke-width="1.2"');
    fondo += rotulo('BAHÍA ' + (j + 1), TX + 3.3, y0 + 0.5, 0, 'x', 'class="esc-bahia"');
    capas.push({ k: TX + 5.6 + y0 + 0.4, s: caja(TX + 5.3, y0 + 0.2, 0, 0.7, 0.4, 0.45, '#c0392b') });   // caja de herramientas
    const ot = t.ots[0];
    let lugar = '', pos = [TX + 4.3, yc];
    if (ot) {
      const i = D.maqs.findIndex((m) => m.c === ot.c);
      const m = D.maqs[i];
      if (m && m.sit === 'En taller') {
        if (!(ot.c in enBahia)) { enBahia[ot.c] = [TX + 1.85, yc]; maquina(i, TX + 1.85, yc, false, 0.74); usados.add(i); }
        else pos = [enBahia[ot.c][0] + 1.5, enBahia[ot.c][1] + 0.6];
      } else if (m && m.sit === 'En obra') { lugar = 'en obra'; t._obra = i; }
    }
    if (t._obra == null) tecnico(j, pos[0], pos[1], lugar);
  });
  enTaller.filter((i) => !usados.has(i)).forEach((i, n) => maquina(i, W - 1.0, 1.5 + n * SY, false));

  // tablero de próximos ingresos en el muro del taller
  const bx = TX + 0.2, bw = 6.2, bz = 0.75, bh = 2.3;
  fondo += poly([[bx, 0, bz], [bx + bw, 0, bz], [bx + bw, 0, bz + bh], [bx, 0, bz + bh]], '#1b212b', 'stroke="#f2b200" stroke-width="1.5"');
  fondo += rotulo('PRÓXIMOS A MANTENIMIENTO', bx + 0.25, 0, bz + bh - 0.42, 'x', 'class="esc-tab esc-tab-t"');
  D.prox.forEach((p, n) => {
    fondo += rotulo(p.f.padEnd(10) + p.c.padEnd(9) + p.niv.padEnd(8) + p.tec, bx + 0.25, 0, bz + bh - 0.78 - n * 0.27, 'x', 'class="esc-tab"');
  });
  if (!D.prox.length) fondo += rotulo('Sin ingresos previstos', bx + 0.25, 0, bz + bh - 0.8, 'x', 'class="esc-tab"');

  // pista y camión de despacho entre el almacén y las obras
  const RX = W + 0.5, OX = W + 2.7;
  const porZona = {};
  idx.filter((i) => D.maqs[i].sit === 'En obra').forEach((i) => { (porZona[D.maqs[i].zona] = porZona[D.maqs[i].zona] || []).push(i); });
  const zonas = Object.keys(porZona).sort((a, b) => porZona[b].length - porZona[a].length);
  const OC = 6, OSX = 1.3, OSY = 1.5, tope = Math.max(DEP + 4.5, 15);
  let ox = OX, oy = -1.5, finY = DEP;
  const obraDe = {};
  zonas.forEach((z) => {
    const lista = porZona[z], filas = Math.ceil(lista.length / OC), prof = filas * OSY + 0.95, ancho = OC * OSX + 0.5;
    if (oy > -1.5 && oy + prof > tope) { ox += ancho + 0.7; oy = -1.5; }
    fondo += poly([[ox, oy + prof, -0.18], [ox + ancho, oy + prof, -0.18], [ox + ancho, oy + prof, 0], [ox, oy + prof, 0]], '#cdb98f') +
      poly([[ox + ancho, oy, -0.18], [ox + ancho, oy + prof, -0.18], [ox + ancho, oy + prof, 0], [ox + ancho, oy, 0]], '#bba67a') +
      poly([[ox, oy, 0], [ox + ancho, oy, 0], [ox + ancho, oy + prof, 0], [ox, oy + prof, 0]], '#efe4cc', 'stroke="#d9c9a3"');
    fondo += rotulo('OBRAS · ' + z.toUpperCase() + ': ' + lista.length, ox + 0.25, oy + prof - 0.14, 0, 'x', 'class="esc-zona" fill="#7a6740"');
    lista.forEach((i, n) => {
      const cx = ox + 0.85 + (n % OC) * OSX, cy = oy + 0.7 + Math.floor(n / OC) * OSY;
      obraDe[i] = [cx, cy];
      maquina(i, cx, cy, true, 0.7);
    });
    oy += prof + 0.7; finY = Math.max(finY, oy);
  });
  fondo = poly([[RX, -2.2, 0], [RX + 1.7, -2.2, 0], [RX + 1.7, finY, 0], [RX, finY, 0]], '#aeb5bf') +
    linea([RX + 0.85, -2.0, 0], [RX + 0.85, finY - 0.2, 0], '#fff', 1.4, 'stroke-dasharray="8 8"') + fondo;
  const cy0 = Math.min(DEP * 0.55, finY - 4);
  capas.push({ k: RX + 0.85 + cy0 + 1.5, s: caja(RX + 0.3, cy0, 0, 1.1, 3.0, 0.22, NEGRO) + caja(RX + 0.25, cy0, 0.22, 1.2, 2.1, 1.15, '#f2b200') +
      caja(RX + 0.3, cy0 + 2.15, 0.22, 1.1, 0.8, 0.85, '#d5402b') + poly([[RX + 0.42, cy0 + 2.95, 0.6], [RX + 1.28, cy0 + 2.95, 0.6], [RX + 1.28, cy0 + 2.95, 0.98], [RX + 0.42, cy0 + 2.95, 0.98]], '#bfe3f2') });
  // técnicos que atienden una falla en obra: junto a la máquina
  D.tecs.forEach((t, j) => { if (t._obra != null && obraDe[t._obra]) tecnico(j, obraDe[t._obra][0] + 0.62, obraDe[t._obra][1] + 0.1, 'en obra'); });

  // ---------------------------------------------------------------- montaje
  capas.sort((a, b) => a.k - b.k);
  const m = 34, vb = [lim.x0 - m, lim.y0 - m - 14, lim.x1 - lim.x0 + 2 * m, lim.y1 - lim.y0 + 2 * m + 14];
  cont.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb.map(f1).join(' ')}" role="img" aria-label="Vista isométrica del almacén de máquinas">` +
    fondo + capas.map((c) => c.s).join('') + encima.join('') + '</svg>';
  const svg = cont.firstChild;

  // zoom: «Ajustar» = ancho de la tarjeta; +/− amplían y permiten desplazarse
  let zoom = 1;
  const aplicarZoom = () => { svg.style.width = (zoom * 100) + '%'; };
  aplicarZoom();
  const zb = document.getElementById('esc-zoom');
  if (zb) zb.addEventListener('click', (ev) => {
    const b = ev.target.closest('button'); if (!b) return;
    const d = Number(b.dataset.z);
    zoom = d === 0 ? 1 : Math.max(1, Math.min(3, zoom + d * 0.4));
    aplicarZoom();
  });

  // ficha emergente
  const tip = document.getElementById('escena-tip'), wrap = cont.parentElement;
  const chip = (e) => `<span class="chip ${({ 'Vigente': 'ok', 'Por vencer': 'warn', 'Vencido': 'bad' })[e] || 'none'}">${esc(e)}</span>`;
  function contenido(el) {
    if (el.dataset.i != null) {
      const q = D.maqs[Number(el.dataset.i)];
      let h = `<b class="mono">${esc(q.c)}</b> ${esc(q.mm)}<br><small>${esc(q.cat)}${q.alt ? ' · ' + Number(q.alt).toFixed(1) + ' m' : ''}</small><br>`;
      h += `<span class="chip ${q.sit === 'En obra' ? 'info' : (q.sit === 'Disponible' ? 'ok' : 'warn')}">${q.sit === 'Disponible' ? 'Libre en patio' : esc(q.sit)}</span> ${chip(q.est)}`;
      if (q.vence) h += `<br><small>Servicio ${q.est === 'Vencido' ? 'venció' : 'vence'} el ${q.vence}</small>`;
      if (q.sit === 'En obra') h += `<br>${esc(q.cli || '')}<br><small>${esc(q.obra || '')} · ${esc(q.zona)}</small><br><small>Retorna ${q.ret || '—'}${q.sup ? ' (prórroga supuesta)' : ''}</small>`;
      if (q.ing) h += `<div class="tip-ing">Entra a taller: <b>${q.ing.dia} ${q.ing.f}</b>${q.ing.d > 0 ? ' (en ' + q.ing.d + ' d)' : ' (hoy)'}<br>${esc(q.ing.tipo)} ${esc(q.ing.niv || '')}${q.ing.tec ? ' · ' + esc(q.ing.tec) : ''}</div>`;
      return h;
    }
    const t = D.tecs[Number(el.dataset.j)];
    let h = `<b>${esc(t.n)}</b><br><span class="chip ${t.estado === 'Trabajando' ? 'accent' : (t.estado === 'Libre' ? 'ok' : 'none')}">${esc(t.estado)}</span>`;
    t.ots.forEach((o) => { h += `<br><small><b class="mono">${o.ot}</b> ${esc(o.c)} · ${esc(o.tipo)} ${esc(o.niv || '')} · ${o.obra ? 'en obra' : 'taller'}${o.hasta ? ' · hasta ' + o.hasta : ''}</small>`; });
    if (!t.ots.length) h += `<br><small>${t.prox ? 'Próxima asignación: ' + esc(t.prox) : 'Sin asignaciones en los próximos 7 días'}</small>`;
    return h;
  }
  svg.addEventListener('mousemove', (ev) => {
    const el = ev.target.closest('.maq, .tec');
    if (!el) { tip.hidden = true; return; }
    if (tip._el !== el) { tip.innerHTML = contenido(el); tip._el = el; }
    tip.hidden = false;
    const r = wrap.getBoundingClientRect();
    let x = ev.clientX - r.left + wrap.scrollLeft + 14, y = ev.clientY - r.top + wrap.scrollTop + 14;
    if (ev.clientX - r.left + 260 > r.width) x -= 260;
    if (ev.clientY - r.top + tip.offsetHeight + 20 > r.height) y -= tip.offsetHeight + 24;
    tip.style.left = x + 'px'; tip.style.top = Math.max(wrap.scrollTop + 4, y) + 'px';
  });
  svg.addEventListener('mouseleave', () => { tip.hidden = true; tip._el = null; });
})();
