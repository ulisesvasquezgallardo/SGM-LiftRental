"""Exporta el SGM a una copia HTML estática de sólo lectura para compartir.

Recorre todas las páginas de consulta (GET) del sistema con el cliente de pruebas de
Flask, guarda cada una como .html con enlaces relativos y copia la carpeta static.
La copia se abre con doble clic en index.html, sin Python ni servidor. Los formularios
(registrar, editar, aplicar, etc.) y los documentos PDF de las carpetas no se incluyen:
al pulsarlos se muestra un aviso.

Uso:  python exportar_html.py          -> compartir/SGM_LiftRental_html/ y su .zip
"""
import html
import re
import shutil
import sys
import zipfile
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import app as A  # noqa: E402

SALIDA = APP_DIR / "compartir" / "SGM_LiftRental_html"
ZIP = SALIDA.with_suffix(".zip")
MAX_PAGINAS = 4000
SEMANAS_ATRAS, SEMANAS_ADELANTE = 8, 12   # ventana del programa del taller (navegación por semanas)

# Páginas que no se exportan: formularios de alta/edición, documentos de las carpetas y JSON internos.
EXCLUIR = re.compile(
    r"^/static/|^/equipos/[^/]+/doc/|/estado\.json$|/(nuevo|nueva|editar|retorno)$"
)
ATRIB = re.compile(r'\b(href|data-href|src|action)="([^"]*)"')
JSON_URL = re.compile(r'("url":\s*")(/[^"]*)(")')

AVISO = ("Esta es una copia de consulta del SGM Lift Rental: aquí no se pueden registrar ni "
         "modificar datos. Esa acción está disponible en el sistema original.")
CABEZA = """<script>/* copia estática */(function(){var r=history.replaceState;
history.replaceState=function(){try{return r.apply(history,arguments)}catch(e){}};
var M=%s;
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('[data-sin-copia]');
if(a){e.preventDefault();e.stopImmediatePropagation();alert(M);}},true);
document.addEventListener('submit',function(e){e.preventDefault();e.stopImmediatePropagation();alert(M);},true);
})();</script>
<style>.aviso-copia{position:sticky;top:0;z-index:50;background:#fff4cc;color:#5a4300;border-bottom:1px solid #f2b200;
padding:6px 16px;font:12.5px/1.4 "Segoe UI",system-ui,sans-serif;text-align:center}
@media print{.aviso-copia{display:none}}</style>"""


def interno(url):
    """Normaliza un enlace interno ('/ruta?q#f') -> (ruta_con_query, fragmento) o None."""
    url = html.unescape(url)
    if not url.startswith("/") or url.startswith("//"):
        return None
    base, _, frag = url.partition("#")
    return base, frag


def archivo(url):
    p = urlsplit(url)
    ruta = p.path.strip("/")
    ext = ".csv" if ruta.endswith(".csv") else ".html"
    if ext == ".csv":
        ruta = ruta[:-4]
    nombre = re.sub(r"[^A-Za-z0-9._-]+", "_", ruta.replace("/", "--")) or "index"
    if p.query:
        q = "_".join(f"{k}-{v}" for k, v in sorted(parse_qsl(p.query, keep_blank_values=True)))
        nombre += "__" + re.sub(r"[^A-Za-z0-9._-]+", "_", q)
    return nombre + ext


def enlaces(texto):
    for attr, v in ATRIB.findall(texto):
        if attr != "action":      # los formularios no se recorren
            yield v
    for _, v, _ in JSON_URL.findall(texto):
        yield v


def en_ventana(url, hoy):
    """Las vistas con ?desde= (programa del taller) sólo se exportan cerca de hoy."""
    desde = dict(parse_qsl(urlsplit(url).query)).get("desde")
    if not desde:
        return True
    try:
        d = date.fromisoformat(desde)
    except ValueError:
        return False
    return hoy - timedelta(weeks=SEMANAS_ATRAS) <= d <= hoy + timedelta(weeks=SEMANAS_ADELANTE)


