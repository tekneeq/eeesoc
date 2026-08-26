(() => {
  const state = {
    matches: [],
    selectedId: null,
    minute: 53,
    meta: null,
    live: null,
    // null = all leagues; Set of slugs when filtering
    liveFilter: null,
    liveTimer: null,
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
    if (name === "live") refreshLive();
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
      btn.className =
        "match-row" +
        (m.is_preset ? " preset-row" : "") +
        (m.match_id === state.selectedId ? " selected" : "");
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

  function renderChiclets() {
    const row = $("#liveChiclets");
    row.innerHTML = "";
    if (!state.live) return;

    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "chiclet" + (state.liveFilter == null ? " on" : "");
    const liveTotal = state.live.live_total ?? state.live.total ?? 0;
    allBtn.innerHTML = `<span class="chiclet-label">ALL</span><span class="chiclet-count">${liveTotal}</span>`;
    allBtn.addEventListener("click", () => {
      state.liveFilter = null;
      renderChiclets();
      renderLiveBoard();
    });
    row.appendChild(allBtn);

    for (const c of state.live.chiclets || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      const active = state.liveFilter instanceof Set && state.liveFilter.has(c.slug);
      btn.className = "chiclet" + (active ? " on" : "") + (!c.live_count ? " dim" : "");
      btn.disabled = !c.live_count && !active;
      btn.innerHTML = `<span class="chiclet-label">${c.label}</span><span class="chiclet-count">${c.live_count}</span>`;
      btn.addEventListener("click", () => {
        if (!c.live_count) return;
        if (state.liveFilter instanceof Set && state.liveFilter.has(c.slug) && state.liveFilter.size === 1) {
          state.liveFilter = null;
        } else {
          state.liveFilter = new Set([c.slug]);
        }
        renderChiclets();
        renderLiveBoard();
      });
      row.appendChild(btn);
    }
  }

  function renderLiveBoard() {
    const board = $("#liveBoard");
    board.innerHTML = "";
    if (!state.live) {
      board.innerHTML = `<p class="lede">Fetching live board…</p>`;
      return;
    }

    let leagues = state.live.leagues || [];
    if (state.liveFilter instanceof Set) {
      leagues = leagues.filter((g) => state.liveFilter.has(g.slug));
    }

    if (!leagues.length) {
      board.innerHTML = `<p class="lede empty-live">No live matches right now. Chiclets will light up when kickoff hits.</p>`;
      return;
    }

    for (const group of leagues) {
      const section = document.createElement("section");
      section.className = "league-block";
      section.innerHTML = `
        <header class="league-head">
          <span class="league-chiclet-tag">${group.chiclet}</span>
          <h2>${group.name}</h2>
          <span class="league-n">${(group.matches || []).length} live</span>
        </header>
      `;
      const list = document.createElement("div");
      list.className = "live-list";
      for (const m of group.matches || []) {
        const row = document.createElement("div");
        row.className = "live-row";
        row.innerHTML = `
          <span class="live-clock"><span class="live-dot"></span>${escapeHtml(m.clock || m.detail || "LIVE")}</span>
          <span class="live-home">${escapeHtml(m.home)}</span>
          <span class="live-score">${m.home_score}&nbsp;–&nbsp;${m.away_score}</span>
          <span class="live-away">${escapeHtml(m.away)}</span>
        `;
        list.appendChild(row);
      }
      section.appendChild(list);
      board.appendChild(section);
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function refreshLive() {
    try {
      const data = await (await fetch("/api/live?live_only=1")).json();
      state.live = data;
      const stamp = new Date();
      $("#liveStamp").textContent = `${data.live_total || 0} live · updated ${stamp.toLocaleTimeString()}`;
      // Drop filter if selected league went quiet
      if (state.liveFilter instanceof Set) {
        const alive = new Set(
          (data.chiclets || []).filter((c) => c.live_count).map((c) => c.slug)
        );
        for (const slug of [...state.liveFilter]) {
          if (!alive.has(slug)) state.liveFilter.delete(slug);
        }
        if (!state.liveFilter.size) state.liveFilter = null;
      }
      renderChiclets();
      renderLiveBoard();
    } catch (err) {
      $("#liveStamp").textContent = "live feed error";
      $("#liveBoard").innerHTML = `<p class="lede">Could not load live board.</p>`;
    }
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

    await refreshLive();
    state.liveTimer = setInterval(refreshLive, 25000);
  }

  boot();
})();
