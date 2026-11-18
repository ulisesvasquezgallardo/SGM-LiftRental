# SGM Lift Rental — Sistema de Gestión de Mantenimiento

> **Versión pública para demostración.** Los nombres de clientes, obras, técnicos y correos fueron
> reemplazados por datos ficticios (Cliente 01, Obra 01, Técnico A…). Los equipos, fechas, horómetros
> y resultados del modelo de optimización se mantienen.

## Usarlo en línea

| | Link | Qué permite |
|---|---|---|
| **Sistema completo** | Publicado en Render (ver abajo) | Registrar, editar, despachar, correr la optimización… |
| **Copia de consulta** | <https://ulisesvasquezgallardo.github.io/SGM-LiftRental/> | Recorrer todas las páginas (sólo lectura) |

### Publicar el sistema completo en Render (gratis)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/ulisesvasquezgallardo/SGM-LiftRental)

1. Pulse el botón e ingrese a Render con su cuenta de GitHub.
2. Confirme el servicio `sgm-liftrental` (plan *Free*) con **Apply / Deploy**.
3. Al terminar (unos minutos) Render da un link `https://sgm-liftrental-xxxx.onrender.com`.

En el plan gratuito el servidor se duerme tras 15 minutos sin uso (la primera visita tarda ~1 minuto)
y, al reiniciarse, los datos vuelven a los de este repositorio. El sistema no tiene inicio de
sesión: cualquiera con el link puede modificar los datos de la demostración.

Aplicación web local (Python + Flask + SQLite) que funciona como **almacén virtual** de la flota de
Corporación Lift Rental Solutions Perú S.A.C.: muestra dónde está cada máquina y en qué estado de
mantenimiento, registra su uso, controla el mantenimiento con el Plan de Procesos de Mantenimiento
(Mntto-PPMTE-2017) y organiza al personal del taller.

## Cómo iniciarlo en una computadora

Doble clic en `iniciar.bat`, o desde una terminal:

```
pip install flask "pulp==2.9.0"
python app.py
```

PuLP 2.9 trae el solver CBC incluido. Las versiones 3 y 4 cargan una DLL que el Control de
aplicaciones de Windows puede bloquear; por eso se fija la 2.9.

y abrir <http://127.0.0.1:5000>.

## De dónde salen los datos

1. **Datos reales.** `datos/semilla.json` reúne los 87 equipos (75 con expediente y 12 que sólo figuran
   en el registro de estatus), su último servicio documentado, 153 lecturas de horómetro, los 71
   contratos en obra al 22/09/2026 y los parámetros por nivel de servicio.
2. **Lectura de los expedientes.** Se leyó cada documento de las 75 carpetas (formato PM, certificado de
   mantenimiento, certificado de operatividad, ficha técnica y check list) y un segundo lector verificó
   cada dato contra el documento. El resultado está en `datos/expedientes.json` y
   `datos/catalogo_tareas.json`:
   - ejecutor y supervisor de cada PM: **Técnico A** (70 de 75 equipos), **Técnico B** (4),
     **Técnico C** (1), todos bajo la supervisión de **Supervisor S.** (SUPRV./JEFE en los 75);
   - certificado de operatividad de cada equipo (fecha, horómetro, observación y recomendación);
   - las listas de tareas A, B, C y D de los tres formatos del plan (tijeras y mástil; brazos
     eléctricos; brazos de combustión), que el sistema usa como lista de chequeo de cada OT;
   - las inconsistencias de cada expediente (p. ej. TECL-018: el PM dice 1100 h pero el programa y el
     próximo mantenimiento confirman 1759 h). La lectura confirmó los valores que ya usaba el sistema.
3. **Operación simulada hasta hoy.** Como la empresa no lleva historial de alquileres ni de incidentes,
   el estado actual se reconstruye simulando día a día desde el 22/09/2026 con la práctica actual
   (`simulacion.py`):
   - los contratos vuelven en su fecha; los ya vencidos se prorrogan por 30 días y los demás con
     probabilidad 0.42 (calibrada para reproducir la ocupación de 84 % del registro);
   - los pedidos llegan con la tasa del registro (ley de Little) y salen con la primera máquina
     compatible del patio, sin mirar el mantenimiento;
   - el taller (3 técnicos, lunes a sábado) recibe lo que vuelve vencido y atiende primero las fallas
     en obra: 25 % por pedido con mantenimiento vencido y 5 % vigente (supuestos del informe),
     a S/ 860 + días parados × tarifa.

   La primera vez que se inicia, el sistema simula hasta la fecha del día. En **Configuración →
   Simulación de la operación** se puede reconstruir hasta otra fecha o con otra semilla; si pasan
   días, el almacén ofrece «Avanzar la simulación hasta hoy». La misma semilla reproduce la misma
   historia.

## Módulos

