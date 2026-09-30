// Frontend del organizador. Sin dependencias. Todo el texto que viene del
// servidor (nombres de archivo incluidos) se inserta con textContent: nunca innerHTML.
"use strict";

const TESTIGO = document.querySelector('meta[name="testigo"]').content;
const $ = (id) => document.getElementById(id);

let analisisId = null;
let trabajoActual = null;
let resultados = null;

// ------------------------------------------------------------------ utilidades
async function api(metodo, url, cuerpo) {
  const opciones = { method: metodo, headers: { "X-Testigo": TESTIGO } };
  if (cuerpo !== undefined) {
    opciones.headers["Content-Type"] = "application/json";
    opciones.body = JSON.stringify(cuerpo);
  }
  const r = await fetch(url, opciones);
  let datos = null;
  try { datos = await r.json(); } catch (_) { /* sin cuerpo JSON */ }
  if (!r.ok) {
    const detalle = datos && datos.detail;
    const texto = Array.isArray(detalle) ? detalle.map((d) => d.msg).join("; ") : detalle;
    throw new Error(texto || `Error ${r.status}`);
  }
  return datos;
}

function el(etiqueta, props = {}, ...hijos) {
  const nodo = document.createElement(etiqueta);
  for (const [k, v] of Object.entries(props)) {
    if (k === "texto") nodo.textContent = v;
    else if (k === "clase") nodo.className = v;
    else if (k in nodo) nodo[k] = v;
    else nodo.setAttribute(k, v);
  }
  for (const h of hijos) if (h !== null && h !== undefined) nodo.append(h);
  return nodo;
}

