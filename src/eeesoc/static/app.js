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
    timelines: {},
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

  function renderMatchChiclets(gridEl, filter, selected, onSelect, opts = {}) {
    const grid = $(gridEl);
    const rows = flatLiveMatches(filter);
    const withTimeline = !!opts.withTimeline;
    const soft = !!opts.soft && withTimeline;

    if (soft && softPatchLiveChiclets(grid, rows, selected, onSelect, opts)) {
      return;
    }

    grid.innerHTML = "";
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
      wrap.className = "match-chiclet-row" + (withTimeline ? " match-chiclet-row-tl" : "");
      for (const m of group.matches) {
        const btn = buildMatchChicletButton(m, selected, onSelect, withTimeline);
        wrap.appendChild(btn);
        if (withTimeline) {
          loadMatchTimeline(m, btn.querySelector(".mc-timeline"), btn.querySelector(".mc-xg"), {
            preferCache: true,
          });
        }
      }
      block.appendChild(wrap);
      grid.appendChild(block);
    }
  }

  function buildMatchChicletButton(m, selected, onSelect, withTimeline) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "match-chiclet" +
      (withTimeline ? " match-chiclet-tl" : "") +
      (selected && selected.event_id === m.event_id ? " on" : "");
    btn.setAttribute("role", "listitem");
    btn.dataset.eventId = m.event_id;
    btn.__match = m;
    btn.__onSelect = onSelect;
    const cached = withTimeline ? state.timelines?.[m.event_id] : null;
    btn.innerHTML = `
      <span class="mc-clock"><span class="live-dot"></span><span class="mc-clock-text">${escapeHtml(m.clock || "LIVE")}</span></span>
      <span class="mc-teams">
        <span class="mc-home">${escapeHtml(shortName(m.home))}</span>
        <span class="mc-score">${m.home_score}–${m.away_score}</span>
        <span class="mc-away">${escapeHtml(shortName(m.away))}</span>
      </span>
      <span class="mc-league">${escapeHtml(m.league_chiclet)}</span>
      ${
        withTimeline
          ? `<div class="mc-charts">
              <span class="mc-timeline" data-tl-for="${escapeHtml(m.event_id)}" aria-label="Match event timeline">${
                cached ? timelineSvg(cached) : `<span class="mc-timeline-loading">timeline…</span>`
              }</span>
              <span class="mc-xg" data-xg-for="${escapeHtml(m.event_id)}" aria-label="Expected goals versus time">${
                cached ? xgSvg(cached) : `<span class="mc-timeline-loading">xG…</span>`
              }</span>
            </div>`
          : ""
      }
    `;
    btn.addEventListener("click", () => {
      if (btn.__onSelect) btn.__onSelect(btn.__match);
    });
    return btn;
  }

  function softPatchLiveChiclets(grid, rows, selected, onSelect, opts = {}) {
    if (!grid) return false;
    const existing = [...grid.querySelectorAll(".match-chiclet[data-event-id]")];
    if (!existing.length && !rows.length) return true;
    if (existing.length !== rows.length) return false;
    const byId = new Map(existing.map((el) => [el.dataset.eventId, el]));
    for (const m of rows) {
      if (!byId.has(String(m.event_id))) return false;
    }

    const refreshTimelines = opts.refreshTimelines !== false;

    for (const m of rows) {
      const btn = byId.get(String(m.event_id));
      btn.__match = m;
      btn.__onSelect = onSelect;
      btn.classList.toggle("on", !!(selected && selected.event_id === m.event_id));
      const clockText = btn.querySelector(".mc-clock-text");
      if (clockText) clockText.textContent = m.clock || "LIVE";
      else {
        const clock = btn.querySelector(".mc-clock");
        if (clock) {
          const dot = clock.querySelector(".live-dot");
          clock.textContent = "";
          if (dot) clock.appendChild(dot);
          const span = document.createElement("span");
          span.className = "mc-clock-text";
          span.textContent = m.clock || "LIVE";
          clock.appendChild(span);
        }
      }
      const score = btn.querySelector(".mc-score");
      if (score) score.textContent = `${m.home_score}–${m.away_score}`;
      const home = btn.querySelector(".mc-home");
      if (home) home.textContent = shortName(m.home);
      const away = btn.querySelector(".mc-away");
      if (away) away.textContent = shortName(m.away);
      if (refreshTimelines) {
        loadMatchTimeline(m, btn.querySelector(".mc-timeline"), btn.querySelector(".mc-xg"), {
          quiet: true,
          force: true,
        });
      }
    }
    return true;
  }

  function timelineSvg(tl) {
    const W = 320;
    const H = 52;
    const pad = 10;
    const axisY = 26;
    const maxM = Math.max(90, Number(tl.max_minute) || 90);
    const now = Math.max(1, Math.min(maxM, Number(tl.minute) || 1));
    const xAt = (m) => pad + ((Number(m) / maxM) * (W - pad * 2));
    const marks = [];
    for (const ev of tl.events || []) {
      const x = xAt(ev.minute).toFixed(1);
      const home = ev.team !== "away";
      const y = home ? axisY - 8 : axisY + 8;
      const xgBit = ev.xg != null ? ` · xG ${Number(ev.xg).toFixed(2)}` : "";
      const title = `${ev.clock || ev.minute + "'"} ${ev.kind}${xgBit} — ${ev.text || ""}`;
      if (ev.kind === "goal") {
        const y1 = home ? axisY - 16 : axisY + 2;
        const y2 = home ? axisY - 2 : axisY + 16;
        marks.push(
          `<g class="tl-goal"><title>${escapeHtml(title)}</title><line x1="${x}" y1="${y1}" x2="${x}" y2="${y2}"/><circle cx="${x}" cy="${y}" r="3.5"/></g>`
        );
      } else if (ev.kind === "shot_on") {
        marks.push(`<g class="tl-sot"><title>${escapeHtml(title)}</title><circle cx="${x}" cy="${y}" r="3.2"/></g>`);
      } else if (ev.kind === "blocked") {
        marks.push(
          `<g class="tl-blocked"><title>${escapeHtml(title)}</title><rect x="${(Number(x) - 2).toFixed(1)}" y="${(y - 2).toFixed(1)}" width="4" height="4"/></g>`
        );
      } else if (ev.kind === "shot") {
        marks.push(`<g class="tl-shot"><title>${escapeHtml(title)}</title><circle cx="${x}" cy="${y}" r="2.2"/></g>`);
      } else if (ev.kind === "corner") {
        marks.push(
          `<g class="tl-corner"><title>${escapeHtml(title)}</title><rect x="${(Number(x) - 2.2).toFixed(1)}" y="${(y - 2.2).toFixed(1)}" width="4.4" height="4.4" transform="rotate(45 ${x} ${y})"/></g>`
        );
      }
    }
    const nowX = xAt(now).toFixed(1);
    const htX = xAt(45).toFixed(1);
    return `<svg class="mc-tl-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="0 to 90 minute event timeline">
      <line x1="${pad}" y1="${axisY}" x2="${W - pad}" y2="${axisY}" class="tl-axis"/>
      <line x1="${pad}" y1="${axisY}" x2="${nowX}" y2="${axisY}" class="tl-progress"/>
      <line x1="${htX}" y1="${axisY - 5}" x2="${htX}" y2="${axisY + 5}" class="tl-ht"/>
      <text x="${pad}" y="${H - 4}" class="tl-label">0'</text>
      <text x="${htX}" y="${H - 4}" class="tl-label" text-anchor="middle">45'</text>
      <text x="${W - pad}" y="${H - 4}" class="tl-label" text-anchor="end">90'</text>
      <line x1="${nowX}" y1="4" x2="${nowX}" y2="${H - 12}" class="tl-now"/>
      ${marks.join("")}
    </svg>`;
  }

  function xgSeriesPath(series, xAt, yAt, nowM) {
    const pts = series || [];
    if (!pts.length) return "";
    const parts = [];
    let lastY = yAt(0);
    parts.push(`M ${xAt(0).toFixed(1)} ${lastY.toFixed(1)}`);
    for (const p of pts) {
      if (p.minute === 0) {
        lastY = yAt(p.cumulative);
        continue;
      }
      const x = xAt(Math.min(nowM, p.minute));
      parts.push(`L ${x.toFixed(1)} ${lastY.toFixed(1)}`);
      lastY = yAt(p.cumulative);
      parts.push(`L ${x.toFixed(1)} ${lastY.toFixed(1)}`);
    }
    parts.push(`L ${xAt(nowM).toFixed(1)} ${lastY.toFixed(1)}`);
    return parts.join(" ");
  }

  function xgSvg(tl) {
    const W = 320;
    const H = 110;
    const padL = 28;
    const padR = 10;
    const padT = 12;
    const padB = 18;
    const maxM = Math.max(90, Number(tl.max_minute) || 90);
    const now = Math.max(1, Math.min(maxM, Number(tl.minute) || 1));
    const xg = tl.xg || { home: [], away: [], home_total: 0, away_total: 0 };
    const yMax = Math.max(0.5, xg.home_total || 0, xg.away_total || 0) * 1.15;
    const xAt = (m) => padL + ((Number(m) / maxM) * (W - padL - padR));
    const yAt = (v) => padT + ((yMax - Number(v)) / yMax) * (H - padT - padB);
    const homePath = xgSeriesPath(xg.home, xAt, yAt, now);
    const awayPath = xgSeriesPath(xg.away, xAt, yAt, now);
    const nowX = xAt(now).toFixed(1);
    const htX = xAt(45).toFixed(1);
    const y0 = yAt(0).toFixed(1);
    const yMid = yAt(yMax / 2).toFixed(1);
    const yTop = yAt(yMax).toFixed(1);
    return `<svg class="mc-xg-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Expected goals versus game time">
      <text x="4" y="${Number(yTop) + 3}" class="tl-label">${yMax.toFixed(1)}</text>
      <text x="4" y="${Number(yMid) + 3}" class="tl-label">${(yMax / 2).toFixed(1)}</text>
      <text x="4" y="${Number(y0) + 3}" class="tl-label">0</text>
      <line x1="${padL}" y1="${yTop}" x2="${W - padR}" y2="${yTop}" class="tl-grid"/>
      <line x1="${padL}" y1="${yMid}" x2="${W - padR}" y2="${yMid}" class="tl-grid"/>
      <line x1="${padL}" y1="${y0}" x2="${W - padR}" y2="${y0}" class="tl-axis"/>
      <line x1="${htX}" y1="${padT}" x2="${htX}" y2="${y0}" class="tl-ht"/>
      <line x1="${nowX}" y1="${padT}" x2="${nowX}" y2="${y0}" class="tl-now"/>
      ${homePath ? `<path d="${homePath}" class="xg-home" fill="none"/>` : ""}
      ${awayPath ? `<path d="${awayPath}" class="xg-away" fill="none"/>` : ""}
      <text x="${padL}" y="${H - 4}" class="tl-label">0'</text>
      <text x="${htX}" y="${H - 4}" class="tl-label" text-anchor="middle">45'</text>
      <text x="${W - padR}" y="${H - 4}" class="tl-label" text-anchor="end">90'</text>
      <text x="${W - padR}" y="10" class="tl-label" text-anchor="end">xG ${Number(xg.home_total || 0).toFixed(2)}–${Number(xg.away_total || 0).toFixed(2)}</text>
    </svg>`;
  }

  async function loadMatchTimeline(m, mount, xgMount, opts = {}) {
    if (!mount) return;
    const quiet = Boolean(opts.quiet);
    const force = Boolean(opts.force);
    const preferCache = Boolean(opts.preferCache);
    const cached = state.timelines?.[m.event_id];
    const fresh = cached && Date.now() - cached._ts < 12000;
    const hasSvg = () => !!mount.querySelector("svg.mc-tl-svg");

    const paint = (tl) => {
      if (mount.isConnected) mount.innerHTML = timelineSvg(tl);
      if (xgMount && xgMount.isConnected) xgMount.innerHTML = xgSvg(tl);
    };

    const chartSig = (tl) =>
      JSON.stringify({
        minute: tl.minute,
        max: tl.max_minute,
        events: (tl.events || []).map((e) => [e.minute, e.kind, e.team, e.xg]),
        xh: tl.xg?.home_total,
        xa: tl.xg?.away_total,
      });

    // Keep existing pictograms visible while refetching — only fill empty mounts from cache.
    if (cached) {
      if (!hasSvg()) paint(cached);
      if (fresh && !force) return;
    } else if (!quiet && !hasSvg()) {
      mount.innerHTML = `<span class="mc-timeline-loading">timeline…</span>`;
      if (xgMount && !xgMount.querySelector("svg")) {
        xgMount.innerHTML = `<span class="mc-timeline-loading">xG…</span>`;
      }
    }

    if (preferCache && fresh && !force) return;

    try {
      const qs = liveQuery(m);
      const tl = await (await fetch(`/api/live/timeline?${qs}`)).json();
      tl._ts = Date.now();
      state.timelines = state.timelines || {};
      const prev = state.timelines[m.event_id];
      state.timelines[m.event_id] = tl;
      // Skip DOM rewrite when nothing meaningful changed (avoids a 25s blink).
      if (hasSvg() && prev && chartSig(prev) === chartSig(tl)) return;
      paint(tl);
    } catch (err) {
      // Soft refresh: leave the last good chart up on network errors.
      if (quiet && (hasSvg() || cached)) return;
      if (mount.isConnected && !hasSvg()) {
        mount.innerHTML = `<span class="mc-timeline-loading">timeline unavailable</span>`;
      }
      if (xgMount && xgMount.isConnected && !xgMount.querySelector("svg")) {
        xgMount.innerHTML = `<span class="mc-timeline-loading">xG unavailable</span>`;
      }
    }
  }

  function renderLiveTabChiclets(opts = {}) {
    renderLeagueChiclets("#leagueChiclets", "liveFilter", () => {
      renderLiveTabChiclets();
    });
    renderMatchChiclets("#matchChiclets", state.liveFilter, state.selectedLive, selectLiveMatch, {
      withTimeline: true,
      soft: !!opts.soft,
      refreshTimelines: opts.refreshTimelines,
    });
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
    renderLiveTabChiclets({ soft: true, refreshTimelines: false });
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

  function renderHistoryBlock(title, evalData, tree, fromPrev) {
    if (!evalData) {
      return `<section class="sl-block"><div class="concede-title">${escapeHtml(title)}</div><p class="concede-lede">No mapped EPL history for this club.</p></section>`;
    }
    const n = evalData.count || 0;
    const forwardTree =
      tree && tree.count
        ? `<div class="concede-when sl-tree">
            <div class="concede-when-label">Branch tree from ${escapeHtml(tree.from)}</div>
            <div class="concede-stats">${distChips(tree.branches, 8)}</div>
          </div>`
        : "";
    const takenTree =
      fromPrev && fromPrev.count
        ? `<div class="concede-when sl-tree-taken">
            <div class="concede-when-label">Took branch ${escapeHtml(fromPrev.from)} → ${escapeHtml(fromPrev.live_to || "now")}</div>
            <div class="concede-stats">${distChips(fromPrev.branches, 8)}</div>
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
      ${forwardTree}
      ${takenTree}
      <div class="concede-stats">
        <span class="score-dist-label">FT from here</span>
        ${distChips(evalData.ft_distribution)}
      </div>
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
    const fromPrev = scorelines.trees_from_prev || {};
    const branchNote = scorelines.prev_scoreline
      ? `<p class="concede-lede">Live path reached <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.scoreline)}</b> from <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.prev_scoreline)}</b>. Branch trees below show what usually happens next — and which branch this match took.</p>`
      : `<p class="concede-lede">Current structure <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.scoreline)}</b> — club history first, then league. Branch trees show the next scoreline states.</p>`;

    el.innerHTML = `
      <div class="concede-title">Scoreline ${escapeHtml(scorelines.scoreline)}</div>
      ${branchNote}
      ${renderHistoryBlock(homeLabel + " history", scorelines.home_history, trees.home, fromPrev.home)}
      ${renderHistoryBlock(awayLabel + " history", scorelines.away_history, trees.away, fromPrev.away)}
      ${renderHistoryBlock("EPL league history", scorelines.league_history, trees.league, fromPrev.league)}
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
    renderGoalContext(sim.situation || { goals: [] });
    renderScorelineEval(sim.scorelines);
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

      renderLiveTabChiclets({ soft: true });
      renderSimilarTabChiclets();

      if (state.selectedLive) {
        const m = state.selectedLive;
        $("#pitchTitle").textContent = `${m.home} ${m.home_score}–${m.away_score} ${m.away} · ${m.clock || "LIVE"}`;
      }

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
