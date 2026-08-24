(() => {
  const state = {
    matches: [],
    selectedId: null,
    minute: 53,
    meta: null,
  };

  const $ = (sel) => document.querySelector(sel);

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      const on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      const on = panel.id === `panel-${name}`;
      panel.classList.toggle("active", on);
      panel.hidden = !on;
    });
  }

  function renderMatches() {
    const q = ($("#matchFilter").value || "").trim().toLowerCase();
    const list = $("#matchList");
    list.innerHTML = "";
    const rows = state.matches.filter((m) => {
      if (!q) return true;
      return `${m.home} ${m.away} ${m.date}`.toLowerCase().includes(q);
    });
    for (const m of rows) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "match-row" + (m.is_preset ? " preset-row" : "") + (m.match_id === state.selectedId ? " selected" : "");
      btn.innerHTML = `
        <span class="date">${m.date}</span>
        <span class="teams">${m.home} vs ${m.away}</span>
        <span class="meta">FT ${m.ft} · ${m.shots}</span>
      `;
      btn.addEventListener("click", () => selectMatch(m.match_id, true));
      list.appendChild(btn);
    }
  }

  async function selectMatch(matchId, goSimilar) {
    state.selectedId = matchId;
    state.minute = Number($("#cutMinute").value) || 53;
    renderMatches();
    const snapRes = await fetch(
      `/api/snapshot?match_id=${encodeURIComponent(matchId)}&minute=${state.minute}`
    );
    const snap = await snapRes.json();
    const simRes = await fetch(
      `/api/similar?match_id=${encodeURIComponent(matchId)}&minute=${state.minute}`
    );
    const sim = await simRes.json();
    $("#freezeTitle").textContent = `${snap.home} vs ${snap.away}`;
    $("#freezeLabel").textContent = `${state.minute}' · ${snap.label}`;
    $("#freezeMeta").textContent = `Frozen snapshot · score ${snap.snapshot.home_goals}-${snap.snapshot.away_goals}`;
    renderSimilar(sim.hits || []);
    if (goSimilar) switchTab("similar");
  }

  function renderSimilar(hits) {
    const list = $("#similarList");
    list.innerHTML = "";
    if (!hits.length) {
      list.innerHTML = `<p class="lede">No lookalikes yet — warm a season or pick another cut.</p>`;
      return;
    }
    for (const h of hits) {
      const row = document.createElement("div");
      row.className = "similar-row";
      row.innerHTML = `
        <span class="score">${h.score.toFixed(3)}</span>
        <span class="date">${h.date}</span>
        <span class="teams">${h.home} vs ${h.away}</span>
        <span class="meta">${h.label} · FT ${h.ft}</span>
      `;
      list.appendChild(row);
    }
  }

  async function loadEvertonPreset() {
    const id = state.meta?.everton_preset_id;
    if (!id) {
      alert("Everton preset not in cache — run with --warm EPL:2025");
      return;
    }
    $("#cutMinute").value = "53";
    await selectMatch(id, true);
  }

  async function boot() {
    const meta = await (await fetch("/api/meta")).json();
    state.meta = meta;
    $("#seasonLabel").textContent = `${meta.season} · ${meta.match_count} matches · ${meta.history_count} history`;
    const data = await (await fetch("/api/matches")).json();
    state.matches = data.matches || [];
    renderMatches();

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    });
    $("#matchFilter").addEventListener("input", renderMatches);
    $("#cutMinute").addEventListener("change", () => {
      if (state.selectedId) selectMatch(state.selectedId, false);
    });
    $("#evertonPreset").addEventListener("click", loadEvertonPreset);
  }

  boot();
})();
