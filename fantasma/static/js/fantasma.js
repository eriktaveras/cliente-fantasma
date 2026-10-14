// Cliente Fantasma: un solo archivo sin build. Cada página se activa por <body data-pagina>.
// Todo texto que viene del servidor o de los modelos entra con textContent: nunca innerHTML.
"use strict";

const T = JSON.parse(document.getElementById("textos").textContent);

function el(tag, clase, texto) {
  const n = document.createElement(tag);
  if (clase) n.className = clase;
  if (texto !== undefined && texto !== null) n.textContent = texto;
  return n;
}

function valor(v) {
  return T.valores[v] || v;
}

function usd(n) {
  return "US$" + Number(n || 0).toFixed(2);
}

// ------------------------------------------------------------ formularios comunes

function alternarModo(form) {
  const paneles = form.querySelectorAll("[data-panel-modo]");
  if (!paneles.length) return;
  const pintar = () => {
    const modo = form.querySelector("[data-modo]:checked")?.value || "generado";
    paneles.forEach((panel) => {
      const activo = panel.dataset.panelModo === modo;
      panel.hidden = !activo;
      panel.querySelectorAll("input, textarea, select").forEach((c) => (c.disabled = !activo));
      panel.querySelectorAll("[data-obligatorio-modo]").forEach((c) => (c.required = activo));
    });
  };
  form.querySelectorAll("[data-modo]").forEach((r) => r.addEventListener("change", pintar));
  pintar();
}

function esperarAlEnviar(form, texto, soloSi) {
  form.addEventListener("submit", (e) => {
    if (e.defaultPrevented) return;
    if (soloSi && !soloSi(e)) return;
    form.querySelectorAll("button[type=submit]").forEach((b) => {
      // Se deshabilita después del envío: deshabilitado antes, el navegador no manda el name/value del botón pulsado.
      setTimeout(() => (b.disabled = true), 0);
    });
    const espera = form.querySelector("[data-espera]");
    if (espera) espera.textContent = texto;
  });
}

function inicio() {
  const form = document.querySelector("[data-form-probar]");
  alternarModo(form);
  const texto = form.elements.texto;
  const urls = form.elements.urls;
  const ejemplo = form.querySelector("[data-ejemplo]");
  const selector = form.querySelector("[data-fuente-selector]");
  const fuentes = form.querySelectorAll("[data-fuente]");
  const elegirFuente = (fuente) => {
    fuentes.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.fuente === fuente)));
    form.querySelectorAll("[data-panel-fuente]").forEach((p) => (p.hidden = p.dataset.panelFuente !== fuente));
  };
  if (selector) {
    selector.hidden = false;
    fuentes.forEach((b) => b.addEventListener("click", () => elegirFuente(b.dataset.fuente)));
    elegirFuente(urls?.value.trim() && !texto.value.trim() ? "web" : "texto");
  }
  const actualizar = () => {
    texto.setCustomValidity("");
    if (ejemplo) ejemplo.hidden = Boolean(texto.value.trim());
  };
  texto.addEventListener("input", actualizar);
  urls?.addEventListener("input", actualizar);
  ejemplo?.addEventListener("click", () => {
    if (texto.value.trim()) return;
    texto.value = T.ui.ejemplo_texto;
    if (!form.elements.nombre.value.trim()) form.elements.nombre.value = "Colmado La Esquina";
    actualizar();
    texto.focus();
  });
  actualizar();
  form.addEventListener("submit", (e) => {
    if (!texto.value.trim() && !urls?.value.trim()) {
      e.preventDefault();
      if (selector) elegirFuente("texto");
      texto.setCustomValidity(T.ui.sin_material);
      texto.reportValidity();
    }
  });
  esperarAlEnviar(form, T.esperando_ficha);
}

// ------------------------------------------------------------ ficha

