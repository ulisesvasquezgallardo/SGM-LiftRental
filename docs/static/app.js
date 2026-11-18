// Comportamiento mínimo de la interfaz (sin dependencias externas).

// Filas que navegan a la ficha al hacer clic fuera de un enlace o botón.
document.addEventListener('click', (ev) => {
  const tr = ev.target.closest('tr.fila-link');
  if (!tr || ev.target.closest('a, button, input, select, form')) return;
  window.location = tr.dataset.href;
});

// Confirmación en formularios marcados con data-confirmar.
document.addEventListener('submit', (ev) => {
  const msg = ev.target.dataset.confirmar;
  if (msg && !window.confirm(msg)) ev.preventDefault();
});

// Filtro del almacén: búsqueda, selects y botones segmentados; lee filtros iniciales de la URL.
function filtrarTabla(id) {
  const tabla = document.getElementById(id);
  if (!tabla) return;
  const filas = [...tabla.tBodies[0].rows];
  const q = document.getElementById('f-q');
  const sels = ['categoria', 'situacion', 'marca'].map((k) => [k, document.getElementById('f-' + k)]);
  const seg = document.getElementById('f-estado');
  const cuenta = document.getElementById('f-cuenta');
  const vacio = document.getElementById('sin-resultados');
  let estado = '';

  const params = new URLSearchParams(location.search);
  sels.forEach(([k, el]) => { if (params.get(k)) el.value = params.get(k); });
  if (params.get('q')) q.value = params.get('q');
  if (params.get('estado')) estado = params.get('estado');

  function marcarSeg() {
    seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === estado));
  }
  function aplicar() {
    const texto = q.value.trim().toLowerCase();
    let n = 0;
    filas.forEach((tr) => {
      let ok = !texto || tr.dataset.texto.includes(texto);
      sels.forEach(([k, el]) => { if (el.value && tr.dataset[k] !== el.value) ok = false; });
      if (estado) {
        // «no-apto» (enlace del panel) = vencido o sin datos: equipos que no deben despacharse.
        const e = tr.dataset.estado;
        if (estado === 'no-apto' ? !(e === 'Vencido' || e === 'Sin datos') : e !== estado) ok = false;
      }
      tr.hidden = !ok;
      if (ok) n++;
    });
    cuenta.textContent = n + ' de ' + filas.length + ' equipos';
    vacio.hidden = n > 0;
    const p = new URLSearchParams();
    if (texto) p.set('q', q.value.trim());
    sels.forEach(([k, el]) => { if (el.value) p.set(k, el.value); });
    if (estado) p.set('estado', estado);
    history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : ''));
  }
  q.addEventListener('input', aplicar);
  sels.forEach(([, el]) => el.addEventListener('change', aplicar));
  seg.addEventListener('click', (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    estado = b.dataset.v;
    marcarSeg();
    aplicar();
  });

  // Ordenar por columna
  tabla.querySelectorAll('th[data-orden]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.title = 'Ordenar';
    let asc = true;
    th.addEventListener('click', () => {
      const k = th.dataset.orden;
      filas.sort((a, b) => {
        const x = a.dataset[k], y = b.dataset[k];
        const nx = parseFloat(x), ny = parseFloat(y);
        const r = isNaN(nx) || isNaN(ny) ? x.localeCompare(y) : nx - ny;
        return asc ? r : -r;
      });
      asc = !asc;
      filas.forEach((tr) => tabla.tBodies[0].appendChild(tr));
    });
  });
  marcarSeg();
  aplicar();
}

// Formulario de órdenes: completa duración, costo y técnico-días según el nivel y la categoría.
function autocompletarParametros(params, categoria) {
  const nivel = document.querySelector('[name=nivel]');
  const tipo = document.querySelector('[name=tipo]');
  if (!nivel) return;
  function poner() {
    if (tipo && tipo.value !== 'Preventivo') return;
    const p = params.find((x) => x.categoria === categoria() && x.nivel === nivel.value);
    if (!p) return;
    document.querySelector('[name=duracion_dias]').value = p.duracion_dias;
    document.querySelector('[name=tecnico_dias]').value = p.tecnico_dias;
    document.querySelector('[name=costo_estimado]').value = p.costo;
  }
  nivel.addEventListener('change', poner);
  return poner;
}

// «Marcar todas» en la lista de tareas del plan de la OT.
document.addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-marcar-todo]');
  if (!b) return;
  b.closest('fieldset').querySelectorAll('input[name=tareas]').forEach((c) => { c.checked = true; });
});
