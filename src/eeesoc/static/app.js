(() => {
  const state = {
    matches: [],
    selectedId: null,
    minute: 53,
    meta: null,
    live: null,
    liveFilter: null, // null = all; Set of league slugs
    similarFilter: null,
    selectedLive: null, // Live tab pitch selection
    selectedSimilarLive: null, // Similar tab live chiclet
    liveTimer: null,
    trackTimer: null,
    similarTimer: null,
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
    if (name === "live" || name === "similar") refreshLive();
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

  function flatLiveMatches(filter) {
    if (!state.live) return [];
    let leagues = state.live.leagues || [];
    if (filter instanceof Set) {
      leagues = leagues.filter((g) => filter.has(g.slug));
    }
    const rows = [];
    for (const g of leagues) {
      for (const m of g.matches || []) {
        rows.push({ ...m, league_name: g.name, league_chiclet: g.chiclet, league_slug: g.slug });
      }
    }
    return rows;
  }

  function renderLeagueChiclets(rowEl, filterKey, onChange) {
    const row = $(rowEl);
    row.innerHTML = "";
    if (!state.live) return;

    const filter = state[filterKey];
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "chiclet" + (filter == null ? " on" : "");
    allBtn.innerHTML = `<span class="chiclet-label">ALL</span><span class="chiclet-count">${state.live.live_total || 0}</span>`;
    allBtn.addEventListener("click", () => {
      state[filterKey] = null;
      onChange();
    });
    row.appendChild(allBtn);

    for (const c of state.live.chiclets || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      const active = filter instanceof Set && filter.has(c.slug);
      btn.className = "chiclet" + (active ? " on" : "") + (!c.live_count ? " dim" : "");
      btn.disabled = !c.live_count && !active;
      btn.innerHTML = `<span class="chiclet-label">${c.label}</span><span class="chiclet-count">${c.live_count}</span>`;
      btn.addEventListener("click", () => {
        if (!c.live_count) return;
        if (state[filterKey] instanceof Set && state[filterKey].has(c.slug) && state[filterKey].size === 1) {
          state[filterKey] = null;
        } else {
          state[filterKey] = new Set([c.slug]);
        }
        onChange();
      });
      row.appendChild(btn);
    }
  }

  function renderMatchChiclets(gridEl, filter, selected, onSelect) {
    const grid = $(gridEl);
    grid.innerHTML = "";
    const rows = flatLiveMatches(filter);
    if (!rows.length) {
      grid.innerHTML = `<p class="lede empty-live">No live matches right now — chiclets light up at kickoff.</p>`;
      return;
    }

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
          "match-chiclet" + (selected && selected.event_id === m.event_id ? " on" : "");
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
        btn.addEventListener("click", () => onSelect(m));
        wrap.appendChild(btn);
      }
      block.appendChild(wrap);
      grid.appendChild(block);
    }
  }

  function renderLiveTabChiclets() {
    renderLeagueChiclets("#leagueChiclets", "liveFilter", () => {
      renderLiveTabChiclets();
    });
    renderMatchChiclets("#matchChiclets", state.liveFilter, state.selectedLive, selectLiveMatch);
  }

  function renderSimilarTabChiclets() {
    renderLeagueChiclets("#similarLeagueChiclets", "similarFilter", () => {
      renderSimilarTabChiclets();
    });
    renderMatchChiclets(
      "#similarMatchChiclets",
      state.similarFilter,
      state.selectedSimilarLive,
      selectSimilarLive
    );
  }

  async function selectLiveMatch(m) {
    state.selectedLive = m;
    renderLiveTabChiclets();
    $("#pitchPanel").hidden = false;
    $("#pitchTitle").textContent = `${m.home} ${m.home_score}–${m.away_score} ${m.away} · ${m.clock || "LIVE"}`;
    await refreshTrack();
    if (state.trackTimer) clearInterval(state.trackTimer);
    state.trackTimer = setInterval(refreshTrack, 8000);
  }

  function liveQuery(m) {
    return new URLSearchParams({
      league: m.league_slug,
      event_id: m.event_id,
      home: m.home,
      away: m.away,
      hs: String(m.home_score),
      as: String(m.away_score),
      clock: m.clock || "",
      chiclet: m.league_chiclet || "",
      home_id: m.home_id || "",
      away_id: m.away_id || "",
    });
  }

  async function selectSimilarLive(m) {
    state.selectedSimilarLive = m;
    state.selectedId = null;
    renderMatches();
    renderSimilarTabChiclets();
    await refreshSimilarLive();
    if (state.similarTimer) clearInterval(state.similarTimer);
    state.similarTimer = setInterval(refreshSimilarLive, 20000);
  }

  async function refreshSimilarLive() {
    const m = state.selectedSimilarLive;
    if (!m) return;
    try {
      const data = await (await fetch(`/api/live/similar?${liveQuery(m)}`)).json();
      renderLiveSimilar(data);
    } catch (err) {
      $("#freezeLabel").textContent = "Could not load live similar situation.";
      $("#goalContext").hidden = true;
      $("#scorelineEval").hidden = true;
      $("#concedeSummary").hidden = true;
    }
  }

  function pct(v) {
    if (v == null) return "—";
    return `${Math.round(Number(v) * 100)}%`;
  }

  function renderGoalContext(situation) {
    const box = $("#goalContext");
    const goals = situation.goals || [];
    if (!goals.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    const path = ["0-0"].concat(
      goals.map((g) => `${g.home_goals}-${g.away_goals}`)
    );
    box.innerHTML = `
      <div class="score-path">
        <span class="score-path-label">Live path</span>
        <span class="score-path-steps">${path.map((s, i) => {
          const tip = i === 0 ? "KO" : `${goals[i - 1].minute}' ${escapeHtml(goals[i - 1].team_name || "")}`;
          return `<span class="score-step${i === path.length - 1 ? " now" : ""}" title="${tip}">${escapeHtml(s)}</span>`;
        }).join('<span class="score-arrow">→</span>')}</span>
      </div>
    `;
  }

  function distChips(rows, limit = 6) {
    return (rows || [])
      .slice(0, limit)
      .map(
        (r) =>
          `<span class="team-stat${r.is_live_branch ? " focal live-branch" : ""}">${escapeHtml(r.score)} <b>${pct(r.pct)}</b> <span class="dim">n=${r.count}</span></span>`
      )
      .join("");
  }

  function renderHistoryBlock(title, evalData, tree) {
    if (!evalData) {
      return `<section class="sl-block"><div class="concede-title">${escapeHtml(title)}</div><p class="concede-lede">No mapped EPL history for this club.</p></section>`;
    }
    const n = evalData.count || 0;
    const treeHtml =
      tree && tree.count
        ? `<div class="concede-when">
            <div class="concede-when-label">Branch from ${escapeHtml(tree.from)} → now</div>
            <div class="concede-stats">${distChips(tree.branches, 8)}</div>
          </div>`
        : "";
    const peers = (evalData.peers || [])
      .slice(0, 6)
      .map((p) => {
        const after = (p.path_after || [])
          .map((x) => `${x.minute}'→${x.score}`)
          .join(" · ");
        return `<div class="peer-row peer-row-goals">
          <span class="min">${p.visit_minute}'</span>
          <span class="date">${escapeHtml(p.date)}</span>
          <span class="teams">${escapeHtml(p.home)} vs ${escapeHtml(p.away)} <span class="ft">hit ${escapeHtml(p.scoreline)} · FT ${escapeHtml(p.ft)}</span></span>
          <span class="meta"><span class="meta-line">${after ? escapeHtml(after) : "ended here"}</span></span>
        </div>`;
      })
      .join("");

    return `<section class="sl-block">
      <div class="concede-title">${escapeHtml(title)} at ${escapeHtml(evalData.scoreline)}</div>
      <p class="concede-lede">
        <b style="color:var(--text)">${n}</b> games ever at this scoreline.
        Ended ${escapeHtml(evalData.scoreline)}: <b style="color:var(--accent)">${pct(evalData.pct_ended_same)}</b>
        · more goals: <b style="color:var(--accent)">${pct(evalData.pct_more_goals)}</b>
        · next for/against: <b>${pct(evalData.pct_next_for)}</b> / <b>${pct(evalData.pct_next_against)}</b>
      </p>
      <div class="concede-stats">
        <span class="score-dist-label">FT from here</span>
        ${distChips(evalData.ft_distribution)}
      </div>
      <div class="concede-stats" style="margin-top:0.45rem">
        <span class="score-dist-label">Next state</span>
        ${distChips(evalData.next_distribution)}
      </div>
      ${treeHtml}
      <div class="peer-list">${peers || `<p class="concede-lede">No peer rows.</p>`}</div>
    </section>`;
  }

  function renderScorelineEval(scorelines) {
    const el = $("#scorelineEval");
    if (!scorelines) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    const homeLabel = scorelines.home_fd
      ? `${scorelines.home} (${scorelines.home_fd})`
      : scorelines.home || "Home";
    const awayLabel = scorelines.away_fd
      ? `${scorelines.away} (${scorelines.away_fd})`
      : scorelines.away || "Away";
    const trees = scorelines.trees || {};
    const branchNote = scorelines.prev_scoreline
      ? `<p class="concede-lede">Followed branch <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.prev_scoreline)} → ${escapeHtml(scorelines.scoreline)}</b> — percentages below were available at the previous state.</p>`
      : `<p class="concede-lede">Current structure <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.scoreline)}</b> — club history first, then league.</p>`;

    el.innerHTML = `
      <div class="concede-title">Scoreline ${escapeHtml(scorelines.scoreline)}</div>
      ${branchNote}
      ${renderHistoryBlock(homeLabel + " history", scorelines.home_history, trees.home)}
      ${renderHistoryBlock(awayLabel + " history", scorelines.away_history, trees.away)}
      ${renderHistoryBlock("EPL league history", scorelines.league_history, trees.league)}
    `;
  }

  function renderLiveSimilar(data) {
    const sit = data.situation || {};
    const snap = sit.snapshot || data.query || {};
    $("#freezeTitle").textContent = `${sit.home || "?"} vs ${sit.away || "?"}`;
    $("#freezeLabel").textContent = `${sit.minute || snap.minute || "?"}′ · ${sit.home_score ?? snap.home_goals}-${sit.away_score ?? snap.away_goals}`;
    $("#freezeMeta").textContent =
      `Live ${sit.clock || ""} · ${snap.home_shots ?? 0}/${snap.home_sot ?? 0} vs ${snap.away_shots ?? 0}/${snap.away_sot ?? 0} shots/SOT`;
    renderGoalContext(sit);
    renderScorelineEval(data.scorelines);
    // Keep goal-minute peers available but collapsed away from primary UX
    const concede = $("#concedeSummary");
    concede.hidden = true;
    concede.innerHTML = "";
    renderSimilar(data.hits || []);
  }

  function pitchXY(x, y) {
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
    add("line", { x1: 525, y1: 25, x2: 525, y2: 655, class: "pitch-line" });
    add("circle", { cx: 525, cy: 340, r: 91.5, class: "pitch-line" });
    add("circle", { cx: 525, cy: 340, r: 3, class: "pitch-spot" });
    add("rect", { x: 25, y: 165.5, width: 165, height: 349, class: "pitch-line" });
    add("rect", { x: 25, y: 256.5, width: 55, height: 167, class: "pitch-line" });
    add("rect", { x: 860, y: 165.5, width: 165, height: 349, class: "pitch-line" });
    add("rect", { x: 970, y: 256.5, width: 55, height: 167, class: "pitch-line" });
    add("rect", { x: 10, y: 290, width: 15, height: 100, class: "pitch-goal" });
    add("rect", { x: 1025, y: 290, width: 15, height: 100, class: "pitch-goal" });
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
      add("line", { x1, y1, x2, y2, class: "pass-line" });
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
      const label = add("text", { x: bx + 14, y: by - 12, class: "ball-label" });
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
    try {
      const track = await (await fetch(`/api/live/track?${liveQuery(m)}`)).json();
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
    state.selectedSimilarLive = null;
    if (state.similarTimer) clearInterval(state.similarTimer);
    state.minute = Number($("#cutMinute").value) || 53;
    renderMatches();
    renderSimilarTabChiclets();
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
    $("#goalContext").hidden = true;
    $("#goalContext").innerHTML = "";
    $("#scorelineEval").hidden = true;
    $("#scorelineEval").innerHTML = "";
    $("#concedeSummary").hidden = true;
    $("#concedeSummary").innerHTML = "";
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
    const head = document.createElement("div");
    head.className = "concede-title";
    head.style.marginBottom = "0.5rem";
    head.textContent = "Snapshot lookalikes";
    list.appendChild(head);
    for (const h of hits) {
      const row = document.createElement("div");
      row.className = "similar-row";
      const s = h.snapshot || {};
      row.innerHTML = `
        <span class="score">${h.score.toFixed(3)}</span>
        <span class="date">${h.date}</span>
        <span class="teams">${h.home} vs ${h.away}</span>
        <span class="meta">${h.label} · FT ${h.ft}</span>
      `;
      // richer line under teams via title
      row.title = `${h.home} ${s.home_shots || 0}/${s.home_sot || 0} vs ${h.away} ${s.away_shots || 0}/${s.away_sot || 0}`;
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

  function syncSelectedLive(all, key, onGone) {
    const selected = state[key];
    if (!selected) return;
    const updated = all.find((m) => m.event_id === selected.event_id);
    if (updated) state[key] = updated;
    else {
      state[key] = null;
      onGone();
    }
  }

  async function refreshLive() {
    try {
      const data = await (await fetch("/api/live?live_only=1")).json();
      state.live = data;
      const stamp = `${data.live_total || 0} live · updated ${new Date().toLocaleTimeString()}`;
      $("#liveStamp").textContent = stamp;
      $("#similarLiveStamp").textContent = stamp;

      for (const filterKey of ["liveFilter", "similarFilter"]) {
        if (state[filterKey] instanceof Set) {
          const alive = new Set((data.chiclets || []).filter((c) => c.live_count).map((c) => c.slug));
          for (const slug of [...state[filterKey]]) {
            if (!alive.has(slug)) state[filterKey].delete(slug);
          }
          if (!state[filterKey].size) state[filterKey] = null;
        }
      }

      const all = flatLiveMatches(null);
      syncSelectedLive(all, "selectedLive", () => {
        $("#pitchPanel").hidden = true;
        if (state.trackTimer) clearInterval(state.trackTimer);
      });
      syncSelectedLive(all, "selectedSimilarLive", () => {
        if (state.similarTimer) clearInterval(state.similarTimer);
      });

      renderLiveTabChiclets();
      renderSimilarTabChiclets();

      if (state.selectedSimilarLive && !state.selectedId) {
        // keep live similar fresh when board refreshes
        refreshSimilarLive();
      }
    } catch (err) {
      $("#liveStamp").textContent = "live feed error";
      $("#similarLiveStamp").textContent = "live feed error";
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
