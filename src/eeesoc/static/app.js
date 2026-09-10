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
    chicletDrag: false,
    collapsed: new Set(),
    winprob: null,
    selectedWpId: null,
    wpFocus: null, // {kind:'day', date} | {kind:'bucket', league, bucket}
    liveScope: "live", // "live" | "upcoming" (today, not started) | "finished"
    htBoard: null, // /api/halftime/zero payload
    htFilter: null, // null = all; Set of league slugs
    htScope: "archive", // "archive" | "similar"
    htSimilar: {}, // event_id → /api/halftime/similar payload
    htTimer: null,
    htLoading: false,
    clinical: null, // /api/clinical board: leagues[slug].teams[]
    clinicalTimer: null,
  };

  const HT_POLL_MS = 20000;
  const CLINICAL_POLL_MS = 10 * 60 * 1000;

  const LIVE_POLL_MS = 8000;
  const TIMELINE_FRESH_MS = 5000;
  const ORDER_KEY = "eeesoc:chicletOrder";
  const COLLAPSE_KEY = "eeesoc:chicletCollapsed";
  const SCOPE_KEY = "eeesoc:liveScope";
  const FINISHED_DAYS_BACK = 1;

  function loadLiveScope() {
    try {
      const v = localStorage.getItem(SCOPE_KEY);
      if (v === "finished" || v === "upcoming") return v;
      return "live";
    } catch (err) {
      return "live";
    }
  }

  function persistLiveScope(scope) {
    state.liveScope = scope;
    try {
      localStorage.setItem(SCOPE_KEY, scope);
    } catch (err) {
      /* private mode — scope lives for the session only */
    }
  }

  function isFinishedMatch(m) {
    return m?.state === "post";
  }

  const DEAD_STATUS = ["postponed", "canceled", "cancelled", "suspended", "abandoned", "forfeit"];

  function isDeadMatch(m) {
    const blob = `${m?.detail || ""} ${m?.clock || ""} ${m?.state || ""}`.toLowerCase();
    return DEAD_STATUS.some((w) => blob.includes(w));
  }

  function kickoffDate(m) {
    if (!m?.start) return null;
    const d = new Date(m.start);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function isSameLocalDay(d, now = new Date()) {
    return (
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    );
  }

  function isUpcomingMatch(m) {
    if (m?.state !== "pre" || isDeadMatch(m)) return false;
    const d = kickoffDate(m);
    return !!(d && isSameLocalDay(d));
  }

  function upcomingKick(m) {
    const d = kickoffDate(m);
    if (!d) return "KO";
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function upcomingWhen(m) {
    const d = kickoffDate(m);
    if (!d) return "Kickoff today";
    const time = upcomingKick(m);
    return isSameLocalDay(d) ? `Today ${time}` : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
  }

  function matchClockLabel(m) {
    if (isFinishedMatch(m)) return "FT";
    if (isUpcomingMatch(m)) return upcomingWhen(m);
    return m?.clock || "LIVE";
  }

  function matchInScope(m, scope) {
    if (scope === "finished") return isFinishedMatch(m);
    if (scope === "upcoming") return isUpcomingMatch(m);
    if (scope === "live") return m?.state === "in";
    return true;
  }

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
    document.querySelectorAll(`.match-chiclet-league[data-group-key="${CSS.escape(key)}"]`).forEach((el) => {
      applyGroupCollapsed(el, key);
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
    document.querySelectorAll(".match-chiclet-league[data-group-key]").forEach((el) => {
      applyGroupCollapsed(el, el.dataset.groupKey);
    });
  }

  function applyGroupCollapsed(block, groupKey) {
    if (!block) return;
    const collapsed = isCollapsed(groupKey);
    block.classList.toggle("collapsed", collapsed);
    const chev = block.querySelector(".match-chiclet-league-label .mc-collapse");
    if (chev) {
      chev.setAttribute("aria-expanded", collapsed ? "false" : "true");
      chev.title = collapsed ? "Expand group" : "Collapse group";
      chev.textContent = collapsed ? "▸" : "▾";
    }
  }

  function bindGroupCollapse(block, groupKey) {
    applyGroupCollapsed(block, groupKey);
    const label = block.querySelector(".match-chiclet-league-label");
    if (!label || label.dataset.bound === "1") return;
    label.dataset.bound = "1";
    const onToggle = (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleCollapsed(groupKey);
    };
    label.addEventListener("click", onToggle);
    label.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") onToggle(e);
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
    if (name === "halftime") refreshHalftime();
    else stopHalftimeTimer();
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

  // scope: "live" (default) | "upcoming" | "finished" | "all"
  function flatLiveMatches(filter, scope = "live") {
    if (!state.live) return [];
    let leagues = state.live.leagues || [];
    if (filter instanceof Set) {
      leagues = leagues.filter((g) => filter.has(g.slug));
    }
    const rows = [];
    for (const g of leagues) {
      for (const m of g.matches || []) {
        if (!matchInScope(m, scope)) continue;
        rows.push({ ...m, league_name: g.name, league_chiclet: g.chiclet, league_slug: g.slug });
      }
    }
    if (scope === "finished") {
      // Most recent full-time first.
      rows.sort((a, b) => String(b.start || "").localeCompare(String(a.start || "")) || a.home.localeCompare(b.home));
    } else if (scope === "upcoming") {
      rows.sort((a, b) => String(a.start || "").localeCompare(String(b.start || "")) || a.home.localeCompare(b.home));
    }
    return rows;
  }

  function scopeCount(c, scope) {
    if (scope === "finished") return Number(c.post_count) || 0;
    if (scope === "upcoming") {
      // "Today" is local — don't trust the server's UTC pre_count.
      return flatLiveMatches(new Set([c.slug]), "upcoming").length;
    }
    return Number(c.live_count) || 0;
  }

  function renderLeagueChiclets(rowEl, filterKey, onChange, scope = "live") {
    const row = $(rowEl);
    row.innerHTML = "";
    if (!state.live) return;

    const filter = state[filterKey];
    const total =
      scope === "finished"
        ? state.live.post_total || 0
        : scope === "upcoming"
          ? flatLiveMatches(null, "upcoming").length
          : state.live.live_total || 0;
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "chiclet" + (filter == null ? " on" : "");
    allBtn.innerHTML = `<span class="chiclet-label">ALL</span><span class="chiclet-count">${total}</span>`;
    allBtn.addEventListener("click", () => {
      state[filterKey] = null;
      onChange();
    });
    row.appendChild(allBtn);

    for (const c of state.live.chiclets || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      const n = scopeCount(c, scope);
      const active = filter instanceof Set && filter.has(c.slug);
      btn.className = "chiclet" + (active ? " on" : "") + (!n ? " dim" : "");
      btn.disabled = !n && !active;
      btn.innerHTML = `<span class="chiclet-label">${c.label}</span><span class="chiclet-count">${n}</span>`;
      btn.addEventListener("click", () => {
        if (!n) return;
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
    const scope = opts.scope || "live";
    const rows = flatLiveMatches(filter, scope);
    const withTimeline = !!opts.withTimeline;
    const soft = !!opts.soft;

    // Never rebuild the list mid-drag — moving a <button> during HTML5 DnD (or a
    // live poll) cancels the gesture and the card snaps back.
    if (state.chicletDrag) {
      if (soft) softPatchLiveChiclets(grid, rows, selected, onSelect, opts);
      return;
    }

    if (soft && softPatchLiveChiclets(grid, rows, selected, onSelect, opts)) {
      return;
    }

    grid.innerHTML = "";
    if (!rows.length) {
      const empty =
        scope === "finished"
          ? "No finished matches yet today — full-time chiclets land here after the whistle."
          : scope === "upcoming"
            ? "No upcoming kickoffs left today — they move to Live at kickoff."
            : "No live matches right now — chiclets light up at kickoff.";
      grid.innerHTML = `<p class="lede empty-live">${empty}</p>`;
      return;
    }

    if (withTimeline) {
      // Flat, user-orderable list — each chiclet carries its own league tag.
      // Finished / upcoming keep kickoff order rather than the drag order.
      const wrap = document.createElement("div");
      wrap.className = "match-chiclet-row match-chiclet-row-tl";
      const ordered = scope === "live" ? orderRows(rows) : rows;
      for (const m of ordered) {
        const btn = buildMatchChicletButton(m, selected, onSelect, withTimeline);
        if (scope === "live") makeChicletDraggable(btn, wrap);
        wrap.appendChild(btn);
        if (isUpcomingMatch(m)) {
          // Nothing to chart until kickoff.
        } else if (isFinishedMatch(m)) {
          // Dozens of full-time games × a dozen play pages each — only fetch what's on screen.
          lazyLoadTimeline(m, btn);
        } else {
          loadMatchTimeline(m, btn.querySelector(".mc-timeline"), btn.querySelector(".mc-xg"), {
            preferCache: true,
          });
        }
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

    for (const [slug, group] of byLeague) {
      const groupKey = `group:league:${slug}`;
      const block = document.createElement("div");
      block.className = "match-chiclet-league";
      block.dataset.groupKey = groupKey;
      block.innerHTML = `<div class="match-chiclet-league-label" role="button" tabindex="0" title="Collapse or expand this league"><span class="league-chiclet-tag">${escapeHtml(group.chiclet)}</span> ${escapeHtml(group.name)} <span class="league-count">${group.matches.length}</span>${collapseToggleHtml(groupKey)}</div>`;
      const wrap = document.createElement("div");
      wrap.className = "match-chiclet-row";
      for (const m of group.matches) {
        const btn = buildMatchChicletButton(m, selected, onSelect, withTimeline);
        wrap.appendChild(btn);
      }
      block.appendChild(wrap);
      bindGroupCollapse(block, groupKey);
      grid.appendChild(block);
    }
  }

  let timelineObserver = null;

  function lazyLoadTimeline(m, btn) {
    const load = () =>
      loadMatchTimeline(m, btn.querySelector(".mc-timeline"), btn.querySelector(".mc-xg"), {
        preferCache: true,
      });
    if (state.timelines?.[m.event_id] || typeof IntersectionObserver === "undefined") {
      load();
      return;
    }
    if (!timelineObserver) {
      timelineObserver = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            timelineObserver.unobserve(entry.target);
            const fn = entry.target.__lazyTimeline;
            if (fn) {
              entry.target.__lazyTimeline = null;
              fn();
            }
          }
        },
        { rootMargin: "300px 0px" }
      );
    }
    btn.__lazyTimeline = load;
    timelineObserver.observe(btn);
  }

  function placeChicletAtY(wrap, card, clientY) {
    const others = [...wrap.querySelectorAll(".match-chiclet")].filter((el) => el !== card);
    const next = others.find((el) => {
      const r = el.getBoundingClientRect();
      return clientY < r.top + r.height / 2;
    });
    if (next) {
      if (next.previousElementSibling !== card) wrap.insertBefore(card, next);
    } else if (wrap.lastElementChild !== card) {
      wrap.appendChild(card);
    }
  }

  function makeChicletDraggable(btn, wrap) {
    // Pointer drag — HTML5 DnD on <button> (and on SVG children) is unreliable:
    // Firefox never starts the drag, and Chromium often cancels it the moment
    // insertBefore moves the drag source. Hold / move is what the Live tab wants.
    const MOVE_PX = 6;
    const TOUCH_HOLD_MS = 160;
    let pointerId = null;
    let originX = 0;
    let originY = 0;
    let dragging = false;
    let holdTimer = null;
    let pointerType = "mouse";

    const clearHold = () => {
      if (holdTimer) {
        clearTimeout(holdTimer);
        holdTimer = null;
      }
    };

    const persistOrder = () => {
      persistChicletOrder(
        [...wrap.querySelectorAll(".match-chiclet[data-event-id]")].map((el) => String(el.dataset.eventId))
      );
    };

    const startDrag = () => {
      if (dragging || pointerId == null) return;
      dragging = true;
      state.chicletDrag = true;
      btn.__suppressClick = true;
      btn.classList.add("dragging");
      try {
        btn.setPointerCapture(pointerId);
      } catch (err) {
        /* capture is optional — window listeners still finish the gesture */
      }
    };

    const stopDrag = (commit) => {
      clearHold();
      btn.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (dragging) {
        try {
          btn.releasePointerCapture(pointerId);
        } catch (err) {
          /* already released */
        }
        btn.classList.remove("dragging");
        state.chicletDrag = false;
        if (commit) persistOrder();
      }
      dragging = false;
      pointerId = null;
    };

    const onMove = (e) => {
      if (e.pointerId !== pointerId) return;
      const dx = e.clientX - originX;
      const dy = e.clientY - originY;
      if (!dragging) {
        if (Math.hypot(dx, dy) < MOVE_PX) return;
        // Touch/pen: movement before the hold completes is a page scroll.
        if (pointerType !== "mouse") {
          stopDrag(false);
          return;
        }
        startDrag();
      }
      e.preventDefault();
      const edge = 56;
      const scroller = document.scrollingElement || document.documentElement;
      if (e.clientY < edge) scroller.scrollBy(0, -20);
      else if (e.clientY > window.innerHeight - edge) scroller.scrollBy(0, 20);
      placeChicletAtY(wrap, btn, e.clientY);
    };

    const onUp = (e) => {
      if (pointerId != null && e.pointerId !== pointerId) return;
      stopDrag(true);
    };

    btn.addEventListener("pointerdown", (e) => {
      if (e.isPrimary === false) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (e.target.closest(".mc-collapse")) return;
      pointerType = e.pointerType || "mouse";
      pointerId = e.pointerId;
      originX = e.clientX;
      originY = e.clientY;
      dragging = false;
      btn.addEventListener("pointermove", onMove, { passive: false });
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
      if (pointerType !== "mouse") {
        holdTimer = setTimeout(startDrag, TOUCH_HOLD_MS);
      }
    });
  }

  function chicletBulletinHtml(tl) {
    const rows = tl?.bulletin || [];
    if (!rows.length) return "";
    const line = (r) => {
      const clock = escapeHtml(r.clock || `${r.minute}'`);
      if (r.kind === "sub") {
        const on = escapeHtml(r.player_on || r.player || "sub");
        const off = r.player_off
          ? ` <span class="mc-bl-arrow">←</span> ${escapeHtml(r.player_off)}`
          : "";
        return `<div class="mc-bl-row sub"><span class="mc-bl-min">${clock}</span><span class="mc-bl-who">${on}${off}</span></div>`;
      }
      const name = escapeHtml(r.player || (r.kind === "own_goal" ? "Own goal" : "Goal"));
      const tag =
        r.kind === "own_goal"
          ? ` <span class="mc-bl-tag og">OG</span>`
          : r.penalty
            ? ` <span class="mc-bl-tag pen">P</span>`
            : "";
      return `<div class="mc-bl-row ${r.kind === "own_goal" ? "og" : "goal"}"><span class="mc-bl-min">${clock}</span><span class="mc-bl-who">${name}${tag}</span></div>`;
    };
    const col = (side) => {
      const items = rows.filter((r) => r.team === side);
      return `<div class="mc-bulletin-col ${side}">${items.map(line).join("")}</div>`;
    };
    return `${col("home")}${col("away")}`;
  }

  // ---------------------------------------------------------------------------
  // Clinical power — goals per 100 xG from the finished-match archive, ranked by league
  // ---------------------------------------------------------------------------

  function teamKey(name) {
    return String(name || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function clinicalFor(leagueSlug, teamId, name) {
    const league = state.clinical?.leagues?.[leagueSlug];
    if (!league) return null;
    const rows = league.teams || [];
    const id = teamId ? String(teamId) : "";
    const byId = id ? rows.find((r) => String(r.team_id || "") === id) : null;
    if (byId) return { ...byId, _league: league };
    const key = teamKey(name);
    const byName = key ? rows.find((r) => r.key === key) : null;
    return byName ? { ...byName, _league: league } : null;
  }

  function clinicalTitle(row, name) {
    if (!row) return `${name}: no finished games archived yet for a clinical power`;
    const basis =
      row.basis === "xg"
        ? `${row.goals} goals from ${Number(row.xg).toFixed(2)} xG`
        : `${row.goals} goals from ${row.sot} on target (league par ${row._league?.par_conversion_pct || 0}%)`;
    const rank = row.rank ? `#${row.rank} of ${row._league?.teams_ranked || 0} in ${row._league?.label || row.league_chiclet}` : `unranked until ${state.clinical?.min_games || 2} games`;
    return `${row.team}: clinical power ${row.power} — ${basis} over ${row.games} game${row.games === 1 ? "" : "s"} · ${row.conversion_pct}% of shots on target scored · ${rank}. 100 = scores exactly what the chances were worth; above 100 is clinical, below is wasteful.`;
  }

  // One side (home or away) of a power row: number · rank · optional stat · tag.
  // spec: { title(row, name), num(row), rank(row), tag(row) → {cls, text}, stat(row) → text|"" }
  function powerSideHtml(m, side, spec) {
    const name = side === "home" ? m.home : m.away;
    const row = clinicalFor(m.league_slug, side === "home" ? m.home_id : m.away_id, name);
    const title = escapeHtml(spec.title(row, name));
    if (!row) {
      return `<span class="mc-power-side ${side} none" title="${title}"><b class="mc-power-num">—</b></span>`;
    }
    const rankNo = spec.rank(row);
    const rank = rankNo ? `#${rankNo}/${row._league?.teams_ranked || "?"}` : `${row.games} game${row.games === 1 ? "" : "s"} · n/r`;
    const tag = spec.tag(row);
    const stat = spec.stat ? spec.stat(row) : "";
    const statHtml = stat ? `<span class="mc-power-stat">${escapeHtml(stat)}</span>` : "";
    return `<span class="mc-power-side ${side} ${tag.cls}" title="${title}"><b class="mc-power-num">${spec.num(row)}</b><span class="mc-power-rank">${rank}</span>${statHtml}<span class="mc-power-tag">${escapeHtml(tag.text)}</span></span>`;
  }

  const CLINICAL_SPEC = {
    title: clinicalTitle,
    num: (r) => r.power,
    rank: (r) => r.rank,
    tag: (r) => (r.clinical ? { cls: "clinical", text: "clinical" } : { cls: "wasteful", text: "wasteful" }),
  };

  function offenseTitle(row, name) {
    if (!row) return `${name}: no finished games archived yet for an offence power`;
    const par = row._league?.par_rates || {};
    const pg = (v, d = 1) => Number(v || 0).toFixed(d);
    const parts = [];
    if (row.basis === "xg") parts.push(`${pg(row.xg_per_game, 2)} xG created per game (league ${pg(par.xg, 2)})`);
    parts.push(`${pg(row.shots_per_game)} shots (league ${pg(par.shots)})`);
    parts.push(`${pg(row.sot_per_game)} on target (league ${pg(par.sot)})`);
    parts.push(`${pg(row.corners_per_game)} corners (league ${pg(par.corners)})`);
    parts.push(`${pg(row.goals_per_game, 2)} goals (league ${pg(par.goals, 2)})`);
    const rank = row.offense_rank
      ? `#${row.offense_rank} of ${row._league?.teams_ranked || 0} in ${row._league?.label || row.league_chiclet}`
      : `unranked until ${state.clinical?.min_games || 2} games`;
    return `${row.team}: offence power ${row.offense_power} — ${parts.join(" · ")} over ${row.games} game${row.games === 1 ? "" : "s"} · ${rank}. Blend of chance creation, shot volume, pressure (corners) and goals vs the league; 100 = a league-typical attack, above 100 is potent, below is blunt.`;
  }

  const OFFENSE_SPEC = {
    title: offenseTitle,
    num: (r) => r.offense_power,
    rank: (r) => r.offense_rank,
    stat: (r) => `${Number(r.scored_per_game ?? r.goals_per_game ?? 0).toFixed(1)} scored/g`,
    tag: (r) => (r.potent ? { cls: "potent", text: "potent" } : { cls: "blunt", text: "blunt" }),
  };

  function defenseTitle(row, name) {
    if (!row) return `${name}: no finished games archived yet for a defence power`;
    const basis =
      row.basis === "xg"
        ? `allows ${Number(row.xga_per_game).toFixed(2)} xG per game (league ${Number(row._league?.par_xga_per_game || 0).toFixed(2)})`
        : `allows ${Number(row.sot_against_per_game).toFixed(1)} shots on target per game (league ${Number(row._league?.par_sot_against_per_game || 0).toFixed(1)})`;
    const rank = row.defense_rank
      ? `#${row.defense_rank} of ${row._league?.teams_ranked || 0} in ${row._league?.label || row.league_chiclet}`
      : `unranked until ${state.clinical?.min_games || 2} games`;
    return `${row.team}: defence power ${row.defense_power} — ${basis} over ${row.games} game${row.games === 1 ? "" : "s"} · ${Number(row.conceded_per_game).toFixed(2)} conceded per game · ${rank}. 100 = allows the league's typical chances; above 100 is solid (fewer / worse chances allowed), below is leaky.`;
  }

  const DEFENSE_SPEC = {
    title: defenseTitle,
    num: (r) => r.defense_power,
    rank: (r) => r.defense_rank,
    stat: (r) => `${Number(r.conceded_per_game || 0).toFixed(1)} allowed/g`,
    tag: (r) => (r.solid ? { cls: "solid", text: "solid" } : { cls: "leaky", text: "leaky" }),
  };

  function formWords(row) {
    const recent = row.recent || [];
    if (!recent.length) return "no results yet";
    return recent
      .map((g) => `${g.letter} ${g.gf}-${g.ga} ${g.venue === "home" ? "v" : "@"} ${g.opponent}`)
      .join(", ");
  }

  function momentumTitle(row, name) {
    if (!row) return `${name}: no finished games archived yet for a momentum reading`;
    const n = (row.recent || []).length;
    const rank = row.momentum_rank
      ? `#${row.momentum_rank} of ${row._league?.teams_ranked || 0} in ${row._league?.label || row.league_chiclet}`
      : `unranked until ${state.clinical?.min_games || 2} games`;
    return `${row.team}: momentum ${row.momentum} — last ${n} result${n === 1 ? "" : "s"} (oldest → newest): ${formWords(row)} · ${row.recent_points} pts, ${row.recent_scored}-${row.recent_allowed} on aggregate · season ${Number(row.points_per_game).toFixed(2)} pts/game (league ${Number(row._league?.par_points_per_game || 0).toFixed(2)}) · ${rank}. Recent points per game weighted toward the newest result, vs the league's; 100 = par form, above is rising, below fading.`;
  }

  const MOMENTUM_SPEC = {
    title: momentumTitle,
    num: (r) => r.momentum,
    rank: (r) => r.momentum_rank,
    stat: (r) => (r.form ? r.form.split("").join(" ") : ""),
    tag: (r) => (r.rising ? { cls: "rising", text: "rising" } : { cls: "fading", text: "fading" }),
  };

  function potentialTitle(row, name) {
    if (!row) return `${name}: no finished games archived yet for a potential reading`;
    const par = row._league?.par_rates || {};
    const creation =
      row.basis === "xg"
        ? `creates ${Number(row.xg_per_game).toFixed(2)} xG per game (league ${Number(par.xg || 0).toFixed(2)}) and allows ${Number(row.xga_per_game).toFixed(2)} (league ${Number(row._league?.par_xga_per_game || 0).toFixed(2)})`
        : `${Number(row.sot_per_game).toFixed(1)} shots on target per game (league ${Number(par.sot || 0).toFixed(1)}) and allows ${Number(row.sot_against_per_game).toFixed(1)} (league ${Number(row._league?.par_sot_against_per_game || 0).toFixed(1)})`;
    const rank = row.potential_rank
      ? `#${row.potential_rank} of ${row._league?.teams_ranked || 0} in ${row._league?.label || row.league_chiclet}`
      : `unranked until ${state.clinical?.min_games || 2} games`;
    const gapWord =
      row.potential_tag === "upside"
        ? "results lag the underlying numbers — should improve"
        : row.potential_tag === "overachieving"
          ? "results are running ahead of the underlying numbers"
          : "results match the underlying numbers";
    return `${row.team}: potential ${row.potential} — ${creation} over ${row.games} game${row.games === 1 ? "" : "s"}; on actual goals (${Number(row.scored_per_game).toFixed(2)} scored, ${Number(row.conceded_per_game).toFixed(2)} allowed per game) the same index reads ${row.results_power}, so ${gapWord} · ${rank}. Underlying strength from chance quality both ways with finishing luck stripped out; 100 = a league-typical side.`;
  }

  const POTENTIAL_SPEC = {
    title: potentialTitle,
    num: (r) => r.potential,
    rank: (r) => r.potential_rank,
    stat: (r) => `results ${r.results_power}`,
    tag: (r) => ({ cls: r.potential_tag || "steady", text: r.potential_tag || "steady" }),
  };

  function cleanSheetTitle(row, name) {
    if (!row) return `${name}: no finished games archived yet for a clean-sheet count`;
    const n = (row.recent || []).length;
    const rank = row.clean_sheet_rank
      ? `#${row.clean_sheet_rank} of ${row._league?.teams_ranked || 0} in ${row._league?.label || row.league_chiclet}`
      : `unranked until ${state.clinical?.min_games || 2} games`;
    const shutouts = (row.recent || []).filter((g) => g.ga === 0).map((g) => `${g.gf}-0 ${g.venue === "home" ? "v" : "@"} ${g.opponent}`);
    const recentWords = n ? `${row.recent_clean_sheets} in the last ${n}${shutouts.length ? ` (${shutouts.join(", ")})` : ""}` : "no recent results";
    return `${row.team}: ${row.clean_sheets} clean sheet${row.clean_sheets === 1 ? "" : "s"} in ${row.games} game${row.games === 1 ? "" : "s"} (${row.clean_sheet_pct}%; league ${row._league?.par_clean_sheet_pct ?? 0}% of team-games) · ${recentWords} · ${rank}. Ranked by total, ties to the club that needed fewer games.`;
  }

  const CLEAN_SHEET_SPEC = {
    title: cleanSheetTitle,
    num: (r) => r.clean_sheets,
    rank: (r) => r.clean_sheet_rank,
    stat: (r) => {
      const n = (r.recent || []).length;
      return n ? `${r.recent_clean_sheets}/${n} last ${n}` : "";
    },
    tag: (r) => ({ cls: r.tight ? "tight" : "porous", text: `${r.clean_sheet_pct}%` }),
  };

  const POWER_ROWS = [
    { spec: CLINICAL_SPEC, label: "⚡ clinical", help: "Clinical power: goals per 100 xG this season, ranked within the league. 100 = par." },
    {
      spec: OFFENSE_SPEC,
      label: "🎯 offence",
      help: "Offence power: chance creation (xG), shot volume, shots on target, pressure (corners) and goals per game vs the league, ranked within the league. 100 = par; higher creates more. Also shows goals scored per game.",
    },
    {
      spec: DEFENSE_SPEC,
      label: "🛡 defence",
      help: "Defence power: league-average xG allowed per game over this club's, ranked within the league. 100 = par; higher allows fewer / worse chances. Also shows goals allowed per game.",
    },
    {
      spec: MOMENTUM_SPEC,
      label: "📈 momentum",
      help: "Momentum: points per game over the last five results, weighted toward the newest, vs the league's points per game. 100 = par form; the letters are the recent results, oldest → newest.",
    },
    {
      spec: POTENTIAL_SPEC,
      label: "🔮 potential",
      help: "Potential: underlying strength from chance quality created and allowed, finishing luck stripped out. 100 = a league-typical side. 'results' is the same index on actual goals — upside when results lag it, overachieving when they run ahead.",
    },
    {
      spec: CLEAN_SHEET_SPEC,
      label: "🧤 clean sheets",
      help: "Clean sheets this season from the archived final scores: total, rank in the league, how many in the last five games, and the share of games kept clean (coloured against the league's share).",
    },
  ];

  function clinicalRowHtml(m) {
    if (!state.clinical) return "";
    return POWER_ROWS.map(
      (row) =>
        `${powerSideHtml(m, "home", row.spec)}<span class="mc-power-label" title="${escapeHtml(row.help)}">${row.label}</span>${powerSideHtml(m, "away", row.spec)}`,
    ).join("\n      ");
  }

  function paintClinicalRows() {
    document.querySelectorAll(".mc-power[data-power-for]").forEach((el) => {
      const card = el.closest(".match-chiclet");
      const m = card?.__match;
      if (!m) return;
      const html = clinicalRowHtml(m);
      if (el.innerHTML !== html) el.innerHTML = html;
    });
  }

  async function refreshClinical() {
    try {
      const board = await (await fetch("/api/clinical")).json();
      state.clinical = board;
      paintClinicalRows();
    } catch (err) {
      /* chiclets simply show — until the next poll */
    }
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
    const finished = isFinishedMatch(m);
    const upcoming = isUpcomingMatch(m);
    if (finished) btn.classList.add("match-chiclet-ft");
    if (upcoming) btn.classList.add("match-chiclet-pre");
    const badge = finished
      ? `<span class="mc-live-badge mc-ft-badge" title="${escapeHtml(m.clock ? `Ended at ${m.clock}` : "Full time")}"><span class="mc-clock-text">FT</span></span>`
      : upcoming
        ? `<span class="mc-live-badge mc-ko-badge" title="Kickoff"><span class="mc-clock-text">${escapeHtml(upcomingKick(m))}</span></span>`
        : `<span class="mc-live-badge"><span class="live-dot"></span><span class="mc-clock-text">${escapeHtml(m.clock || "LIVE")}</span></span>`;
    const chartPlaceholder = upcoming
      ? `<span class="mc-stats" data-stats-for="${escapeHtml(m.event_id)}"><span class="mc-stat mc-stat-empty">waiting for kickoff</span></span>`
      : withTimeline
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
        : "";
    btn.innerHTML = `
      <span class="mc-top">
        <span class="mc-top-left">
          ${withTimeline && !finished && !upcoming ? `<span class="mc-grip" title="Drag to reorder" aria-hidden="true">⠿</span>` : ""}
          ${badge}
          ${finished || upcoming ? `<span class="mc-ft-when">${escapeHtml(finished ? finishedWhen(m) : upcomingWhen(m))}</span>` : ""}
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
      <span class="mc-power" data-power-for="${escapeHtml(m.event_id)}">${clinicalRowHtml(m)}</span>
      ${withTimeline && !upcoming ? `<div class="mc-bulletin" data-bulletin-for="${escapeHtml(m.event_id)}">${cached ? chicletBulletinHtml(cached) : ""}</div>` : ""}
      ${chartPlaceholder}
    `;
    btn.addEventListener("click", () => {
      if (btn.__suppressClick) {
        btn.__suppressClick = false;
        return;
      }
      if (btn.__onSelect) btn.__onSelect(btn.__match);
    });
    bindCollapse(btn);
    return btn;
  }

  function finishedWhen(m) {
    if (!m?.start) return "Full time";
    const d = new Date(m.start);
    if (Number.isNaN(d.getTime())) return "Full time";
    const today = new Date();
    const sameDay =
      d.getFullYear() === today.getFullYear() &&
      d.getMonth() === today.getMonth() &&
      d.getDate() === today.getDate();
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return sameDay ? `Today ${time}` : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
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
        title.textContent = `${m.home} ${home}–${away} ${m.away} · ${matchClockLabel(m)}`;
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
      const finished = isFinishedMatch(m);
      const upcoming = isUpcomingMatch(m);
      const clockText = btn.querySelector(".mc-clock-text");
      // The 1s ticker owns the running clock; only overwrite for frozen states (HT/FT/KO)
      if (clockText && (!tl || tl.frozen || !Number.isFinite(Number(tl.elapsed_seconds)))) {
        clockText.textContent = finished ? "FT" : upcoming ? upcomingKick(m) : m.clock || "LIVE";
      }
      const shown = displayedScore(m, tl);
      applyChicletScore(btn, shown.home, shown.away);
      const names = btn.querySelectorAll(".mc-teams .mc-name");
      if (names.length === 2) {
        names[0].textContent = shortName(m.home);
        names[1].textContent = shortName(m.away);
      }
      // Full-time play-by-play is static and loads lazily on scroll — never re-poll it.
      // Upcoming games have no ESPN timeline until kickoff.
      if (refreshTimelines && !finished && !upcoming) {
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

  // A chart cut at 45' (0-0 HT tab) shows 15'/30' ticks instead of the HT mark.
  function chartAxis(tl) {
    const view = Number(tl.view_max_minute) || 0;
    const maxM = view > 0 ? view : Math.max(90, Number(tl.max_minute) || 90);
    const ticks = maxM <= 45 ? [15, 30] : [45];
    return { maxM, ticks, now: Math.min(maxM, liveNowMinutes(tl)) };
  }

  function timelineSvg(tl) {
    const W = 320;
    const H = 68;
    const pad = 10;
    const axisY = 32;
    const { maxM, ticks, now } = chartAxis(tl);
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
      const playerBit = ev.player ? ` · ${ev.player}` : "";
      const title = `${ev.clock || ev.minute + "'"} ${ev.kind}${playerBit}${xgBit} — ${ev.text || ""}`;
      if (ev.kind === "goal") {
        const y1 = home ? axisY - 24 : axisY + 2;
        const y2 = home ? axisY - 2 : axisY + 24;
        const cy = axisY + dir * 13;
        marks.push(
          `<g class="tl-goal"><title>${escapeHtml(title)}</title><line x1="${x}" y1="${y1}" x2="${x}" y2="${y2}"/><circle cx="${x}" cy="${cy}" r="4"/></g>`
        );
      } else if (ev.kind === "own_goal") {
        const y1 = home ? axisY - 24 : axisY + 2;
        const y2 = home ? axisY - 2 : axisY + 24;
        const cy = axisY + dir * 13;
        const labelY = (cy + dir * 9).toFixed(1);
        marks.push(
          `<g class="tl-og"><title>${escapeHtml(title)}</title><line x1="${x}" y1="${y1}" x2="${x}" y2="${y2}"/><circle cx="${x}" cy="${cy}" r="4"/><text x="${x}" y="${labelY}" class="tl-og-label" text-anchor="middle">OG</text></g>`
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
    const tickMarks = ticks
      .map((t) => {
        const tx = xAt(t).toFixed(1);
        return `<line x1="${tx}" y1="${axisY - 6}" x2="${tx}" y2="${axisY + 6}" class="tl-ht"/>
      <text x="${tx}" y="${H - 4}" class="tl-label" text-anchor="middle">${t}'</text>`;
      })
      .join("");
    return `<svg class="mc-tl-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="0 to ${maxM} minute event timeline" data-pad-l="${pad}" data-pad-r="${pad}" data-width="${W}" data-max="${maxM}">
      <line x1="${pad}" y1="${axisY}" x2="${W - pad}" y2="${axisY}" class="tl-axis"/>
      <line x1="${pad}" y1="${axisY}" x2="${nowX}" y2="${axisY}" class="tl-progress"/>
      ${tickMarks}
      <text x="${pad}" y="${H - 4}" class="tl-label">0'</text>
      <text x="${W - pad}" y="${H - 4}" class="tl-label" text-anchor="end">${maxM}'</text>
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
    const { maxM, ticks, now } = chartAxis(tl);
    const xg = tl.xg || { home: [], away: [], home_total: 0, away_total: 0 };
    const yMax = Math.max(0.5, xg.home_total || 0, xg.away_total || 0) * 1.15;
    const xAt = (m) => padL + ((Number(m) / maxM) * (W - padL - padR));
    const yAt = (v) => padT + ((yMax - Number(v)) / yMax) * (H - padT - padB);
    const homePath = xgSeriesPath(xg.home, xAt, yAt, now);
    const awayPath = xgSeriesPath(xg.away, xAt, yAt, now);
    const nowX = xAt(now).toFixed(1);
    const y0 = yAt(0).toFixed(1);
    const yMid = yAt(yMax / 2).toFixed(1);
    const yTop = yAt(yMax).toFixed(1);
    const tickMarks = ticks
      .map((t) => {
        const tx = xAt(t).toFixed(1);
        return `<line x1="${tx}" y1="${padT}" x2="${tx}" y2="${y0}" class="tl-ht"/>
      <text x="${tx}" y="${H - 4}" class="tl-label" text-anchor="middle">${t}'</text>`;
      })
      .join("");
    return `<svg class="mc-xg-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Expected goals versus game time" data-pad-l="${padL}" data-pad-r="${padR}" data-width="${W}" data-max="${maxM}">
      <text x="4" y="${Number(yTop) + 3}" class="tl-label">${yMax.toFixed(1)}</text>
      <text x="4" y="${Number(yMid) + 3}" class="tl-label">${(yMax / 2).toFixed(1)}</text>
      <text x="4" y="${Number(y0) + 3}" class="tl-label">0</text>
      <line x1="${padL}" y1="${yTop}" x2="${W - padR}" y2="${yTop}" class="tl-grid"/>
      <line x1="${padL}" y1="${yMid}" x2="${W - padR}" y2="${yMid}" class="tl-grid"/>
      <line x1="${padL}" y1="${y0}" x2="${W - padR}" y2="${y0}" class="tl-axis"/>
      ${tickMarks}
      <line x1="${nowX}" y1="${padT}" x2="${nowX}" y2="${y0}" class="tl-now"/>
      ${homePath ? `<path d="${homePath}" class="xg-home" fill="none"/>` : ""}
      ${awayPath ? `<path d="${awayPath}" class="xg-away" fill="none"/>` : ""}
      <text x="${padL}" y="${H - 4}" class="tl-label">0'</text>
      <text x="${W - padR}" y="${H - 4}" class="tl-label" text-anchor="end">${maxM}'</text>
      <text x="${W - padR}" y="11" class="tl-xg-total" text-anchor="end">xG <tspan class="tl-xg-h">${Number(xg.home_total || 0).toFixed(2)}</tspan>–<tspan class="tl-xg-a">${Number(xg.away_total || 0).toFixed(2)}</tspan></text>
    </svg>`;
  }

  function pressureHeadline(tl) {
    const p = tl.pressure;
    if (!p || p.label === "quiet") return "";
    const homeName = shortName(tl.home || "Home");
    const awayName = shortName(tl.away || "Away");
    const win = `last ${p.window || 15}'`;
    if (p.leader === "home") return `${homeName} pressing high — ${awayName} pinned back (${win})`;
    if (p.leader === "away") return `${awayName} pressing high — ${homeName} pinned back (${win})`;
    return "";
  }

  function pressureHtml(tl) {
    const p = tl.pressure;
    if (!p) return "";
    const homeName = shortName(tl.home || "Home");
    const awayName = shortName(tl.away || "Away");
    const win = p.window || 15;
    if (p.label === "quiet" || p.share?.home == null) {
      return `<span class="mc-pressure">
        <span class="mc-pressure-head">Pressure · last ${win}'</span>
        <span class="mc-pressure-meta">Reading the last ${win}'…</span>
      </span>`;
    }
    const h = Math.round(p.share.home * 100);
    const a = Math.max(0, 100 - h);
    const lead = p.leader === "home" ? p.home : p.leader === "away" ? p.away : null;
    const leadName = p.leader === "home" ? homeName : awayName;
    const meta = lead
      ? `${escapeHtml(leadName)} · ${lead.final_third} final-third · ${lead.box} in box · ${lead.corners} corners · ${lead.shots} shots`
      : `Even — ${p.home.final_third} vs ${p.away.final_third} final-third actions`;
    return `<span class="mc-pressure${p.leader ? ` lead-${p.leader}` : ""}">
      <span class="mc-pressure-head">Pressure · last ${win}'</span>
      <span class="mc-pressure-row">
        <b class="mc-pressure-h">${h}%</b>
        <span class="mc-pressure-bar" aria-hidden="true">
          <i class="mc-pressure-seg-h" style="width:${h}%"></i>
          <i class="mc-pressure-seg-a" style="width:${a}%"></i>
        </span>
        <b class="mc-pressure-a">${a}%</b>
      </span>
      <span class="mc-pressure-meta">${meta}</span>
    </span>`;
  }

  function territoryLabel(tl) {
    const terr = tl.territory;
    if (!terr) return "";
    const fromPressure = pressureHeadline(tl);
    if (fromPressure) return fromPressure;
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
    </svg>${pressureHtml(tl)}`;
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
      const board = document.querySelector(`.mc-bulletin[data-bulletin-for="${CSS.escape(String(m.event_id))}"]`);
      if (board) board.innerHTML = chicletBulletinHtml(tl);
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
        events: (tl.events || []).map((e) => [e.minute, e.kind, e.team, e.xg, e.player]),
        bulletin: (tl.bulletin || []).map((e) => [e.minute, e.kind, e.team, e.player, e.player_off]),
        xh: tl.xg?.home_total,
        xa: tl.xg?.away_total,
        fouls: [tl.counts?.home_foul, tl.counts?.away_foul],
        terr: tl.territory?.total,
        press: [tl.pressure?.to_minute, tl.pressure?.home?.final_third, tl.pressure?.away?.final_third],
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
      if (isFinishedMatch(btn.__match) || isUpcomingMatch(btn.__match)) continue;
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
    const scope = state.liveScope || "live";
    renderLeagueChiclets(
      "#leagueChiclets",
      "liveFilter",
      () => {
        renderLiveTabChiclets();
      },
      scope
    );
    renderMatchChiclets("#matchChiclets", state.liveFilter, state.selectedLive, selectLiveMatch, {
      withTimeline: true,
      soft: !!opts.soft,
      refreshTimelines: opts.refreshTimelines,
      scope,
    });
    document.querySelectorAll(".scope-btn").forEach((b) => {
      b.classList.toggle("on", b.dataset.scope === scope);
      b.setAttribute("aria-pressed", b.dataset.scope === scope ? "true" : "false");
    });
  }

  function setLiveScope(scope) {
    if (scope !== "live" && scope !== "finished" && scope !== "upcoming") return;
    if (scope === state.liveScope) return;
    persistLiveScope(scope);
    // League filters belong to a scope — a filter with no rows in the new scope is just confusing.
    state.liveFilter = null;
    renderLiveTabChiclets();
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
    $("#pitchTitle").textContent = `${m.home} ${m.home_score}–${m.away_score} ${m.away} · ${matchClockLabel(m)}`;
    if (state.trackTimer) clearInterval(state.trackTimer);
    if (isUpcomingMatch(m)) {
      state.trackTimer = null;
      const svg = $("#pitchSvg");
      if (svg) svg.replaceChildren();
      $("#pitchStats").innerHTML = "";
      $("#pitchFeed").innerHTML = `<p class="lede">Kickoff ${escapeHtml(upcomingWhen(m))} — pitch tracking starts when the match goes live.</p>`;
      return;
    }
    await refreshTrack();
    // Nothing moves after full time — no point polling the pitch feed.
    state.trackTimer = isFinishedMatch(m) ? null : setInterval(refreshTrack, 5000);
  }

  function liveQuery(m) {
    return new URLSearchParams({
      league: m.league_slug,
      event_id: m.event_id,
      home: m.home,
      away: m.away,
      hs: String(m.home_score),
      as: String(m.away_score),
      // ESPN leaves post-match clocks at "90'+6'"; tell the server it's full time.
      clock: isFinishedMatch(m) ? "FT" : m.clock || "",
      clock_s: m.clock_seconds != null ? String(m.clock_seconds) : "",
      chiclet: m.league_chiclet || "",
      league_name: m.league_name || "",
      start: m.start || "",
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
        s.own_goal || s.type === "own-goal"
          ? "shot-og"
          : s.type === "goal" || s.type === "penalty-goal"
            ? "shot-goal"
            : s.type === "shot-on-target"
              ? "shot-on"
              : "shot-off";
      add("circle", { cx: x, cy: y, r: kind === "shot-goal" || kind === "shot-og" ? 8 : 6, class: kind });
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
        `${track.home} ${track.home_score}–${track.away_score} ${track.away} · ${matchClockLabel(state.selectedLive || track)}` +
        ` · ball @ ${Math.round(track.ball.x)},${Math.round(track.ball.y)} (${track.ball.type})`;
    }
  }

  async function refreshTrack() {
    const m = state.selectedLive;
    if (!m || isUpcomingMatch(m)) return;
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

  const WP_BUCKETS = ["<50", "50-55", ">55-60", ">60"];

  function wpRecordLine(rec) {
    if (!rec || !rec.total) return "—";
    return `${rec.correct}–${rec.wrong} (${wpPct(rec.pct)})`;
  }

  function wpBucketClass(key) {
    if (key === ">60") return "wp-b-hi";
    if (key === ">55-60") return "wp-b-mid";
    if (key === "50-55") return "wp-b-ok";
    return "wp-b-lo";
  }

  function wpBucketChip(key, rec, league, windowName) {
    const empty = !rec || !rec.total;
    const label = empty ? `${key} —` : `${key} ${rec.correct}–${rec.wrong} (${wpPct(rec.pct)})`;
    const focus = state.wpFocus;
    const on =
      focus &&
      focus.kind === "bucket" &&
      focus.bucket === key &&
      String(focus.league || "") === String(league || "") &&
      (focus.window || "last30") === (windowName || "last30");
    return `<button type="button" class="wp-bucket ${wpBucketClass(key)}${on ? " on" : ""}${
      empty ? " dim" : ""
    }" data-bucket="${escapeHtml(key)}" data-league="${escapeHtml(league || "")}" data-window="${escapeHtml(
      windowName || "last30"
    )}" ${empty ? "disabled" : ""}>${escapeHtml(label)}</button>`;
  }

  function wpBucketRow(buckets, league, windowName) {
    const map = buckets || {};
    return `<div class="wp-bucket-row" role="toolbar" aria-label="Pick confidence buckets">${WP_BUCKETS.map(
      (key) => wpBucketChip(key, map[key], league, windowName)
    ).join("")}</div>`;
  }

  function wpDailyChartSvg(days, focusDate) {
    const rows = days || [];
    if (!rows.length) {
      return `<p class="lede empty-live">No graded matchdays in this window yet.</p>`;
    }
    const W = 720;
    const H = 168;
    const padL = 36;
    const padR = 10;
    const padT = 22;
    const padB = 36;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const n = rows.length;
    const gap = n > 20 ? 2 : 4;
    const barW = Math.max(4, (innerW - gap * (n - 1)) / n);
    const yAt = (pct) => padT + innerH * (1 - pct);
    const ticks = [0, 0.25, 0.5, 0.75, 1];
    const grid = ticks
      .map((t) => {
        const y = yAt(t).toFixed(1);
        const cls = t === 0.5 ? "wp-chart-mid" : "wp-chart-grid";
        return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" class="${cls}"/>
          <text x="${padL - 6}" y="${(Number(y) + 3).toFixed(1)}" class="wp-chart-tick" text-anchor="end">${Math.round(
            t * 100
          )}%</text>`;
      })
      .join("");
    const bars = rows
      .map((d, i) => {
        const pct = d.total ? Number(d.pct) || 0 : 0;
        const x = padL + i * (barW + gap);
        const h = innerH * pct;
        const y = yAt(pct);
        const on = focusDate && focusDate === d.date;
        const tipBuckets = WP_BUCKETS.map((key) => {
          const b = (d.buckets || {})[key];
          if (!b || !b.total) return `${key}: —`;
          return `${key}: ${b.correct}–${b.wrong} (${wpPct(b.pct)})`;
        }).join(" · ");
        const title = `${d.date} — ${d.correct} of ${d.total} (${wpPct(d.pct)}) · ${tipBuckets}`;
        const label = n <= 16 || i % 2 === 0 ? d.date.slice(5) : "";
        return `<g class="wp-chart-bar${on ? " on" : ""}" data-date="${escapeHtml(d.date)}" role="button" tabindex="0">
          <title>${escapeHtml(title)}</title>
          <rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(
            1.2,
            h
          ).toFixed(1)}"/>
          <text x="${(x + barW / 2).toFixed(1)}" y="${Math.max(padT - 4, y - 4).toFixed(
            1
          )}" class="wp-chart-frac" text-anchor="middle">${d.correct}/${d.total}</text>
          ${
            label
              ? `<text x="${(x + barW / 2).toFixed(1)}" y="${H - 8}" class="wp-chart-x" text-anchor="middle">${escapeHtml(
                  label
                )}</text>`
              : ""
          }
        </g>`;
      })
      .join("");
    return `<svg class="wp-daily-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Daily WinProb hit rate">${grid}${bars}</svg>`;
  }

  function wpFocusBanner(focus, rec) {
    if (!focus) return "";
    if (focus.kind === "day") {
      const day = (rec.daily || []).find((d) => d.date === focus.date);
      if (!day) return "";
      return `<div class="wp-focus-banner">
        <div>
          <span class="wp-focus-title">${escapeHtml(day.date)} · ${escapeHtml(wpRecordLine(day))}</span>
          <span class="wp-focus-sub">${WP_BUCKETS.map((key) => {
            const b = (day.buckets || {})[key];
            return b && b.total ? `${key} ${b.correct}–${b.wrong}` : "";
          })
            .filter(Boolean)
            .join(" · ")}</span>
        </div>
        <button type="button" class="wp-focus-clear">Clear</button>
      </div>`;
    }
    const league = focus.league;
    const block = league
      ? (rec.leagues || []).find((g) => g.chiclet === league || g.slug === league)
      : rec;
    const buckets = (focus.window === "season" ? block?.season : block?.last30)?.buckets || {};
    const b = buckets[focus.bucket];
    const who = league || (focus.window === "season" ? "Season" : "All");
    return `<div class="wp-focus-banner">
      <div>
        <span class="wp-focus-title">${escapeHtml(who)} · ${escapeHtml(focus.bucket)} · ${escapeHtml(
          wpRecordLine(b)
        )}</span>
        <span class="wp-focus-sub">Click a chip or bar to pin those graded picks</span>
      </div>
      <button type="button" class="wp-focus-clear">Clear</button>
    </div>`;
  }

  function filterWpPicks(picks, focus, rec) {
    const rows = picks || [];
    const cutoff = rec?.cutoff || "";
    if (!focus) {
      const recent = cutoff ? rows.filter((r) => r.date >= cutoff) : rows;
      return recent.slice(-20).reverse();
    }
    return rows
      .filter((r) => {
        if (focus.kind === "day") return r.date === focus.date;
        if (focus.league && r.league_chiclet !== focus.league && r.league !== focus.league) {
          return false;
        }
        if ((focus.window || "last30") !== "season" && cutoff && r.date < cutoff) return false;
        return (r.bucket || "") === focus.bucket;
      })
      .slice()
      .reverse();
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
        ? `last ${rec.window_days || 30} days (to ${rec.anchor})`
        : `last ${rec.window_days || 30} days`;
    const last30 = rec.last30 || {};
    const daysWith = (rec.daily || []).length;
    const leagueRows = (rec.leagues || [])
      .map((g) => {
        const line = g.last30?.total
          ? `${g.last30.correct}–${g.last30.wrong} (${wpPct(g.last30.pct)})`
          : "no graded picks";
        return `<div class="wp-league-row">
          <span class="league-chiclet-tag">${escapeHtml(g.chiclet)}</span>
          <span class="wp-league-line">${escapeHtml(line)}</span>
          ${wpBucketRow((g.last30 && g.last30.buckets) || {}, g.chiclet, "last30")}
        </div>`;
      })
      .join("");

    const focusDate = state.wpFocus && state.wpFocus.kind === "day" ? state.wpFocus.date : "";
    $("#wpRecord").innerHTML = `
      <div class="wp-record-board">
        <div class="wp-record-head">
          <span class="wp-record-label">WinProb record — ${escapeHtml(windowLabel)}</span>
          <span class="wp-record-meta">${daysWith} days with picks · ${last30.total || 0} picks</span>
        </div>
        <div class="wp-record-main">${escapeHtml(wpRecordLine(last30))}</div>
        ${wpBucketRow(last30.buckets || {}, "", "last30")}
        <div class="wp-record-season">season ${escapeHtml(wpRecordLine(rec.season))}</div>
        ${
          rec.season?.buckets
            ? `<div class="wp-season-buckets">${wpBucketRow(rec.season.buckets, "", "season")}</div>`
            : ""
        }
        <div class="wp-league-records">${leagueRows || ""}</div>
        <div class="wp-daily-chart" id="wpDailyChart">${wpDailyChartSvg(rec.daily || [], focusDate)}</div>
        ${wpFocusBanner(state.wpFocus, rec)}
      </div>`;
    bindWpRecord($("#wpRecord"), rec);

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
      const groupKey = `group:wp:${day}`;
      const block = document.createElement("div");
      block.className = "match-chiclet-league";
      block.dataset.groupKey = groupKey;
      block.innerHTML = `<div class="match-chiclet-league-label" role="button" tabindex="0" title="Collapse or expand this day"><span class="league-chiclet-tag">${escapeHtml(day)}</span> ${list.length} scheduled${collapseToggleHtml(groupKey)}</div>`;
      const wrap = document.createElement("div");
      wrap.className = "match-chiclet-row";
      for (const f of list) wrap.appendChild(buildWpChiclet(f));
      block.appendChild(wrap);
      bindGroupCollapse(block, groupKey);
      grid.appendChild(block);
    }

    const picks = rec.picks || data.recent_results || [];
    const focused = filterWpPicks(picks, state.wpFocus, rec);
    const title = state.wpFocus ? "Pinned graded picks" : "Recent graded picks";
    renderWpResults(focused, title);
  }

  function bindWpRecord(root, rec) {
    if (!root) return;
    root.querySelectorAll(".wp-bucket:not([disabled])").forEach((btn) => {
      btn.addEventListener("click", () => {
        const bucket = btn.dataset.bucket;
        const league = btn.dataset.league || "";
        const next = {
          kind: "bucket",
          bucket,
          league,
          window: btn.dataset.window || "last30",
        };
        const cur = state.wpFocus;
        const same =
          cur &&
          cur.kind === "bucket" &&
          cur.bucket === next.bucket &&
          String(cur.league || "") === String(next.league || "") &&
          (cur.window || "last30") === next.window;
        state.wpFocus = same ? null : next;
        renderWinprob();
      });
    });
    root.querySelectorAll(".wp-chart-bar").forEach((g) => {
      const activate = () => {
        const date = g.getAttribute("data-date");
        const cur = state.wpFocus;
        state.wpFocus = cur && cur.kind === "day" && cur.date === date ? null : { kind: "day", date };
        renderWinprob();
      };
      g.addEventListener("click", activate);
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      });
    });
    root.querySelector(".wp-focus-clear")?.addEventListener("click", () => {
      state.wpFocus = null;
      renderWinprob();
    });
  }

  function renderWpResults(rows, title) {
    const box = $("#wpResults");
    if (!rows.length) {
      box.innerHTML = state.wpFocus
        ? `<div class="concede-title">${escapeHtml(title || "Pinned graded picks")}</div><p class="lede empty-live">No picks in this slice.</p>`
        : "";
      return;
    }
    const items = rows
      .map((r) => {
        const mark = r.correct
          ? `<span class="wp-mark hit" title="Pick was right">✓</span>`
          : `<span class="wp-mark miss" title="Pick was wrong">✗</span>`;
        const league = r.league_chiclet
          ? `<span class="wp-result-league">${escapeHtml(r.league_chiclet)}</span>`
          : "";
        return `<div class="wp-result-row${r.correct ? " hit" : " miss"}">
          ${mark}
          <span class="date">${escapeHtml(r.date)}</span>
          ${league}
          <span class="teams">${escapeHtml(r.home)} vs ${escapeHtml(r.away)}</span>
          <span class="meta">picked <b>${escapeHtml(r.pick === "draw" ? "Draw" : r.pick_team)}</b> ${wpPct(r.pick_prob)} · ${escapeHtml(r.bucket || "")} · FT ${escapeHtml(r.ft)}</span>
        </div>`;
      })
      .join("");
    box.innerHTML = `<div class="concede-title">${escapeHtml(title || "Recent graded picks")}</div>${items}`;
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

  function localTodayIso() {
    const d = new Date();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${mm}-${dd}`;
  }

  function parseMatchDay(m) {
    if (m.start) {
      const d = new Date(m.start);
      if (!Number.isNaN(d.getTime())) {
        const mm = String(d.getMonth() + 1).padStart(2, "0");
        const dd = String(d.getDate()).padStart(2, "0");
        return `${d.getFullYear()}-${mm}-${dd}`;
      }
    }
    if (m.iso_date) return m.iso_date;
    const raw = String(m.date || "").trim();
    const hit = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
    if (hit) return `${hit[3]}-${hit[2].padStart(2, "0")}-${hit[1].padStart(2, "0")}`;
    return "";
  }

  function isTodayMatch(m) {
    const day = parseMatchDay(m);
    return Boolean(day) && day === localTodayIso();
  }

  function matchKickoffLabel(m) {
    if (!m.start) return "Scheduled";
    const d = new Date(m.start);
    if (Number.isNaN(d.getTime())) return "Scheduled";
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function appendMatchSection(list, title) {
    const head = document.createElement("div");
    head.className = "match-day-label";
    head.textContent = title;
    list.appendChild(head);
  }

  function appendMatchRow(list, m) {
    const selectable = Boolean(m.match_id) && !m.scheduled;
    const el = document.createElement(selectable ? "button" : "div");
    if (selectable) el.type = "button";
    el.className =
      "match-row" +
      (isTodayMatch(m) ? " today-row" : "") +
      (m.scheduled ? " scheduled-row" : "") +
      (m.match_id === state.selectedId ? " selected" : "");
    const meta = m.scheduled
      ? `${matchKickoffLabel(m)} · scheduled`
      : `FT ${escapeHtml(m.ft)} · ${escapeHtml(m.shots)}`;
    el.innerHTML = `
      <span class="date">${escapeHtml(m.date)}</span>
      <span class="teams">${escapeHtml(m.home)} vs ${escapeHtml(m.away)}</span>
      <span class="meta">${meta}</span>
    `;
    if (selectable) {
      el.addEventListener("click", () => selectMatch(m.match_id, true));
    }
    list.appendChild(el);
  }

  function renderMatches() {
    const q = ($("#matchFilter").value || "").trim().toLowerCase();
    const list = $("#matchList");
    list.innerHTML = "";
    const rows = state.matches.filter((m) => {
      if (m.is_preset) return false;
      if (!q) return true;
      return `${m.home} ${m.away} ${m.date}`.toLowerCase().includes(q);
    });
    const todayRows = rows
      .filter(isTodayMatch)
      .sort((a, b) => (a.start || "").localeCompare(b.start || "") || (a.home || "").localeCompare(b.home || ""));
    const restRows = rows
      .filter((m) => !isTodayMatch(m) && !m.scheduled)
      .sort((a, b) => (parseMatchDay(b) || "").localeCompare(parseMatchDay(a) || "") || (a.home || "").localeCompare(b.home || ""));
    if (todayRows.length) {
      appendMatchSection(list, "Today");
      for (const m of todayRows) appendMatchRow(list, m);
    }
    if (restRows.length) {
      if (todayRows.length) appendMatchSection(list, "All matches");
      for (const m of restRows) appendMatchRow(list, m);
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

  // ---------------------------------------------------------------------------
  // 0-0 HT tab — archived goalless first halves + live lookalike matching
  // ---------------------------------------------------------------------------

  function stopHalftimeTimer() {
    if (state.htTimer) clearInterval(state.htTimer);
    state.htTimer = null;
  }

  function htIsActive() {
    const panel = $("#panel-halftime");
    return !!panel && !panel.hidden;
  }

  function htWhen(rec) {
    const d = rec?.start ? new Date(rec.start) : null;
    if (!d || Number.isNaN(d.getTime())) return rec?.date || "";
    return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }

  function htStatPair(label, h, a) {
    return `<span class="mc-stat" title="${label} — home vs away"><span class="mc-stat-label">${label}</span><b class="mc-h">${h}</b><span class="mc-stat-sep">–</span><b class="mc-a">${a}</b></span>`;
  }

  function htCutStats(fh) {
    const h = fh?.home || {};
    const a = fh?.away || {};
    const xg = fh?.has_xg === false ? htStatPair("xG", "—", "—") : htStatPair("xG", Number(h.xg || 0).toFixed(2), Number(a.xg || 0).toFixed(2));
    return (
      htStatPair("Shots", h.shots || 0, a.shots || 0) +
      htStatPair("On target", h.sot || 0, a.sot || 0) +
      htStatPair("Corners", h.corners || 0, a.corners || 0) +
      xg
    );
  }

  function htXgChart(tl, hasXg) {
    if (hasXg === false) {
      return `<span class="mc-xg ht-no-xg" title="ESPN publishes no expected-goals feed for this competition">no xG feed for this league</span>`;
    }
    return `<span class="mc-xg" aria-label="First-half expected goals">${xgSvg(tl)}</span>`;
  }

  // p10–p90 range drawn as a bar on a fixed scale, median as a tick.
  function htBandLine(label, band, digits, scaleMax) {
    if (!band) return "";
    const f = (v) => Number(v || 0).toFixed(digits);
    const pct = (v) => Math.max(0, Math.min(100, (Number(v || 0) / scaleMax) * 100));
    const left = pct(band.p10);
    const width = Math.max(1.5, pct(band.p90) - left);
    return `<div class="ht-band">
      <span class="ht-band-label">${label}</span>
      <span class="ht-band-bar" aria-hidden="true"><i style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%"></i><em style="left:${pct(band.p50).toFixed(1)}%"></em></span>
      <span class="ht-band-range"><b>${f(band.p10)}</b>–<b>${f(band.p90)}</b></span>
      <span class="ht-band-med">med ${f(band.p50)}</span>
    </div>`;
  }

  function htOutcomeBar(o) {
    if (!o) return "";
    const h = Number(o.home_win_pct) || 0;
    const d = Number(o.draw_pct) || 0;
    const a = Math.max(0, 100 - h - d);
    return `<div class="ht-outcome-bar" role="img" aria-label="Full-time results: home ${h}%, draw ${d}%, away ${a}%">
      <i class="home" style="width:${h}%"><span>H ${h}%</span></i><i class="draw" style="width:${d}%"><span>D ${d}%</span></i><i class="away" style="width:${a}%"><span>A ${a}%</span></i>
    </div>`;
  }

  function htOutcomePills(o) {
    if (!o) return "";
    const fg = o.first_2h_goal_pct || {};
    return `<div class="ht-pills">
      <span class="ht-pill"><b>${o.any_2h_goal_pct}%</b> saw a goal after the break</span>
      <span class="ht-pill"><b>${o.ended_0_0_pct}%</b> stayed 0-0</span>
      <span class="ht-pill"><b>${o.over_1_5_pct}%</b> went over 1.5</span>
      <span class="ht-pill"><b>${Number(o.avg_2h_goals || 0).toFixed(2)}</b> 2H goals avg</span>
      <span class="ht-pill" title="Minute window of the first second-half goal, share of all 0-0 halves">1st goal <b>46–60′ ${fg["46-60"] || 0}%</b> · <b>61–75′ ${fg["61-75"] || 0}%</b> · <b>76–90′ ${fg["76-90+"] || 0}%</b></span>
      ${o.with_first_goal ? `<span class="ht-pill" title="When a goal did come, how often the side with more first-half xG scored it"><b>${o.xg_leader_scored_first_pct}%</b> first goal to the xG leader</span>` : ""}
    </div>`;
  }

  function htProfileHtml(p, opts = {}) {
    if (!p || !p.n) {
      return `<div class="ht-card ht-empty">${escapeHtml(opts.empty || "No 0-0 first halves archived yet — full-time games land here as they finish, or hit Backfill.")}</div>`;
    }
    const hasXg = Number(p.n_xg) > 0;
    const curve = hasXg
      ? (p.xg_curve || [])
          .map((c) => `<span class="ht-pill">by ${c.minute}′ xG <b>${Number(c.p10).toFixed(2)}–${Number(c.p90).toFixed(2)}</b></span>`)
          .join("") +
        (p.n_xg < p.n ? `<span class="ht-pill" title="Leagues without an ESPN xG feed are left out of the xG bands">xG from <b>${p.n_xg}</b> halves with an xG feed</span>` : "")
      : `<span class="ht-pill">no xG feed for these leagues</span>`;
    const s = p.sides || {};
    const xgSide = (side) => (hasXg ? ` · xG ${Number(s[side]?.xg || 0).toFixed(2)}` : "");
    const minuteBit = p.minute && p.minute < 45 ? ` · cut at ${p.minute}′` : "";
    return `<div class="ht-card">
      <div class="ht-card-head">
        <span class="ht-card-title">${escapeHtml(opts.title || "What a 0-0 first half looks like")}</span>
        <span class="ht-card-n">${p.n} halves${minuteBit}</span>
      </div>
      <div class="ht-card-body">
        <div class="ht-bands">
          <div class="ht-sub">80% of them fall inside these bands · both teams combined · p10–p90</div>
          ${htBandLine("Shots", p.bands?.shots, 0, 20)}
          ${htBandLine("On target", p.bands?.sot, 0, 8)}
          ${htBandLine("Blocked", p.bands?.blocked, 0, 8)}
          ${htBandLine("Corners", p.bands?.corners, 0, 12)}
          ${hasXg ? htBandLine("xG", p.bands?.xg, 2, 2) : ""}
          <div class="ht-sides">
            <span class="mc-h">home avg ${Number(s.home?.shots || 0).toFixed(1)} sh · ${Number(s.home?.sot || 0).toFixed(1)} sot${xgSide("home")}</span>
            <span class="mc-a">away avg ${Number(s.away?.shots || 0).toFixed(1)} sh · ${Number(s.away?.sot || 0).toFixed(1)} sot${xgSide("away")}</span>
          </div>
          <div class="ht-pills">${curve}</div>
        </div>
        <div class="ht-outcomes">
          <div class="ht-sub">After the break · full-time results</div>
          ${htOutcomeBar(p.outcomes)}
          ${htOutcomePills(p.outcomes)}
        </div>
      </div>
    </div>`;
  }

  function htGoalsHtml(goals) {
    if (!goals || !goals.length) return `<span class="ht-g none">stayed 0-0</span>`;
    return goals
      .map((g) => {
        const who = g.player ? shortName(g.player) : g.kind === "own_goal" ? "Own goal" : "Goal";
        const tag = g.kind === "own_goal" ? ` <span class="mc-bl-tag og">OG</span>` : "";
        return `<span class="ht-g ${g.team === "away" ? "away" : "home"}"><span class="ht-g-min">${escapeHtml(g.clock || `${g.minute}'`)}</span> ${escapeHtml(who)}${tag}</span>`;
      })
      .join("");
  }

  function htArchiveCard(rec) {
    const tl = rec.timeline || {};
    return `<div class="match-chiclet match-chiclet-tl match-chiclet-ft ht-chiclet" role="listitem" data-event-id="${escapeHtml(rec.event_id || "")}">
      <span class="mc-top">
        <span class="mc-top-left">
          <span class="mc-live-badge mc-ft-badge" title="Level at the break"><span class="mc-clock-text">HT 0-0</span></span>
          <span class="mc-ft-when">${escapeHtml(htWhen(rec))}</span>
        </span>
        <span class="mc-top-right"><span class="mc-league">${escapeHtml(rec.league_chiclet || "")}</span></span>
      </span>
      <span class="mc-teams">
        <span class="mc-home"><span class="mc-name">${escapeHtml(shortName(rec.home))}</span><i class="mc-key mc-key-home" title="Home — green in charts"></i></span>
        <span class="mc-score" title="Full-time score"><b class="mc-score-h">${rec.ft_home}</b><span class="mc-score-sep">–</span><b class="mc-score-a">${rec.ft_away}</b><span class="ht-ft-tag">FT</span></span>
        <span class="mc-away"><i class="mc-key mc-key-away" title="Away — blue in charts"></i><span class="mc-name">${escapeHtml(shortName(rec.away))}</span></span>
      </span>
      <div class="ht-2h"><span class="ht-2h-label">2H</span>${htGoalsHtml(rec.second_half_goals)}</div>
      <span class="mc-stats">${htCutStats(rec.first_half)}</span>
      <div class="mc-charts ht-charts">
        <span class="mc-timeline" aria-label="First-half event timeline">${timelineSvg(tl)}</span>
        ${htXgChart(tl, rec.has_xg)}
      </div>
    </div>`;
  }

  function htFilterLabel() {
    const board = state.htBoard;
    if (!(state.htFilter instanceof Set) || !board) return "all leagues";
    const labels = (board.chiclets || []).filter((c) => state.htFilter.has(c.slug)).map((c) => c.label);
    return labels.join(", ") || "all leagues";
  }

  function renderHtLeagueChiclets() {
    const row = $("#htLeagueChiclets");
    row.innerHTML = "";
    const board = state.htBoard;
    if (!board) return;
    const filter = state.htFilter;
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "chiclet" + (filter == null ? " on" : "");
    allBtn.innerHTML = `<span class="chiclet-label">ALL</span><span class="chiclet-count">${board.zero_total || 0}</span>`;
    allBtn.addEventListener("click", () => {
      state.htFilter = null;
      refreshHalftime();
    });
    row.appendChild(allBtn);
    for (const c of board.chiclets || []) {
      const n = Number(c.count) || 0;
      const active = filter instanceof Set && filter.has(c.slug);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chiclet" + (active ? " on" : "") + (!n ? " dim" : "");
      btn.disabled = !n && !active;
      btn.title = `${c.archived || 0} archived games · ${n} were 0-0 at half time`;
      btn.innerHTML = `<span class="chiclet-label">${escapeHtml(c.label)}</span><span class="chiclet-count">${n}</span>`;
      btn.addEventListener("click", () => {
        if (!n) return;
        if (state.htFilter instanceof Set && state.htFilter.has(c.slug) && state.htFilter.size === 1) {
          state.htFilter = null;
        } else {
          state.htFilter = new Set([c.slug]);
        }
        refreshHalftime();
      });
      row.appendChild(btn);
    }
  }

  function renderHtArchive() {
    const board = state.htBoard;
    const grid = $("#htArchiveGrid");
    const profile = $("#htProfile");
    if (!board) return;
    profile.innerHTML = htProfileHtml(board.profile, {
      title: `What a 0-0 first half looks like — ${htFilterLabel()}`,
    });
    grid.innerHTML = "";
    const leagues = board.leagues || [];
    if (!leagues.length) {
      const running = board.backfill?.running;
      grid.innerHTML = `<p class="lede empty-live">${
        running
          ? "Backfilling recent full-time games from ESPN — 0-0 halves appear here as they land."
          : "No 0-0 first halves archived for this filter yet."
      }</p>`;
      return;
    }
    for (const g of leagues) {
      const o = g.profile?.outcomes || {};
      const section = document.createElement("div");
      section.className = "match-chiclet-league";
      section.innerHTML = `
        <div class="match-chiclet-league-label">
          <span class="league-chiclet-tag">${escapeHtml(g.chiclet || "")}</span>
          <span>${escapeHtml(g.name || g.slug)}</span>
          <span class="league-count">${g.matches.length} × 0-0 HT · ${o.ended_0_0_pct ?? 0}% stayed 0-0 · ${o.any_2h_goal_pct ?? 0}% saw a 2H goal${
            Number(g.profile?.n_xg) > 0 ? ` · median xG ${Number(g.profile?.bands?.xg?.p50 || 0).toFixed(2)}` : " · no xG feed"
          }</span>
        </div>
        <div class="match-chiclet-row">${g.matches.map(htArchiveCard).join("")}</div>`;
      grid.appendChild(section);
    }
  }

  function htFirstHalfClock(m) {
    const c = String(m?.clock || "").toUpperCase();
    if (c.includes("HT") || c.includes("HALF")) return true;
    const mm = c.match(/(\d+)/);
    if (!mm) return true; // just kicked off, no clock yet
    return Number(mm[1]) <= 45;
  }

  function htLiveZeroRows() {
    return flatLiveMatches(state.htFilter, "live").filter(
      (m) => !Number(m.home_score) && !Number(m.away_score) && htFirstHalfClock(m)
    );
  }

  function htLookalikeHtml(rec, minute) {
    const tl = { ...(rec.timeline || {}), elapsed_seconds: Math.max(1, minute) * 60 };
    const cut = rec.cut?.total || {};
    const pct = Number(rec.match_pct) || 0;
    return `<div class="ht-look" style="--pct:${pct}">
      <span class="ht-look-pct" title="Similarity of the first ${minute}′ — shots, on target, blocked, corners, xG shape">${pct}%</span>
      <span class="ht-look-body">
        <span class="ht-look-head">
          <span class="mc-league">${escapeHtml(rec.league_chiclet || "")}</span>
          <span class="ht-look-when">${escapeHtml(htWhen(rec))}</span>
          <span class="ht-look-teams"><span class="mc-h">${escapeHtml(shortName(rec.home))}</span> <b>${rec.ft_home}–${rec.ft_away}</b> <span class="mc-a">${escapeHtml(shortName(rec.away))}</span> <span class="ht-ft-tag">FT</span></span>
        </span>
        <span class="ht-look-cut">at ${minute}′ · ${cut.shots || 0} sh · ${cut.sot || 0} sot · ${cut.corners || 0} ck${
          rec.has_xg === false ? " · no xG feed" : ` · xG ${Number(cut.xg || 0).toFixed(2)}`
        }</span>
        <span class="ht-2h"><span class="ht-2h-label">2H</span>${htGoalsHtml(rec.second_half_goals)}</span>
      </span>
      <span class="ht-look-strip">${timelineSvg(tl)}</span>
    </div>`;
  }

  function htBandVerdict(value, band, label, digits) {
    if (!band) return "";
    const v = Number(value) || 0;
    const f = (x) => Number(x || 0).toFixed(digits);
    const where = v < band.p10 ? "quieter than" : v > band.p90 ? "busier than" : "inside";
    const cls = where === "inside" ? "in" : "out";
    return `<span class="ht-pill ht-verdict ${cls}">${label} <b>${f(v)}</b> — ${where} the 80% band (${f(band.p10)}–${f(band.p90)})</span>`;
  }

  function htLiveCardHtml(m, data) {
    const tl = { ...(data.timeline || {}), view_max_minute: 45, _ts: Date.now(), _syncedAt: Date.now() };
    const minute = Number(data.minute) || 1;
    const head = `<div class="ht-live-head">
        <span class="mc-live-badge"><span class="live-dot"></span><span class="mc-clock-text">${escapeHtml(m.clock || "LIVE")}</span></span>
        <span class="mc-league">${escapeHtml(m.league_chiclet || "")}</span>
        <span class="ht-live-teams"><span class="mc-h">${escapeHtml(m.home)}</span> <b>0–0</b> <span class="mc-a">${escapeHtml(m.away)}</span></span>
      </div>`;
    if (!data.is_zero_zero_first_half) {
      return `${head}<p class="ht-live-note">No longer a goalless first half (a goal landed or the second half is under way) — it drops from the 0-0 pool.</p>`;
    }
    if (!data.archive_n) {
      return `${head}<p class="ht-live-note">Nothing to compare against yet — the archive has no 0-0 first halves. Hit Backfill.</p>`;
    }
    const live = data.live || {};
    const pop = data.population || {};
    const rank = data.rank || {};
    const lo = data.lookalike_outcomes;
    const looks = data.lookalikes || [];
    const early =
      minute < 10
        ? `<p class="ht-live-note">Early doors — with ${minute}′ played most archived halves still look alike; the match % separates as shots and corners land.</p>`
        : "";
    return `${head}${early}
      <span class="mc-stats">${htCutStats(live)}</span>
      <div class="mc-charts ht-charts">
        <span class="mc-timeline" aria-label="Live first-half event timeline">${timelineSvg(tl)}</span>
        ${htXgChart(tl, live.has_xg)}
      </div>
      <div class="ht-verdicts">
        <div class="ht-sub">Against ${pop.n || 0} archived 0-0 halves cut at ${minute}′ · all leagues</div>
        ${htBandVerdict(live.total?.shots, pop.bands?.shots, "Shots", 0)}
        ${htBandVerdict(live.total?.sot, pop.bands?.sot, "On target", 0)}
        ${live.has_xg && Number(pop.n_xg) > 0 ? htBandVerdict(live.total?.xg, pop.bands?.xg, "xG", 2) : ""}
        <span class="ht-pill">busier than <b>${rank.shots_pct || 0}%</b> on shots · <b>${rank.sot_pct || 0}%</b> on target${
          rank.xg_pct != null ? ` · <b>${rank.xg_pct}%</b> on xG` : ""
        }</span>
      </div>
      <div class="ht-look-summary">
        <div class="ht-sub">Closest ${looks.length} lookalikes · avg match <b>${data.avg_match_pct || 0}%</b> · how they finished</div>
        ${htOutcomeBar(lo)}
        ${htOutcomePills(lo)}
      </div>
      <div class="ht-looks">${looks.map((r) => htLookalikeHtml(r, minute)).join("")}</div>`;
  }

  async function loadHtSimilar(m, card) {
    try {
      const qs = liveQuery(m);
      qs.set("limit", "6");
      const data = await (await fetch(`/api/halftime/similar?${qs}`)).json();
      state.htSimilar[m.event_id] = data;
      if (!card.isConnected) return;
      card.innerHTML = htLiveCardHtml(m, data);
    } catch (err) {
      if (card.isConnected && !card.querySelector(".ht-live-head")) {
        card.innerHTML = `<div class="ht-live-loading">similar feed error — retrying…</div>`;
      }
    }
  }

  async function renderHtSimilar() {
    const wrap = $("#htSimilar");
    if (!state.live) await refreshLive();
    const rows = htLiveZeroRows();
    if (!rows.length) {
      wrap.innerHTML = `<p class="lede empty-live">No live 0-0 first halves right now — ${
        state.live?.live_total || 0
      } games in play. Cards appear here at kickoff while a game is still 0-0 in the first half, and drop off at the first goal or the restart.</p>`;
      return;
    }
    const wanted = new Set(rows.map((m) => String(m.event_id)));
    for (const el of [...wrap.querySelectorAll(".ht-live-card")]) {
      if (!wanted.has(el.dataset.eventId)) el.remove();
    }
    const stray = wrap.querySelector(".empty-live");
    if (stray) stray.remove();
    for (const m of rows) {
      let card = wrap.querySelector(`.ht-live-card[data-event-id="${CSS.escape(String(m.event_id))}"]`);
      if (!card) {
        card = document.createElement("div");
        card.className = "ht-live-card";
        card.dataset.eventId = String(m.event_id);
        card.innerHTML = `<div class="ht-live-loading">matching ${escapeHtml(m.home)} v ${escapeHtml(m.away)} against the archive…</div>`;
        wrap.appendChild(card);
      }
      loadHtSimilar(m, card);
    }
  }

  function htStampText(board) {
    const bf = board?.backfill || {};
    let bit = `${board?.zero_total || 0} × 0-0 HT of ${board?.archive_total || 0} archived`;
    if (bf.running) {
      bit += ` · backfilling ${bf.days_back}d (${bf.archived || 0}/${Math.max(0, (bf.scanned || 0) - (bf.skipped || 0))} new)…`;
    }
    return bit;
  }

  function setHtScope(scope) {
    if (scope !== "archive" && scope !== "similar") return;
    state.htScope = scope;
    document.querySelectorAll(".ht-scope-btn").forEach((b) => b.classList.toggle("on", b.dataset.htscope === scope));
    refreshHalftime();
  }

  async function refreshHalftime() {
    const similar = state.htScope === "similar";
    $("#htProfile").hidden = similar;
    $("#htArchiveGrid").hidden = similar;
    $("#htSimilar").hidden = !similar;
    if (state.htLoading) return;
    state.htLoading = true;
    try {
      const q =
        state.htFilter instanceof Set && state.htFilter.size
          ? `?league=${encodeURIComponent([...state.htFilter].join(","))}`
          : "";
      const board = await (await fetch(`/api/halftime/zero${q}`)).json();
      state.htBoard = board;
      $("#htStamp").textContent = `${htStampText(board)} · updated ${new Date().toLocaleTimeString()}`;
      renderHtLeagueChiclets();
      if (similar) await renderHtSimilar();
      else renderHtArchive();
    } catch (err) {
      $("#htStamp").textContent = "0-0 archive error";
    } finally {
      state.htLoading = false;
    }
    stopHalftimeTimer();
    state.htTimer = setInterval(() => {
      if (document.hidden || !htIsActive()) return;
      if (state.htScope === "similar") renderHtSimilar();
      else if (state.htBoard?.backfill?.running) refreshHalftime();
    }, HT_POLL_MS);
  }

  async function triggerHtBackfill() {
    const btn = $("#htBackfill");
    if (btn) btn.disabled = true;
    try {
      await fetch("/api/halftime/backfill?days=30");
    } catch (err) {
      /* stamp shows the archive error state on the next refresh */
    }
    setTimeout(() => {
      if (btn) btn.disabled = false;
      refreshHalftime();
    }, 2500);
  }

  async function refreshLive() {
    try {
      // One board for both scopes: in-play rows are state "in", full-time rows are "post".
      const data = await (await fetch(`/api/live?live_only=0&days_back=${FINISHED_DAYS_BACK}`)).json();
      state.live = data;
      const when = new Date().toLocaleTimeString();
      const upcomingN = flatLiveMatches(null, "upcoming").length;
      $("#liveStamp").textContent =
        `${data.live_total || 0} live · ${upcomingN} upcoming · ${data.post_total || 0} finished · updated ${when}`;
      $("#similarLiveStamp").textContent = `${data.live_total || 0} live · updated ${when}`;

      const scopeFor = { liveFilter: state.liveScope || "live", similarFilter: "live" };
      for (const filterKey of ["liveFilter", "similarFilter"]) {
        if (state[filterKey] instanceof Set) {
          const alive = new Set(
            (data.chiclets || []).filter((c) => scopeCount(c, scopeFor[filterKey])).map((c) => c.slug)
          );
          for (const slug of [...state[filterKey]]) {
            if (!alive.has(slug)) state[filterKey].delete(slug);
          }
          if (!state[filterKey].size) state[filterKey] = null;
        }
      }

      const all = flatLiveMatches(null, "all");
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
        $("#pitchTitle").textContent = `${m.home} ${m.home_score}–${m.away_score} ${m.away} · ${matchClockLabel(m)}`;
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
    state.liveScope = loadLiveScope();
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
    document.querySelectorAll(".scope-btn").forEach((b) => {
      b.addEventListener("click", () => setLiveScope(b.dataset.scope));
    });
    $("#collapseAll")?.addEventListener("click", () => {
      const ids = flatLiveMatches(state.liveFilter, state.liveScope).map((m) => m.event_id);
      setAllCollapsed(ids, true);
    });
    $("#expandAll")?.addEventListener("click", () => {
      const ids = flatLiveMatches(state.liveFilter, state.liveScope).map((m) => m.event_id);
      setAllCollapsed(ids, false);
    });
    const similarGroupIds = () => {
      const rows = flatLiveMatches(state.similarFilter);
      const slugs = new Set(rows.map((m) => m.league_slug).filter(Boolean));
      return [
        ...rows.map((m) => m.event_id),
        ...[...slugs].map((slug) => `group:league:${slug}`),
      ];
    };
    $("#similarCollapseAll")?.addEventListener("click", () => setAllCollapsed(similarGroupIds(), true));
    $("#similarExpandAll")?.addEventListener("click", () => setAllCollapsed(similarGroupIds(), false));
    document.querySelectorAll(".ht-scope-btn").forEach((b) => {
      b.addEventListener("click", () => setHtScope(b.dataset.htscope));
    });
    $("#htBackfill")?.addEventListener("click", triggerHtBackfill);

    refreshClinical();
    state.clinicalTimer = setInterval(() => {
      if (!document.hidden) refreshClinical();
    }, CLINICAL_POLL_MS);
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
