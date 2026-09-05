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
    liveTickTimer: null,
    chicletOrder: [],
    collapsed: new Set(),
    winprob: null,
    selectedWpId: null,
  };

  const LIVE_POLL_MS = 8000;
  const TIMELINE_FRESH_MS = 5000;
  const ORDER_KEY = "eeesoc:chicletOrder";
  const COLLAPSE_KEY = "eeesoc:chicletCollapsed";

  function loadChicletOrder() {
    try {
      const raw = JSON.parse(localStorage.getItem(ORDER_KEY) || "[]");
      return Array.isArray(raw) ? raw.map(String) : [];
    } catch (err) {
      return [];
    }
  }

  function persistChicletOrder(order) {
    state.chicletOrder = order;
    try {
      localStorage.setItem(ORDER_KEY, JSON.stringify(order));
    } catch (err) {
      /* private mode — order lives for the session only */
    }
  }

  function loadCollapsed() {
    try {
      const raw = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]");
      return new Set(Array.isArray(raw) ? raw.map(String) : []);
    } catch (err) {
      return new Set();
    }
  }

  function persistCollapsed() {
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...state.collapsed]));
    } catch (err) {
      /* private mode — collapse lives for the session only */
    }
  }

  function isCollapsed(id) {
    return state.collapsed.has(String(id));
  }

  function applyCollapsed(el, collapsed) {
    if (!el) return;
    el.classList.toggle("collapsed", collapsed);
    const chev = el.querySelector(".mc-collapse");
    if (chev) {
      chev.setAttribute("aria-expanded", collapsed ? "false" : "true");
      chev.title = collapsed ? "Expand chiclet" : "Collapse chiclet";
      chev.textContent = collapsed ? "▸" : "▾";
    }
  }

  function toggleCollapsed(id) {
    const key = String(id);
    if (state.collapsed.has(key)) state.collapsed.delete(key);
    else state.collapsed.add(key);
    persistCollapsed();
    document.querySelectorAll(`.match-chiclet[data-event-id="${CSS.escape(key)}"]`).forEach((el) => {
      applyCollapsed(el, state.collapsed.has(key));
    });
  }

  function setAllCollapsed(ids, collapsed) {
    for (const id of ids) {
      if (collapsed) state.collapsed.add(String(id));
      else state.collapsed.delete(String(id));
    }
    persistCollapsed();
    document.querySelectorAll(".match-chiclet[data-event-id]").forEach((el) => {
      applyCollapsed(el, isCollapsed(el.dataset.eventId));
    });
  }

  function bindCollapse(btn) {
    applyCollapsed(btn, isCollapsed(btn.dataset.eventId));
    const chev = btn.querySelector(".mc-collapse");
    if (!chev || chev.dataset.bound === "1") return;
    chev.dataset.bound = "1";
    const onToggle = (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleCollapsed(btn.dataset.eventId);
    };
    chev.addEventListener("click", onToggle);
    chev.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") onToggle(e);
    });
  }

  function collapseToggleHtml(id) {
    const collapsed = isCollapsed(id);
    return `<span class="mc-collapse" role="button" tabindex="0" aria-expanded="${
      collapsed ? "false" : "true"
    }" title="${collapsed ? "Expand chiclet" : "Collapse chiclet"}">${collapsed ? "▸" : "▾"}</span>`;
  }

  function orderRows(rows) {
    const order = state.chicletOrder || [];
    if (!order.length) return rows;
    const idx = new Map(order.map((id, i) => [String(id), i]));
    return [...rows].sort((a, b) => {
      const ia = idx.has(String(a.event_id)) ? idx.get(String(a.event_id)) : order.length;
      const ib = idx.has(String(b.event_id)) ? idx.get(String(b.event_id)) : order.length;
      return ia - ib;
    });
  }

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
    if (name === "winprob") refreshWinprob();
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
    const soft = !!opts.soft;

    if (soft && softPatchLiveChiclets(grid, rows, selected, onSelect, opts)) {
      return;
    }

    grid.innerHTML = "";
    if (!rows.length) {
      grid.innerHTML = `<p class="lede empty-live">No live matches right now — chiclets light up at kickoff.</p>`;
      return;
    }

    if (withTimeline) {
      // Flat, user-orderable list — each chiclet carries its own league tag.
      const wrap = document.createElement("div");
      wrap.className = "match-chiclet-row match-chiclet-row-tl";
      makeChicletDropZone(wrap);
      for (const m of orderRows(rows)) {
        const btn = buildMatchChicletButton(m, selected, onSelect, withTimeline);
        makeChicletDraggable(btn, wrap);
        wrap.appendChild(btn);
        loadMatchTimeline(m, btn.querySelector(".mc-timeline"), btn.querySelector(".mc-xg"), {
          preferCache: true,
        });
      }
      grid.appendChild(wrap);
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
        const btn = buildMatchChicletButton(m, selected, onSelect, withTimeline);
        wrap.appendChild(btn);
      }
      block.appendChild(wrap);
      grid.appendChild(block);
    }
  }

  function makeChicletDraggable(btn, wrap) {
    btn.draggable = true;
    btn.addEventListener("dragstart", (e) => {
      btn.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", btn.dataset.eventId || "");
    });
    btn.addEventListener("dragend", () => {
      btn.classList.remove("dragging");
      const ids = [...wrap.querySelectorAll(".match-chiclet[data-event-id]")].map(
        (el) => String(el.dataset.eventId)
      );
      persistChicletOrder(ids);
    });
  }

  function makeChicletDropZone(wrap) {
    wrap.addEventListener("dragover", (e) => {
      const dragging = wrap.querySelector(".match-chiclet.dragging");
      if (!dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const siblings = [...wrap.querySelectorAll(".match-chiclet:not(.dragging)")];
      const next = siblings.find((el) => {
        const r = el.getBoundingClientRect();
        return e.clientY < r.top + r.height / 2;
      });
      if (next) {
        if (next.previousElementSibling !== dragging) wrap.insertBefore(dragging, next);
      } else if (wrap.lastElementChild !== dragging) {
        wrap.appendChild(dragging);
      }
    });
    wrap.addEventListener("drop", (e) => e.preventDefault());
  }

  function chicletStatsHtml(tl) {
    if (!tl) {
      return `<span class="mc-stat mc-stat-empty">shots · on target · corners · xG</span>`;
    }
    const c = tl.counts || {};
    const xg = tl.xg || {};
    const pair = (label, h, a) =>
      `<span class="mc-stat" title="${label} — home vs away"><span class="mc-stat-label">${label}</span><b class="mc-h">${h}</b><span class="mc-stat-sep">–</span><b class="mc-a">${a}</b></span>`;
    return (
      pair("Shots", (c.home_shot || 0) + (c.home_shot_on || 0) + (c.home_blocked || 0) + (c.home_goal || 0), (c.away_shot || 0) + (c.away_shot_on || 0) + (c.away_blocked || 0) + (c.away_goal || 0)) +
      pair("On target", (c.home_shot_on || 0) + (c.home_goal || 0), (c.away_shot_on || 0) + (c.away_goal || 0)) +
      pair("Corners", c.home_corner || 0, c.away_corner || 0) +
      pair("Fouls", c.home_foul || 0, c.away_foul || 0) +
      pair("xG", Number(xg.home_total || 0).toFixed(2), Number(xg.away_total || 0).toFixed(2))
    );
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
    const shown = displayedScore(m, cached);
    btn.innerHTML = `
      <span class="mc-top">
        <span class="mc-top-left">
          ${withTimeline ? `<span class="mc-grip" title="Drag to reorder" aria-hidden="true">⠿</span>` : ""}
          <span class="mc-live-badge"><span class="live-dot"></span><span class="mc-clock-text">${escapeHtml(m.clock || "LIVE")}</span></span>
        </span>
        <span class="mc-top-right">
          <span class="mc-league">${escapeHtml(m.league_chiclet)}</span>
          ${collapseToggleHtml(m.event_id)}
        </span>
      </span>
      <span class="mc-teams">
        <span class="mc-home"><span class="mc-name">${escapeHtml(shortName(m.home))}</span><i class="mc-key mc-key-home" title="Home — green in charts"></i></span>
        <span class="mc-score"><b class="mc-score-h">${shown.home}</b><span class="mc-score-sep">–</span><b class="mc-score-a">${shown.away}</b></span>
        <span class="mc-away"><i class="mc-key mc-key-away" title="Away — blue in charts"></i><span class="mc-name">${escapeHtml(shortName(m.away))}</span></span>
      </span>
      ${
        withTimeline
          ? `<span class="mc-stats" data-stats-for="${escapeHtml(m.event_id)}">${chicletStatsHtml(cached)}</span>
            <div class="mc-charts">
              <span class="mc-timeline" data-tl-for="${escapeHtml(m.event_id)}" aria-label="Match event timeline">${
                cached ? timelineSvg(cached) : `<span class="mc-timeline-loading">timeline…</span>`
              }</span>
              <span class="mc-xg" data-xg-for="${escapeHtml(m.event_id)}" aria-label="Expected goals versus time">${
                cached ? xgSvg(cached) : `<span class="mc-timeline-loading">xG…</span>`
              }</span>
              <span class="mc-territory" data-terr-for="${escapeHtml(m.event_id)}" aria-label="Territory map">${
                cached ? territorySvg(cached) : `<span class="mc-timeline-loading">territory…</span>`
              }</span>
            </div>`
          : ""
      }
    `;
    btn.addEventListener("click", () => {
      if (btn.__onSelect) btn.__onSelect(btn.__match);
    });
    bindCollapse(btn);
    return btn;
  }

  function displayedScore(m, tl) {
    const boardH = Number(m?.home_score) || 0;
    const boardA = Number(m?.away_score) || 0;
    const playH = Number(tl?.home_score);
    const playA = Number(tl?.away_score);
    return {
      home: Math.max(boardH, Number.isFinite(playH) ? playH : 0),
      away: Math.max(boardA, Number.isFinite(playA) ? playA : 0),
    };
  }

  function applyChicletScore(btn, home, away) {
    if (!btn) return;
    const scoreH = btn.querySelector(".mc-score-h");
    const scoreA = btn.querySelector(".mc-score-a");
    if (!scoreH || !scoreA) return;
    const changed = scoreH.textContent !== String(home) || scoreA.textContent !== String(away);
    scoreH.textContent = String(home);
    scoreA.textContent = String(away);
    if (btn.__match) {
      btn.__match = { ...btn.__match, home_score: home, away_score: away };
    }
    if (state.selectedLive && String(state.selectedLive.event_id) === String(btn.dataset.eventId)) {
      state.selectedLive = { ...state.selectedLive, home_score: home, away_score: away };
      const title = $("#pitchTitle");
      if (title && !$("#pitchPanel")?.hidden) {
        const m = state.selectedLive;
        title.textContent = `${m.home} ${home}–${away} ${m.away} · ${m.clock || "LIVE"}`;
      }
    }
    if (changed) flashGoal(btn);
  }

  function flashGoal(btn) {
    btn.classList.remove("mc-goal-flash");
    // restart the animation
    void btn.offsetWidth;
    btn.classList.add("mc-goal-flash");
    setTimeout(() => btn.classList.remove("mc-goal-flash"), 4200);
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

    const refreshTimelines = opts.refreshTimelines !== false && !!opts.withTimeline;

    for (const m of rows) {
      const btn = byId.get(String(m.event_id));
      btn.__match = m;
      btn.__onSelect = onSelect;
      btn.classList.toggle("on", !!(selected && selected.event_id === m.event_id));
      const tl = state.timelines?.[m.event_id];
      const clockText = btn.querySelector(".mc-clock-text");
      // The 1s ticker owns the running clock; only overwrite for frozen states (HT/FT)
      if (clockText && (!tl || tl.frozen || !Number.isFinite(Number(tl.elapsed_seconds)))) {
        clockText.textContent = m.clock || "LIVE";
      }
      const shown = displayedScore(m, tl);
      applyChicletScore(btn, shown.home, shown.away);
      const names = btn.querySelectorAll(".mc-teams .mc-name");
      if (names.length === 2) {
        names[0].textContent = shortName(m.home);
        names[1].textContent = shortName(m.away);
      }
      if (refreshTimelines) {
        loadMatchTimeline(m, btn.querySelector(".mc-timeline"), btn.querySelector(".mc-xg"), {
          quiet: true,
          force: true,
        });
      }
      bindCollapse(btn);
    }
    return true;
  }

  function liveElapsedSeconds(tl) {
    const base = Number(tl.elapsed_seconds);
    const fallback = Math.max(0, (Number(tl.minute) || 1) - 1) * 60;
    const start = Number.isFinite(base) ? base : fallback;
    if (tl.frozen) return start;
    const synced = tl._syncedAt || tl._ts || Date.now();
    return Math.max(0, Math.min(99 * 60, start + (Date.now() - synced) / 1000));
  }

  function liveNowMinutes(tl) {
    const maxM = Math.max(90, Number(tl.max_minute) || 90);
    return Math.max(0.5, Math.min(maxM, liveElapsedSeconds(tl) / 60));
  }

  function formatTickClock(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    return `${Math.floor(s / 60)}'${String(s % 60).padStart(2, "0")}`;
  }

  // Per-kind lanes (distance from axis) so a busy minute stays readable:
  // shots hug the axis, corners sit furthest out, goals span the lane stack.
  const TL_LANES = { shot: 6, shot_on: 11, blocked: 16, corner: 21 };

  function timelineSvg(tl) {
    const W = 320;
    const H = 68;
    const pad = 10;
    const axisY = 32;
    const maxM = Math.max(90, Number(tl.max_minute) || 90);
    const now = liveNowMinutes(tl);
    const xAt = (m) => pad + ((Number(m) / maxM) * (W - pad * 2));
    const marks = [];
    // Same minute + side + kind → nudge horizontally instead of stacking.
    const seen = new Map();
    const nudge = (ev) => {
      const key = `${ev.minute}:${ev.team}:${ev.kind}`;
      const n = seen.get(key) || 0;
      seen.set(key, n + 1);
      return n * 3.2;
    };
    for (const ev of tl.events || []) {
      const x = (Number(xAt(ev.minute)) + nudge(ev)).toFixed(1);
      const home = ev.team !== "away";
      const dir = home ? -1 : 1;
      const laneY = (kind) => axisY + dir * (TL_LANES[kind] || 8);
      const xgBit = ev.xg != null ? ` · xG ${Number(ev.xg).toFixed(2)}` : "";
      const title = `${ev.clock || ev.minute + "'"} ${ev.kind}${xgBit} — ${ev.text || ""}`;
      if (ev.kind === "goal") {
        const y1 = home ? axisY - 24 : axisY + 2;
        const y2 = home ? axisY - 2 : axisY + 24;
        const cy = axisY + dir * 13;
        marks.push(
          `<g class="tl-goal"><title>${escapeHtml(title)}</title><line x1="${x}" y1="${y1}" x2="${x}" y2="${y2}"/><circle cx="${x}" cy="${cy}" r="4"/></g>`
        );
      } else if (ev.kind === "shot_on") {
        marks.push(
          `<g class="tl-sot"><title>${escapeHtml(title)}</title><circle cx="${x}" cy="${laneY("shot_on")}" r="3"/></g>`
        );
      } else if (ev.kind === "blocked") {
        const y = laneY("blocked");
        marks.push(
          `<g class="tl-blocked"><title>${escapeHtml(title)}</title><rect x="${(Number(x) - 2).toFixed(1)}" y="${(y - 2).toFixed(1)}" width="4" height="4"/></g>`
        );
      } else if (ev.kind === "shot") {
        marks.push(
          `<g class="tl-shot"><title>${escapeHtml(title)}</title><circle cx="${x}" cy="${laneY("shot")}" r="2.4"/></g>`
        );
      } else if (ev.kind === "corner") {
        const y = laneY("corner");
        marks.push(
          `<g class="tl-corner"><title>${escapeHtml(title)}</title><rect x="${(Number(x) - 2.2).toFixed(1)}" y="${(y - 2.2).toFixed(1)}" width="4.4" height="4.4" transform="rotate(45 ${x} ${y})"/></g>`
        );
      }
    }
    const nowX = xAt(now).toFixed(1);
    const htX = xAt(45).toFixed(1);
    return `<svg class="mc-tl-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="0 to 90 minute event timeline" data-pad-l="${pad}" data-pad-r="${pad}" data-width="${W}" data-max="${maxM}">
      <line x1="${pad}" y1="${axisY}" x2="${W - pad}" y2="${axisY}" class="tl-axis"/>
      <line x1="${pad}" y1="${axisY}" x2="${nowX}" y2="${axisY}" class="tl-progress"/>
      <line x1="${htX}" y1="${axisY - 6}" x2="${htX}" y2="${axisY + 6}" class="tl-ht"/>
      <text x="${pad}" y="${H - 4}" class="tl-label">0'</text>
      <text x="${htX}" y="${H - 4}" class="tl-label" text-anchor="middle">45'</text>
      <text x="${W - pad}" y="${H - 4}" class="tl-label" text-anchor="end">90'</text>
      <line x1="${nowX}" y1="4" x2="${nowX}" y2="${H - 14}" class="tl-now"/>
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
    const now = liveNowMinutes(tl);
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
    return `<svg class="mc-xg-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Expected goals versus game time" data-pad-l="${padL}" data-pad-r="${padR}" data-width="${W}" data-max="${maxM}">
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
      <text x="${W - padR}" y="11" class="tl-xg-total" text-anchor="end">xG <tspan class="tl-xg-h">${Number(xg.home_total || 0).toFixed(2)}</tspan>–<tspan class="tl-xg-a">${Number(xg.away_total || 0).toFixed(2)}</tspan></text>
    </svg>`;
  }

  function territoryLabel(tl) {
    const terr = tl.territory;
    if (!terr) return "";
    const homeName = shortName(tl.home || "Home");
    const awayName = shortName(tl.away || "Away");
    switch (terr.label) {
      case "warming_up":
        return "Reading the game…";
      case "midfield":
        return "Midfield battle";
      case "home_attacking":
        return `${homeName} camped forward — ${awayName} pinned back`;
      case "away_attacking":
        return `${awayName} camped forward — ${homeName} pinned back`;
      default:
        return "Even territory";
    }
  }

  function territorySvg(tl) {
    const terr = tl.territory;
    if (!terr || !terr.total) {
      return `<span class="mc-timeline-loading">territory…</span>`;
    }
    const W = 320;
    const H = 148;
    const padX = 10;
    const padT = 8;
    const padB = 26;
    const pw = W - padX * 2;
    const ph = H - padT - padB;
    const cols = terr.cols || 6;
    const rows = terr.rows || 4;
    const cw = pw / cols;
    const ch = ph / rows;
    const maxCell = Math.max(1, Number(terr.max) || 1);
    const cells = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const v = ((terr.cells || [])[r] || [])[c] || 0;
        if (!v) continue;
        const heat = Math.pow(v / maxCell, 0.7);
        cells.push(
          `<rect x="${(padX + c * cw).toFixed(1)}" y="${(padT + r * ch).toFixed(1)}" width="${cw.toFixed(1)}" height="${ch.toFixed(1)}" class="terr-cell" style="fill-opacity:${(heat * 0.6).toFixed(3)}"><title>${v} actions</title></rect>`
        );
      }
    }
    const thirds = terr.thirds || {};
    const pctText = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
    const midX = padX + pw / 2;
    const t1 = padX + pw / 6;
    const t3 = padX + (5 * pw) / 6;
    const boxH = ph * 0.55;
    const boxW = pw * 0.16;
    const goalY = padT + (ph - boxH) / 2;
    return `<svg class="mc-terr-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Territory map — where the game is being played">
      ${cells.join("")}
      <rect x="${padX}" y="${padT}" width="${pw}" height="${ph}" class="terr-line" fill="none"/>
      <line x1="${midX}" y1="${padT}" x2="${midX}" y2="${padT + ph}" class="terr-line"/>
      <circle cx="${midX}" cy="${padT + ph / 2}" r="${ph * 0.18}" class="terr-line" fill="none"/>
      <rect x="${padX}" y="${goalY}" width="${boxW}" height="${boxH}" class="terr-line" fill="none"/>
      <rect x="${padX + pw - boxW}" y="${goalY}" width="${boxW}" height="${boxH}" class="terr-line" fill="none"/>
      <line x1="${(padX + pw / 3).toFixed(1)}" y1="${padT}" x2="${(padX + pw / 3).toFixed(1)}" y2="${padT + ph}" class="terr-third"/>
      <line x1="${(padX + (2 * pw) / 3).toFixed(1)}" y1="${padT}" x2="${(padX + (2 * pw) / 3).toFixed(1)}" y2="${padT + ph}" class="terr-third"/>
      <text x="${t1}" y="${padT + 12}" class="terr-pct" text-anchor="middle">${pctText(thirds.home_def)}</text>
      <text x="${midX}" y="${padT + 12}" class="terr-pct" text-anchor="middle">${pctText(thirds.mid)}</text>
      <text x="${t3}" y="${padT + 12}" class="terr-pct" text-anchor="middle">${pctText(thirds.home_att)}</text>
      <text x="${padX}" y="${H - 14}" class="tl-label"><tspan class="tl-xg-h">◀ ${escapeHtml(shortName(tl.home || "Home"))}</tspan> defend</text>
      <text x="${W - padX}" y="${H - 14}" class="tl-label" text-anchor="end"><tspan class="tl-xg-a">${escapeHtml(shortName(tl.away || "Away"))} ▶</tspan> defend</text>
      <text x="${midX}" y="${H - 3}" class="terr-headline" text-anchor="middle">${escapeHtml(territoryLabel(tl))}</text>
    </svg>`;
  }

  async function loadMatchTimeline(m, mount, xgMount, opts = {}) {
    if (!mount) return;
    const quiet = Boolean(opts.quiet);
    const force = Boolean(opts.force);
    const preferCache = Boolean(opts.preferCache);
    const cached = state.timelines?.[m.event_id];
    const fresh = cached && Date.now() - cached._ts < TIMELINE_FRESH_MS;
    const hasSvg = () => !!mount.querySelector("svg.mc-tl-svg");

    const paint = (tl) => {
      if (mount.isConnected) mount.innerHTML = timelineSvg(tl);
      if (xgMount && xgMount.isConnected) xgMount.innerHTML = xgSvg(tl);
      const stats = document.querySelector(`.mc-stats[data-stats-for="${CSS.escape(String(m.event_id))}"]`);
      if (stats) stats.innerHTML = chicletStatsHtml(tl);
      const terr = document.querySelector(`.mc-territory[data-terr-for="${CSS.escape(String(m.event_id))}"]`);
      if (terr) terr.innerHTML = territorySvg(tl);
      const card = mount.closest(".match-chiclet");
      if (card) {
        const shown = displayedScore(m, tl);
        applyChicletScore(card, shown.home, shown.away);
      }
    };

    const chartSig = (tl) =>
      JSON.stringify({
        minute: tl.minute,
        max: tl.max_minute,
        events: (tl.events || []).map((e) => [e.minute, e.kind, e.team, e.xg]),
        xh: tl.xg?.home_total,
        xa: tl.xg?.away_total,
        fouls: [tl.counts?.home_foul, tl.counts?.away_foul],
        terr: tl.territory?.total,
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
      tl._syncedAt = Date.now();
      state.timelines = state.timelines || {};
      const prev = state.timelines[m.event_id];
      state.timelines[m.event_id] = tl;
      // Always push the play-derived score — a stale 0-0 board must not win.
      const card = mount.closest(".match-chiclet");
      if (card) {
        const shown = displayedScore(m, tl);
        applyChicletScore(card, shown.home, shown.away);
      }
      // Skip SVG rewrite when nothing meaningful changed (the ticker keeps the cursor moving).
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

  function moveNowCursor(svg, nowMinutes) {
    const padL = Number(svg.dataset.padL || 10);
    const padR = Number(svg.dataset.padR || 10);
    const W = Number(svg.dataset.width || 320);
    const maxM = Number(svg.dataset.max || 90);
    const x = (padL + (Math.min(maxM, nowMinutes) / maxM) * (W - padL - padR)).toFixed(1);
    const progress = svg.querySelector(".tl-progress");
    if (progress) progress.setAttribute("x2", x);
    const now = svg.querySelector(".tl-now");
    if (now) {
      now.setAttribute("x1", x);
      now.setAttribute("x2", x);
    }
  }

  function kickoffElapsedSeconds(m) {
    if (!m) return null;
    if (m.start) {
      const t = Date.parse(m.start);
      if (Number.isFinite(t)) {
        const elapsed = (Date.now() - t) / 1000;
        // First-half window only — after HT, start-based clocks run fast.
        if (elapsed >= 0 && elapsed <= 50 * 60) return elapsed;
      }
    }
    const cs = Number(m.clock_seconds);
    return Number.isFinite(cs) ? cs : null;
  }

  function tickLiveClocks() {
    const panel = $("#panel-live");
    if (!panel || panel.hidden || document.hidden) return;
    const grid = $("#matchChiclets");
    if (!grid) return;
    for (const btn of grid.querySelectorAll(".match-chiclet[data-event-id]")) {
      const eventId = btn.dataset.eventId;
      const tl = state.timelines?.[eventId];
      if (tl) {
        const secs = liveElapsedSeconds(tl);
        const nowM = Math.max(0.5, secs / 60);
        if (!tl.frozen) {
          const clockEl = btn.querySelector(".mc-clock-text");
          if (clockEl) clockEl.textContent = formatTickClock(secs);
        }
        const tlSvg = btn.querySelector(".mc-timeline svg");
        if (tlSvg) moveNowCursor(tlSvg, nowM);
        const xgSvgEl = btn.querySelector(".mc-xg svg");
        if (xgSvgEl) moveNowCursor(xgSvgEl, nowM);
        continue;
      }
      const secs = kickoffElapsedSeconds(btn.__match);
      if (secs == null) continue;
      const clockEl = btn.querySelector(".mc-clock-text");
      if (clockEl) clockEl.textContent = formatTickClock(secs);
    }
  }

  function ensureLiveTicker() {
    if (state.liveTickTimer) return;
    state.liveTickTimer = setInterval(tickLiveClocks, 1000);
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

  function renderSimilarTabChiclets(opts = {}) {
    renderLeagueChiclets("#similarLeagueChiclets", "similarFilter", () => {
      renderSimilarTabChiclets();
    });
    renderMatchChiclets(
      "#similarMatchChiclets",
      state.similarFilter,
      state.selectedSimilarLive,
      selectSimilarLive,
      { soft: !!opts.soft }
    );
  }

  async function selectLiveMatch(m) {
    state.selectedLive = m;
    renderLiveTabChiclets({ soft: true, refreshTimelines: false });
    $("#pitchPanel").hidden = false;
    $("#pitchTitle").textContent = `${m.home} ${m.home_score}–${m.away_score} ${m.away} · ${m.clock || "LIVE"}`;
    await refreshTrack();
    if (state.trackTimer) clearInterval(state.trackTimer);
    state.trackTimer = setInterval(refreshTrack, 5000);
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
      clock_s: m.clock_seconds != null ? String(m.clock_seconds) : "",
      chiclet: m.league_chiclet || "",
      home_id: m.home_id || "",
      away_id: m.away_id || "",
    });
  }

  async function selectSimilarLive(m) {
    state.selectedSimilarLive = m;
    state.selectedId = null;
    renderMatches();
    renderSimilarTabChiclets({ soft: true });
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
    const atMin = evalData.at_minute != null ? Number(evalData.at_minute) : null;
    const atLabel = atMin != null ? ` @ ${atMin}′` : "";
    const forwardTree =
      tree && tree.count
        ? `<div class="concede-when sl-tree">
            <div class="concede-when-label">Branch tree from ${escapeHtml(tree.from)}${
              tree.at_minute != null ? ` @ ${Number(tree.at_minute)}′` : ""
            }</div>
            <div class="concede-stats">${distChips(tree.branches, 8)}</div>
          </div>`
        : "";
    const takenTree =
      fromPrev && fromPrev.count
        ? `<div class="concede-when sl-tree-taken">
            <div class="concede-when-label">Took branch ${escapeHtml(fromPrev.from)}${
              fromPrev.at_minute != null ? ` @ ${Number(fromPrev.at_minute)}′` : ""
            } → ${escapeHtml(fromPrev.live_to || "now")}</div>
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
          <span class="teams">${escapeHtml(p.home)} vs ${escapeHtml(p.away)} <span class="ft">${
            atMin != null ? `${escapeHtml(p.scoreline)} at ${atMin}′` : `hit ${escapeHtml(p.scoreline)}`
          } · FT ${escapeHtml(p.ft)}</span></span>
          <span class="meta"><span class="meta-line">${after ? escapeHtml(after) : "ended here"}</span></span>
        </div>`;
      })
      .join("");

    const sample =
      atMin != null
        ? `<b style="color:var(--text)">${n}</b> games were ${escapeHtml(evalData.scoreline)} at the ${atMin}′ mark.`
        : `<b style="color:var(--text)">${n}</b> games ever at this scoreline.`;

    return `<section class="sl-block">
      <div class="concede-title">${escapeHtml(title)} at ${escapeHtml(evalData.scoreline)}${atLabel}</div>
      <p class="concede-lede">
        ${sample}
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
    const minuteBit =
      scorelines.minute != null
        ? ` — only games at this score at the <b style="color:var(--accent)">${Number(scorelines.minute)}′</b> mark count, so time left is priced in`
        : "";
    const branchNote = scorelines.prev_scoreline
      ? `<p class="concede-lede">Live path reached <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.scoreline)}</b> from <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.prev_scoreline)}</b>${minuteBit}. Branch trees below show what usually happens next — and which branch this match took.</p>`
      : `<p class="concede-lede">Current structure <b style="color:var(--accent);font-family:var(--mono)">${escapeHtml(scorelines.scoreline)}</b>${minuteBit} — club history first, then league. Branch trees show the next scoreline states.</p>`;

    el.innerHTML = `
      <div class="concede-title">Scoreline ${escapeHtml(scorelines.scoreline)}${scorelines.minute != null ? ` @ ${Number(scorelines.minute)}′` : ""}</div>
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

  // —— WinProb tab ——

  function wpPct(v) {
    if (v == null) return "—";
    return `${Math.round(Number(v) * 100)}%`;
  }

  async function refreshWinprob() {
    const stamp = $("#winprobStamp");
    try {
      const data = await (await fetch("/api/winprob")).json();
      state.winprob = data;
      stamp.textContent = `${(data.fixtures || []).length} scheduled · updated ${new Date().toLocaleTimeString()}`;
      renderWinprob();
    } catch (err) {
      stamp.textContent = "winprob feed error";
    }
  }

  function wpRecordCard(label, rec) {
    if (!rec || !rec.total) {
      return `<div class="wp-record-card">
        <span class="wp-record-label">${escapeHtml(label)}</span>
        <span class="wp-record-main">—</span>
        <span class="wp-record-sub">no graded picks yet</span>
      </div>`;
    }
    return `<div class="wp-record-card">
      <span class="wp-record-label">${escapeHtml(label)}</span>
      <span class="wp-record-main">${rec.correct}–${rec.wrong}</span>
      <span class="wp-record-sub">${wpPct(rec.pct)} of ${rec.total} picks</span>
    </div>`;
  }

  function wpProbBarHtml(probs) {
    if (!probs) {
      return `<span class="wp-nomodel">No EPL model mapping for this fixture.</span>`;
    }
    const h = Math.round(probs.home * 100);
    const d = Math.round(probs.draw * 100);
    const a = Math.max(0, 100 - h - d);
    return `
      <span class="wp-bar" aria-hidden="true">
        <i class="wp-seg wp-seg-h" style="width:${h}%"></i>
        <i class="wp-seg wp-seg-d" style="width:${d}%"></i>
        <i class="wp-seg wp-seg-a" style="width:${a}%"></i>
      </span>
      <span class="wp-pcts">
        <span class="wp-pct-h" title="Home win">H ${wpPct(probs.home)}</span>
        <span class="wp-pct-d" title="Draw">D ${wpPct(probs.draw)}</span>
        <span class="wp-pct-a" title="Away win">A ${wpPct(probs.away)}</span>
      </span>
    `;
  }

  function buildWpChiclet(f) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "match-chiclet wp-chiclet" + (state.selectedWpId === f.event_id ? " on" : "");
    btn.setAttribute("role", "listitem");
    btn.dataset.eventId = f.event_id;
    btn.title = "Double-click for last five games + head-to-head";
    const kick = f.start
      ? new Date(f.start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : "TBD";
    const pickBit =
      f.pick && f.probs
        ? `<span class="wp-pick">Pick <b class="wp-pick-${f.pick}">${escapeHtml(
            f.pick === "draw" ? "Draw" : shortName(f.pick_team || "")
          )}</b> ${wpPct(f.pick_prob)}</span>`
        : "";
    btn.innerHTML = `
      <span class="mc-top">
        <span class="wp-kick">${escapeHtml(kick)}</span>
        <span class="mc-top-right">
          <span class="mc-league">${escapeHtml(f.league_chiclet || "EPL")}</span>
          ${collapseToggleHtml(f.event_id)}
        </span>
      </span>
      <span class="mc-teams">
        <span class="mc-home"><span class="mc-name">${escapeHtml(shortName(f.home))}</span><i class="mc-key mc-key-home" title="Home"></i></span>
        <span class="wp-vs">vs</span>
        <span class="mc-away"><i class="mc-key mc-key-away" title="Away"></i><span class="mc-name">${escapeHtml(shortName(f.away))}</span></span>
      </span>
      ${wpProbBarHtml(f.probs)}
      ${pickBit}
    `;
    btn.addEventListener("click", () => {
      state.selectedWpId = f.event_id;
      document
        .querySelectorAll("#wpFixtures .wp-chiclet")
        .forEach((el) => el.classList.toggle("on", el.dataset.eventId === String(f.event_id)));
    });
    btn.addEventListener("dblclick", () => openWpDetail(f));
    bindCollapse(btn);
    return btn;
  }

  function renderWinprob() {
    const data = state.winprob;
    if (!data) return;

    const rec = data.record || {};
    const todayIso = new Date().toISOString().slice(0, 10);
    const windowLabel =
      rec.anchor && rec.anchor !== todayIso
        ? `Past ${rec.window_days || 30} days (to ${rec.anchor})`
        : `Past ${rec.window_days || 30} days`;
    $("#wpRecord").innerHTML =
      wpRecordCard(windowLabel, rec.last30) + wpRecordCard("Season", rec.season);

    const grid = $("#wpFixtures");
    grid.innerHTML = "";
    const fixtures = data.fixtures || [];
    if (!fixtures.length) {
      grid.innerHTML = `<p class="lede empty-live">No scheduled EPL fixtures in the next ${data.days || 8} days.</p>`;
    }
    const byDay = new Map();
    for (const f of fixtures) {
      const d = f.start ? new Date(f.start) : null;
      const key = d
        ? d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
        : "Date TBD";
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(f);
    }
    for (const [day, list] of byDay) {
      const block = document.createElement("div");
      block.className = "match-chiclet-league";
      block.innerHTML = `<div class="match-chiclet-league-label"><span class="league-chiclet-tag">${escapeHtml(day)}</span> ${list.length} scheduled</div>`;
      const wrap = document.createElement("div");
      wrap.className = "match-chiclet-row";
      for (const f of list) wrap.appendChild(buildWpChiclet(f));
      block.appendChild(wrap);
      grid.appendChild(block);
    }

    renderWpResults(data.recent_results || []);
  }

  function renderWpResults(rows) {
    const box = $("#wpResults");
    if (!rows.length) {
      box.innerHTML = "";
      return;
    }
    const items = rows
      .map((r) => {
        const mark = r.correct
          ? `<span class="wp-mark hit" title="Pick was right">✓</span>`
          : `<span class="wp-mark miss" title="Pick was wrong">✗</span>`;
        return `<div class="wp-result-row${r.correct ? " hit" : " miss"}">
          ${mark}
          <span class="date">${escapeHtml(r.date)}</span>
          <span class="teams">${escapeHtml(r.home)} vs ${escapeHtml(r.away)}</span>
          <span class="meta">picked <b>${escapeHtml(r.pick === "draw" ? "Draw" : r.pick_team)}</b> ${wpPct(r.pick_prob)} · FT ${escapeHtml(r.ft)}</span>
        </div>`;
      })
      .join("");
    box.innerHTML = `<div class="concede-title">Recent graded picks</div>${items}`;
  }

  function wpFormRows(form) {
    const rows = (form.last5 || [])
      .map(
        (g) => `<div class="wp-form-row">
          <span class="wp-form-badge ${g.result.toLowerCase()}">${g.result}</span>
          <span class="date">${escapeHtml(g.date)}</span>
          <span class="teams">${escapeHtml(g.venue)} · ${escapeHtml(g.home)} ${escapeHtml(g.ft)} ${escapeHtml(g.away)}</span>
        </div>`
      )
      .join("");
    const s = form.season || {};
    const seasonLine = s.played
      ? `P${s.played} · W${s.wins} D${s.draws} L${s.losses} · GF ${s.gf} GA ${s.ga}`
      : "No season games yet";
    return `<div class="wp-form-col">
      <div class="wp-form-title">${escapeHtml(form.team)}</div>
      <div class="wp-form-season">${escapeHtml(seasonLine)}</div>
      ${rows || `<p class="concede-lede">No mapped games.</p>`}
    </div>`;
  }

  async function openWpDetail(f) {
    const box = $("#wpDetail");
    box.hidden = false;
    box.innerHTML = `<p class="lede">Loading ${escapeHtml(f.home)} vs ${escapeHtml(f.away)}…</p>`;
    const qs = new URLSearchParams({
      home: f.home,
      away: f.away,
      home_id: f.home_id || "",
      away_id: f.away_id || "",
    });
    try {
      const d = await (await fetch(`/api/winprob/detail?${qs}`)).json();
      if (d.error) {
        box.innerHTML = `<p class="lede">${escapeHtml(d.error)}</p>`;
        return;
      }
      const p = d.prediction || {};
      const kick = f.start
        ? new Date(f.start).toLocaleString([], {
            weekday: "short",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })
        : "TBD";
      const h2h = (d.h2h || [])
        .map(
          (g) => `<div class="wp-form-row wp-h2h-row">
            <span class="date">${escapeHtml(g.date)}</span>
            <span class="teams">${escapeHtml(g.home)} ${escapeHtml(g.ft)} ${escapeHtml(g.away)}</span>
          </div>`
        )
        .join("");
      box.innerHTML = `
        <div class="wp-detail-head">
          <div class="concede-title">${escapeHtml(d.home_fd)} vs ${escapeHtml(d.away_fd)} · ${escapeHtml(kick)}</div>
          <button type="button" class="wp-detail-close" id="wpDetailClose" title="Close">✕</button>
        </div>
        <p class="concede-lede">
          Model pick <b style="color:var(--accent)">${escapeHtml(p.pick === "draw" ? "Draw" : p.pick_team || "—")}</b>
          at <b>${wpPct(p.pick_prob)}</b>
          · expected goals <b class="wp-pct-h">${Number(p.lambda_home || 0).toFixed(2)}</b>–<b class="wp-pct-a">${Number(p.lambda_away || 0).toFixed(2)}</b>
        </p>
        ${wpProbBarHtml(p.probs)}
        <div class="wp-form-grid">
          ${wpFormRows(d.home_form || { team: d.home_fd, last5: [] })}
          ${wpFormRows(d.away_form || { team: d.away_fd, last5: [] })}
        </div>
        ${h2h ? `<div class="wp-form-title" style="margin-top:0.9rem">Head-to-head</div>${h2h}` : ""}
      `;
      $("#wpDetailClose").addEventListener("click", () => {
        box.hidden = true;
        box.innerHTML = "";
      });
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      box.innerHTML = `<p class="lede">Could not load fixture detail.</p>`;
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
    renderSimilarTabChiclets({ soft: true });
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
      renderSimilarTabChiclets({ soft: true });

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
    state.chicletOrder = loadChicletOrder();
    state.collapsed = loadCollapsed();
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
    $("#collapseAll")?.addEventListener("click", () => {
      const ids = flatLiveMatches(state.liveFilter).map((m) => m.event_id);
      setAllCollapsed(ids, true);
    });
    $("#expandAll")?.addEventListener("click", () => {
      const ids = flatLiveMatches(state.liveFilter).map((m) => m.event_id);
      setAllCollapsed(ids, false);
    });

    await refreshLive();
    state.liveTimer = setInterval(() => {
      if (document.hidden) return;
      refreshLive();
    }, LIVE_POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshLive();
    });
    ensureLiveTicker();
  }

  boot();
})();
