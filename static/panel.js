/* Painel de detalhes do artigo: autores, links, resumo, triagem e expansao.
   Exposto como window.Panel; graph.js chama Panel.show(id). */

(function () {
  "use strict";

  const el = document.getElementById("panel");

  const MARKS = [
    { status: "unread", label: "não lido" },
    { status: "interested", label: "interessa" },
    { status: "read", label: "lido" },
    { status: "discarded", label: "descartado" },
  ];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  const fmt = new Intl.NumberFormat("pt-BR");

  let current = null;
  let noteTimer = null;

  function render(p) {
    const authors = p.authors.length
      ? p.authors
          .map(
            (a) =>
              `<li>${esc(a.name)}${
                a.affiliation ? ` <span class="aff">· ${esc(a.affiliation)}</span>` : ""
              }</li>`
          )
          .join("")
      : `<li class="aff">autoria não informada</li>`;

    const links = [
      p.doi_url && `<a href="${esc(p.doi_url)}" target="_blank" rel="noopener">DOI</a>`,
      p.oa_url && `<a href="${esc(p.oa_url)}" target="_blank" rel="noopener">PDF aberto</a>`,
      p.landing_url &&
        p.landing_url !== p.doi_url &&
        `<a href="${esc(p.landing_url)}" target="_blank" rel="noopener">página</a>`,
      `<a href="${esc(p.openalex_url)}" target="_blank" rel="noopener">OpenAlex</a>`,
    ]
      .filter(Boolean)
      .join("");

    const coverage =
      p.total_citers == null
        ? "nunca expandido — nada buscado ainda"
        : `mostrando ${fmt.format(p.shown_citers || 0)} de ${fmt.format(
            p.total_citers
          )} citantes`;

    el.innerHTML = `
      <h2>${esc(p.title)}</h2>
      <p class="meta">
        ${p.year ? `<b>${p.year}</b>` : "ano desconhecido"}
        ${p.venue ? ` · ${esc(p.venue)}` : ""}
        · <b>${fmt.format(p.cited_by_count)}</b> citações
        · nível ${p.depth}
      </p>

      ${
        p.topics.length
          ? `<ul class="chips">${p.topics.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`
          : ""
      }

      <h3>Links</h3>
      <div class="links">${links}</div>

      <h3>Autores</h3>
      <ul class="authors">${authors}</ul>

      <h3>Resumo</h3>
      <p class="abstract${p.abstract_available ? "" : " missing"}">${
        p.abstract_available
          ? esc(p.abstract)
          : "Resumo não disponível no OpenAlex nem no Semantic Scholar."
      }</p>

      <h3>Triagem</h3>
      <div class="marks">${MARKS.map(
        (m) =>
          `<button type="button" data-status="${m.status}" aria-pressed="${
            p.mark === m.status
          }">${m.label}</button>`
      ).join("")}</div>
      <textarea class="note" placeholder="anotação sobre este artigo…">${esc(
        p.note
      )}</textarea>

      <h3>Escavar</h3>
      <div class="expand-box">
        <button type="button" id="expand-btn">Expandir: quem citou este artigo</button>
        <span class="coverage">${coverage}</span>
      </div>
    `;

    el.querySelectorAll(".marks button").forEach((b) => {
      b.addEventListener("click", () => setMark(p.id, b.dataset.status));
    });

    const note = el.querySelector(".note");
    note.addEventListener("input", () => {
      clearTimeout(noteTimer);
      noteTimer = setTimeout(
        () => setMark(p.id, currentMark(), note.value, { silent: true }),
        600
      );
    });

    el.querySelector("#expand-btn").addEventListener("click", () => expand(p.id));
  }

  function currentMark() {
    const on = el.querySelector('.marks button[aria-pressed="true"]');
    return on ? on.dataset.status : "unread";
  }

  async function setMark(id, status, note, opts) {
    const body = {
      status,
      note: note != null ? note : (el.querySelector(".note") || {}).value || "",
    };
    const r = await fetch(`/api/paper/${encodeURIComponent(id)}/mark`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      window.Graph.status("não consegui salvar a marcação", "err");
      return;
    }
    el.querySelectorAll(".marks button").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.status === status));
    });
    window.Graph.applyMark(id, status);
    if (current) current.mark = status;
    if (!(opts && opts.silent)) window.Graph.status("marcação salva");
  }

  async function expand(id) {
    const btn = el.querySelector("#expand-btn");
    btn.disabled = true;
    btn.textContent = "buscando citantes…";
    try {
      const params = window.Graph.params();
      const r = await fetch("/api/expand", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paper_id: id,
          top_n: params.top_n,
          min_year: params.min_year,
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "falha ao expandir");

      await window.Graph.reload();
      window.Graph.status(
        data.total_citers === 0
          ? "ninguém citou este artigo (ainda)"
          : `+${data.new_nodes} artigos novos — ${data.shown} de ${fmt.format(
              data.total_citers
            )} citantes`
      );
      show(id);
    } catch (err) {
      window.Graph.status(err.message, "err");
      btn.disabled = false;
      btn.textContent = "Expandir: quem citou este artigo";
    }
  }

  async function show(id) {
    el.innerHTML = `<p class="empty">carregando…</p>`;
    try {
      const r = await fetch(`/api/paper/${encodeURIComponent(id)}`);
      const p = await r.json();
      if (!r.ok) throw new Error(p.detail || "artigo não encontrado");
      p.shown_citers = window.Graph.citerCount(id);
      current = p;
      render(p);
    } catch (err) {
      el.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    }
  }

  function clear(message) {
    current = null;
    el.innerHTML = `<p class="empty">${esc(message)}</p>`;
  }

  window.Panel = { show, clear };
})();