function tamano(n) {
  const unidades = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < unidades.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${v} B` : `${v.toFixed(1)} ${unidades[i]}`;
}

function lista(texto) {
  return texto.split(",").map((s) => s.trim()).filter(Boolean);
}

function mostrarError(mensaje) {
  const p = $("error");
  p.textContent = mensaje || "";
  p.hidden = !mensaje;
}

// ------------------------------------------------------------------- trabajos
function seguir(id, alTerminar) {
  trabajoActual = id;
  $("estado").hidden = false;
  $("btn-cancelar").disabled = false;
  $("btn-analizar").disabled = true;
  $("btn-aplicar").disabled = true;

  const tick = async () => {
    let t;
    try {
      t = await api("GET", `/api/trabajos/${id}`);
    } catch (e) {
      mostrarError(e.message);
      terminar();
      return;
    }
    const fase = t.fase || (t.tipo === "analisis" ? "analizando" : "aplicando");
    $("estado-texto").textContent = t.total ? `${fase}: ${t.hechos} / ${t.total}` : `${fase}…`;
    const barra = $("barra");
    if (t.total) { barra.max = t.total; barra.value = t.hechos; } else { barra.removeAttribute("value"); }
    if (t.estado === "en_curso") { setTimeout(tick, 400); return; }
    terminar();
    if (t.estado === "error") mostrarError(t.error);
    else if (t.estado === "cancelado" && !t.aplicado) mostrarError("Trabajo cancelado.");
    alTerminar(t);
  };
  tick();
}

function terminar() {
  trabajoActual = null;
  $("estado").hidden = true;
  $("btn-analizar").disabled = false;
  actualizarSeleccion();
}

$("btn-cancelar").addEventListener("click", async () => {
  if (!trabajoActual) return;
  $("btn-cancelar").disabled = true;
  $("estado-texto").textContent = "Cancelando…";
  try { await api("POST", `/api/trabajos/${trabajoActual}/cancelar`); } catch (e) { mostrarError(e.message); }
});

// ------------------------------------------------------------------- análisis
$("form-analisis").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  mostrarError("");
  $("aplicado").hidden = true;
  $("resultados").hidden = true;
  const cuerpo = {
    ruta: $("ruta").value.trim(),
    umbral_grande_mb: Number($("umbral").value) || 100,
    excluir: lista($("excluir").value),
    ext_basura: lista($("ext-basura").value),
  };
  try {
    const { id } = await api("POST", "/api/analisis", cuerpo);
    seguir(id, (t) => {
      if (t.estado === "completado") { analisisId = t.id; pintarResultados(t.resultados); }
    });
  } catch (e) {
    mostrarError(e.message);
  }
});

function pintarResultados(r) {
  resultados = r;
  $("resultados").hidden = false;
  $("raiz").textContent = `Raíz: ${r.raiz} · cuarentena: ${r.cuarentena}`;

  const s = r.resumen;
  const datos = [
    [s.archivos, `archivos (${tamano(s.bytes)})`],
    [s.grupos_duplicados, "grupos de duplicados"],
    [tamano(s.bytes_recuperables), "recuperables en duplicados"],
    [s.basura, "archivos basura"],
    [s.grandes, "archivos grandes"],
    [s.carpetas_vacias, "carpetas vacías"],
    [s.cero_bytes, "archivos de 0 bytes"],
    [s.errores, "errores"],
  ];
  $("resumen").replaceChildren(...datos.map(([v, t]) => el("div", { clase: "dato" }, el("b", { texto: String(v) }), el("span", { texto: t }))));

  const cont = $("categorias");
  cont.replaceChildren();

  // Duplicados: la copia conservada no se puede marcar.
  const filasDup = [];
  r.duplicados.forEach((g, n) => {
    filasDup.push(el("tr", { clase: "grupo" },
      el("td", { colSpan: 4, texto: `Grupo ${n + 1} · ${g.archivos.length} copias de ${tamano(g.tamano)}` })));
    for (const a of g.archivos) filasDup.push(filaArchivo(a, !a.conservada));
  });
  cont.append(categoria("Duplicados", r.duplicados.length, "Se conserva siempre una copia de cada grupo (ruta más corta → más antigua → orden alfabético).", filasDup, true));

  cont.append(categoria("Basura / temporales", r.basura.length, "", r.basura.map((a) => filaArchivo(a, !a.conservada)), true));
  cont.append(categoria("Archivos grandes", r.grandes.length, "Solo se mueven si los marcas tú.", r.grandes.map((a) => filaArchivo(a, false)), true));
  cont.append(categoria("Carpetas vacías", r.vacias.length, "Se eliminan con rmdir (solo si siguen vacías) y quedan en el log.",
    r.vacias.map((v) => el("tr", {}, el("td", {}, casilla(v.id, false, false, v.ruta)), el("td", { clase: "ruta", texto: v.ruta, colSpan: 3 }))), true));
  cont.append(categoria("Archivos de 0 bytes", r.cero_bytes.length, "Solo informativo.", r.cero_bytes.map((a) => filaArchivo(a, false, true)), false));
  cont.append(categoria("Enlaces duros", r.enlaces_duros.length, "Moverlos no libera espacio: solo se informan.",
    r.enlaces_duros.map((g) => el("tr", {}, el("td", { clase: "ruta", colSpan: 4, texto: g.archivos.join("  =  ") }))), false));
  const incidencias = r.omitidos.concat(r.errores);
  cont.append(categoria("Omitidos y errores", incidencias.length, "",
    incidencias.map((i) => el("tr", {}, el("td", { clase: "ruta", colSpan: 3, texto: i.ruta }), el("td", { texto: i.motivo }))), false));
  unificarCasillas();
  actualizarSeleccion();
}

// Un mismo archivo puede aparecer en varias categorías (p. ej. un .bak duplicado):
// sus casillas se mantienen sincronizadas y se cuenta una sola vez.
function casilla(id, marcada, bloqueada, ruta) {
  const c = el("input", { type: "checkbox", checked: marcada, disabled: bloqueada });
  c.dataset.id = id;
  c.dataset.ruta = ruta;
  c.addEventListener("change", () => {
    for (const otra of casillasDe(ruta)) if (!otra.disabled) otra.checked = c.checked;
    actualizarSeleccion();
  });
  return c;
}

function casillasDe(ruta) {
  return [...document.querySelectorAll("#categorias input[type=checkbox]")].filter((c) => c.dataset.ruta === ruta);
}

// Si un archivo está marcado en alguna categoría, se marca en todas.
function unificarCasillas() {
  const marcadas = new Set(seleccionados().map((c) => c.dataset.ruta));
  for (const c of document.querySelectorAll("#categorias input[type=checkbox]")) {
    if (!c.disabled && marcadas.has(c.dataset.ruta)) c.checked = true;
  }
}

function filaArchivo(a, marcada, sinCasilla = false) {
  const primera = sinCasilla ? el("td") : el("td", {}, casilla(a.id, marcada && !a.conservada, a.conservada, a.ruta));
  const ruta = el("td", { clase: "ruta", texto: a.ruta });
  if (a.conservada) ruta.append(" ", el("span", { clase: "etiqueta", texto: "se conserva" }));
  if (!sinCasilla) primera.firstChild.dataset.tamano = a.tamano;
  return el("tr", {}, primera, ruta, el("td", { clase: "num", texto: tamano(a.tamano) }), el("td", { texto: a.modificado }));
}

function categoria(titulo, cantidad, nota, filas, seleccionable) {
  const d = el("details", { clase: "tarjeta categoria", open: seleccionable && cantidad > 0 });
  d.append(el("summary", {}, `${titulo} `, el("span", { clase: "contador", texto: `(${cantidad})` })));
  if (nota) d.append(el("p", { clase: "nota", texto: nota }));
  if (cantidad > 0) d.append(el("div", { clase: "tabla" }, el("table", {}, el("tbody", {}, ...filas))));
  return d;
}

function seleccionados() {
  const vistos = new Map();
  for (const c of document.querySelectorAll("#categorias input[type=checkbox]:checked")) {
    vistos.set(c.dataset.id, c);
  }
  return [...vistos.values()];
}

// Recuento por archivo único, que es lo que el servidor moverá como máximo.
function resumenSeleccion() {
  const archivos = new Map();
  const carpetas = new Set();
  for (const c of seleccionados()) {
    if (c.dataset.id.startsWith("v")) carpetas.add(c.dataset.ruta);
    else archivos.set(c.dataset.ruta, Number(c.dataset.tamano || 0));
  }
  let bytes = 0;
  for (const t of archivos.values()) bytes += t;
  return { archivos: archivos.size, bytes, carpetas: carpetas.size };
}

function actualizarSeleccion() {
  const r = resumenSeleccion();
  $("seleccion").textContent = `${r.archivos} archivos (${tamano(r.bytes)}) y ${r.carpetas} carpetas seleccionados`;
  $("btn-aplicar").disabled = !analisisId || r.archivos + r.carpetas === 0 || trabajoActual !== null;
}

// --------------------------------------------------------------------- aplicar
$("btn-aplicar").addEventListener("click", () => {
  const r = resumenSeleccion();
  $("dialogo-texto").textContent =
    `Se moverán hasta ${r.archivos} archivos (${tamano(r.bytes)}) a la cuarentena y se eliminarán hasta ` +
    `${r.carpetas} carpetas vacías. Lo que no supere la revalidación se omitirá. ¿Continuar?`;
  $("dialogo").showModal();
});

$("dialogo").addEventListener("close", async () => {
  if ($("dialogo").returnValue !== "aceptar") return;
  mostrarError("");
  const ids = seleccionados().map((c) => c.dataset.id);
  try {
    const { id } = await api("POST", "/api/aplicar", { analisis: analisisId, ids });
    seguir(id, (t) => { if (t.aplicado) pintarAplicado(t.aplicado); });
  } catch (e) {
    mostrarError(e.message);
  }
});

function pintarAplicado(a) {
  const s = $("aplicado");
  s.hidden = false;
  const omitidos = a.omitidos.map((i) => el("li", {}, el("code", { texto: i.ruta }), ` — ${i.motivo}`));
  const hijos = [
    el("h2", { texto: a.cancelado ? "Aplicación cancelada" : "Cambios aplicados", clase: a.cancelado ? "error" : "ok" }),
    el("p", { texto: `${a.movidos} archivos movidos a cuarentena y ${a.carpetas} carpetas vacías eliminadas.` }),
    el("p", {}, "Log: ", el("code", { texto: a.log || "" })),
    el("p", { clase: "nota" }, "Para deshacer: ", el("code", { texto: `organizador restore "${a.log}"` })),
    omitidos.length ? el("details", {}, el("summary", { texto: `${omitidos.length} omitidos` }), el("ul", {}, ...omitidos)) : null,
    el("p", { clase: "nota", texto: "Vuelve a analizar para ver el estado actual de la carpeta." }),
  ];
  s.replaceChildren(...hijos.filter(Boolean));
  // El análisis anterior ya no refleja el disco: se oculta para no reutilizarlo.
  $("resultados").hidden = true;
  $("categorias").replaceChildren();
  analisisId = null;
  actualizarSeleccion();
}

// ------------------------------------------------------------------- descargas
for (const b of document.querySelectorAll("[data-descarga]")) {
  b.addEventListener("click", async () => {
    const id = analisisId;
    if (!id) { mostrarError("No hay un análisis vigente para descargar."); return; }
    const r = await fetch(`/api/trabajos/${id}/informe.${b.dataset.descarga}`, { headers: { "X-Testigo": TESTIGO } });
    if (!r.ok) { mostrarError(`No se pudo descargar (${r.status})`); return; }
    const url = URL.createObjectURL(await r.blob());
    const enlace = el("a", { href: url, download: `informe.${b.dataset.descarga}` });
    document.body.append(enlace);
    enlace.click();
    enlace.remove();
    URL.revokeObjectURL(url);
  });
}