| Módulo | Qué hace |
|---|---|
| **Almacén en vivo** (inicio) | **Vista isométrica del almacén** (maqueta con el patio, las bahías del taller con sus técnicos, el tablero de próximos ingresos y las obras por zona; cada máquina se dibuja según su tipo, el color del piso indica su mantenimiento y al pasar el cursor se abre su ficha). Debajo: Patio (listos para despachar / no despachables / fuera de servicio), equipos en mantenimiento y quién los atiende, obras por zona con su fecha de retorno, **qué equipos entran a taller y cuándo** (OT programadas + pronóstico ubicado en la capacidad libre de cada técnico) y **personal libre o trabajando** hoy y los próximos 7 días. |
| **Cartera de pedidos** | Pedidos por atender (conjunto J del modelo): se registran los del área comercial o se genera una cartera calibrada con la ley de Little, como en el informe. Desde cada pedido se despacha el equipo que asignó el modelo. |
| **Optimización (PLEM)** | El modelo de programación lineal entera mixta del informe (4.1) sobre el estado vivo de la flota. Resuelve E0 (práctica actual), E1 (asignación sin control), E2 (modelo propuesto) y E3 (valor terminal) con un evaluador único. Muestra las tablas 7, 8 y 9 del informe, el tiempo de solución, la verificación de restricciones, la carga del taller y el plan (Gantt, servicios, asignaciones y pedidos no atendidos). «Aplicar la primera semana» crea las OT y reserva los equipos (horizonte rodante). |
| **Panel de control** | Indicadores de la flota, equipos en obra vencidos, contratos vencidos sin retorno, carga del taller e incidentes. |
| **Equipos y expedientes** | Inventario filtrable. Cada ficha reúne datos técnicos, estado de mantenimiento, historial de uso, órdenes, lecturas, incidentes, el expediente documental (PDF de la carpeta del equipo) y la lectura verificada de ese expediente con sus inconsistencias. |
| **Uso y alquileres** | Contratos en obra y cerrados, prórrogas y registro de retorno con horómetro. |
| **Despacho asistido** | Busca equipos compatibles y los ordena por estado de mantenimiento. Un equipo vencido sólo sale como excepción documentada. |
| **Órdenes de trabajo** | Preventivos A … A+B+C+D con la lista de tareas del formato del plan (el cierre exige marcarlas o confirmar las pendientes), correctivos e inspecciones; supervisor de cada OT. |
| **Programa del taller** | Agenda por técnico y carga diaria frente a la capacidad. «Programar automáticamente» asigna fecha y técnico. |
| **Personal** | Técnicos (cuentan en la capacidad) y supervisor (no cuenta), con su participación en los PM documentados. |
| **Incidentes en obra** | Fallas con el estado de mantenimiento del equipo en esa fecha, costo y días parados. |
| **Configuración** | Criterios de vencimiento, capacidad, parámetros por nivel y simulación. |

## Reglas que aplica

- **Vencimiento**: al cumplir la frecuencia por horas (100 h tijeras y mástil, 250 h brazos y los
  GTJZ con esa leyenda) o 90 días, lo que ocurra primero. Los 90 días corren desde el último
  preventivo o desde el último certificado de operatividad emitido después de él (la inspección al
  despachar renueva el plazo; se puede desactivar en Configuración). Las horas se cuentan siempre
  desde el preventivo.
- **Por vencer**: 15 días o menos, o 20 % o menos de las horas.
- **Pronóstico de ingreso a taller**: un equipo en obra entra al retornar si en esa fecha está
  vencido; uno en patio entra en su fecha de vencimiento; cada ingreso se ubica en el primer día con
  técnicos libres.
- **Despacho**: bloquea vencidos y sin datos (salvo excepción registrada) y advierte si el servicio
  vence durante el contrato.

## Estatus diario (datos reales)

Menú **Estatus diario**: se carga el libro «Estatus de los equipos» que emite operaciones. Sólo se usan las hojas diarias («15-11», «16-11», «17-11»…); MOVILIZACIONES u otras hojas no se leen. `importar_estatus.py` aplica las hojas en orden de fecha:

- **TALLER / DISPONIBLE**: cierra el contrato. Con estatus de días seguidos, el retorno es el día del estatus que ya no lo muestra en obra; si hay un salto de varios días, se estima con la regla del informe (renovación por 30 días). «reparacion» → Fuera de servicio.
- **Cliente y obra**: misma salida → actualiza vencimiento y obra (prórrogas y traslados); salida distinta → cierra el anterior y abre el nuevo con el estado de mantenimiento a la salida. Sin vencimiento: salida + 30 días (o los días que indique la fila).
- **VENDIDO**: el equipo sale de la flota activa. «Se fue como la TECLxxx»: el contrato pasa al equipo que está en obra.
- **Horómetros**: sólo se registran si superan la última lectura y no son un valor copiado en varios equipos.
- Las filas de equipos de terceros (terceros) no se cargan.
- **Cruce entre días**: despachos que una hoja aún no refleja, equipos que desaparecen un día y vuelven, filas con fechas de un contrato anterior, columnas que cambian, contratos vencidos que siguen en obra y datos faltantes del último estatus.