function ficha() {
  const form = document.querySelector("[data-form-ficha]");
  const actualizarCatalogo = () => {
    const contador = form.querySelector("[data-catalogo-count]");
    if (contador) contador.textContent = "· " + [...form.querySelectorAll('[name="producto_nombre"]')].filter((c) => c.value.trim()).length;
  };
  form.addEventListener("input", actualizarCatalogo);
  alternarModo(form);
  esperarAlEnviar(form, T.lanzando, (e) => e.submitter?.value === "lanzar");

  // Enter en un campo no lanza la prueba: el primer botón del formulario es "Lanzar" y cuesta dinero.
  form.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault();
  });

  const activarQuitar = (raiz) => {
    raiz.querySelectorAll("[data-quitar]").forEach((b) => {
      b.hidden = false;
      b.addEventListener("click", () => {
        const fila = b.closest("[data-fila]");
        const lista = fila.parentElement;
        if (lista.querySelectorAll("[data-fila]").length > 1) {
          const siguiente = fila.nextElementSibling || fila.previousElementSibling;
          fila.remove();
          siguiente?.querySelector("input, textarea")?.focus();
        } else {
          fila.querySelectorAll("input, textarea").forEach((c) => (c.value = ""));
        }
        actualizarCatalogo();
      });
    });
  };
  activarQuitar(form);

  form.querySelectorAll("[data-agregar]").forEach((b) => {
    b.hidden = false;
    const tipo = b.dataset.agregar;
    const lista = form.querySelector(`[data-lista="${tipo}"]`);
    const plantilla = form.querySelector(`[data-plantilla="${tipo}"]`);
    const ultima = lista.lastElementChild;
    if (ultima && [...ultima.querySelectorAll("input, textarea")].every((c) => !c.value.trim())) ultima.hidden = true;
    b.addEventListener("click", () => {
      const vacia = lista.querySelector("[data-fila][hidden]");
      if (vacia) {
        vacia.hidden = false;
        vacia.querySelector("input, textarea")?.focus();
        return;
      }
      const nueva = plantilla.content.firstElementChild.cloneNode(true);
      lista.appendChild(nueva);
      activarQuitar(nueva);
      nueva.querySelector("input, textarea")?.focus();
    });
  });
}

// ------------------------------------------------------------ en vivo