def exportar():
    cli = A.app.test_client()
    with A.app.test_request_context():
        ref = A.L.fecha_ref(A.cfg())
    paginas, cola, vistos, omitidas = {}, deque(["/"]), {"/"}, set()
    while cola:
        url = cola.popleft()
        if len(paginas) >= MAX_PAGINAS:
            omitidas.add(url)
            continue
        r = cli.get(url)
        if r.status_code in (301, 302, 303, 307, 308):
            destino = interno(urlsplit(r.headers["Location"])._replace(scheme="", netloc="").geturl())
            paginas[url] = ("redir", destino[0] if destino else "/")
            if destino and destino[0] not in vistos:
                vistos.add(destino[0])
                cola.append(destino[0])
            continue
        if r.status_code != 200:
            omitidas.add(url)
            continue
        if r.mimetype != "text/html":
            paginas[url] = ("bin", r.data)
            continue
        texto = r.get_data(as_text=True)
        paginas[url] = ("html", texto)
        for v in enlaces(texto):
            n = interno(v)
            if (n and n[0] not in vistos and not EXCLUIR.search(urlsplit(n[0]).path)
                    and en_ventana(n[0], ref)):
                vistos.add(n[0])
                cola.append(n[0])

    def destino_de(base):
        tipo = paginas.get(base, (None,))[0]
        if tipo == "redir":
            base = paginas[base][1]
            tipo = paginas.get(base, (None,))[0]
        return archivo(base) if tipo in ("html", "bin") else None

    hoy = datetime.now().strftime("%d/%m/%Y %H:%M")
    banda = (f'<div class="aviso-copia">Copia de consulta del SGM Lift Rental generada el {hoy} · '
             f'sólo lectura: los formularios y documentos PDF están disponibles en el sistema original.</div>')

    def reescribir(texto):
        def rep_attr(m):
            attr, val = m.group(1), m.group(2)
            n = interno(val)
            if not n:
                return m.group(0)
            base, frag = n
            if base.startswith("/static/"):
                return f'{attr}="{base[1:]}"'
            f = destino_de(base)
            if f and attr != "action":
                return f'{attr}="{html.escape(f + ("#" + frag if frag else ""))}"'
            return f'{attr}="#" data-sin-copia="1"'

        def rep_json(m):
            f = destino_de(m.group(2))
            return m.group(1) + (f or "#") + m.group(3)

        texto = ATRIB.sub(rep_attr, texto)
        texto = JSON_URL.sub(rep_json, texto)
        texto = texto.replace("<head>", "<head>\n" + CABEZA % repr(AVISO).replace("\\'", "’"), 1)
        texto = re.sub(r"(<body[^>]*>)", r"\1\n" + banda, texto, count=1)
        return texto

    if SALIDA.exists():   # borrar archivo por archivo (OneDrive puede bloquear las carpetas un momento)
        for f in sorted(SALIDA.rglob("*"), reverse=True):
            if f.is_file():
                f.unlink()
            else:
                try:
                    f.rmdir()
                except OSError:
                    pass
    SALIDA.mkdir(parents=True, exist_ok=True)
    shutil.copytree(APP_DIR / "static", SALIDA / "static", dirs_exist_ok=True)
    n_html = n_csv = 0
    for url, (tipo, dato) in paginas.items():
        if tipo == "redir":
            continue
        ruta = SALIDA / archivo(url)
        if tipo == "html":
            ruta.write_text(reescribir(dato), encoding="utf-8")
            n_html += 1
        else:
            ruta.write_bytes(dato)
            n_csv += 1
    (SALIDA / "LEEME.txt").write_text(
        "SGM Lift Rental - copia de consulta (sólo lectura)\r\n\r\n"
        "1. Descomprima el .zip en una carpeta.\r\n"
        "2. Abra index.html con doble clic (Chrome, Edge o Firefox).\r\n\r\n"
        "Se pueden recorrer todas las páginas (almacén, panel, equipos, órdenes, taller, "
        "pedidos, optimización...). Registrar o modificar datos sólo es posible en el sistema "
        f"original.\r\nGenerada el {hoy}.\r\n", encoding="utf-8")

    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(SALIDA.rglob("*")):
            z.write(f, Path(SALIDA.name) / f.relative_to(SALIDA))
    return n_html, n_csv, sorted(omitidas)


if __name__ == "__main__":
    n_html, n_csv, omitidas = exportar()
    tam = sum(f.stat().st_size for f in SALIDA.rglob("*")) / 1e6
    print(f"{n_html} páginas HTML y {n_csv} archivos CSV en {SALIDA} ({tam:.1f} MB)")
    print(f"Zip: {ZIP} ({ZIP.stat().st_size / 1e6:.1f} MB)")
    if omitidas:
        print(f"{len(omitidas)} páginas no exportadas:", *omitidas[:20], sep="\n  ")
