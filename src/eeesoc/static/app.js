(() => {
  const state = {
    matches: [],
    selectedId: null,
    minute: 53,
    meta: null,
    live: null,
    liveFilter: null, // null = all; Set of league slugs
    selectedLive: null, // match dict
    liveTimer: null,
    trackTimer: null,
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

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function shortName(name) {
    const s = String(name || "");
    if (s.length <= 14) return s;
    const parts = s.split(/\s+/);
    if (parts.length === 1) return s.slice(0, 12) + "…";
    return parts.map((p, i) => (i === parts.length - 1 ? p : p[0] + ".")).join(" ");
  }

  function flatLiveMatches() {
    if (!state.live) return [];
    let leagues = state.live.leagues || [];
    if (state.liveFilter instanceof Set) {
      leagues = leagues.filter((g) => state.liveFilter.has(g.slug));
    }
    const rows = [];
    for (const g of leagues) {
      for (const m of g.matches || []) {
        rows.push({ ...m, league_name: g.name, league_chiclet: g.chiclet, league_slug: g.slug });
      }
    }
    return rows;
  }

  function renderLeagueChiclets() {
    const row = $("#leagueChiclets");
    row.innerHTML = "";
    if (!state.live) return;

    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "chiclet" + (state.liveFilter == null ? " on" : "");
    allBtn.innerHTML = `<span class="chiclet-label">ALL</span><span class="chiclet-count">${state.live.live_total || 0}</span>`;
    allBtn.addEventListener("click", () => {
      state.liveFilter = null;
      renderLeagueChiclets();
      renderMatchChiclets();
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
        renderLeagueChiclets();
        renderMatchChiclets();
      });
      row.appendChild(btn);
    }
  }

  function renderMatchChiclets() {
    const grid = $("#matchChiclets");
    grid.innerHTML = "";
    const rows = flatLiveMatches();
    if (!rows.length) {
      grid.innerHTML = `<p class="lede empty-live">No live matches right now — chiclets light up at kickoff.</p>`;
      if (!state.selectedLive) $("#pitchPanel").hidden = true;
      return;
    }

    // Group visually by league with a tiny label, but matches themselves are chiclets
    const byLeague = new Map();
    for (const m of rows) {
      const key = m.league_slug;
      if (!byLeague.has(key)) byLeague.set(key, { name: m.league_name, chiclet: m.league_chiclet, matches: [] });
      byLeague.get(key).matches.push(m);
    }

    for (const [, group] of byLeague) {
      const block = document.createElement("div");
      block.className = "match-chiclet-league";
      block.innerHTML = `<div class="match-chiclet-league-label"><span class="league-chiclet-tag">${escapeHtml(group.chiclet)}</span> ${escapeHtml(group.name)}</div>`;
      const wrap = document.createElement("div");
      wrap.className = "match-chiclet-row";
      for (const m of group.matches) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className =
          "match-chiclet" +
          (state.selectedLive && state.selectedLive.event_id === m.event_id ? " on" : "");
        btn.setAttribute("role", "listitem");
        btn.innerHTML = `
          <span class="mc-clock"><span class="live-dot"></span>${escapeHtml(m.clock || "LIVE")}</span>
          <span class="mc-teams">
            <span class="mc-home">${escapeHtml(shortName(m.home))}</span>
            <span class="mc-score">${m.home_score}–${m.away_score}</span>
            <span class="mc-away">${escapeHtml(shortName(m.away))}</span>
          </span>
          <span class="mc-league">${escapeHtml(m.league_chiclet)}</span>
        `;
        btn.addEventListener("click", () => selectLiveMatch(m));
        wrap.appendChild(btn);
      }
      block.appendChild(wrap);
      grid.appendChild(block);
    }
  }

  async function selectLiveMatch(m) {
    state.selectedLive = m;
    renderMatchChiclets();
    $("#pitchPanel").hidden = false;
    $("#pitchTitle").textContent = `${m.home} ${m.home_score}–${m.away_score} ${m.away} · ${m.clock || "LIVE"}`;
    await refreshTrack();
    if (state.trackTimer) clearInterval(state.trackTimer);
    state.trackTimer = setInterval(refreshTrack, 8000);
  }

  function pitchXY(x, y) {
    // ESPN: X 0–100 along length, Y 0–100 across width.
    // SVG viewBox 1050×680 with 25px margin → pitch 1000×630
    const px = 25 + (Number(x) / 100) * 1000;
    const py = 25 + (Number(y) / 100) * 630;
    return [px, py];
  }

  function drawPitchBase(svg) {
    svg.innerHTML = "";
    const ns = "http://www.w3.org/2000/svg";
    const add = (tag, attrs) => {
      const el = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      svg.appendChild(el);
      return el;
    };

    add("rect", { x: 0, y: 0, width: 1050, height: 680, class: "pitch-bg" });
    add("rect", { x: 25, y: 25, width: 1000, height: 630, class: "pitch-field" });
    // halfway
    add("line", { x1: 525, y1: 25, x2: 525, y2: 655, class: "pitch-line" });
    add("circle", { cx: 525, cy: 340, r: 91.5, class: "pitch-line" });
    add("circle", { cx: 525, cy: 340, r: 3, class: "pitch-spot" });
    // boxes
    add("rect", { x: 25, y: 165.5, width: 165, height: 349, class: "pitch-line" });
    add("rect", { x: 25, y: 256.5, width: 55, height: 167, class: "pitch-line" });
    add("rect", { x: 860, y: 165.5, width: 165, height: 349, class: "pitch-line" });
    add("rect", { x: 970, y: 256.5, width: 55, height: 167, class: "pitch-line" });
    // goals
    add("rect", { x: 10, y: 290, width: 15, height: 100, class: "pitch-goal" });
    add("rect", { x: 1025, y: 290, width: 15, height: 100, class: "pitch-goal" });
    // arcs (approx)
    add("path", {
      d: "M190 278 A60 60 0 0 1 190 402",
      class: "pitch-line",
      fill: "none",
    });
    add("path", {
      d: "M860 278 A60 60 0 0 0 860 402",
      class: "pitch-line",
      fill: "none",
    });
  }

  function renderPitch(track) {
    const svg = $("#pitchSvg");
    drawPitchBase(svg);
    const ns = "http://www.w3.org/2000/svg";
    const add = (tag, attrs) => {
      const el = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      svg.appendChild(el);
      return el;
    };

    for (const p of track.passes || []) {
      if (p.x == null || p.y == null || p.x2 == null || p.y2 == null) continue;
      const [x1, y1] = pitchXY(p.x, p.y);
      const [x2, y2] = pitchXY(p.x2, p.y2);
      add("line", {
        x1,
        y1,
        x2,
        y2,
        class: "pass-line",
      });
      add("circle", { cx: x1, cy: y1, r: 3.5, class: "pass-dot" });
    }

    for (const s of track.shots || []) {
      if (s.x == null || s.y == null) continue;
      const [x, y] = pitchXY(s.x, s.y);
      const kind =
        s.type === "goal" || s.type === "penalty-goal"
          ? "shot-goal"
          : s.type === "shot-on-target"
            ? "shot-on"
            : "shot-off";
      add("circle", { cx: x, cy: y, r: kind === "shot-goal" ? 8 : 6, class: kind });
    }

    if (track.ball && track.ball.x != null && track.ball.y != null) {
      const [bx, by] = pitchXY(track.ball.x, track.ball.y);
      add("circle", { cx: bx, cy: by, r: 14, class: "ball-halo" });
      add("circle", { cx: bx, cy: by, r: 7, class: "ball" });
      const label = add("text", {
        x: bx + 14,
        y: by - 12,
        class: "ball-label",
      });
      label.textContent = `${track.ball.clock || ""} ${track.ball.type || ""}`.trim();
    }

    const c = track.counts || {};
    $("#pitchStats").innerHTML = `
      <span class="stat-chiclet">Passes <b>${c.passes || 0}</b></span>
      <span class="stat-chiclet">Shots <b>${c.shots || 0}</b></span>
      <span class="stat-chiclet">On target <b>${c.shots_on || 0}</b></span>
      <span class="stat-chiclet">Goals <b>${c.goals || 0}</b></span>
    `;

    const feed = $("#pitchFeed");
    feed.innerHTML = "";
    for (const ev of (track.recent || []).slice(0, 18)) {
      const row = document.createElement("div");
      row.className = "feed-row";
      row.innerHTML = `<span class="feed-clock">${escapeHtml(ev.clock || "")}</span>
        <span class="feed-type">${escapeHtml(ev.type || "")}</span>
        <span class="feed-text">${escapeHtml(ev.text || "")}</span>`;
      feed.appendChild(row);
    }

    if (track.ball) {
      $("#pitchTitle").textContent =
        `${track.home} ${track.home_score}–${track.away_score} ${track.away} · ${track.clock || "LIVE"}` +
        ` · ball @ ${Math.round(track.ball.x)},${Math.round(track.ball.y)} (${track.ball.type})`;
    }
  }

  async function refreshTrack() {
    const m = state.selectedLive;
    if (!m) return;
    const qs = new URLSearchParams({
      league: m.league_slug,
      event_id: m.event_id,
      home: m.home,
      away: m.away,
      hs: String(m.home_score),
      as: String(m.away_score),
      clock: m.clock || "",
      chiclet: m.league_chiclet || "",
    });
    try {
      const track = await (await fetch(`/api/live/track?${qs}`)).json();
      renderPitch(track);
    } catch (err) {
      $("#pitchFeed").innerHTML = `<p class="lede">Could not load pitch tracking.</p>`;
    }
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

  async function refreshLive() {
    try {
      const data = await (await fetch("/api/live?live_only=1")).json();
      state.live = data;
      $("#liveStamp").textContent = `${data.live_total || 0} live · updated ${new Date().toLocaleTimeString()}`;
      if (state.liveFilter instanceof Set) {
        const alive = new Set((data.chiclets || []).filter((c) => c.live_count).map((c) => c.slug));
        for (const slug of [...state.liveFilter]) {
          if (!alive.has(slug)) state.liveFilter.delete(slug);
        }
        if (!state.liveFilter.size) state.liveFilter = null;
      }
      // Keep selection in sync with refreshed scores/clock
      if (state.selectedLive) {
        const all = [];
        for (const g of data.leagues || []) {
          for (const m of g.matches || []) {
            all.push({ ...m, league_name: g.name, league_chiclet: g.chiclet, league_slug: g.slug });
          }
        }
        const updated = all.find((m) => m.event_id === state.selectedLive.event_id);
        if (updated) state.selectedLive = updated;
        else {
          state.selectedLive = null;
          $("#pitchPanel").hidden = true;
          if (state.trackTimer) clearInterval(state.trackTimer);
        }
      }
      renderLeagueChiclets();
      renderMatchChiclets();
    } catch (err) {
      $("#liveStamp").textContent = "live feed error";
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
