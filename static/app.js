(() => {
  const $ = (sel) => document.querySelector(sel);

  const form = $("#form-upload");
  const inputBoletos = $("#boletos");
  const contagemBoletos = $("#contagem-boletos");
  const btnProcessar = $("#btn-processar");
  const btnBaixar = $("#btn-baixar");
  const btnVoltar = $("#btn-voltar");
  const btnTentarNovo = $("#btn-tentar-novo");
  const tbody = document.querySelector("#tabela-resultado tbody");

  const modal = $("#modal-preview");
  const modalIframe = $("#modal-iframe");
  const modalTitulo = $("#modal-titulo");
  const modalFechar = $("#modal-fechar");

  let sessionAtual = null;

  function mostrar(id) {
    ["#passo-upload", "#passo-processando", "#passo-revisao", "#passo-erro"]
      .forEach((s) => $(s).classList.add("hidden"));
    $(id).classList.remove("hidden");
  }

  inputBoletos.addEventListener("change", () => {
    const n = inputBoletos.files.length;
    contagemBoletos.textContent = n
      ? `${n} arquivo(s) selecionado(s)`
      : "";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const comp = $("#comprovantes").files[0];
    const boletos = Array.from(inputBoletos.files);
    if (!comp || !boletos.length) return;

    const fd = new FormData();
    fd.append("comprovantes", comp);
    boletos.forEach((f) => fd.append("boletos", f, f.name));

    mostrar("#passo-processando");
    btnProcessar.disabled = true;

    try {
      const resp = await fetch("/processar", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.erro || "Erro no servidor");
      }
      sessionAtual = data.session_id;
      renderizarResultado(data);
      mostrar("#passo-revisao");
    } catch (err) {
      $("#msg-erro").textContent = err.message;
      mostrar("#passo-erro");
    } finally {
      btnProcessar.disabled = false;
    }
  });

  function renderizarResultado(data) {
    // Contagem por metodo
    const porMetodo = { linha_digitavel: 0, valor_beneficiario: 0, valor: 0 };
    data.boletos.forEach((b) => {
      if (b.casado && b.casamento_metodo) porMetodo[b.casamento_metodo] = (porMetodo[b.casamento_metodo] || 0) + 1;
    });

    // Resumo
    const resumo = $("#resumo");
    const semMatch = data.total_boletos - data.casados;
    resumo.innerHTML = `
      <span class="pill">Boletos: <strong>${data.total_boletos}</strong></span>
      <span class="pill">Comprovantes: <strong>${data.total_comprovantes}</strong></span>
      <span class="pill ok">Casados: <strong>${data.casados}</strong></span>
      <span class="pill ${semMatch ? "warn" : ""}">Sem comprovante: <strong>${semMatch}</strong></span>
      ${porMetodo.linha_digitavel ? `<span class="pill metodo-1">Linha digitavel: <strong>${porMetodo.linha_digitavel}</strong></span>` : ""}
      ${porMetodo.valor_beneficiario ? `<span class="pill metodo-2">Valor + nome: <strong>${porMetodo.valor_beneficiario}</strong></span>` : ""}
      ${porMetodo.valor ? `<span class="pill metodo-3">So valor: <strong>${porMetodo.valor}</strong></span>` : ""}
    `;

    // Tabela
    tbody.innerHTML = "";
    data.boletos.forEach((b, i) => {
      const tr = document.createElement("tr");
      const statusTxt = b.casado
        ? `<span class="status-ok">CASADO</span>`
        : `<span class="status-warn">SEM COMPROVANTE</span>`;
      const metodoTxt = b.casado ? metodoBadge(b.casamento_metodo) : "-";
      const urlPreview = `/preview/${sessionAtual}/${encodeURI(b.pdf_relativo)}`;
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td>${escapeHtml(b.nome_arquivo)}</td>
        <td>${statusTxt}${b.erro ? `<br><small class="hint">${escapeHtml(b.erro)}</small>` : ""}</td>
        <td>${metodoTxt}</td>
        <td>${b.valor_boleto || "-"}</td>
        <td>${b.vencimento || "-"}</td>
        <td>${b.comprovante_pagina ?? "-"}</td>
        <td><button class="btn-preview" data-url="${urlPreview}" data-titulo="${escapeHtml(b.nome_arquivo)}">Preview</button></td>
      `;
      tbody.appendChild(tr);
    });

    // Orfaos
    const orfaosEl = $("#orfaos");
    if (data.comprovantes_orfaos && data.comprovantes_orfaos.length) {
      orfaosEl.textContent =
        `Comprovantes sem boleto correspondente — paginas: ${data.comprovantes_orfaos.join(", ")}`;
    } else {
      orfaosEl.textContent = "";
    }

    // Wire preview buttons
    tbody.querySelectorAll(".btn-preview").forEach((btn) => {
      btn.addEventListener("click", () => abrirPreview(btn.dataset.url, btn.dataset.titulo));
    });
  }

  function abrirPreview(url, titulo) {
    modalTitulo.textContent = titulo;
    modalIframe.src = url;
    modal.classList.remove("hidden");
  }

  function fecharModal() {
    modal.classList.add("hidden");
    modalIframe.src = "about:blank";
  }

  modalFechar.addEventListener("click", fecharModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) fecharModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) fecharModal();
  });

  btnBaixar.addEventListener("click", () => {
    if (!sessionAtual) return;
    window.location.href = `/download/${sessionAtual}`;
  });

  btnVoltar.addEventListener("click", () => {
    sessionAtual = null;
    form.reset();
    contagemBoletos.textContent = "";
    mostrar("#passo-upload");
  });

  btnTentarNovo.addEventListener("click", () => {
    mostrar("#passo-upload");
  });

  function metodoBadge(metodo) {
    const mapa = {
      linha_digitavel: { label: "linha digitavel", cls: "metodo-1" },
      valor_beneficiario: { label: "valor + nome", cls: "metodo-2" },
      valor: { label: "so valor", cls: "metodo-3" },
    };
    const m = mapa[metodo];
    if (!m) return "-";
    return `<span class="badge ${m.cls}">${m.label}</span>`;
  }

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }
})();