La primera carga reemplaza la operación simulada: parte del registro real del 22/09/2026 y conserva la configuración, las corridas y los pedidos registrados a mano. Las siguientes aplican sólo las hojas nuevas. El informe de cada carga (observaciones, supuestos, horómetros rechazados) queda en la misma página. La base simulada anterior quedó respaldada en `datos/respaldos/`.

## Documentos de mantenimiento (carpetas de cada equipo)

Menú **Documentos de mantto.**: `importar_documentos.py` recorre las carpetas TECL001, BECL003, … (con sus
subcarpetas y .zip; un archivo repetido cuenta una vez) y lee con `pdftotext` (viene con Git para Windows):

- el formato **Plan de Procesos de Mantenimiento**: FECHA, HOROMETRO, ejecutor, supervisor y próximo mantto.;
- el **certificado de mantenimiento**: fecha y horómetro del programa (si su fecha no coincide con la del formato
  de la carpeta, se toma la que sí coincide);
- el **certificado de operatividad** (se emite al despachar): lectura de horómetro e inspección que renueva el
  plazo de calendario (no cuenta como servicio preventivo).

Agrega a la base, sin duplicar, cada preventivo que falte (orden Completada, origen «Documento de carpeta») y las
lecturas, y lista lo que no cuadra: fecha del nombre del archivo distinta de la del formato, certificados con dos
fechas, horómetros que bajan, frecuencia del formato distinta de la del sistema, equipos sin carpeta y OT
programadas que el servicio documentado deja innecesarias. Se ejecuta al iniciar por primera vez, en cada recarga
completa del estatus y con el botón «Volver a leer las carpetas». La lectura queda en caché en
`datos/documentos.json` (sólo se vuelve a leer un PDF si cambió).

## Modelo de optimización en operación

`optimizador.py` implementa la formulación del informe sin cambios:

- **Variables**: `x[m,j]` asignación, `y[m,t]` inicio de servicio, `z[j]` pedido no atendido
  (binarias) y `H[m,t]` horas desde el último servicio (continua).
- **Objetivo (5)**: costo de servicios + ingreso perdido + traslado + falla esperada (π = 5 %).
  En E3 se suma (14), el valor terminal 1,05·κ de cada servicio que quedaría pendiente.
- **Restricciones (6)–(13)**: cobertura, exclusividad alquiler/taller, horas con reinicio, tope
  de horas, tope de calendario, capacidad del taller por día hábil y un servicio por máquina.
  Los servicios caben en el horizonte y no incluyen domingos.

La instancia se arma con los datos del sistema a la fecha de inicio:

- el último preventivo y el horómetro de cada equipo (`h0`, `d0`);
- el nivel que le toca y sus parámetros (`δ`, `κ`, `τ`);
- la tasa de uso entre 0,84 (`u`);
- el retorno estimado de los equipos en obra;
- las órdenes en ejecución, que restan capacidad;
- los pedidos pendientes de la cartera.

Cada corrida se guarda con su instancia completa, de modo que los resultados se pueden reproducir
y exportar (CSV).

**Uso semanal (horizonte rodante)**:

1. Actualizar los horómetros y la cartera de pedidos.
2. Resolver el modelo.
3. Revisar E2/E3 frente a E0.
4. «Aplicar la primera semana».
5. Despachar desde la cartera.

La semana siguiente se repite con el estado nuevo.

## Archivos

```
app.py                  rutas y pantallas
logica.py               estado de mantenimiento, despacho, capacidad, pronóstico y almacén en vivo
importar_estatus.py     carga del libro «Estatus de los equipos» (contratos, ventas, horómetros, observaciones)
importar_documentos.py  lectura de los formatos PM y certificados de cada carpeta de equipo
optimizador.py          modelo PLEM (PuLP + CBC), escenarios E0-E3, evaluador único y plan aplicable
simulacion.py           reconstrucción de la operación desde el registro del 22/09/2026
db.py                   esquema SQLite y carga inicial (semilla + expedientes)
importar_excel.py       regenera datos/semilla.json desde los libros Excel del proyecto
importar_expedientes.py regenera datos/expedientes.json y catalogo_tareas.json desde la lectura
exportar_html.py        genera compartir/SGM_LiftRental_html(.zip): copia HTML de sólo lectura para compartir
templates/, static/     interfaz (static/logo_liftrental.jpg = logo de la empresa: barra lateral, ícono de la pestaña y certificado)
```

Variables de entorno opcionales: `SGM_DOCS_DIR` (carpeta con las subcarpetas de cada equipo; por
defecto, la carpeta «Practicas Pre 3 Documentos»), `PORT` y `SGM_DEBUG=1`.

El sistema no tiene usuarios ni contraseñas: está pensado para uso local en la red del taller.