function vivo() {
  const raiz = document.querySelector("[data-vivo]");
  const modo = raiz.dataset.modo;
  const contenedor = raiz.querySelector("[data-conversaciones]");
  const vacio = raiz.querySelector("[data-vacio]");
  const final = raiz.querySelector("[data-final]");
  const conexion = raiz.querySelector("[data-conexion]");
  const conexionTexto = raiz.querySelector("[data-conexion-texto]");
  const etapaTexto = raiz.querySelector("[data-etapa-texto]");
  const bitacora = raiz.querySelector("[data-bitacora]");
  const cifra = (k) => raiz.querySelector(`[data-${k}]`);

  const convs = new Map();
  const grupos = new Map();
  const notas = new Map();
  let terminada = false;
  let filtro = "todas";
  const vacioFiltro = raiz.querySelector("[data-filtro-vacio]");
  const filtros = raiz.querySelector("[data-filtros]");
  function filtrar() {
    let visibles = 0;
    convs.forEach((c, clave) => {
      c.li.hidden = filtro === "fallas" && (!notas.has(clave) || notas.get(clave).paso);
      if (!c.li.hidden) visibles += 1;
    });
    grupos.forEach((lista) => (lista.parentElement.hidden = ![...lista.children].some((li) => !li.hidden)));
    if (vacioFiltro) vacioFiltro.hidden = filtro !== "fallas" || visibles > 0;
    if (vacio) vacio.hidden = filtro === "fallas" || convs.size > 0;
  }
  if (filtros) {
    filtros.hidden = false;
    filtros.querySelectorAll("[data-filtro]").forEach((b) => b.addEventListener("click", () => {
      filtro = b.dataset.filtro;
      filtros.querySelectorAll("[data-filtro]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      filtrar();
    }));
  }

  function marcarEtapa(i) {
    raiz.querySelectorAll("[data-etapa-paso]").forEach((p) => {
      const n = Number(p.dataset.etapaPaso);
      p.classList.toggle("etapas__paso--hecho", n < i || (terminada && n <= i));
      p.classList.toggle("etapas__paso--activo", n === i && !terminada);
      if (n === i && !terminada) p.setAttribute("aria-current", "step");
      else p.removeAttribute("aria-current");
    });
  }

  function grupo(version) {
    if (grupos.has(version)) return grupos.get(version);
    vacio.hidden = true;
    const seccion = el("section", "grupo");
    const inicial = T["version_0_" + modo] || T.version_0;
    const titulo = el("h2", "rotulo rotulo--verde grupo__titulo", version === 0 ? inicial : T.version_n.replace("{n}", version));
    const lista = el("ol", "grupo__lista");
    seccion.append(titulo, lista);
    // La versión más nueva arriba: es la que se está probando ahora.
    contenedor.prepend(seccion);
    grupos.set(version, lista);
    return lista;
  }

  function tarjeta(ev) {
    const clave = `${ev.caso_id}|${ev.version}|${ev.repeticion}`;
    if (convs.has(clave)) return convs.get(clave);
    const li = el("li", "conv conv--viva");
    const cabeza = el("div", "conv__cabeza");
    const persona = el("div", "conv__persona");
    persona.append(el("span", "", ev.persona || ev.caso_id), el("span", "dato", ev.repeticion ? `r${ev.repeticion}` : ""));
    const ejes = el("ul", "ejes");
    const e = ev.ejes || {};
    if (e.dialecto) {
      const p = el("li", "pildora pildora--chica pildora--tinta", valor(e.dialecto));
      p.title = T.ejes.dialecto;
      ejes.append(p);
    }
    if (e.intencion) ejes.append(el("li", "pildora pildora--chica", valor(e.intencion)));
    if (e.ataque && e.ataque !== "ninguno") ejes.append(el("li", "pildora pildora--chica pildora--rojo", valor(e.ataque)));
    if (e.estilo) ejes.append(el("li", "pildora pildora--chica pildora--apagada", valor(e.estilo)));
    cabeza.append(persona, ejes);
    const chat = el("ol", "chat conv__chat");
    chat.setAttribute("aria-label", ev.persona || ev.caso_id);
    const pie = el("div", "conv__pie", "…");
    li.append(cabeza, chat, pie);
    grupo(ev.version).append(li);
    const c = { li, chat, pie };
    convs.set(clave, c);
    cifra("n-conv").textContent = convs.size;
    filtrar();
    return c;
  }

  function turno(ev) {
    const c = tarjeta(ev);
    const cerca = c.chat.scrollHeight - c.chat.scrollTop - c.chat.clientHeight < 60;
    const b = el("li", `burbuja burbuja--${ev.rol === "cliente" ? "cliente" : "agente"}`);
    b.append(el("span", "oculto-visual", (ev.rol === "cliente" ? T.cliente : T.bot) + ": "));
    b.append(el("span", "burbuja__texto", ev.texto || ""));
    if (ev.tools && ev.tools.length) b.append(el("span", "burbuja__tools", `${T.usa} ${ev.tools.join(", ")}`));
    c.chat.append(b);
    if (cerca) c.chat.scrollTop = c.chat.scrollHeight;
  }

  function evaluacion(ev) {
    const clave = `${ev.caso_id}|${ev.version}|${ev.repeticion}`;
    notas.set(clave, ev);
    const c = convs.get(clave);
    if (c) {
      c.li.classList.remove("conv--viva");
      c.pie.textContent = "";
      const tono = ev.critica ? "pildora--rojo" : ev.paso ? "pildora--verde" : "pildora--ambar";
      const p = el("span", `pildora pildora--chica ${tono}`);
      p.append(el("span", "pildora__punto"), document.createTextNode(`${ev.critica ? T.critica : ev.paso ? T.pasa : T.falla} · ${Math.round(ev.puntaje * 100)}`));
      c.pie.append(p);
    }
    cifra("n-eval").textContent = notas.size;
    cifra("n-pasan").textContent = [...notas.values()].filter((n) => n.paso).length;
    filtrar();
  }

  function log(texto) {
    const li = el("li", "", texto);
    bitacora.append(li);
    while (bitacora.children.length > 300) bitacora.firstElementChild.remove();
  }

  function fin(ev) {
    terminada = true;
    marcarEtapa(4);
    etapaTexto.textContent = T.etapa_fin;
    convs.forEach((c) => c.li.classList.remove("conv--viva"));
    final.replaceChildren();
    const tono = ev.estado === "error" ? "aviso--rojo" : ev.estado === "presupuesto" ? "aviso--ambar" : "aviso--verde";
    const aviso = el("div", `aviso ${tono} aviso--fila`);
    aviso.setAttribute("role", "status");
    const texto = el("p", "aviso__texto", T["fin_" + ev.estado] || T.fin_terminada);
    if (ev.estado === "error" && ev.motivo) texto.append(" ", el("span", "dato", ev.motivo));
    if (!ev.boleta) texto.append(" ", T.sin_boleta);
    aviso.append(texto);
    if (ev.boleta) {
      const a = el("a", "boton boton--primario", T.ver_boleta);
      a.href = raiz.dataset.boleta;
      aviso.append(a);
    }
    final.append(aviso);
    final.hidden = false;
  }

  const manejadores = {
    etapa(ev) {
      const i = { investigando: 0, generando: 1, conversando: 2, sugerencia: 3 }[ev.etapa];
      if (i !== undefined) marcarEtapa(i);
      etapaTexto.textContent = T["etapa_" + ev.etapa] || ev.etapa;
    },
    log: (ev) => log(ev.texto),
    // Un reintento llega como otra `conversacion` con la misma clave: se borra lo del intento fallido.
    conversacion: (ev) => tarjeta(ev).chat.replaceChildren(),
    turno,
    evaluacion,
    version(ev) {
      if (ev.n > 0 && !terminada) {
        marcarEtapa(3);
        etapaTexto.textContent = T["etapa_corrigiendo_" + modo] || T.etapa_corrigiendo;
      }
    },
    costo: (ev) => (cifra("costo").textContent = usd(ev.usd)),
    error: (ev) => log("error: " + ev.mensaje),
    fin(ev) {
      fin(ev);
      cifra("costo").textContent = usd(ev.costo_usd);
      fuente.close();
      conexion.hidden = true;
    },
  };

  const fuente = new EventSource(raiz.dataset.eventos);
  fuente.onopen = () => {
    conexion.classList.remove("conexion--caida");
    conexion.classList.add("conexion--viva");
    conexionTexto.textContent = T.vivo_conectado;
  };
  fuente.onerror = () => {
    if (terminada) return;
    conexion.classList.add("conexion--caida");
    conexionTexto.textContent = T.vivo_reconectando;
  };
  fuente.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    manejadores[ev.tipo]?.(ev);
  };

  if (raiz.dataset.activa === "no") {
    terminada = true;
    marcarEtapa(4);
    conexion.hidden = true;
  } else {
    // Si el proceso se reinició, el historial de eventos se perdió y el `fin` nunca llega: el estado lo confirma.
    const vigilar = setInterval(async () => {
      if (terminada) return clearInterval(vigilar);
      const r = await fetch(raiz.dataset.estadoUrl, { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      const e = await r.json();
      if (!e.activa && !["investigando", "generando", "corriendo"].includes(e.estado)) {
        clearInterval(vigilar);
        if (!terminada) fin({ estado: e.estado, motivo: e.motivo, boleta: Boolean(e.evaluadas) });
        fuente.close();
        conexion.hidden = true;
      }
    }, 20000);
  }
}

// ------------------------------------------------------------ boleta

function boleta() {
  document.querySelectorAll("[data-copiar]").forEach((b) => {
    b.addEventListener("click", async () => {
      const origen = document.querySelector(b.dataset.copiar);
      try {
        await navigator.clipboard.writeText(origen.value);
      } catch {
        origen.select();
        document.execCommand("copy");
      }
      b.textContent = T.copiado;
      setTimeout(() => (b.textContent = T.copiar), 2000);
    });
  });
  // Cada conversación abre en el mensaje que falló, no al principio.
  const centrar = (chat) => {
    const f = chat.querySelector("[data-falla]");
    if (f) chat.scrollTop = f.offsetTop - chat.offsetTop - 24;
  };
  document.querySelectorAll("details.falla__conv").forEach((d) => {
    const chat = d.querySelector("[data-chat]");
    if (d.open) centrar(chat);
    d.addEventListener("toggle", () => d.open && centrar(chat));
  });
}

const paginas = { inicio, ficha, vivo, boleta };
paginas[document.body.dataset.pagina]?.();
