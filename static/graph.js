/* Canvas do grafo: render, escalas de cor, filtros, layouts e crawl com progresso.
   Exposto como window.Graph para o painel chamar. */

(function () {
  "use strict";

  cytoscape.use(window.cytoscapeFcose);

  const $ = (id) => document.getElementById(id);
  const statusEl = $("status");
  const countsEl = $("counts");
  const fmt = new Intl.NumberFormat("pt-BR");

  const SURFACE = "#0c0e13";

  let cy = null;
  let graph = { nodes: [], edges: [] };
  let seedId = null;
  let scales = { year: null, cited: null };
  let groupSlots = new Map(); // chave minuscula -> {label, color, shape, slot, count}
  let timeline = null; // geometria da linha do tempo, em coordenadas de modelo
  let relayoutTimer = null;

  /* ============================ escalas de cor ============================= */

  // Rampas sequenciais. A de citacoes e um heatmap frio->quente; a de ano vai de
  // azul profundo a ambar. Ambas legiveis sobre o fundo escuro do canvas.
  const RAMPS = {
    citations: [
      [0.0, [47, 58, 92]],
      [0.25, [63, 94, 156]],
      [0.5, [86, 156, 158]],
      [0.72, [214, 148, 78]],
      [0.88, [227, 106, 74]],
      [1.0, [242, 214, 110]],
    ],
    year: [
      [0.0, [43, 63, 122]],
      [0.35, [47, 106, 140]],
      [0.6, [72, 150, 133]],
      [0.8, [178, 179, 98]],
      [1.0, [242, 208, 107]],
    ],
  };

  // Paleta CATEGORICA para os grupos tematicos. Validada em modo all-pairs contra o
  // fundo #0c0e13 (um grafo de nos e forma all-pairs: qualquer grupo pode acabar ao
  // lado de qualquer outro). Resultado: banda de luminosidade, piso de croma,
  // contraste e piso de visao normal (pior par ΔE 15,2) todos aprovados; a separacao
  // para daltonismo fica na faixa 6–8, que so e admissivel COM codificacao
  // secundaria — por isso cada slot carrega tambem uma FORMA distinta, e a identidade
  // nunca depende so da cor. Os slots sao atribuidos em ordem fixa por frequencia,
  // nunca ciclados: do 8o grupo em diante tudo cai em "outros", cinza e sem forma.
  const GROUP_SLOTS = [
    { color: "#668af9", shape: "round-rectangle" },
    { color: "#b78a16", shape: "triangle" },
    { color: "#30a890", shape: "round-diamond" },
    { color: "#be296a", shape: "hexagon" },
    { color: "#b15ad1", shape: "vee" },
    { color: "#587c18", shape: "pentagon" },
    { color: "#0179a3", shape: "star" },
  ];
  const OTHER_GROUP = { color: "#6b7382", shape: "ellipse", label: "outros" };
  const NO_DATA = "#454b5a";

  function rampColor(ramp, t) {
    if (!isFinite(t)) return NO_DATA;
    const x = Math.min(1, Math.max(0, t));
    for (let i = 1; i < ramp.length; i++) {
      const [p1, c1] = ramp[i - 1];
      const [p2, c2] = ramp[i];
      if (x <= p2) {
        const k = (x - p1) / (p2 - p1 || 1);
        const c = c1.map((v, j) => Math.round(v + (c2[j] - v) * k));
        return `rgb(${c[0]},${c[1]},${c[2]})`;
      }
    }
    const last = ramp[ramp.length - 1][1];
    return `rgb(${last[0]},${last[1]},${last[2]})`;
  }

  function rampCss(ramp) {
    return `linear-gradient(to right, ${ramp
      .map(([p, c]) => `rgb(${c[0]},${c[1]},${c[2]}) ${Math.round(p * 100)}%`)
      .join(", ")})`;
  }

  // Citacoes sao fortemente assimetricas: uns poucos artigos com milhares, a maioria
  // com dezenas. Numa escala linear tudo viraria a mesma cor, entao normalizamos em log.
  function logNorm(v, max) {
    if (!max || max <= 0) return 0;
    return Math.log1p(Math.max(0, v || 0)) / Math.log1p(max);
  }

  function groupBy() {
    return $("f-group").value; // subfield | field | topic
  }

  // O rotulo do grupo tematico do artigo, na granularidade escolhida. `topics[]` do
  // OpenAlex vem ordenado por score, entao o primeiro e o topico dominante.
  function groupLabel(node) {
    const dim = groupBy();
    if (dim === "field") return node.field || null;
    if (dim === "subfield") return node.subfield || null;
    return (node.topics || [])[0] || null;
  }

  function groupKey(node) {
    const label = groupLabel(node);
    return label ? label.toLowerCase() : null;
  }

  function computeScales() {
    const years = graph.nodes.map((n) => n.year).filter((y) => typeof y === "number");
    const cites = graph.nodes.map((n) => n.cited_by_count || 0);
    scales = {
      year: years.length ? { min: Math.min(...years), max: Math.max(...years) } : null,
      cited: cites.length ? { min: Math.min(...cites), max: Math.max(...cites) } : null,
    };

    const counts = new Map();
    graph.nodes.forEach((n) => {
      const k = groupKey(n);
      if (!k) return;
      const cur = counts.get(k) || { n: 0, label: groupLabel(n) };
      cur.n += 1;
      counts.set(k, cur);
    });

    // O grupo do proprio seminal sempre ganha slot, mesmo que seja raro no grafo: e o
    // ponto de partida da pesquisa, nao pode cair no cinza dos "outros".
    const seedKey = (() => {
      const seed = graph.nodes.find((n) => n.depth === 0);
      return seed ? groupKey(seed) : null;
    })();

    const ordered = [...counts.entries()].sort(
      (a, b) => b[1].n - a[1].n || a[1].label.localeCompare(b[1].label, "pt-BR")
    );
    if (seedKey && counts.has(seedKey)) {
      const i = ordered.findIndex(([k]) => k === seedKey);
      if (i > 0) ordered.unshift(...ordered.splice(i, 1));
    }

    groupSlots = new Map();
    ordered.slice(0, GROUP_SLOTS.length).forEach(([key, info], i) => {
      groupSlots.set(key, { ...GROUP_SLOTS[i], label: info.label, count: info.n, slot: i });
    });
  }

  function colorMode() {
    return $("f-color").value; // group | citations | year
  }

  function encodingOf(node) {
    const mode = colorMode();
    const slot = groupSlots.get(groupKey(node));
    // A FORMA sempre carrega o grupo, em qualquer modo de cor: e a codificacao
    // secundaria que a paleta exige, e mantem o agrupamento legivel mesmo quando a
    // cor esta dizendo outra coisa.
    const shape = node.depth === 0 ? "diamond" : slot ? slot.shape : OTHER_GROUP.shape;

    if (mode === "group") {
      return { color: slot ? slot.color : OTHER_GROUP.color, shape };
    }
    if (mode === "year") {
      const s = scales.year;
      if (!s || typeof node.year !== "number") return { color: NO_DATA, shape };
      const span = Math.max(1, s.max - s.min);
      return { color: rampColor(RAMPS.year, (node.year - s.min) / span), shape };
    }
    const s = scales.cited;
    return {
      color: s ? rampColor(RAMPS.citations, logNorm(node.cited_by_count, s.max)) : NO_DATA,
      shape,
    };
  }

  function nodeSize(count) {
    // sqrt para que 10.000 citacoes nao gerem um no 1000x maior que 10.
    return Math.round(Math.min(56, Math.max(14, Math.sqrt(count || 0) * 1.45 + 14)));
  }

  /* ================================ legenda =============================== */

  const SHAPE_SVG = {
    "round-rectangle": '<rect x="1.5" y="2.5" width="11" height="9" rx="2.5"/>',
    triangle: '<polygon points="7,2 12.5,12 1.5,12"/>',
    "round-diamond": '<polygon points="7,1.5 12.5,7 7,12.5 1.5,7"/>',
    hexagon: '<polygon points="7,1.5 12.2,4.5 12.2,9.5 7,12.5 1.8,9.5 1.8,4.5"/>',
    vee: '<polygon points="1.5,2 7,8 12.5,2 7,12.5"/>',
    pentagon: '<polygon points="7,1.5 12.5,5.7 10.4,12.2 3.6,12.2 1.5,5.7"/>',
    star: '<polygon points="7,1.2 8.7,5.3 13,5.6 9.7,8.4 10.8,12.6 7,10.3 3.2,12.6 4.3,8.4 1,5.6 5.3,5.3"/>',
    ellipse: '<circle cx="7" cy="7" r="5"/>',
  };

  function shapeSwatch(color, shape) {
    return `<svg width="14" height="14" viewBox="0 0 14 14" fill="${color}" aria-hidden="true">${
      SHAPE_SVG[shape] || SHAPE_SVG.ellipse
    }</svg>`;
  }

  function renderLegend() {
    const box = $("legend-scale");
    const mode = colorMode();

    // A legenda de grupos aparece sempre que houver grupos, mesmo colorindo por ano
    // ou citacoes: a FORMA continua carregando o grupo, e sem legenda a forma nao
    // significa nada.
    if (groupSlots.size) {
      const active = $("f-topic").value;
      const entries = [...groupSlots.entries()].sort((a, b) => a[1].slot - b[1].slot);
      const anyOther = graph.nodes.some((n) => groupKey(n) && !groupSlots.has(groupKey(n)));
      const swatchColor = (s) => (mode === "group" ? s.color : "#9aa3b4");

      box.innerHTML =
        (mode === "group" ? "" : `${scaleRampHtml(mode)}<span class="spacer-sm"></span>`) +
        `<span class="topics">` +
        entries
          .map(([key, s]) => {
            const last = lastYearOf(key);
            const dead = isDead(key);
            return `<button type="button" class="topic" data-group="${escapeHtml(key)}"
                 aria-pressed="${active === key}"
                 title="${escapeHtml(s.label)} · ${s.count} artigo(s)${
              last ? ` · mais recente: ${last}` : ""
            }">${shapeSwatch(swatchColor(s), s.shape)}<span${dead ? ' class="dead"' : ""}>${escapeHtml(
              shortLabel(s.label)
            )}${dead ? ` · parou em ${last}` : ""}</span></button>`;
          })
          .join("") +
        (anyOther
          ? `<span class="topic">${shapeSwatch(
              mode === "group" ? OTHER_GROUP.color : "#9aa3b4",
              OTHER_GROUP.shape
            )}<span>${OTHER_GROUP.label}</span></span>`
          : "") +
        `</span>`;

      box.querySelectorAll("button.topic").forEach((b) =>
        b.addEventListener("click", () => {
          const key = b.dataset.group;
          $("f-topic").value = $("f-topic").value === key ? "" : key;
          applyFilters();
          renderLegend();
        })
      );
      return;
    }

    box.innerHTML = mode === "group" ? "" : scaleRampHtml(mode);
  }

  function scaleRampHtml(mode) {
    const isYear = mode === "year";
    const s = isYear ? scales.year : scales.cited;
    if (!s) return "";
    // Anos NAO levam separador de milhar: "2.025" nao e um ano.
    const lo = isYear ? String(s.min) : "0";
    const hi = isYear ? String(s.max) : fmt.format(s.max);
    return (
      `<span class="ramp-lo">${lo}</span>` +
      `<i class="ramp" style="background:${rampCss(isYear ? RAMPS.year : RAMPS.citations)}"></i>` +
      `<span class="ramp-hi">${hi}</span>` +
      `<span class="ramp-what">${isYear ? "ano" : "citações (log)"}</span>`
    );
  }

  function shortLabel(s) {
    return s.length > 30 ? s.slice(0, 28) + "…" : s;
  }

  // Um grupo cujo artigo mais recente e antigo demais e um assunto que esfriou —
  // exatamente o que a linha do tempo existe para revelar.
  function lastYearOf(key) {
    let last = null;
    graph.nodes.forEach((n) => {
      if (groupKey(n) === key && typeof n.year === "number" && (last == null || n.year > last)) {
        last = n.year;
      }
    });
    return last;
  }

  function deadBefore() {
    return (scales.year ? scales.year.max : new Date().getFullYear()) - 3;
  }

  function isDead(key) {
    const last = lastYearOf(key);
    return last != null && last < deadBefore();
  }

  /* ============================== construcao ============================== */

  function shortTitle(t) {
    const s = String(t || "");
    return s.length > 54 ? s.slice(0, 52) + "…" : s;
  }

  function toElements() {
    const nodes = graph.nodes.map((n) => {
      const enc = encodingOf(n);
      return {
        data: {
          id: n.id,
          label: shortTitle(n.title),
          title: n.title,
          year: n.year,
          cited: n.cited_by_count || 0,
          depth: n.depth,
          mark: n.mark,
          frontier: n.frontier,
          topics: (n.topics || []).map((t) => t.toLowerCase()),
          groupKey: groupKey(n),
          groupLabel: groupLabel(n),
          color: enc.color,
          shape: enc.shape,
          size: nodeSize(n.cited_by_count),
          seed: n.depth === 0,
        },
      };
    });
    const edges = graph.edges.map((e) => ({
      data: { id: `${e.citing}->${e.cited}`, source: e.citing, target: e.cited },
    }));
    return [...nodes, ...edges];
  }

  const STYLE = [
    {
      selector: "node",
      style: {
        "background-color": "data(color)",
        shape: "data(shape)",
        width: "data(size)",
        height: "data(size)",
        label: "data(label)",
        "font-size": 9,
        "text-wrap": "wrap",
        "text-max-width": 120,
        "text-valign": "bottom",
        "text-margin-y": 4,
        color: "#c3cbdb",
        // Contorno no texto: sem isso os rotulos desaparecem sobre as arestas.
        "text-outline-width": 2.5,
        "text-outline-color": SURFACE,
        "text-outline-opacity": 0.95,
        "text-opacity": 0,
        "border-width": 1,
        "border-color": SURFACE,
        "z-index": 10,
        "transition-property": "border-width, border-color, opacity, background-color",
        "transition-duration": "150ms",
      },
    },
    // `named` e ligado por JS nos nos que merecem rotulo, para o canvas nao virar sopa.
    { selector: "node[?named]", style: { "text-opacity": 0.92 } },
    {
      selector: "node[?seed]",
      style: {
        "border-width": 2.5,
        "border-color": "#eef1f6",
        "text-opacity": 1,
        color: "#eef1f6",
        "font-size": 12,
        "font-weight": "bold",
        "z-index": 40,
      },
    },
    {
      selector: "node[?frontier]",
      style: { "border-width": 1.5, "border-color": "#828b9c", "border-style": "dashed" },
    },
    {
      selector: 'node[mark = "interested"]',
      style: { "border-width": 3.5, "border-color": "#f0c14b", "border-style": "solid", "z-index": 30 },
    },
    {
      selector: 'node[mark = "read"]',
      style: { "border-width": 3, "border-color": "#57c48a", "border-style": "solid" },
    },
    { selector: 'node[mark = "discarded"]', style: { opacity: 0.2 } },
    {
      selector: "node:selected",
      style: {
        "border-width": 4,
        "border-color": "#6aa9ff",
        "border-style": "solid",
        "text-opacity": 1,
        "font-size": 12,
        color: "#eef1f6",
        "z-index": 50,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1,
        "line-color": "#2b3244",
        "target-arrow-color": "#39415a",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.65,
        "curve-style": "bezier",
        "control-point-step-size": 28,
        opacity: 0.55,
        "z-index": 1,
      },
    },
    {
      selector: "edge.highlight",
      style: {
        "line-color": "#6aa9ff",
        "target-arrow-color": "#6aa9ff",
        width: 2,
        opacity: 1,
        "z-index": 20,
      },
    },
    // Na linha do tempo as arestas sao ruido: a leitura e a distribuicao no tempo, nao
    // a conectividade. Ficam quase invisiveis e voltam ao clicar num no.
    { selector: "edge.faded", style: { opacity: 0.08, "arrow-scale": 0.4 } },
    { selector: ".dimmed", style: { opacity: 0.07, "text-opacity": 0 } },
    { selector: ".hidden", style: { display: "none" } },
  ];

  /* =============================== layouts =============================== */

  function visible() {
    return cy.elements().not(".hidden");
  }

  function layoutName() {
    return $("f-layout").value;
  }

  function layoutOpts() {
    const nodes = visible().nodes();
    const n = Math.max(1, nodes.length);
    const maxDepth = Math.max(0, ...nodes.map((x) => x.data("depth") || 0));
    const common = { animate: true, animationDuration: 550, fit: true, padding: 45 };

    switch (layoutName()) {
      case "timeline":
        return timelineLayout(nodes, common);

      case "radial":
        // Aneis concentricos por nivel: o seminal no centro, quem o citou no primeiro
        // anel, e assim por diante. E a forma que a estrutura do grafo realmente tem,
        // e o unico layout de forca que continua legivel com centenas de nos no anel
        // externo.
        return {
          ...common,
          name: "concentric",
          concentric: (node) => maxDepth - (node.data("depth") || 0) + 1,
          levelWidth: () => 1,
          // Espacamento cresce com o tamanho do grafo; senao o anel externo se cola.
          minNodeSpacing: Math.max(14, Math.min(48, Math.round(1400 / Math.sqrt(n)))),
          spacingFactor: 1,
          avoidOverlap: true,
          equidistant: false,
          startAngle: (3 / 2) * Math.PI,
        };

      case "layers":
        return layersLayout(nodes, common);

      default:
        // `randomize: false` faz o fcose partir das posicoes semeadas por grupo (ver
        // seedOrganicByGroup) em vez de um estado aleatorio. Sem isso o grafo vira uma
        // bolha uniforme: as citacoes apontam quase todas para o seminal e para os
        // hubs do primeiro nivel, entao a topologia sozinha nao separa os temas.
        return {
          ...common,
          name: "fcose",
          quality: n > 300 ? "default" : "proof",
          randomize: false,
          // Distancia e repulsao escalam com o tamanho: parametros fixos produzem
          // novelo em grafo grande e ilhas soltas em grafo pequeno.
          idealEdgeLength: Math.round(60 + 260 / Math.sqrt(n)),
          nodeRepulsion: Math.round(6000 + 60000 / Math.sqrt(n)),
          nodeSeparation: 110,
          // Gravidade baixa: gravidade alta puxaria os clusters de volta ao centro e
          // desfaria o agrupamento tematico da semeadura.
          gravity: 0.08,
          gravityRange: 4,
          numIter: 1600,
          packComponents: true,
          nodeDimensionsIncludeLabels: false,
        };
    }
  }

  /* ------------------------------- organico ------------------------------ */

  /* Semeia cada grupo tematico num setor proprio antes de rodar o fcose.

     O fcose parte dessas posicoes (randomize: false) e as relaxa respeitando as
     arestas, o que produz clusters tematicos visiveis em vez de uma bolha uniforme.
     Sem a semeadura nao ha o que agrupar: quase toda citacao aponta para o seminal ou
     para um punhado de hubs, e a topologia sozinha nao distingue os temas. */
  function seedOrganicByGroup(nodes) {
    const lanes = [...groupSlots.entries()].sort((a, b) => a[1].slot - b[1].slot).map(([k]) => k);
    const sectors = lanes.length + 1; // +1 para "outros"
    const sectorOf = new Map(lanes.map((k, i) => [k, i]));
    const R = 42 * Math.sqrt(Math.max(4, nodes.length));

    const seen = new Map();
    nodes.forEach((node) => {
      if (node.data("depth") === 0) {
        node.position({ x: 0, y: 0 }); // seminal no centro
        return;
      }
      const key = node.data("groupKey");
      const sector = sectorOf.has(key) ? sectorOf.get(key) : sectors - 1;
      const i = seen.get(sector) || 0;
      seen.set(sector, i + 1);

      const center = ((sector + 0.5) / sectors) * 2 * Math.PI;
      // Angulo aureo dentro do setor: espalha sem sobrepor e sem depender de sorteio.
      const spread = (Math.PI / sectors) * 0.85;
      const angle = center + (((i * 0.618034) % 1) - 0.5) * 2 * spread;
      const radius = R * (0.42 + 0.58 * (((i * 0.381966) % 1) ** 0.5));
      node.position({ x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
    });
  }

  /* ------------------------------- camadas ------------------------------- */

  /* Um nivel por faixa horizontal, seminal no topo.

     Nao usamos `breadthfirst`: as arestas apontam do citante para o citado, ou seja o
     seminal e um SORVEDOURO do grafo, e o breadthfirst dirigido o joga para baixo e
     achata todo o resto numa fileira unica. Como ja conhecemos a profundidade de cada
     no (o crawl a gravou), posicionar direto e exato e sempre legivel. */
  function layersLayout(nodes, common) {
    const LEVEL_GAP = 76;
    const CELL_W = 132;
    const CELL_H = 62;

    const byDepth = new Map();
    nodes.forEach((n) => {
      const d = n.data("depth") || 0;
      if (!byDepth.has(d)) byDepth.set(d, []);
      byDepth.get(d).push(n);
    });

    const positions = {};
    let y = 0;
    [...byDepth.keys()]
      .sort((a, b) => a - b)
      .forEach((depth) => {
        const level = byDepth.get(depth).sort((a, b) => b.data("cited") - a.data("cited"));
        // Quebra o nivel em varias linhas para manter uma proporcao ~3:1, em vez de
        // uma fileira interminavel.
        const cols = Math.max(1, Math.min(level.length, Math.ceil(Math.sqrt(level.length * 3))));
        const rows = Math.ceil(level.length / cols);
        level.forEach((node, i) => {
          const r = Math.floor(i / cols);
          const c = i % cols;
          // Ultima linha centralizada, para nao deixar um degrau solto na direita.
          const inRow = r === rows - 1 ? level.length - r * cols : cols;
          positions[node.id()] = {
            x: (c - (inRow - 1) / 2) * CELL_W,
            y: y + r * CELL_H,
          };
        });
        y += rows * CELL_H + LEVEL_GAP;
      });

    return { ...common, name: "preset", positions: (n) => positions[n.id()] || { x: 0, y: 0 } };
  }

  /* ---------------------------- linha do tempo --------------------------- */

  const SUB_W = 66; // largura de uma sub-coluna dentro de um ano
  const STACK_GAP = 9; // folga entre artigos empilhados
  const LANE_PAD = 34;
  const MIN_LANE = 62;
  // Uma pilha mais alta que isto quebra em sub-colunas dentro do mesmo ano. Sem esse
  // teto, um ano com 30 artigos criava uma raia 30x mais alta que as vizinhas e
  // achatava todas as outras contra o zero.
  const MAX_STACK = 9;

  /* Posiciona cada artigo em (ano, raia do grupo). Cada raia e um tema; cada coluna um
     ano. Uma raia que seca a partir de certo ano e um assunto que morreu — ler isso de
     relance e o objetivo da visao. */
  function timelineLayout(nodes, common) {
    const years = [
      ...new Set(nodes.map((n) => n.data("year")).filter((y) => typeof y === "number")),
    ].sort((a, b) => a - b);
    const hasUndated = nodes.some((n) => typeof n.data("year") !== "number");
    const colOf = new Map(years.map((y, i) => [y, i + (hasUndated ? 1 : 0)]));
    const colCount = years.length + (hasUndated ? 1 : 0);

    // Ordem das raias: slots primeiro (na ordem fixa da paleta), depois "outros".
    const laneKeys = [...groupSlots.entries()]
      .sort((a, b) => a[1].slot - b[1].slot)
      .map(([k]) => k);
    const laneIndex = new Map(laneKeys.map((k, i) => [k, i]));
    const OTHER_LANE = laneKeys.length;

    const laneOf = (node) => {
      const k = node.data("groupKey");
      return k != null && laneIndex.has(k) ? laneIndex.get(k) : OTHER_LANE;
    };
    const colIndexOf = (node) =>
      colOf.has(node.data("year")) ? colOf.get(node.data("year")) : 0;

    // Agrupa por celula (raia, coluna), mais citado primeiro.
    const cells = new Map();
    const cellKey = (lane, col) => `${lane}|${col}`;
    nodes
      .sort((a, b) => b.data("cited") - a.data("cited"))
      .forEach((n) => {
        const k = cellKey(laneOf(n), colIndexOf(n));
        if (!cells.has(k)) cells.set(k, []);
        cells.get(k).push(n);
      });

    // Cada celula vira uma grade de sub-colunas: `rows` artigos por pilha, no maximo
    // MAX_STACK. Altura em unidades reais, nao em contagem — os nos variam de 14 a 56
    // px de diametro e usar contagem os fazia se sobrepor.
    const shapeOfCell = (cell) => {
      const subCols = Math.max(1, Math.ceil(cell.length / MAX_STACK));
      const rows = Math.ceil(cell.length / subCols);
      let tallest = 0;
      for (let s = 0; s < subCols; s++) {
        const stack = cell.slice(s * rows, (s + 1) * rows);
        const h = stack.reduce((sum, n) => sum + n.data("size") + STACK_GAP, 0) - STACK_GAP;
        tallest = Math.max(tallest, h);
      }
      return { subCols, rows, height: Math.max(0, tallest) };
    };
    const cellShape = new Map();
    cells.forEach((cell, k) => cellShape.set(k, shapeOfCell(cell)));

    const laneCount = OTHER_LANE + 1;
    const laneHeight = [];
    for (let lane = 0; lane < laneCount; lane++) {
      let tallest = 0;
      for (let col = 0; col < Math.max(1, colCount); col++) {
        const shape = cellShape.get(cellKey(lane, col));
        if (shape) tallest = Math.max(tallest, shape.height);
      }
      laneHeight.push(Math.max(MIN_LANE, tallest + LANE_PAD));
    }
    const laneTop = [0];
    for (let i = 1; i <= laneCount; i++) laneTop.push(laneTop[i - 1] + laneHeight[i - 1]);

    // Largura de cada ano = maior numero de sub-colunas que algum tema usou nele.
    const colWidth = [];
    for (let col = 0; col < Math.max(1, colCount); col++) {
      let widest = 1;
      for (let lane = 0; lane < laneCount; lane++) {
        const shape = cellShape.get(cellKey(lane, col));
        if (shape) widest = Math.max(widest, shape.subCols);
      }
      colWidth.push(widest * SUB_W);
    }
    const colX = [0];
    for (let i = 1; i <= colWidth.length; i++) colX.push(colX[i - 1] + colWidth[i - 1]);

    // Estica no eixo x para preencher o canvas: sem isso o desenho fica alto e
    // estreito, com metade da tela vazia.
    const el = $("cy");
    const aspect = el.clientHeight ? el.clientWidth / el.clientHeight : 1.8;
    const totalH = laneTop[laneCount] || 1;
    const naturalW = colX[colWidth.length] || 1;
    const stretch = Math.max(1, Math.min(7, (totalH * aspect) / naturalW));

    const positions = {};
    cells.forEach((cell, k) => {
      const [lane, col] = k.split("|").map(Number);
      const { subCols, rows } = cellShape.get(k);
      const centerX = colX[col] + colWidth[col] / 2;
      for (let s = 0; s < subCols; s++) {
        const stack = cell.slice(s * rows, (s + 1) * rows);
        const h = stack.reduce((sum, n) => sum + n.data("size") + STACK_GAP, 0) - STACK_GAP;
        // Centraliza a pilha verticalmente na raia e as sub-colunas no ano.
        let y = laneTop[lane] + (laneHeight[lane] - h) / 2;
        const x = centerX + (s - (subCols - 1) / 2) * SUB_W;
        stack.forEach((n) => {
          const size = n.data("size");
          positions[n.id()] = { x: x * stretch, y: y + size / 2 };
          y += size + STACK_GAP;
        });
      }
    });

    timeline = {
      years,
      hasUndated,
      colOf,
      colCenter: colWidth.map((w, i) => (colX[i] + w / 2) * stretch),
      colWidth: colWidth.map((w) => w * stretch),
      laneTop,
      laneHeight,
      laneKeys,
      otherLane: OTHER_LANE,
    };

    return {
      ...common,
      name: "preset",
      positions: (node) => positions[node.id()] || { x: 0, y: 0 },
      animationDuration: 500,
    };
  }

  const TL_GUTTER = 206; // calha esquerda reservada aos nomes das raias
  const TL_AXIS = 30; // faixa inferior reservada aos rotulos de ano

  /* Enquadra a linha do tempo deixando margens assimetricas: `cy.fit` so aceita
     padding uniforme, e com ele os nomes das raias cobriam a primeira coluna de anos
     e o eixo de anos cobria a ultima raia. */
  function fitTimeline() {
    const el = $("cy");
    const bb = visible().boundingBox();
    if (!bb.w || !bb.h || !el.clientWidth) return;
    const pad = 24;
    const w = Math.max(50, el.clientWidth - TL_GUTTER - pad);
    const h = Math.max(50, el.clientHeight - TL_AXIS - pad * 2);
    const zoom = Math.max(cy.minZoom(), Math.min(cy.maxZoom(), Math.min(w / bb.w, h / bb.h)));
    cy.zoom(zoom);
    cy.pan({ x: TL_GUTTER - bb.x1 * zoom, y: pad - bb.y1 * zoom });
  }

  function updateTimelineOverlay() {
    const box = $("tl-overlay");
    if (!cy || layoutName() !== "timeline" || !timeline) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    const z = cy.zoom();
    const pan = cy.pan();
    const sx = (x) => x * z + pan.x;
    const sy = (y) => y * z + pan.y;

    const parts = [];
    const cutoff = deadBefore();

    timeline.laneKeys.concat(["__other__"]).forEach((key, lane) => {
      const top = sy(timeline.laneTop[lane]);
      const height = timeline.laneHeight[lane] * z;
      const slot = groupSlots.get(key);
      const label = slot ? shortLabel(slot.label) : OTHER_GROUP.label;
      const last = slot ? lastYearOf(key) : null;
      const dead = last != null && last < cutoff;
      if (lane % 2 === 1) {
        parts.push(`<div class="lane-band" style="top:${top}px;height:${height}px"></div>`);
      }
      parts.push(`<div class="lane-rule" style="top:${top}px"></div>`);
      parts.push(
        `<div class="lane-name${dead ? " dead" : ""}" style="top:${top + height / 2}px;` +
          `border-left-color:${slot ? slot.color : OTHER_GROUP.color}"` +
          ` title="${escapeHtml(slot ? slot.label : OTHER_GROUP.label)}">${escapeHtml(
            label
          )}${dead ? ` · parou em ${last}` : ""}</div>`
      );
    });
    parts.push(
      `<div class="lane-rule" style="top:${sy(
        timeline.laneTop[timeline.otherLane + 1]
      )}px"></div>`
    );

    // Rotulos de ano: rareia os ticks quando o zoom aperta as colunas.
    const narrowest = Math.min(...timeline.colWidth) * z;
    const step = Math.max(1, Math.ceil(48 / Math.max(1, narrowest)));
    timeline.years.forEach((year, i) => {
      const col = timeline.colOf.get(year);
      const x = sx(timeline.colCenter[col]);
      parts.push(`<div class="year-grid" style="left:${x}px"></div>`);
      if (i % step && i !== timeline.years.length - 1) return;
      parts.push(`<div class="year-tick" style="left:${x}px">${year}</div>`);
    });
    if (timeline.hasUndated) {
      parts.push(`<div class="year-tick" style="left:${sx(timeline.colCenter[0])}px">s/ ano</div>`);
    }

    box.innerHTML = parts.join("");
  }

  function relayout() {
    if (!cy || !visible().nodes().length) return;
    const isTimeline = layoutName() === "timeline";
    if (!isTimeline) timeline = null;
    cy.edges().toggleClass("faded", isTimeline);

    if (layoutName() === "fcose") seedOrganicByGroup(visible().nodes());

    const layout = visible().layout(layoutOpts());
    layout.one("layoutstop", () => {
      // `fit` do layout preset as vezes deixa conteudo fora da viewport; enquadrar
      // explicitamente sobre os visiveis garante que nada fique cortado.
      if (isTimeline) fitTimeline();
      else cy.fit(visible(), 45);
      nameLabels();
      updateTimelineOverlay();
    });
    layout.run();
    updateTimelineOverlay();
  }

  function scheduleRelayout() {
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(relayout, 220);
  }

  /* ================================ render ================================ */

  function render() {
    computeScales();
    const elements = toElements();
    if (!cy) {
      cy = cytoscape({
        container: $("cy"),
        elements,
        style: STYLE,
        minZoom: 0.06,
        maxZoom: 3,
        textureOnViewport: true,
        pixelRatio: 1,
      });
      bindCanvas();
    } else {
      cy.batch(() => {
        cy.elements().remove();
        cy.add(elements);
      });
    }
    rebuildFilters();
    applyFilters({ relayout: false });
    relayout();
    renderLegend();
  }

  function bindCanvas() {
    cy.on("tap", "node", (ev) => {
      focus(ev.target);
      window.Panel.show(ev.target.id());
    });
    cy.on("tap", (ev) => {
      if (ev.target === cy) {
        cy.elements().removeClass("dimmed highlight");
        cy.edges().toggleClass("faded", layoutName() === "timeline");
        cy.$(":selected").unselect();
      }
    });
    // Hover sempre revela o titulo, independente do corte de rotulos.
    cy.on("mouseover", "node", (ev) => ev.target.style("text-opacity", 1));
    cy.on("mouseout", "node", (ev) => {
      if (!ev.target.selected() && !ev.target.data("named")) ev.target.removeStyle("text-opacity");
    });
    cy.on("zoom", nameLabels);
    cy.on("viewport", updateTimelineOverlay);
  }

  function focus(node) {
    cy.elements().removeClass("dimmed highlight");
    const neighborhood = node.closedNeighborhood();
    cy.elements().difference(neighborhood).addClass("dimmed");
    // As arestas do no clicado voltam a aparecer mesmo na linha do tempo.
    node.connectedEdges().addClass("highlight").removeClass("faded");
  }

  /* =============================== rotulos =============================== */

  // Com zoom afastado so os artigos mais citados ganham rotulo; aproximando, todos.
  // Sem isso, centenas de titulos sobrepostos tornam o grafo ilegivel.
  function nameLabels() {
    if (!cy) return;
    const nodes = visible().nodes().not("[?seed]");
    const zoom = cy.zoom();
    // Na linha do tempo nenhum rotulo fixo: as pilhas sao densas e os titulos se
    // sobrepunham em cima dos nos. Hover e clique continuam revelando.
    const budget =
      layoutName() === "timeline"
        ? 0
        : zoom > 1.1
        ? nodes.length
        : Math.max(8, Math.round(14 * zoom + 6));
    const ranked = nodes.sort((a, b) => b.data("cited") - a.data("cited"));
    cy.batch(() => {
      ranked.forEach((n, i) => n.data("named", i < budget));
      cy.nodes(".hidden").forEach((n) => n.data("named", false));
    });
  }

  /* =============================== filtros =============================== */

  // O slider de citacoes anda em log: os primeiros passos separam 0 de 50, os ultimos
  // separam 2.000 de 20.000. Num slider linear, 90% do curso nao filtraria nada.
  function citedThreshold() {
    const s = scales.cited;
    const pct = Number($("f-cited").value) / 100;
    if (!s || !pct) return 0;
    return Math.round(Math.expm1(Math.log1p(s.max) * pct));
  }

  function rebuildFilters() {
    const yr = scales.year;
    const slider = $("f-year");
    if (yr && yr.max > yr.min) {
      slider.disabled = false;
      slider.min = yr.min;
      slider.max = yr.max;
      if (!slider.dataset.touched) slider.value = yr.min;
      slider.value = Math.min(Math.max(Number(slider.value), yr.min), yr.max);
    } else {
      slider.disabled = true;
      slider.value = yr ? yr.min : 0;
    }
    $("f-year-out").textContent = yr ? slider.value : "—";

    const cited = $("f-cited");
    const hasSpread = scales.cited && scales.cited.max > 0;
    cited.disabled = !hasSpread;
    if (!hasSpread) cited.value = 0;
    $("f-cited-out").textContent = hasSpread ? fmt.format(citedThreshold()) : "—";

    // Grupos sem distincao de caixa: o casing do OpenAlex e inconsistente entre
    // registros e geraria opcoes duplicadas.
    const byKey = new Map();
    graph.nodes.forEach((n) => {
      const label = groupLabel(n);
      if (!label) return;
      const k = label.toLowerCase();
      if (!byKey.has(k)) byKey.set(k, { label, n: 0 });
      byKey.get(k).n += 1;
    });
    const select = $("f-topic");
    const keep = select.value;
    select.innerHTML =
      `<option value="">todos</option>` +
      [...byKey.entries()]
        .sort((a, b) => b[1].n - a[1].n || a[1].label.localeCompare(b[1].label, "pt-BR"))
        .map(
          ([k, v]) =>
            `<option value="${escapeHtml(k)}">${escapeHtml(v.label)} (${v.n})</option>`
        )
        .join("");
    select.value = byKey.has(keep) ? keep : "";
  }

  function applyFilters(opts) {
    if (!cy) return;
    const minYear = $("f-year").disabled ? null : Number($("f-year").value);
    const minCited = citedThreshold();
    const group = $("f-topic").value;
    const hideDiscarded = $("f-hide-discarded").checked;

    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const d = n.data();
        const failsYear = minYear != null && typeof d.year === "number" && d.year < minYear;
        const failsCited = minCited > 0 && d.cited < minCited;
        const failsTopic = group && d.groupKey !== group;
        const failsMark = hideDiscarded && d.mark === "discarded";
        // O seminal nunca desaparece: e a ancora do grafo.
        n.toggleClass(
          "hidden",
          !d.seed && (failsYear || failsCited || failsTopic || failsMark)
        );
      });
      cy.edges().forEach((e) => {
        e.toggleClass("hidden", e.source().hasClass("hidden") || e.target().hasClass("hidden"));
      });
    });

    nameLabels();
    updateCounts();
    if (!opts || opts.relayout !== false) scheduleRelayout();
  }

  function recolor() {
    if (!cy) return;
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const row = byId.get(n.id());
        if (!row) return;
        const enc = encodingOf(row);
        n.data("color", enc.color);
        n.data("shape", enc.shape);
      });
    });
    renderLegend();
  }

  function updateCounts() {
    if (!cy) {
      countsEl.textContent = "";
      return;
    }
    const total = cy.nodes().length;
    const shown = cy.nodes().not(".hidden").length;
    const frontier = cy.nodes().not(".hidden").filter((n) => n.data("frontier")).length;
    countsEl.textContent =
      `${fmt.format(shown)}${shown !== total ? ` de ${fmt.format(total)}` : ""} artigos` +
      ` · ${fmt.format(cy.edges().not(".hidden").length)} citações` +
      ` · ${fmt.format(frontier)} não expandidos`;
  }

  /* ================================ crawl ================================ */

  function status(msg, kind) {
    statusEl.textContent = msg || "";
    statusEl.className = "status" + (kind ? ` ${kind}` : "");
  }

  function params() {
    const minYear = $("p-minyear").value.trim();
    return {
      depth: Number($("p-depth").value) || 2,
      top_n: Number($("p-topn").value) || 20,
      min_year: minYear ? Number(minYear) : null,
      max_nodes: Number($("p-maxnodes").value) || 500,
    };
  }

  async function reload() {
    graph = await (await fetch("/api/graph")).json();
    render();
  }

  async function runCrawl(id) {
    seedId = id;
    $("seed-btn").disabled = true;
    status("percorrendo as citações…");

    try {
      const res = await fetch("/api/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seed_id: id, ...params() }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `o servidor respondeu ${res.status}`);
      }

      // SSE via fetch: precisamos de POST, o que EventSource nao permite.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let last = null;

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop();
        for (const chunk of chunks) {
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const ev = JSON.parse(line.slice(6));
          last = ev;
          if (ev.error) throw new Error(ev.error);
          status(
            ev.done
              ? "montando o grafo…"
              : `${fmt.format(ev.nodes)} artigos · nível ${ev.depth} · ${fmt.format(
                  ev.queued
                )} na fila`
          );
        }
      }

      await reload();
      const n = last ? last.nodes : graph.nodes.length;
      status(
        last && last.truncated
          ? `${fmt.format(n)} artigos — o limite de nós interrompeu o crawl; ` +
              `aumente "máx. de nós" ou escave clicando nos nós`
          : `${fmt.format(n)} artigos no grafo. Clique num nó para ver os detalhes.`,
        last && last.truncated ? "warn" : null
      );
      if (cy && cy.$id(id).length) window.Panel.show(id);
    } catch (err) {
      status(err.message, "err");
    } finally {
      $("seed-btn").disabled = false;
    }
  }

  /* ============================= candidatos ============================== */

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function showCandidates(list) {
    const box = $("candidates");
    $("candidates-list").innerHTML = list
      .map(
        (c, i) => `<li><button type="button" class="pick" data-i="${i}">
            <span class="pick-title">${escapeHtml(c.title)}</span>
            <span class="pick-meta">${c.year || "s/ ano"}${
          c.venue ? ` · ${escapeHtml(c.venue)}` : ""
        } · ${fmt.format(c.cited_by_count)} citações${
          c.authors.length ? ` · ${escapeHtml(c.authors[0].name)} et al.` : ""
        }</span>
          </button></li>`
      )
      .join("");
    box.hidden = false;
    box.querySelectorAll("button.pick").forEach((b) =>
      b.addEventListener("click", () => {
        box.hidden = true;
        runCrawl(list[Number(b.dataset.i)].id);
      })
    );
  }

  /* ================================= bind ================================ */

  $("seed-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const raw = $("seed-input").value.trim();
    if (!raw) return;
    $("candidates").hidden = true;
    $("seed-btn").disabled = true;
    status("procurando o artigo…");
    try {
      const r = await fetch("/api/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: raw }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `o servidor respondeu ${r.status}`);
      if (data.paper) {
        await runCrawl(data.paper.id);
      } else {
        status(`${data.candidates.length} candidatos — escolha o seu seminal`);
        showCandidates(data.candidates);
      }
    } catch (err) {
      status(err.message, "err");
    } finally {
      $("seed-btn").disabled = false;
    }
  });

  $("candidates-cancel").addEventListener("click", () => {
    $("candidates").hidden = true;
    status("");
  });

  $("f-year").addEventListener("input", (ev) => {
    ev.target.dataset.touched = "1";
    $("f-year-out").textContent = ev.target.value;
    applyFilters();
  });
  $("f-cited").addEventListener("input", () => {
    $("f-cited-out").textContent = fmt.format(citedThreshold());
    applyFilters();
  });
  $("f-topic").addEventListener("change", () => {
    applyFilters();
    renderLegend();
  });
  $("f-hide-discarded").addEventListener("change", () => applyFilters());
  $("f-color").addEventListener("change", recolor);
  // Trocar a granularidade refaz slots, formas, raias e o filtro de grupo.
  $("f-group").addEventListener("change", () => {
    computeScales();
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const row = byId.get(n.id());
        if (!row) return;
        const enc = encodingOf(row);
        n.data("groupKey", groupKey(row));
        n.data("groupLabel", groupLabel(row));
        n.data("color", enc.color);
        n.data("shape", enc.shape);
      });
    });
    rebuildFilters();
    applyFilters({ relayout: false });
    relayout();
    renderLegend();
  });
  $("f-layout").addEventListener("change", relayout);
  $("f-reset").addEventListener("click", () => {
    const yr = $("f-year");
    yr.value = yr.min;
    delete yr.dataset.touched;
    $("f-year-out").textContent = yr.disabled ? "—" : yr.value;
    $("f-cited").value = 0;
    $("f-cited-out").textContent = $("f-cited").disabled ? "—" : "0";
    $("f-topic").value = "";
    $("f-hide-discarded").checked = false;
    applyFilters();
    renderLegend();
  });
  window.addEventListener("resize", updateTimelineOverlay);

  window.Graph = {
    status,
    params,
    reload,
    applyMark(id, mark) {
      if (!cy) return;
      const n = cy.$id(id);
      if (!n.length) return;
      n.data("mark", mark);
      const row = graph.nodes.find((x) => x.id === id);
      if (row) row.mark = mark;
      // Marcar nao deve reembaralhar o grafo debaixo do cursor; so refiltra.
      applyFilters({ relayout: false });
    },
    citerCount(id) {
      return graph.edges.filter((e) => e.cited === id).length;
    },
  };

  // Retoma o grafo salvo: fechar o servidor e voltar nao perde nada.
  (async function boot() {
    await reload();
    if (graph.nodes.length) {
      const seed = graph.nodes.find((n) => n.depth === 0) || graph.nodes[0];
      seedId = seed.id;
      status(`${fmt.format(graph.nodes.length)} artigos retomados do banco.`);
      window.Panel.clear("Clique num nó para ver autores, resumo e links.");
    }
  })();
})();
