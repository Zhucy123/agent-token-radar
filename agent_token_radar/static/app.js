/* agent-token-radar dashboard.
   Vanilla JS, no build step.
   - i18n: one string table fetched from /api/i18n (zh | en), no fallback chain.
   - icons: official brand glyphs from /api/icons, rendered as inline SVG.
   - all agent-supplied text goes through textContent, never innerHTML. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  // Language precedence: ?lang= in the URL (shareable / scriptable) ->
  // saved preference -> Chinese.
  const urlLang = new URLSearchParams(location.search).get("lang");
  // `?app=1` is set by the desktop window (desktop.py). It changes only the
  // chrome -- the page gets a tighter gutter and desktop scrollbars, since
  // there is no browser toolbar eating vertical space. Data and behaviour are
  // identical, so the same build still works in a browser.
  const isAppWindow = new URLSearchParams(location.search).get("app") === "1";
  if (isAppWindow) document.documentElement.dataset.shell = "app";
  const state = {
    range: new URLSearchParams(location.search).get("range") || "7d",
    lang: (urlLang || localStorage.getItem("atr.lang") || "zh").toLowerCase().startsWith("en") ? "en" : "zh",
    chart: null,
    timer: null,
    interval: 30,
    S: {},          // active string table
    icons: {},      // agent -> icon definition
    agents: [],     // adapter display info
    data: null,     // last overview payload
    trend: null,
  };

  /* The section jump bar is data-driven, like SETTINGS below: one entry per
     section, no six copies of markup. Each id is one of the section headings'
     id in index.html, so href="#id" is a genuine native anchor jump. */
  const NAV_SECTIONS = [
    { id: "cards-title", key: "nav.summary" },
    { id: "trend-title", key: "nav.trend" },
    { id: "agent-title", key: "nav.byAgent" },
    { id: "model-title", key: "nav.byModel" },
    { id: "project-title", key: "nav.byProject" },
    { id: "sessions-title", key: "nav.topSessions" },
  ];

  // Chart colours. The brand colour of an icon is a poor choice here: several
  // official marks are near-black (Codex, Hermes), which disappears on the dark
  // theme and reads as "no data". This is a fixed categorical ramp in rank
  // order, tuned so every entry keeps >=3:1 against BOTH the light and the dark
  // panel -- a mid-tone ramp, not a light-only one. Colour is never the only
  // signal: each series also gets a legend label, and the tooltip names it.
  const PALETTE_LIGHT = ["#2563eb", "#7c3aed", "#0b8f61", "#c2740a",
                         "#c026a3", "#0e7490", "#4d7c0f", "#b91c1c"];
  const PALETTE_DARK = ["#6ba4fb", "#a78bfa", "#3ddc97", "#fbbf24",
                        "#f472c6", "#38bdf8", "#a3e635", "#f87171"];
  // Kept in sync with the CSS media query; read live so an OS theme change is
  // picked up on the next render (dark-mode guide: matchMedia `change`).
  const chartColor = (index) => {
    const ramp = prefersDark() ? PALETTE_DARK : PALETTE_LIGHT;
    return ramp[index % ramp.length];
  };

  /* -------------------------------------------------------------- settings

     Every setting is one entry in SETTINGS: what to render, what it may be,
     where it is stored and which CSS custom property it drives. Rendering,
     loading, saving and resetting all walk this list, so adding a setting is
     a config entry plus its i18n labels -- no new markup and no new handlers.

     Why `html { font-size }` rather than a multiplier on every text token:
     the stylesheet is entirely in rem (typography guide), so the root size is
     the ONE place that scales everything. A per-token multiplier would have to
     be threaded through every var() and would still miss `clamp()` ranges.

     Why the font stack is a token (`--font-ui`) rather than a body rule: the
     canvas chart sets its own `ctx.font`, so the stack must be readable from
     JS to keep the axis labels in the same typeface as the page.

     Storage: one flat key per setting (`atr.fs`, `atr.ff`) instead of a single
     `atr.settings` JSON blob. The existing language preference is already a
     flat key (`atr.lang`) and the inline <head> script below must read these
     before first paint; a flat key parses with no JSON.parse and cannot be
     corrupted by one bad write. A blob would also have to be re-serialised on
     every change, so one broken field would lose the others. */
  const SETTINGS = [
    {
      key: "fs",                      // localStorage: atr.fs
      attr: "data-fs",                // <html data-fs="...">
      type: "segmented",
      label: "settings.fontSize",
      hint: "settings.fontSize.hint",
      // Root font-size per step. 100% is the stylesheet's design size
      // (base = 13px), so "Normal" is exactly today's look.
      //   小    90%  -> base 11.7px
      //   标准 100%  -> base 13px   (default)
      //   大   115%  -> base 14.95px
      //   特大 130%  -> base 16.9px
      // Multiplied in JS and written as a percentage on <html>, so the user's
      // browser zoom preference still compounds rather than being overridden.
      options: [
        { value: "small", pct: 90, label: "settings.fs.small" },
        { value: "normal", pct: 100, label: "settings.fs.normal" },
        { value: "large", pct: 115, label: "settings.fs.large" },
        { value: "xlarge", pct: 130, label: "settings.fs.xlarge" },
      ],
      fallback: "normal",
      // Writes the scale and reveals it to CSS as one custom property.
      apply(value) {
        const opt = this.options.find((o) => o.value === value) || this.options[1];
        document.documentElement.style.fontSize = `${opt.pct}%`;
        document.documentElement.dataset.fs = opt.value;
      },
    },
    {
      key: "ff",                      // localStorage: atr.ff
      attr: "data-ff",                // <html data-ff="...">
      // A <select> rather than segments: 19 faces will not fit a segmented row,
      // and a native select gives the OS font picker, type-ahead, keyboard
      // navigation and a scrollable list for free. Grouped with <optgroup> so
      // "which kind of face is this" reads at a glance.
      type: "select",
      label: "settings.fontFamily",
      hint: "settings.fontFamily.hint",
      // Installed-font stacks only: the project makes no network requests, so a
      // webfont is not an option.
      //
      // Every stack ends with a CJK face and then a generic family. That order
      // is the whole anti-tofu strategy: Latin text picks the named Western
      // face, Chinese text falls through to the CJK face, and if that face is
      // absent the generic family still renders something instead of boxes.
      // Measured in the real WebView2: all 19 stacks paint a full CJK sample
      // with no blank glyphs, and the Latin-only stacks all resolve CJK to the
      // same 微软雅黑 fallback width (383px at 64px) -- i.e. Chinese renders
      // identically no matter which Western face is chosen.
      options: [
        // --- default ---
        {
          group: "settings.ff.g.default",
          value: "system",
          label: "settings.ff.system",
          // Same stack the stylesheet shipped with; the default.
          stack: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif',
        },
        // --- sans (Latin) ---
        {
          group: "settings.ff.g.sans",
          value: "segoe",
          label: "settings.ff.segoe",
          stack: '"Segoe UI", "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "arial",
          label: "settings.ff.arial",
          stack: 'Arial, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "verdana",
          label: "settings.ff.verdana",
          stack: 'Verdana, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "tahoma",
          label: "settings.ff.tahoma",
          stack: 'Tahoma, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "trebuchet",
          label: "settings.ff.trebuchet",
          stack: '"Trebuchet MS", "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "calibri",
          label: "settings.ff.calibri",
          stack: 'Calibri, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "candara",
          label: "settings.ff.candara",
          stack: 'Candara, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.sans",
          value: "corbel",
          label: "settings.ff.corbel",
          stack: 'Corbel, "Microsoft YaHei", sans-serif',
        },
        // --- serif (Latin) ---
        {
          group: "settings.ff.g.serif",
          value: "georgia",
          label: "settings.ff.georgia",
          stack: 'Georgia, "Microsoft YaHei", serif',
        },
        {
          group: "settings.ff.g.serif",
          value: "times",
          label: "settings.ff.times",
          stack: '"Times New Roman", "Microsoft YaHei", serif',
        },
        {
          group: "settings.ff.g.serif",
          value: "cambria",
          label: "settings.ff.cambria",
          stack: 'Cambria, "Microsoft YaHei", serif',
        },
        {
          group: "settings.ff.g.serif",
          value: "palatino",
          label: "settings.ff.palatino",
          stack: '"Palatino Linotype", "Microsoft YaHei", serif',
        },
        // --- Chinese (CJK-first) ---
        {
          group: "settings.ff.g.cjk",
          value: "yahei",
          label: "settings.ff.yahei",
          stack: '"Microsoft YaHei", "Segoe UI", sans-serif',
        },
        {
          group: "settings.ff.g.cjk",
          value: "dengxian",
          label: "settings.ff.dengxian",
          stack: 'DengXian, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.cjk",
          value: "simhei",
          label: "settings.ff.simhei",
          stack: 'SimHei, "Microsoft YaHei", sans-serif',
        },
        {
          group: "settings.ff.g.cjk",
          value: "simsun",
          label: "settings.ff.simsun",
          stack: 'SimSun, "Microsoft YaHei", serif',
        },
        {
          group: "settings.ff.g.cjk",
          value: "kaiti",
          label: "settings.ff.kaiti",
          stack: 'KaiTi, "Microsoft YaHei", serif',
        },
        {
          group: "settings.ff.g.cjk",
          value: "fangsong",
          label: "settings.ff.fangsong",
          stack: 'FangSong, "Microsoft YaHei", serif',
        },
        {
          group: "settings.ff.g.cjk",
          value: "noto-sans",
          label: "settings.ff.notoSans",
          stack: '"Noto Sans SC", "Microsoft YaHei", sans-serif',
        },
        // --- monospace ---
        {
          group: "settings.ff.g.mono",
          value: "cascadia",
          label: "settings.ff.cascadia",
          stack: '"Cascadia Code", "Cascadia Mono", "Microsoft YaHei", monospace',
        },
        {
          group: "settings.ff.g.mono",
          value: "consolas",
          label: "settings.ff.consolas",
          stack: 'Consolas, "Microsoft YaHei", monospace',
        },
        {
          group: "settings.ff.g.mono",
          value: "lucida",
          label: "settings.ff.lucida",
          stack: '"Lucida Console", "Microsoft YaHei", monospace',
        },
      ],
      fallback: "system",
      apply(value) {
        const opt = this.options.find((o) => o.value === value) || this.options[0];
        // Only the token is written; the stylesheet applies it to <html>.
        document.documentElement.style.setProperty("--font-ui", opt.stack);
        document.documentElement.dataset.ff = opt.value;
      },
    },
    {
      key: "fw",                      // localStorage: atr.fw
      attr: "data-fw",                // <html data-fw="...">
      type: "segmented",
      label: "settings.fontWeight",
      hint: "settings.fontWeight.hint",
      // A WEIGHT MULTIPLIER, not an absolute weight.
      //
      // Every font-weight rule in the stylesheet reads
      // `calc(<its own number> * var(--fw-scale, 1))`, so one value scales the
      // whole ladder proportionally and relationships hold at every step.
      //
      // The obvious alternatives were measured and rejected in-browser:
      // `font-weight: bolder` on <html> left all 24 declared weights untouched
      // and only pushed body/td from 400 to 700 -- which darkens the body
      // relative to the headings and INVERTS the hierarchy. A bare number
      // (`600`) flattened six sampled elements to 3 distinct weights. calc() in
      // font-weight IS supported (400 * 1.2 computed to 480), which is what
      // makes the multiplier approach work.
      options: [
        { value: "light", label: "settings.fw.light", scale: "0.85" },
        { value: "normal", label: "settings.fw.normal", scale: "1" },
        { value: "medium", label: "settings.fw.medium", scale: "1.15" },
        { value: "bold", label: "settings.fw.bold", scale: "1.25" },
      ],
      fallback: "normal",
      apply(value) {
        const opt = this.options.find((o) => o.value === value) || this.options[1];
        document.documentElement.style.setProperty("--fw-scale", opt.scale);
        document.documentElement.dataset.fw = opt.value;
      },
    },
    {
      key: "fst",                     // localStorage: atr.fst
      attr: "data-fst",
      type: "segmented",
      label: "settings.fontStyle",
      hint: "settings.fontStyle.hint",
      options: [
        { value: "normal", label: "settings.fst.normal", style: "normal" },
        { value: "italic", label: "settings.fst.italic", style: "italic" },
      ],
      fallback: "normal",
      apply(value) {
        const opt = this.options.find((o) => o.value === value) || this.options[0];
        document.documentElement.style.setProperty("--font-style-ui", opt.style);
        document.documentElement.dataset.fst = opt.value;
      },
    },
    {
      key: "ink",                     // localStorage: atr.ink
      attr: "data-ink",
      // A dropdown, not a colour picker: a free picker invites an unreadable
      // page (white text on white) with no undo. These are checked pairs that
      // keep AA contrast against the current theme's background, plus an
      // explicit "default" that restores the themed colour.
      type: "select",
      label: "settings.textColor",
      hint: "settings.textColor.hint",
      options: [
        {
          group: "settings.ink.g.default",
          value: "default",
          label: "settings.ink.default",
          // Empty string => remove the property, so --ink-ui falls back to the
          // theme's --text and follows dark/light mode again.
          ink: "",
        },
        {
          group: "settings.ink.g.tones",
          value: "slate",
          label: "settings.ink.slate",
          ink: "light-dark(#334155, #cbd5e1)",
        },
        {
          group: "settings.ink.g.tones",
          value: "ink",
          label: "settings.ink.ink",
          ink: "light-dark(#0f172a, #f1f5f9)",
        },
        {
          group: "settings.ink.g.tones",
          value: "blue",
          label: "settings.ink.blue",
          ink: "light-dark(#1d4ed8, #93c5fd)",
        },
        {
          group: "settings.ink.g.tones",
          value: "green",
          label: "settings.ink.green",
          ink: "light-dark(#166534, #86efac)",
        },
        {
          group: "settings.ink.g.tones",
          value: "violet",
          label: "settings.ink.violet",
          ink: "light-dark(#5b21b6, #c4b5fd)",
        },
        {
          group: "settings.ink.g.tones",
          value: "sepia",
          label: "settings.ink.sepia",
          ink: "light-dark(#5b4636, #d8c3a5)",
        },
      ],
      fallback: "default",
      apply(value) {
        const opt = this.options.find((o) => o.value === value) || this.options[0];
        const root = document.documentElement;
        if (opt.ink) root.style.setProperty("--ink-ui", opt.ink);
        else root.style.removeProperty("--ink-ui");
        root.dataset.ink = opt.value;
      },
    },
    {
      key: "stroke",                  // localStorage: atr.stroke
      attr: "data-stroke",
      type: "segmented",
      label: "settings.textStroke",
      hint: "settings.textStroke.hint",
      // Off is the default so the page looks untouched until asked. Kept
      // intentionally subtle: a heavy stroke destroys CJK legibility because
      // the strokes run into each other, so the widest step is 1.2px.
      options: [
        { value: "off", label: "settings.stroke.off", width: "0", colour: "" },
        {
          value: "thin",
          label: "settings.stroke.thin",
          width: "0.4px",
          colour: "light-dark(#94a3b8, #64748b)",
        },
        {
          value: "medium",
          label: "settings.stroke.medium",
          width: "0.8px",
          colour: "light-dark(#64748b, #94a3b8)",
        },
        {
          value: "strong",
          label: "settings.stroke.strong",
          width: "1.2px",
          colour: "light-dark(#475569, #cbd5e1)",
        },
      ],
      fallback: "off",
      apply(value) {
        const opt = this.options.find((o) => o.value === value) || this.options[0];
        const root = document.documentElement;
        root.style.setProperty("--stroke-w", opt.width);
        if (opt.colour) root.style.setProperty("--stroke-c", opt.colour);
        else root.style.removeProperty("--stroke-c");
        root.dataset.stroke = opt.value;
      },
    },
  ];

  const settingStore = {
    read(setting) {
      try {
        const v = localStorage.getItem(`atr.${setting.key}`);
        if (v && setting.options.some((o) => o.value === v)) return v;
      } catch { /* private mode: fall through to the default */ }
      return setting.fallback;
    },
    write(setting, value) {
      try { localStorage.setItem(`atr.${setting.key}`, value); } catch { /* ignore */ }
    },
    clear(setting) {
      try { localStorage.removeItem(`atr.${setting.key}`); } catch { /* ignore */ }
    },
  };

  /** Apply one setting, plus the side effects a pure CSS change cannot cover. */
  function applySetting(setting, value) {
    setting.apply(value);
    // A canvas does not inherit CSS: the chart bakes its font and size in at
    // draw time, so both settings must force a redraw (same reasoning as the
    // prefers-color-scheme listener).
    if (state.trend) renderChart(state.trend);
  }

  function applyAllSettings() {
    for (const s of SETTINGS) applySetting(s, settingStore.read(s));
  }

  /** Current value of every setting, as a plain object. */
  function readSettings() {
    const out = {};
    for (const s of SETTINGS) out[s.key] = settingStore.read(s);
    return out;
  }

  const fmt = (n) => new Intl.NumberFormat(state.lang === "zh" ? "zh-CN" : "en-US").format(Math.trunc(Number(n) || 0));
  const short = (s, n = 34) => (s && s.length > n ? s.slice(0, n - 1) + "…" : (s || "—"));
  const time = (ms) => (ms
    ? new Date(Number(ms)).toLocaleString(state.lang === "zh" ? "zh-CN" : "en-US", { hour12: false })
    : "—");

  /** t(key, vars) — returns the key itself if untranslated (a visible bug). */
  function t(key, vars) {
    let s = state.S[key];
    if (s === undefined) return key;
    if (vars) for (const k in vars) s = s.split(`{${k}}`).join(vars[k]);
    return s;
  }

  function setStatus(text, busy = false) {
    $("status").textContent = text;
    $("refreshBtn").disabled = busy;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  /* ---------------------------------------------------------------- icons */

  const SVG_NS = "http://www.w3.org/2000/svg";

  // Brand marks that are pure black (or near it) get a light variant so they
  // stay visible on the dark theme. The query is read live, not cached, so an
  // OS theme change is picked up on the next render.
  const prefersDark = () => window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches;

  // The chart draws to a canvas, so it cannot follow a CSS colour change by
  // itself: the icon variants and the palette are baked in at draw time. Any JS
  // that branches on prefers-color-scheme MUST also listen for `change`, or the
  // canvas keeps light-mode colours after the OS flips to dark.
  let themeQuery = null;
  function watchColorScheme(onChange) {
    if (!window.matchMedia) return;
    themeQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => onChange();
    if (themeQuery.addEventListener) themeQuery.addEventListener("change", handler);
    else if (themeQuery.addListener) themeQuery.addListener(handler); // older Safari
  }

  function iconNode(agent, size = 18, decorative = false) {
    const ic = state.icons[agent] || state.icons["_default"];
    const label = (ic && ic.label) || agent;
    const isBitmap = ic && ic.kind === "image" && ic.src;
    const isDocument = ic && ic.kind === "svg" && ic.src;

    // Bitmap brand icons and whole vendor SVG documents come as data URIs: use
    // <img> so the browser decodes them natively. That also keeps gradients,
    // masks and clip paths intact, which path extraction cannot preserve.
    if (isBitmap || isDocument) {
      const el = document.createElement("img");
      el.src = ic.src;
      el.width = size;
      el.height = size;
      el.alt = decorative ? "" : label;
      // ``tiled`` = the vendor artwork fills its own square (no alpha), so it
      // needs a plate behind it on the dark theme instead of a white block.
      // ``mono`` = transparent line art drawn in near-black, which disappears
      // on a dark panel unless the plate and the strokes are inverted.
      const cls = ["agent-icon"];
      if (ic.tiled) cls.push("is-tiled");
      if (ic.mono) cls.push("is-mono");
      el.className = cls.join(" ");
      if (decorative) el.setAttribute("aria-hidden", "true");
      el.loading = "lazy";
      el.decoding = "async";
      return el;
    }

    const el = document.createElementNS(SVG_NS, "svg");
    el.setAttribute("viewBox", (ic && ic.viewBox) || "0 0 24 24");
    el.setAttribute("width", String(size));
    el.setAttribute("height", String(size));
    el.setAttribute("class", "agent-icon");
    el.setAttribute("focusable", "false");
    if (decorative) {
      el.setAttribute("aria-hidden", "true");
    } else {
      el.setAttribute("role", "img");
      el.setAttribute("aria-label", label);
    }
    if (ic) {
      // A dark-colour variant is the vendor's own inverse mark, not a filter.
      const colour = (prefersDark() && ic.dark_color) ? ic.dark_color : ic.color;
      if (ic.mode !== "stroke") el.style.color = `#${colour}`;
      for (const d of ic.paths || []) {
        const p = document.createElementNS(SVG_NS, "path");
        p.setAttribute("d", d);
        if (ic.mode === "stroke") {
          p.setAttribute("fill", "none");
          p.setAttribute("stroke", "currentColor");
          p.setAttribute("stroke-width", "1.9");
          p.setAttribute("stroke-linecap", "round");
          p.setAttribute("stroke-linejoin", "round");
        } else {
          p.setAttribute("fill", "currentColor");
        }
        el.appendChild(p);
      }
    }
    return el;
  }

  /** Agent name cell: icon + label, plus an "unofficial" hint when relevant. */
  function agentCell(agent) {
    const wrap = document.createElement("span");
    wrap.className = "agent-cell";
    // Decorative: the name sits right next to it, so an alt/aria-label here
    // would make screen readers announce the agent twice.
    wrap.appendChild(iconNode(agent, 18, true));
    const name = document.createElement("span");
    name.textContent = displayAgent(agent);
    wrap.appendChild(name);
    const ic = state.icons[agent];
    if (ic && !ic.official) {
      const badge = document.createElement("span");
      badge.className = "badge-unofficial";
      badge.textContent = t("icon.unofficial");
      badge.title = t("icon.unofficial");
      wrap.appendChild(badge);
    }
    return wrap;
  }

  function displayAgent(name) {
    const a = state.agents.find((x) => x.name === name);
    return a ? a.display_name : name;
  }

  /**
   * Agent cell for a row that may stand for several Agents.
   *
   * The project table merges one row per working directory, so the cell has to
   * serve both "one agent" and "N agents". The icons are what makes the column
   * scannable at a glance, which is the reason the other tables show them; a
   * merged row therefore shows each contributing Agent's mark rather than a
   * bare count. They are capped so a directory opened by many Agents cannot
   * push the numeric columns off-screen.
   */
  function agentLabelCell(names, label) {
    const wrap = document.createElement("span");
    wrap.className = "agent-cell agent-cell--stack";
    const MAX_ICONS = 3;
    for (const name of names.slice(0, MAX_ICONS)) {
      wrap.appendChild(iconNode(name, 18, true));
    }
    const text = document.createElement("span");
    text.textContent = label;
    wrap.appendChild(text);
    if (names.length > MAX_ICONS) {
      const more = document.createElement("span");
      more.className = "agent-more";
      more.textContent = `+${names.length - MAX_ICONS}`;
      more.title = names.slice(MAX_ICONS).map(displayAgent).join(", ");
      wrap.appendChild(more);
    }
    return wrap;
  }

  /* ------------------------------------------------------------- rendering */

  function renderCards(totals) {
    $("v-total").textContent = fmt(totals.total_tokens);
    // The hit rate is computed server-side over the aggregate (SUM(cached) /
    // SUM(uncached + cached)), never by averaging per-row ratios.
    // A null means the data contradicts itself, so the card shows a dash
    // rather than a confident percentage (Number(null) would be 0).
    $("v-hit").textContent = pct(totals.cache_hit);
    // "Input" here is the UNCACHED input, not the raw input column: agents
    // disagree about whether input_tokens already contains the cache buckets
    // (Codex includes it, DSH does not), so the raw column is not comparable
    // across agents. See UNCACHED_INPUT_SQL in store.py.
    $("v-input").textContent = fmt(totals.uncached_input_tokens);
    $("v-output").textContent = fmt(totals.output_tokens);
    $("v-cache").textContent = fmt(totals.cache_read_tokens);
    $("v-reasoning").textContent = fmt(totals.reasoning_tokens);

    // Cost. The number alone would read as a bill, so the coverage travels with
    // it: a total that quietly drops the unpriced Models is a subset, and the
    // sub-line is where that shows.
    const cost = Number(totals.cost_usd || 0);
    const ratio = Number(totals.priced_ratio || 0);
    const costEl = $("v-cost");
    const subEl = $("v-cost-sub");
    if (costEl) costEl.textContent = money(cost);
    if (subEl) {
      // Below full coverage the card states the gap instead of hiding it.
      subEl.textContent = ratio < 0.999
        ? `${t("card.cost.sub")} · ${t("cost.coverage", { pct: pct(ratio) })}`
        : t("card.cost.sub");
      subEl.title = t("cost.coverageTip");
    }
  }

  /** A USD amount, small values keeping enough digits to stay meaningful. */
  function money(value) {
    const n = Number(value || 0);
    if (!isFinite(n)) return "$0.00";
    if (n > 0 && n < 0.01) return "$" + n.toFixed(4);
    return "$" + n.toLocaleString(undefined, {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }

  /**
   * A cost cell, or the "unpriced" marker.
   *
   * A NULL from the server means the Model has no official price. Rendering it
   * as $0.00 would state, falsely, that the usage was free -- so it renders as
   * a dash with the reason on hover instead.
   */
  function moneyCell(value) {
    if (value === null || value === undefined) {
      const span = document.createElement("span");
      span.className = "cost-unpriced";
      span.textContent = "—";
      span.title = t("cost.unpriced");
      return span;
    }
    const span = document.createElement("span");
    span.textContent = money(value);
    return span;
  }

  /**
   * Cache hit rate as a percentage string.
   *
   * Rounded to one decimal, except that a near-total hit rate reads better as
   * "100%" than as "99.99%". A true zero (or a missing value) renders "0%".
   *
   * null means the backend could not compute an honest rate because the row
   * contradicts itself (more cache reads than its own total). That renders a
   * dash, not "0%": claiming a confident zero would state something about the
   * data we know is untrue. Number(null) is 0, so this check must come first.
   */
  function pct(ratio) {
    if (ratio === null || ratio === undefined) return "—";
    const r = Number(ratio);
    if (!isFinite(r) || r <= 0) return "0%";
    return `${(r * 100).toFixed(r >= 0.995 ? 0 : 1)}%`;
  }

  /**
   * Split a "provider/model" string that some agents bake into the model name.
   * Keeps the model name and the provider column from disagreeing.
   */
  function splitEmbeddedProvider(model, provider) {
    if (!model || !provider) return [model, provider];
    const prefix = `${provider}/`;
    if (model.toLowerCase().startsWith(prefix.toLowerCase()) && model.length > prefix.length) {
      return [model.slice(prefix.length), provider];
    }
    return [model, provider];
  }

  /**
   * Model usage, nested under each agent.
   *
   * Grouping matters: a bare "model" list mixes the same model name served by
   * different backends, and hides which agent actually spent the tokens. Each
   * agent gets a header row (icon + name + its total), then its models sorted
   * by usage. The provider appears as a quiet second line under the model name
   * so `deepseek-v4.1-flash` from `workbuddy` stays distinguishable from the
   * same model from `tokenrhythm`.
   */
  function renderModelTable(data) {
    const tbody = $("modelTable").querySelector("tbody");
    tbody.replaceChildren();

    const rows = data.by_model || [];
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty";
      // 1 name column + events/total/hit/uncached/cache-read/output
      td.colSpan = 7;
      td.textContent = t("empty.table");
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    // agent -> its model rows, both in descending total order
    const byAgent = new Map();
    for (const r of rows) {
      if (!byAgent.has(r.agent)) byAgent.set(r.agent, []);
      byAgent.get(r.agent).push(r);
    }
    // Per-agent subtotals, so the group header reads as a real row aligned to
    // the columns instead of one number floating under the last header.
    const agentTotals = new Map();
    const agentSums = new Map();
    for (const [agent, list] of byAgent) {
      const s = { events: 0, total: 0, uncached: 0, cacheRead: 0, output: 0 };
      // `cost` is null until a row with a published price contributes, so a
      // group made only of unpriced Models stays visibly unknown rather than
      // collapsing to $0.00.
      s.cost = null;
      s.priced = 0;
      for (const x of list) {
        s.events += x.events || 0;
        s.total += x.total_tokens || 0;
        s.uncached += x.uncached_input_tokens || 0;
        s.cacheRead += x.cache_read_tokens || 0;
        s.output += x.output_tokens || 0;
        s.priced += x.priced_tokens || 0;
        if (x.cost_usd !== null && x.cost_usd !== undefined) {
          s.cost = (s.cost || 0) + Number(x.cost_usd);
        }
      }
      agentTotals.set(agent, s.total);
      agentSums.set(agent, s);
    }
    // Hit rate is aggregate-then-divide, never an average of per-row rates.
    const agentHit = (agent) => {
      const s = agentSums.get(agent);
      const denom = s.uncached + s.cacheRead;
      return denom > 0 ? s.cacheRead / denom : null;
    };
    const order = [...byAgent.keys()].sort((a, b) => agentTotals.get(b) - agentTotals.get(a));
    const grand = [...agentTotals.values()].reduce((a, b) => a + b, 0) || 1;

    for (const agent of order) {
      const list = byAgent.get(agent);

      // ---- group header: agent identity + share of the whole
      const head = document.createElement("tr");
      head.className = "model-group";
      const hth = document.createElement("th");
      hth.scope = "colgroup";
      // One column, matching the single name column of the rows below. A wider
      // span silently pushes every number one column to the right.
      hth.colSpan = 1;
      hth.appendChild(agentCell(agent));
      const share = document.createElement("span");
      share.className = "group-share";
      share.textContent = `${((agentTotals.get(agent) / grand) * 100).toFixed(1)}%`;
      hth.appendChild(share);
      head.appendChild(hth);
      // The group line must line up with the columns below it: a single
      // right-aligned number reads as if it belonged to the last column.
      const sums = agentSums.get(agent);
      const headVals = [
        fmt(sums.events),
        fmt(sums.total),
        pct(agentHit(agent)),
        fmt(sums.uncached),
        fmt(sums.cacheRead),
        fmt(sums.output),
      ];
      const headLabels = [
        t("col.events"), t("col.total"), t("col.cacheHit"),
        t("col.input"), t("col.cacheRead"), t("col.output"), t("col.cost"),
      ];
      headVals.forEach((value, i) => {
        const td = document.createElement("td");
        td.className = "num";
        td.textContent = value;
        td.dataset.label = headLabels[i];
        head.appendChild(td);
      });
      // The group's cost is a sum of row costs, so it can be null even when
      // the group has rows.
      const costTd = document.createElement("td");
      costTd.className = "num";
      costTd.appendChild(moneyCell(sums.cost));
      costTd.dataset.label = t("col.cost");
      head.appendChild(costTd);
      tbody.appendChild(head);

      // ---- one row per (model, provider)
      for (const r of list) {
        const tr = document.createElement("tr");

        const th = document.createElement("th");
        th.scope = "row";
        const [modelName, provider] = splitEmbeddedProvider(r.model, r.provider);
        const name = document.createElement("span");
        name.className = "model-name";
        name.textContent = modelName || t("model.unknown");
        th.appendChild(name);
        // The agent logged a routing alias; the gateway says what really ran.
        // Show the real model as the headline and the alias beneath it, so the
        // row never claims a model the backend did not serve.
        const alias = String(r.alias || "").trim();
        if (alias) {
          const a = document.createElement("span");
          a.className = "model-alias";
          a.textContent = t("model.routedFrom", { alias });
          a.title = t("model.aliasHint", { alias });
          th.appendChild(a);
        }
        if (provider) {
          // Provider name and its provenance tag share ONE line. Stacking them
          // made every model row three text lines tall (~80px), which read as a
          // wall of whitespace across 25 rows.
          const meta = document.createElement("span");
          meta.className = "model-meta";
          const prov = document.createElement("span");
          prov.className = "model-provider";
          prov.textContent = provider;
          meta.appendChild(prov);
          // Only a name-based GUESS is marked. ``measured`` is the normal case
          // (the record states it, or the local gateway logged the request) and
          // ``product`` is a structural fact, so tagging them on every row just
          // adds noise. A guess must never read like a measurement, which is
          // exactly what the dashed ``inferred`` chip says.
          const src = String(r.provider_source || "");
          if (src === "inferred") {
            const tag = document.createElement("span");
            tag.className = "prov-tag prov-inferred";
            tag.textContent = t("prov.inferred");
            tag.title = t("prov.inferredHint");
            meta.appendChild(tag);
            th.title = t("prov.inferredHint");
          }
          th.appendChild(meta);
        }
        tr.appendChild(th);

        const modelHeaders = [
          t("col.events"), t("col.total"), t("col.cacheHit"),
          t("col.input"), t("col.cacheRead"), t("col.output"),
          t("col.cost"), t("col.unitPrice"),
        ];
        let mi = 0;
        for (const value of [
          fmt(r.events),
          fmt(r.total_tokens),
          pct(r.cache_hit),
          fmt(r.uncached_input_tokens),
          fmt(r.cache_read_tokens),
          fmt(r.output_tokens),
        ]) {
          const td = document.createElement("td");
          td.textContent = value;
          td.classList.add("num");
          td.dataset.label = modelHeaders[mi++];
          tr.appendChild(td);
        }
        // Cost, plus the per-1M unit price that produced it. The unit price is
        // the honest half of the pair: it is a published fact, while the cost
        // depends on how much of the usage had a price at all.
        const costTd = document.createElement("td");
        costTd.className = "num";
        costTd.appendChild(moneyCell(r.cost_usd));
        costTd.dataset.label = modelHeaders[mi++];
        tr.appendChild(costTd);

        const unitTd = document.createElement("td");
        unitTd.className = "num";
        const pin = r.price_input_usd;
        const pout = r.price_output_usd;
        if (pin === null || pin === undefined) {
          unitTd.appendChild(moneyCell(null));
        } else {
          // The unit column is the vendor's PEAK list rate. For a time-of-day
          // vendor the billed rate can be half of it, so say which one this is
          // instead of letting it pass for an average.
          const peakTag = r.has_peak_pricing ? t("cost.peakTag") : "";
          unitTd.textContent = `${money(pin)} / ${money(pout)}${peakTag}`;
          const offpeak = r.price_input_offpeak_usd;
          const detail = [
            `${t("cost.perMillion")}:`,
            `${t("col.input")} ${money(pin)} · ${t("col.output")} ${money(pout)}`,
          ];
          if (r.has_peak_pricing && offpeak !== null && offpeak !== undefined) {
            detail.push(
              `${t("cost.offpeakTag")} ${t("col.input")} ${money(offpeak)}`
              + ` · ${t("col.output")} ${money(r.price_output_offpeak_usd)}`
            );
          }
          unitTd.title = detail.join("  ");
        }
        unitTd.dataset.label = modelHeaders[mi++];
        tr.appendChild(unitTd);
        tbody.appendChild(tr);
      }
    }
  }

  /**
   * `agentCol` is the column index (1-based) that should render an agent icon
   * instead of plain text, or -1 for none. Positional on purpose: matching on
   * the header text would break as soon as the language changes.
   *
   * `moneyCol` is the column index (also 1-based) holding a USD amount, or -1
   * for none. A money column must be rendered by ``moneyCell`` rather than
   * copied through as text: the raw value is a float, so $8.171517142 would
   * print with nine decimals and its neighbours in the same table would not
   * agree with the Model and project tables that DO format. ``moneyCell`` also
   * keeps the NULL -> dash meaning ("no published price"), which a plain
   * toFixed would destroy.
   */
  function fillTable(tableId, headers, rows, numericFrom, firstIsAgent = false,
                     agentCol = -1, moneyCol = -1) {
    const tbody = $(tableId).querySelector("tbody");
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty";
      td.colSpan = headers.length;
      td.textContent = t("empty.table");
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const row of rows) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.scope = "row";
      if (firstIsAgent) th.appendChild(agentCell(row[0]));
      else th.textContent = row[0];
      tr.appendChild(th);
      for (let i = 1; i < headers.length; i++) {
        const td = document.createElement("td");
        const value = row[i];
        if (i === agentCol) {
          td.appendChild(agentCell(value));
        } else if (i === moneyCol) {
          td.appendChild(moneyCell(value));
        } else if (value === null || value === undefined) {
          // Only one column can be null: cost, for a Model with no published
          // price. A dash says "unknown"; 0 would say "free".
          td.appendChild(moneyCell(value));
        } else {
          td.textContent = value;
        }
        if (i >= numericFrom) td.classList.add("num");
        // Narrow (stacked) layout shows each value without its column header,
        // so the cell carries its own label in the active language.
        td.dataset.label = headers[i];
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  /**
   * Project usage: which working directory the tokens were spent on.
   *
   * The token totals cannot answer this, and it is the question people actually
   * ask ("what did this cost me in project X"). One folder is often opened by
   * several Agents, so the row is the PROJECT (agents merged) and expanding it
   * shows each Agent's own share -- otherwise the table split one project into
   * two rows that each looked small.
   */
  function renderProjectTable(data) {
    const rows = data.by_project || [];
    const tbody = $("projectTable").querySelector("tbody");
    tbody.replaceChildren();

    const headers = [
      t("col.project"), t("col.agent"), t("col.events"), t("col.total"),
      t("col.cacheHit"), t("col.input"), t("col.cacheRead"), t("col.output"),
      t("col.cost"),
    ];
    const grand = (data.totals && data.totals.total_tokens) || 0;

    // One row per project, plus one child row per agent underneath it. The
    // children are real table rows, not a nested table, so the columns line up
    // with the header.
    const addRow = (opts) => {
      const tr = document.createElement("tr");
      if (opts.child) tr.className = "project-child";

      const th = document.createElement("th");
      th.scope = "row";
      const name = document.createElement("span");
      name.className = "project-name";
      if (opts.child) {
        name.textContent = "↳";
        name.classList.add("project-branch");
        th.appendChild(name);
      } else {
        // The full path is the identity; the label shows the tail so the table
        // stays readable, with the whole path on hover.
        const short = document.createElement("span");
        short.className = "project-name";
        short.textContent = shortProject(opts.full) || t("project.none");
        short.title = opts.full;
        name.replaceWith(short);
        th.appendChild(short);
      }
      tr.appendChild(th);

      const cells = [
        opts.agentLabel,
        fmt(opts.events),
        grand && opts.total
          ? `${fmt(opts.total)}  (${((opts.total / grand) * 100).toFixed(1)}%)`
          : fmt(opts.total),
        pct(opts.cacheHit),
        fmt(opts.uncached),
        fmt(opts.cacheRead),
        fmt(opts.output),
      ];
      cells.forEach((value, i) => {
        const td = document.createElement("td");
        // The agent column is icon + name, like every other table.
        if (i === 0 && opts.agentNode) td.appendChild(opts.agentNode);
        else td.textContent = value;
        if (i > 0) td.classList.add("num");
        td.dataset.label = headers[i + 1];
        tr.appendChild(td);
      });
      // Cost is appended separately because it can be null (no published price)
      // and must not be written through the textContent path as "null".
      const costTd = document.createElement("td");
      costTd.className = "num";
      costTd.appendChild(moneyCell(opts.cost));
      costTd.dataset.label = headers[headers.length - 1];
      tr.appendChild(costTd);
      tbody.appendChild(tr);
      return tr;
    };

    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty";
      td.colSpan = headers.length;
      td.textContent = t("empty.table");
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      for (const r of rows) {
        const agents = (r.agents || []).slice();
        const multi = agents.length > 1;
        // A single-agent project has nothing to expand: show the agent name
        // directly instead of a toggle that reveals one identical row.
        const label = multi
          ? `${agents.length} ${t("project.agents")}`
          : (agents[0] ? displayAgent(agents[0].agent) : "");
        const parentAgentNames = agents.map((a) => a.agent);

        const parent = addRow({
          full: String(r.project || ""),
          agentLabel: label,
          // A single-Agent project borrows the exact cell the other tables use
          // (icon + name + unofficial badge); a merged row shows one icon per
          // contributing Agent, capped at three.
          agentNode: multi
            ? agentLabelCell(parentAgentNames, label)
            : (agents[0] ? agentCell(agents[0].agent) : label),
          events: r.events,          total: r.total_tokens,
          cacheHit: r.cache_hit,
          uncached: r.uncached_input_tokens,
          cacheRead: r.cache_read_tokens,
          output: r.output_tokens,
          cost: r.cost_usd,
        });

        if (!multi) continue;

        // Disclosure control lives on the first cell so the row stays one row.
        parent.classList.add("project-parent");
        parent.setAttribute("aria-expanded", "false");
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "project-toggle";
        toggle.setAttribute("aria-expanded", "false");
        toggle.title = t("project.expandHint");
        toggle.innerHTML = '<span aria-hidden="true">▸</span>';
        parent.firstChild.prepend(toggle);

        const children = agents.map((a) => addRow({
          child: true,
          agentLabel: displayAgent(a.agent),
          agentNode: agentCell(a.agent),
          events: a.events,
          total: a.total_tokens,
          cacheHit: a.cache_hit,
          uncached: a.uncached_input_tokens,
          cacheRead: a.cache_read_tokens,
          output: a.output_tokens,
          cost: a.cost_usd,
        }));
        for (const child of children) child.hidden = true;

        toggle.addEventListener("click", () => {
          const open = toggle.getAttribute("aria-expanded") === "true";
          toggle.setAttribute("aria-expanded", String(!open));
          toggle.firstChild.textContent = open ? "▸" : "▾";
          parent.setAttribute("aria-expanded", String(!open));
          for (const child of children) child.hidden = open;
        });
        // The row itself toggles too: the whole line is the target, which is
        // what people try first. The guard must use closest(): the click lands
        // on the inner <span> of the button, never on the button element, so
        // comparing ev.target to the button let the click run twice and the
        // expand immediately collapsed again.
        parent.addEventListener("click", (ev) => {
          if (ev.target.closest && ev.target.closest(".project-toggle")) return;
          toggle.click();
        });
      }
    }

    // State the coverage instead of hiding rows that have no directory.
    const stats = data.project_stats || {};
    const note = $("projectNote");
    if (note) {
      const total = Number(stats.total || 0);
      const withP = Number(stats.with_project || 0);
      if (total) {
        note.textContent = `${t("project.coverage", {
          with: fmt(withP), total: fmt(total),
          pct: `${((withP / total) * 100).toFixed(1)}%`,
        })} · ${t("project.explain")}`;
      } else {
        note.textContent = "";
      }
    }
  }

  /** Show the last one or two path segments; the full path is the tooltip. */
  function shortProject(path) {
    if (!path) return "";
    const parts = path.split("/").filter(Boolean);
    if (parts.length <= 2) return path;
    return "…/" + parts.slice(-2).join("/");
  }

  function renderTables(data) {
    const agents = data.by_agent || [];
    const grand = agents.reduce((a, x) => a + (x.total_tokens || 0), 0) || 1;

    fillTable("agentTable",
      [t("col.agent"), t("col.events"), t("col.total"), t("col.cacheHit"),
       t("col.input"), t("col.cacheRead"), t("col.output"), t("col.cost")],
      agents.map((x) => [
        x.agent,
        fmt(x.events),
        `${fmt(x.total_tokens)}  (${((x.total_tokens / grand) * 100).toFixed(1)}%)`,
        pct(x.cache_hit),
        fmt(x.uncached_input_tokens), fmt(x.cache_read_tokens), fmt(x.output_tokens),
        x.cost_usd,
      ]), 1, true, -1, 7);
    //     ^numericFrom  ^firstIsAgent  ^agentCol  ^moneyCol
    // moneyCol=7 is 参考费用 (the 8th column, 0-based index 7). Without it the
    // raw float printed as $8.171517142 while the Model and project tables
    // showed $8.17.

    // The model table is grouped by agent, so it renders itself.
    renderModelTable(data);

    // Which project the tokens went to.
    renderProjectTable(data);

    // Column 2 is the agent: fillTable renders the icon there itself.
    fillTable("sessionTable",
      ["#", t("col.session"), t("col.agent"), t("col.events"), t("col.total"),
       t("col.cacheHit"), t("col.input"), t("col.output"), t("col.lastSeen")],
      (data.top_sessions || []).map((x, i) => [
        String(i + 1), "", x.agent,
        fmt(x.events), fmt(x.total_tokens), pct(x.cache_hit),
        fmt(x.uncached_input_tokens), fmt(x.output_tokens),
        time(x.last_seen),
      ]), 4, false, 2);

    // Replace the placeholder session cell with title + id. Done here rather
    // than inside fillTable because this cell is the only two-line one, and a
    // bare UUID tells the user nothing about what the session was about.
    const sbody = $("sessionTable").querySelector("tbody");
    (data.top_sessions || []).forEach((x, i) => {
      const tr = sbody.children[i];
      if (!tr) return;
      const cell = tr.children[1];
      if (!cell) return;
      cell.replaceChildren();
      const title = (x.title || "").trim();
      if (title) {
        const tEl = document.createElement("span");
        tEl.className = "session-title";
        tEl.textContent = title;
        cell.appendChild(tEl);
        const idEl = document.createElement("span");
        idEl.className = "session-id";
        idEl.textContent = short(x.session_id || "(unknown)");
        idEl.title = x.session_id || "";
        cell.appendChild(idEl);
      } else {
        const idEl = document.createElement("span");
        idEl.className = "session-id only";
        idEl.textContent = short(x.session_id || "(unknown)");
        idEl.title = x.session_id || "";
        cell.appendChild(idEl);
      }
    });
  }

  /* ---------------------------------------------------------------- chart */
  /* A small stacked-bar renderer on a plain 2D canvas. No third-party chart
     library: this dashboard is a local, offline-first tool, so a CDN <script>
     is a single point of failure (and it silently blanked the panel before).
     Everything here is plain Canvas2D, sized for devicePixelRatio. */

  /* Resolve a CSS custom property to a colour Canvas2D can actually parse.
     getPropertyValue() returns the *specified* token, so with light-dark()
     it hands back the literal string `light-dark(#16202e, #e7ecf3)`. Assigning
     that to ctx.fillStyle is a silent no-op: the context keeps whatever fill
     was set last, which painted the axis labels with the previous series
     colour. Reading through a throwaway element makes the browser compute the
     value first, so we get rgb(...) whichever theme is live. */
  const probe = document.createElement("span");
  probe.setAttribute("aria-hidden", "true");
  probe.style.cssText = "position:absolute;width:0;height:0;visibility:hidden;pointer-events:none";
  (document.body || document.documentElement).appendChild(probe);

  const cssVar = (name) => {
    probe.style.color = "";
    probe.style.color = `var(${name})`;
    const resolved = getComputedStyle(probe).color;
    // color always resolves to rgb()/rgba(), never to a light-dark() token.
    return resolved && resolved !== "rgba(0, 0, 0, 0)" ? resolved : "#888";
  };

  function niceMax(v) {
    if (v <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / pow;
    const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * pow;
  }

  /* Axis ticks. The unit suffix follows the UI language: 亿/万 in Chinese,
     B/M/K in English. Leaving "1.0B" in the Chinese UI breaks the rule that a
     Chinese screen shows Chinese -- a magnitude suffix is a word, not a proper
     noun like token or agent. Both forms round to one decimal so the tick
     labels stay the same width and the gutter never reflows on a switch. */
  const axisLabel = (v) => {
    const zh = state.lang === "zh";
    if (zh) {
      if (v >= 1e8) return (v / 1e8).toFixed(1) + " 亿";
      if (v >= 1e4) return (v / 1e4).toFixed(0) + " 万";
      return String(Math.round(v));
    }
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(0) + "K";
    return String(Math.round(v));
  };

  /**
   * Draw a stacked bar chart. `days` are x labels, `series` is
   * [{name, colour, values[]}]. Returns hit-test boxes for tooltips.
   */
  function drawStackedBars(ctx, cssW, cssH, days, series, unit) {
    const ink = cssVar("--text");
    const muted = cssVar("--muted");
    const line = cssVar("--line");

    // The y-axis labels live in a sticky DOM strip (see .chart-yaxis), not on
    // the canvas: the canvas is wider than its viewport on a long range and
    // scrolls, so anything painted into its left edge would scroll away.
    const padL = 10, padR = 18, padT = 22, padB = 46;
    const plotW = Math.max(10, cssW - padL - padR);
    const plotH = Math.max(10, cssH - padT - padB);

    // y scale
    const totals = days.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const top = niceMax(Math.max(1, ...totals));

    ctx.clearRect(0, 0, cssW, cssH);
    // rem, not px: Canvas2D parses the CSS font shorthand, so the label follows
    // the user's browser font-size preference just like the DOM text. The
    // family comes from the live --font-ui token, so a typeface chosen in
    // Settings reaches the axis labels too instead of leaving them on the
    // system stack while the rest of the page changes.
    const fontFamily = getComputedStyle(document.documentElement)
      .getPropertyValue("--font-ui").trim()
      || 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.font = `0.6875rem ${fontFamily}`;
    ctx.textBaseline = "middle";

    // horizontal grid only -- the tick text is DOM so it can stick
    const TICKS = 4;
    for (let i = 0; i <= TICKS; i++) {
      const frac = i / TICKS;
      const y = padT + plotH - frac * plotH;
      ctx.strokeStyle = line;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, Math.round(y) + 0.5);
      ctx.lineTo(padL + plotW, Math.round(y) + 0.5);
      ctx.stroke();
    }
    paintYAxis(top, TICKS, padT, plotH, cssH);

    // bars
    const slot = plotW / Math.max(1, days.length);
    // Fill most of the slot on a dense axis (16px bars read better than 4px
    // slivers), but never balloon past 56px on a short range.
    const barW = Math.max(6, Math.min(56, slot * 0.72));
    const boxes = [];

    // Label thinning. Rotated 45 degrees, a "MM-DD" label occupies about 40px
    // of horizontal space once the glyph height is projected; below that they
    // collide, so print every Nth label. The LAST day is always labelled so the
    // axis ends on a known date rather than trailing off after the final tick.
    const minLabelGap = 56;                       // px between rotated labels
    const labelStep = Math.max(1, Math.ceil(minLabelGap / Math.max(1, slot)));

    days.forEach((day, i) => {
      const cx = padL + slot * (i + 0.5);
      const x = cx - barW / 2;
      let acc = 0;
      for (const s of series) {
        const v = s.values[i] || 0;
        if (v <= 0) continue;
        const h = (v / top) * plotH;
        const y = padT + plotH - ((acc + v) / top) * plotH;
        ctx.fillStyle = s.colour;
        ctx.fillRect(x, y, barW, Math.max(1, h));
        acc += v;
      }
      boxes.push({
        x, y: padT, w: barW, h: plotH,
        title: day,
        total: totals[i],
        parts: series.filter((s) => (s.values[i] || 0) > 0)
          .map((s) => ({ label: s.name, value: s.values[i], colour: s.colour })),
      });

      // x label -- thinned, and rotated when the slot is still tight. The
      // bucket key is a date; on a weekly or monthly axis printing MM-DD is
      // misleading, so shorten it to the month (monthly) or keep MM-DD (weekly,
      // where the bar's start date is the useful reference).
      const isLast = i === days.length - 1;
      const show = i % labelStep === 0 || isLast;
      // Never let the final label sit on top of the one before it.
      const tooClose = isLast && (i % labelStep) !== 0 && slot < minLabelGap;
      if (show && !tooClose) {
        ctx.fillStyle = muted;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const label = unit === "month" ? day.slice(0, 7) : day.slice(5);
        if (slot < 58) {
          ctx.save();
          ctx.translate(cx, padT + plotH + 8);
          ctx.rotate(-Math.PI / 4);
          ctx.textAlign = "right";
          ctx.fillText(label, 0, 0);
          ctx.restore();
        } else {
          ctx.fillText(label, cx, padT + plotH + 8);
        }
        ctx.textBaseline = "middle";
      }
    });

    return { boxes, padL, padT, plotW, plotH, ink };
  }

  /**
   * Paint the y-axis ticks into the sticky DOM strip. Boxes are positioned in
   * the same coordinate space the canvas uses (padT = 22px from the panel top,
   * plotH tall), so a tick always lines up with its grid line.
   */
  function paintYAxis(top, ticks, padT, plotH, cssH) {
    const strip = $("chartYAxis");
    if (!strip) return;
    strip.replaceChildren();
    strip.style.blockSize = cssH + "px";
    const muted = cssVar("--muted");
    for (let i = 0; i <= ticks; i++) {
      const frac = i / ticks;
      const y = padT + plotH - frac * plotH;
      const tick = document.createElement("span");
      tick.className = "yaxis-tick";
      tick.textContent = axisLabel(top * frac);
      tick.style.top = y + "px";
      tick.style.color = muted;
      strip.appendChild(tick);
    }
  }

  /**
   * The one-bucket case ("today", or a range that contains a single day).
   *
   * A bar chart needs a time axis. With one bucket it degenerates into a single
   * column filling the panel and tells the reader nothing the summary cards do
   * not already say. What a one-day view does raise is "who used it", so this
   * draws a ranked share list: bar length is the agent's part of the day.
   */
  function renderSingleBucket(trend) {
    const canvas = $("trendChart");
    const shares = $("agentShares");
    const fallback = $("chartFallback");
    if (!shares) return;          // markup not updated yet -- keep the old path
    canvas.hidden = true;
    fallback.hidden = true;
    shares.hidden = false;
    shares.replaceChildren();
    // A ResizeObserver left over from the bar chart would fire ~120ms later and
    // call its own `draw`, which re-fills the legend and the canvas behind this
    // view. That race is why switching to "today" appeared to need two clicks:
    // the first one was undone just after it rendered.
    if (state._chartObserver) {
      state._chartObserver.disconnect();
      state._chartObserver = null;
    }
    // The share rows carry their own name + value, so a legend would repeat
    // every one of them.
    const legend = $("chartLegend");
    if (legend) legend.replaceChildren();

    const day = trend.days[0];
    const rows = (trend.agents || [])
      .map((a) => ({
        agent: a,
        name: displayAgent(a),
        value: (trend.series && trend.series[a] && trend.series[a][0]) || 0,
      }))
      .filter((r) => r.value > 0)
      .sort((x, y) => y.value - x.value);

    if (!rows.length) {
      shares.hidden = true;
      canvas.hidden = true;
      // Fall through with the share heading already applied, or the section
      // would keep saying "by Agent" while showing "no data".
      const t0 = $("trend-title");
      if (t0) t0.textContent = t("sec.trendShare");
      fallback.hidden = false;
      fallback.textContent = t("trend.noData");
      return;
    }

    const total = rows.reduce((a, r) => a + r.value, 0) || 1;

    // The section heading says "stacked by Agent", which describes a time
    // series. A one-day view is a share breakdown, so say that instead.
    const title = $("trend-title");
    if (title) title.textContent = t("sec.trendShare");

    const head = document.createElement("p");
    head.className = "shares-head";
    head.textContent = t("trend.shareHead", { day, total: fmt(total) });
    shares.appendChild(head);

    const list = document.createElement("ul");
    list.className = "shares-list";
    rows.forEach((r, i) => {
      const pctVal = (r.value / total) * 100;
      const li = document.createElement("li");
      li.className = "share-row";

      const label = document.createElement("span");
      label.className = "share-name";
      label.appendChild(iconNode(r.agent, 16, true));
      const nm = document.createElement("span");
      nm.textContent = r.name;
      label.appendChild(nm);

      const track = document.createElement("span");
      track.className = "share-track";
      const bar = document.createElement("span");
      bar.className = "share-bar";
      bar.style.inlineSize = `${Math.max(1.5, pctVal)}%`;
      bar.style.background = chartColor(i);
      track.appendChild(bar);

      const val = document.createElement("span");
      val.className = "share-value";
      val.textContent = `${fmt(r.value)} · ${pctVal.toFixed(1)}%`;

      li.appendChild(label);
      li.appendChild(track);
      li.appendChild(val);
      list.appendChild(li);
    });
    shares.appendChild(list);
  }

  function renderChart(trend) {
    const canvas = $("trendChart");
    const fallback = $("chartFallback");
    const days = trend.days || [];
    const agents = trend.agents || [];

    if (!days.length || !agents.length) {
      canvas.hidden = true;
      fallback.hidden = false;
      fallback.textContent = t("trend.noData");
      return;
    }

    // A single bucket has no time axis: one bar filling the panel says nothing
    // that the cards above do not already say. Show the day's split between
    // agents instead, which is the question a one-day view actually raises.
    if (days.length === 1) {
      renderSingleBucket(trend);
      return;
    }

    canvas.hidden = false;
    const shares = $("agentShares");
    if (shares) shares.hidden = true;
    fallback.hidden = true;
    // Restore the time-series heading after a one-day view changed it.
    const trendTitle = $("trend-title");
    if (trendTitle) trendTitle.textContent = t("sec.trend");

    // Largest spender first, so the biggest band sits at the bottom (easiest to
    // read) and keeps its colour stable as data changes.
    const ordered = agents
      .map((a) => ({
        agent: a,
        name: displayAgent(a),
        values: (trend.series && trend.series[a]) || [],
      }))
      .sort((x, y) => {
        const sx = x.values.reduce((a, b) => a + (b || 0), 0);
        const sy = y.values.reduce((a, b) => a + (b || 0), 0);
        return sy - sx;
      });

    const series = ordered.map((o, i) => ({
      name: o.name,
      colour: chartColor(i),
      values: o.values,
    }));

    const draw = () => {
      const scroller = $("chartScroller");
      const host = scroller || canvas.parentElement;
      const rect = host.getBoundingClientRect();
      const availW = Math.max(240, rect.width);

      // A bar needs a readable minimum width. The backend already picks the
      // bucket size so a long range is a handful of weekly/monthly bars; this
      // is the safety net for the remaining case (daily data over several
      // months), where the plot grows and the wrapper scrolls sideways rather
      // than squeezing 130 days into 8px slivers.
      const MIN_SLOT = 26;
      const needW = days.length * MIN_SLOT + 28;
      const cssW = Math.max(availW, needW);
      const cssH = Math.max(180, rect.height);

      // Cap the backing store. A 4K/5K display has dpr 2-3, and an uncapped
      // canvas would allocate width x height x dpr^2 -- tens of megabytes for a
      // chart that is replaced on every render and on every scan tick. 2 is the
      // point of diminishing returns; above it the extra pixels are invisible.
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const bw = Math.round(cssW * dpr);
      const bh = Math.round(cssH * dpr);
      if (canvas.width !== bw) canvas.width = bw;
      if (canvas.height !== bh) canvas.height = bh;
      canvas.style.width = cssW + "px";
      canvas.style.height = cssH + "px";
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const layout = drawStackedBars(ctx, cssW, cssH, days, series, trend.unit || "day");
      renderLegend(series);
      bindTooltip(canvas, ctx, cssW, cssH, days, series, layout);

      // When the plot is wider than the viewport, the newest data is on the
      // RIGHT -- which is where a reader of "all time" wants to land, not at
      // the left edge looking at the oldest bar. Only scroll when the bucket
      // set actually changed: the 30s auto-refresh re-draws the same range and
      // must not yank the chart back while the user is reading the past.
      const key = `${trend.range}|${days.length}|${days[days.length - 1] || ""}`;
      const firstPaint = state._chartScrollKey !== key;
      state._chartScrollKey = key;
      if (firstPaint && scroller && scroller.scrollWidth > scroller.clientWidth + 1) {
        // Two frames: assigning scrollLeft in the same tick as the canvas width
        // change lands against the OLD scrollWidth, leaving the view short of
        // the end. The second assignment after layout settles reaches it.
        const toEnd = () => { scroller.scrollLeft = scroller.scrollWidth; };
        toEnd();
        requestAnimationFrame(() => requestAnimationFrame(toEnd));
      }
    };

    draw();
    // ResizeObserver, not window.resize: the panel also changes size when the
    // table above it reflows or the window is resized without the viewport
    // height changing -- both used to leave the canvas stretched.
    if (state._chartObserver) state._chartObserver.disconnect();
    if (typeof ResizeObserver === "function") {
      let timer = null;
      state._chartObserver = new ResizeObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(draw, 120);
      });
      state._chartObserver.observe($("chartScroller") || canvas.parentElement);
    }
  }

  function renderLegend(series) {
    const box = $("chartLegend");
    if (!box) return;
    box.replaceChildren();
    for (const s of series) {
      const item = document.createElement("span");
      item.className = "legend-item";
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.background = s.colour;
      const label = document.createElement("span");
      const total = s.values.reduce((a, b) => a + (b || 0), 0);
      label.textContent = `${s.name} · ${fmt(total)}`;
      item.appendChild(swatch);
      item.appendChild(label);
      box.appendChild(item);
    }
  }

  function bindTooltip(canvas, ctx, cssW, cssH, days, series, layout) {
    const tip = $("chartTip");
    if (!tip) return;
    const panel = tip.parentElement;   // .chart-panel -- the tooltip's offset parent
    const boxes = layout.boxes;
    canvas.onmousemove = (ev) => {
      const r = canvas.getBoundingClientRect();
      const mx = ev.clientX - r.left;
      const my = ev.clientY - r.top;
      const hit = boxes.find((b) => mx >= b.x - 6 && mx <= b.x + b.w + 6
        && my >= b.y && my <= b.y + b.h);
      if (!hit) { tip.hidden = true; return; }
      tip.hidden = false;
      tip.replaceChildren();
      const head = document.createElement("div");
      head.className = "tip-head";
      head.textContent = `${hit.title} · ${fmt(hit.total)}`;
      tip.appendChild(head);
      // A bar can be split across every Agent that spent tokens that day, so
      // the row count is bounded by the SPACE, not by a fixed number: the panel
      // is only ~240-336px tall and clips its children, so a tooltip taller
      // than the panel loses rows at an edge no matter where it is placed.
      //
      // The trimming is driven by the browser's own measurement rather than by
      // an assumed line height: fill every row, then drop the tail until the
      // box fits the panel, folding what was dropped into a count. Guessing the
      // row height would drift with the font size and the zoom level.
      const pad = 8;
      const avail = Math.max(48, panel.clientHeight - pad * 2);
      const rows = hit.parts.map((p) => {
        const row = document.createElement("div");
        row.className = "tip-row";
        const sw = document.createElement("span");
        sw.className = "legend-swatch";
        sw.style.background = p.colour;
        const txt = document.createElement("span");
        txt.textContent = `${p.label}: ${fmt(p.value)}`;
        row.appendChild(sw);
        row.appendChild(txt);
        return row;
      });
      for (const row of rows) tip.appendChild(row);

      let folded = 0;
      while (folded < rows.length - 1 && tip.offsetHeight > avail) {
        tip.removeChild(rows[rows.length - 1 - folded]);
        folded += 1;
      }
      if (folded) {
        const more = document.createElement("div");
        more.className = "tip-row tip-more";
        more.textContent = t("chart.more", { n: fmt(folded) });
        tip.appendChild(more);
        // The count row costs height too; one more may have to go.
        while (folded < rows.length - 1 && tip.offsetHeight > avail) {
          tip.removeChild(rows[rows.length - 1 - folded]);
          more.textContent = t("chart.more", { n: fmt(++folded) });
        }
      }

      // Position against the PANEL, not the canvas: the canvas scrolls inside
      // its wrapper, so canvas-relative coordinates would leave the tooltip
      // behind after a sideways scroll.
      //
      // The panel clips its children (`overflow: hidden`, for the rounded
      // corners), so the tooltip has to stay inside it on BOTH axes. Measuring
      // after the content is in place is what makes that possible; flipping to
      // the other side of the cursor is what keeps it near the pointer when the
      // bar sits against an edge.
      const pr = panel.getBoundingClientRect();
      const gap = 14;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      const curbX = ev.clientX - pr.left;
      const curbY = ev.clientY - pr.top;

      let px = curbX + gap;
      if (px + tw > pr.width - pad) px = curbX - tw - gap;   // flip left
      px = Math.max(pad, Math.min(px, Math.max(pad, pr.width - tw - pad)));

      let py = curbY + gap;
      if (py + th > pr.height - pad) py = curbY - th - gap;  // flip above
      py = Math.max(pad, Math.min(py, Math.max(pad, pr.height - th - pad)));

      tip.style.left = px + "px";
      tip.style.top = py + "px";
    };
    canvas.onmouseleave = () => { tip.hidden = true; };
  }

  /**
   * The source list: one tile per agent, the vendor's official mark above the
   * agent's name. Rendering it as a grid rather than a "/"-separated sentence
   * is what the user asked for -- and it doubles as a visible check that every
   * agent really does have its own official icon.
   */
  function renderSourceList() {
    const list = $("sourceList");
    if (!list) return;
    list.replaceChildren();
    for (const a of state.agents) {
      const li = document.createElement("li");
      li.className = "source-item";
      // The name is right below the icon, so the icon stays decorative.
      li.appendChild(iconNode(a.name, 24, true));
      const name = document.createElement("span");
      name.className = "source-name";
      name.textContent = a.display_name;
      li.appendChild(name);
      const ic = state.icons[a.name];
      if (ic && !ic.official) {
        li.classList.add("is-unofficial");
        li.title = t("icon.unofficial");
      }
      list.appendChild(li);
    }
  }

  function renderFooter() {
    renderSourceList();

    // How much of the provider column is evidence and how much is a guess.
    // Stated next to the data instead of buried in documentation.
    const stats = (state.data && state.data.provider_stats) || null;
    const el = $("providerNote");
    if (!el) return;
    if (!stats || !stats.totals) {
      el.textContent = "";
      return;
    }
    const inferred = stats.totals.inferred || 0;
    const unknown = stats.totals[""] || 0;
    // Only the guesses are worth a note. ``measured`` is the normal case and
    // ``product`` is structural, so neither gets a line: they were noise.
    el.textContent = [
      inferred ? t("prov.statsInferred", { inferred: fmt(inferred) }) : "",
      unknown ? t("prov.statsUnknown", { unknown: fmt(unknown) }) : "",
    ].filter(Boolean).join(" · ");

    // Where the money figures came from. A cost is only as good as the price
    // behind it, so the vendors and the date are stated rather than implied.
    const pricing = (state.data && state.data.pricing) || null;
    const pe = $("pricingNote");
    if (pe) {
      if (!pricing || !pricing.model_count) {
        pe.textContent = "";
      } else {
        const vendors = Object.keys(pricing.sources || {}).join(" · ");
        pe.textContent = t("footer.pricing", {
          date: pricing.updated || "", vendors,
        });
      }
    }

    // Time-of-day pricing, stated explicitly. Without this line a DeepSeek
    // figure that differs from a flat-rate hand calculation looks like a bug;
    // with it, the reader knows the window is applied per request.
    const peakEl = $("peakNote");
    if (peakEl) {
      const schedules = (pricing && pricing.peak_hours) || {};
      const parts = Object.values(schedules).map((s) => {
        const windows = (s.utc_windows || [])
          .map(([a, b]) => `${a}–${b}`).join(" / ");
        return t("footer.peakRange", { vendor: s.vendor, windows });
      }).filter(Boolean);
      peakEl.textContent = parts.length
        ? t("footer.peak", { schedule: parts.join("；") })
        : "";
    }
  }

  /* ------------------------------------------------------- section jump bar */

  /** Build the jump links once from NAV_SECTIONS; labels come from i18n. */
  function renderSectionNav() {
    const nav = $("sectionNav");
    if (!nav) return;
    nav.replaceChildren();
    for (const section of NAV_SECTIONS) {
      const a = document.createElement("a");
      a.href = `#${section.id}`;
      a.dataset.navKey = section.key;
      a.textContent = t(section.key);
      nav.appendChild(a);
    }
  }

  /** Refresh the link labels after a language switch (no rebuild needed). */
  function refreshSectionNavLabels() {
    const nav = $("sectionNav");
    if (!nav) return;
    for (const a of nav.querySelectorAll("a[data-nav-key]")) {
      a.textContent = t(a.dataset.navKey);
    }
  }

  /**
   * Highlight the section currently in view (scroll-spy).
   *
   * IntersectionObserver only -- no scroll listener computing offsets. A thin
   * band just under the sticky topbar is the "active" line: a section is current
   * once its heading crosses into that band. Because a short final section may
   * never reach the top of a long page, a separate bottom sentinel highlights the
   * last entry when the page is scrolled to the bottom.
   */
  function initSectionSpy() {
    const nav = $("sectionNav");
    if (!nav || !("IntersectionObserver" in window)) return;
    const links = [...nav.querySelectorAll("a[data-nav-key]")];
    const targets = NAV_SECTIONS
      .map((s) => document.getElementById(s.id))
      .filter(Boolean);
    const footer = document.querySelector("main > footer");

    const setCurrent = (id) => {
      for (const a of links) {
        const active = a.hash === `#${id}`;
        a.classList.toggle("is-current", active);
        // The visual state must be mirrored for assistive tech (scrollspy guide).
        if (active) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      }
    };

    // The topbar wraps to two lines on narrow windows (the status line is on its
    // own row), so its real height is taller than the CSS constant --topbar-h.
    // Measure it and feed the value back to scroll-padding-block-start, otherwise
    // a jumped-to heading lands under the sticky bar. The same measurement drives
    // the observer's top band, so "current" and "landing position" agree.
    // Resolve a rem-based custom property to PIXELS. getPropertyValue() on an
    // *unregistered* custom property returns the raw token stream ("1rem"),
    // not a resolved length -- parseFloat("1rem") is 1, which silently made the
    // gap 1px instead of 16px. A throwaway probe element forces the browser to
    // do the unit conversion, which is the only reliable way short of
    // registering each token with @property.
    const readTokenPx = (name, fallback) => {
      const raw = getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim();
      if (!raw) return fallback;
      // Already a length the browser has resolved (e.g. "16px"): use it.
      const direct = /^-?\d*\.?\d+px$/i.test(raw) ? parseFloat(raw) : NaN;
      if (Number.isFinite(direct)) return direct;
      // Otherwise let the browser convert the token for us.
      const probe = document.createElement("div");
      probe.setAttribute("aria-hidden", "true");
      probe.style.cssText =
        "position:absolute;visibility:hidden;pointer-events:none;padding:0;border:0";
      probe.style.paddingTop = raw;
      document.body.appendChild(probe);
      const resolved = parseFloat(getComputedStyle(probe).paddingTop);
      probe.remove();
      return Number.isFinite(resolved) ? resolved : fallback;
    };

    let lastTopbarH = 0;
    let lastNavH = 0;
    const measureBars = () => {
      const bar = document.querySelector(".topbar");
      const barH = bar ? Math.round(bar.getBoundingClientRect().height) : 60;
      if (barH !== lastTopbarH) {
        lastTopbarH = barH;
        document.documentElement.style.setProperty("--topbar-h", `${barH}px`);
      }
      // The nav is sticky under the topbar; its height also feeds scroll-padding
      // and the spy band, so a jumped-to heading clears both bars.
      const navH = nav ? Math.round(nav.getBoundingClientRect().height) : 0;
      if (navH !== lastNavH) {
        lastNavH = navH;
        document.documentElement.style.setProperty("--nav-h", `${navH}px`);
      }
      return barH + navH;
    };

    let spyObserver = null;
    let bottomObserver = null;
    const buildObservers = () => {
      if (spyObserver) spyObserver.disconnect();
      if (bottomObserver) bottomObserver.disconnect();
      // Same gap the stylesheet puts between the bars and the heading.
      const gap = readTokenPx("--space-4", 16);
      const topInset = measureBars() + gap;

      // Which headings are inside the band right now. IntersectionObserver
      // delivers only the entries whose state CHANGED, so the callback's array
      // is a delta, not the current set. Treating it as the set is a real bug:
      // after jumping to a heading, the next one down enters the band and the
      // callback sees only that new entry, so the highlight jumps ahead to a
      // section the reader is not looking at. Keep the whole set instead.
      const inBand = new Set();
      const pickTopmost = () => {
        let best = null;
        let bestTop = Infinity;
        for (const el of inBand) {
          const top = el.getBoundingClientRect().top;
          if (top < bestTop) {
            bestTop = top;
            best = el;
          }
        }
        if (best) setCurrent(best.id);
      };

      spyObserver = new IntersectionObserver((entries) => {
        let changed = false;
        for (const e of entries) {
          if (e.isIntersecting) {
            if (!inBand.has(e.target)) {
              inBand.add(e.target);
              changed = true;
            }
          } else if (inBand.delete(e.target)) {
            changed = true;
          }
        }
        if (changed) pickTopmost();
      }, {
        // Band from just under the sticky bar to ~45% down the viewport. The
        // jumped-to heading sits at topbar+16px, comfortably inside it.
        rootMargin: `-${topInset}px 0px -55% 0px`,
        threshold: 0,
      });
      targets.forEach((el) => spyObserver.observe(el));

      // Bottom-of-page fallback: the last section may be too short to ever reach
      // the band, so highlight it once the footer comes into view.
      if (footer && NAV_SECTIONS.length) {
        bottomObserver = new IntersectionObserver((entries) => {
          if (entries.some((e) => e.isIntersecting)) {
            setCurrent(NAV_SECTIONS[NAV_SECTIONS.length - 1].id);
          }
        }, { rootMargin: "0px 0px -15% 0px", threshold: 0 });
        bottomObserver.observe(footer);
      }
    };

    measureBars();
    buildObservers();

    // Reflect a click immediately (the observer catches up during the scroll),
    // so the highlight follows the finger even before scrolling settles.
    nav.addEventListener("click", (ev) => {
      const a = ev.target.closest("a[data-nav-key]");
      if (a && a.hash) setCurrent(a.hash.slice(1));
    });

    // The bar's height changes when the status line wraps/unwraps or the font
    // size setting changes. Keep scroll-padding and the spy band in sync.
    if ("ResizeObserver" in window) {
      const ro = new ResizeObserver(() => {
        const bar2 = document.querySelector(".topbar");
        const barH = bar2 ? Math.round(bar2.getBoundingClientRect().height) : 60;
        const navH = nav ? Math.round(nav.getBoundingClientRect().height) : 0;
        if (barH !== lastTopbarH || navH !== lastNavH) buildObservers();
      });
      const bar = document.querySelector(".topbar");
      if (bar) ro.observe(bar);
      ro.observe(nav);
    }

    // Initial state: highlight the first entry until the observer reports.
    if (NAV_SECTIONS.length) setCurrent(NAV_SECTIONS[0].id);
  }

  /* ------------------------------------------------------------- i18n pass */

  function applyI18n() {
    document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
    document.title = t("app.title");

    for (const el of document.querySelectorAll("[data-i18n]")) {
      const key = el.dataset.i18n;
      // Preserve child elements (e.g. the refresh icon) by only replacing
      // text when the target has no element children.
      if (el.children.length === 0) el.textContent = t(key);
      else {
        const tn = [...el.childNodes].find((n) => n.nodeType === 3 && n.textContent.trim());
        if (tn) tn.textContent = " " + t(key);
      }
    }
    for (const el of document.querySelectorAll("[data-i18n-aria]")) {
      el.setAttribute("aria-label", t(el.dataset.i18nAria));
    }
    // title/alt are user-visible too: a hard-coded tooltip stays English in
    // Chinese mode. aria-hidden does not hide a native tooltip.
    for (const el of document.querySelectorAll("[data-i18n-title]")) {
      el.setAttribute("title", t(el.dataset.i18nTitle));
    }
    for (const el of document.querySelectorAll("[data-i18n-alt]")) {
      el.setAttribute("alt", t(el.dataset.i18nAlt));
    }
    // Jump-bar labels are generated from NAV_SECTIONS, not data-i18n markup.
    refreshSectionNavLabels();
    // Range buttons / lang buttons keep their own pressed state.
    for (const b of $("langSwitch").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
    }
    // The settings panel is built from the string table, so its labels have to
    // be rebuilt when the language changes. Re-rendering also re-checks the
    // right radio, which keeps the stored value and the visible state in sync.
    if ($("settingsBody")) renderSettings();
  }

  /* ------------------------------------------------------------------ data */

  async function load() {
    try {
      const [overview, trend] = await Promise.all([
        api(`/api/overview?range=${encodeURIComponent(state.range)}`),
        api(`/api/trend?range=${encodeURIComponent(state.range)}`),
      ]);
      state.data = overview;
      state.trend = trend;
      renderCards(overview.totals || {});
      renderTables(overview);
      renderChart(trend);
      renderFooter();
      setStatus(t("status.loaded", { rows: fmt(overview.db_rows), time: time(overview.generated_at) }));
    } catch (e) {
      setStatus(t("status.loadFailed", { err: e.message }));
    }
  }

  async function refresh() {
    setStatus(t("status.scanning"), true);
    try {
      const r = await api("/api/refresh", { method: "POST" });
      if (r.skipped) { setStatus(t("status.scanSkipped")); return; }
      await load();
      setStatus(t("status.scanDone", { new: fmt(r.inserted), updated: fmt(r.updated), time: time(r.at) }));
    } catch (e) {
      setStatus(t("status.scanFailed", { err: e.message }));
    } finally {
      $("refreshBtn").disabled = false;
    }
  }

  async function setLang(lang) {
    state.lang = lang;
    localStorage.setItem("atr.lang", lang);
    try {
      const tbl = await api(`/api/i18n?lang=${encodeURIComponent(lang)}`);
      state.S = tbl.strings || {};
    } catch { state.S = {}; }
    applyI18n();
    // Re-render with the new language (tables carry translated headers).
    if (state.data) {
      renderTables(state.data);
      renderFooter();
      renderChart(state.trend || { days: [], agents: [], series: {} });
      // The status line was written in the previous language; rewrite it so the
      // bar does not keep showing a stale "Loading…" after switching.
      setStatus(t("status.loaded", {
        rows: fmt(state.data.db_rows),
        time: time(state.data.generated_at),
      }));
    }
    for (const b of $("langSwitch").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.lang === lang));
    }
  }

  /* ------------------------------------------------- settings dialog (dom)

     Built from SETTINGS, one branch per control type:

     * ``segmented`` -- a row of radio inputs. Radio gives arrow-key navigation
       and one accessible name per group for free, and the label wraps the input
       so the whole segment is the hit target. Right for 2-4 short choices.
     * ``select`` -- a native <select> with <optgroup> sections. Right when the
       list is long (19 typefaces): the OS supplies search/type-ahead, keyboard
       navigation and a scrolling popup, none of which a hand-rolled menu gets
       right. Each option is rendered IN its own typeface via an inline style, so
       the list previews itself instead of showing 19 opaque names. */
  function renderSettings() {
    const body = $("settingsBody");
    if (!body) return;
    body.replaceChildren();

    const current = readSettings();
    for (const s of SETTINGS) {
      const group = document.createElement("fieldset");
      group.className = "setting";
      group.dataset.setting = s.key;

      const legend = document.createElement("legend");
      legend.className = "setting-label";
      legend.textContent = t(s.label);
      group.appendChild(legend);

      if (s.hint) {
        const hint = document.createElement("p");
        hint.className = "setting-hint";
        hint.id = `setting-${s.key}-hint`;
        hint.textContent = t(s.hint);
        group.appendChild(hint);
        group.setAttribute("aria-describedby", hint.id);
      }

      if (s.type === "select") {
        group.appendChild(buildSelectSetting(s, current[s.key]));
      } else {
        group.appendChild(buildSegmentedSetting(s, current[s.key]));
      }
      body.appendChild(group);
    }
  }

  /** Radio-row control for a short list of choices. */
  function buildSegmentedSetting(s, currentValue) {
    const seg = document.createElement("div");
    seg.className = "setting-seg";
    for (const opt of s.options) {
      const label = document.createElement("label");
      label.className = "setting-opt";

      const input = document.createElement("input");
      input.type = "radio";
      input.name = `setting-${s.key}`;
      input.value = opt.value;
      input.checked = currentValue === opt.value;
      // The visible span carries the label, so the input itself is only the
      // state holder; the wrapping <label> supplies its accessible name.
      input.addEventListener("change", () => {
        if (!input.checked) return;
        settingStore.write(s, opt.value);
        applySetting(s, opt.value);
        markSettingsDirty(s.key, opt.value);
      });
      label.appendChild(input);

      const span = document.createElement("span");
      span.textContent = t(opt.label);
      label.appendChild(span);
      seg.appendChild(label);
    }
    return seg;
  }

  /** Native <select> with grouped options, each previewed in its own face. */
  function buildSelectSetting(s, currentValue) {
    const wrap = document.createElement("div");
    wrap.className = "setting-select";

    const select = document.createElement("select");
    select.className = "setting-dropdown";
    select.name = `setting-${s.key}`;
    select.id = `setting-${s.key}`;

    let currentGroup = null;
    let optgroup = null;
    for (const opt of s.options) {
      if (opt.group !== currentGroup) {
        currentGroup = opt.group;
        optgroup = document.createElement("optgroup");
        optgroup.label = t(opt.group);
        select.appendChild(optgroup);
      }
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = t(opt.label);
      // Preview the face in the list itself. <option> honours font-family in
      // Chromium, which is the engine inside the desktop window. Where it is
      // ignored the option still reads correctly, just in the default face.
      if (opt.stack) o.style.fontFamily = opt.stack;
      o.selected = opt.value === currentValue;
      optgroup.appendChild(o);
    }

    select.addEventListener("change", () => {
      settingStore.write(s, select.value);
      applySetting(s, select.value);
      markSettingsDirty(s.key, select.value);
    });

    wrap.appendChild(select);
    return wrap;
  }

  /** Reflect a changed value in the visible control, whatever its shape. */
  function markSettingsDirty(key, value) {
    const group = document.querySelector(`#settingsBody [data-setting="${key}"]`);
    if (!group) return;
    // Radio rows: check the matching input.
    for (const input of group.querySelectorAll("input[type=radio]")) {
      input.checked = input.value === value;
    }
    // Dropdown: move the selection. Without this a reset would apply the value
    // to the page while the closed <select> kept showing the old one.
    for (const sel of group.querySelectorAll("select")) {
      sel.value = value;
    }
  }

  function resetSettings() {
    for (const s of SETTINGS) {
      settingStore.clear(s);
      applySetting(s, s.fallback);
    }
    renderSettings();
  }

  /**
   * Wire the dialog once. Native <dialog> + showModal() already provides the
   * focus trap, top layer, Esc/back-gesture dismissal and focus return to the
   * invoker, so none of that is reimplemented here. The only additions are the
   * fallback open path for browsers without Invoker Commands, and a re-render
   * on open so the controls show the values that survived a reload.
   */
  function wireSettingsDialog() {
    const dlg = $("settingsDialog");
    const opener = $("settingsBtn");
    if (!dlg || !opener) return;

    // Invoker Commands arrived 2025; where it is missing `commandfor` does
    // nothing, so opening would silently fail on an older browser.
    if (!("commandForElement" in HTMLButtonElement.prototype)) {
      opener.addEventListener("click", () => dlg.showModal());
    }

    // Values may have been changed in another tab; re-read on every open.
    dlg.addEventListener("beforetoggle", (e) => {
      if (e.newState === "open") renderSettings();
    });
    // Keep the fallback working where `beforetoggle` is unsupported.
    opener.addEventListener("click", () => renderSettings());

    $("settingsReset").addEventListener("click", resetSettings);
    initUpdateBox();

    dlg.addEventListener("close", () => {
      // Focus return is the browser's job; this only covers the case where the
      // dialog was closed by clicking the backdrop while focus sat inside.
      if (!dlg.open && document.activeElement === document.body) opener.focus();
    });
  }

  /* ----------------------------------------------------------------- update

     The desktop build and the web build share one rule: ask the server, never
     call GitHub from the page. The server owns the daily cache and the request
     itself (updates.py), so the browser stays same-origin and this module needs
     no CORS handling, no token and no fetch of a third-party host.

     The server also checks once at startup, so opening Settings usually shows a
     result immediately; the button is for "I want to know RIGHT now", which
     forces past the cache. */

  function renderUpdate(status) {
    const line = $("updateStatus");
    const dl = $("updateDownload");
    const apply = $("updateApply");
    const hint = $("updateApplyHint");
    const meta = $("updateMeta");
    if (!line) return;
    const v = status.current_version || "";

    // Which affordance to offer depends on how this copy is running. A checkout
    // updates with git pull; only the packaged build can run an installer.
    const installed = !!status.installed_build;
    const canApply = installed && !!status.installer_url;

    const hideActions = () => {
      if (dl) dl.hidden = true;
      if (apply) apply.hidden = true;
      if (hint) hint.hidden = true;
    };

    line.dataset.state = status.state || "unknown";
    switch (status.state) {
      case "checking":
        line.textContent = t("update.checking");
        hideActions();
        meta.textContent = t("update.current", { version: v });
        break;
      case "update_available":
        line.textContent = t("update.available", {
          latest: status.latest_version, current: v,
        });
        if (dl) dl.href = status.latest_url || "#";
        if (canApply) {
          // One click does the rest; the UAC prompt is the only stop, and the
          // hint says so before it appears rather than after.
          if (apply) apply.hidden = false;
          if (dl) dl.hidden = true;
          if (hint) {
            hint.hidden = false;
            hint.textContent = t("update.uacHint");
          }
        } else {
          if (apply) apply.hidden = true;
          if (dl) dl.hidden = false;
          if (hint) {
            hint.hidden = false;
            hint.textContent = t("update.sourceHint");
          }
        }
        meta.textContent = status.stale
          ? t("update.unavailableHint")
          : t("update.checkedAt", { time: time(Number(status.checked_at) * 1000) });
        break;
      case "up_to_date":
        line.textContent = t("update.upToDate", { version: v });
        hideActions();
        meta.textContent = status.stale
          ? t("update.unavailableHint")
          : t("update.checkedAt", { time: time(Number(status.checked_at) * 1000) });
        break;
      case "unavailable":
        // Never an error dialog: an offline machine is a normal machine.
        line.textContent = t("update.unavailable");
        hideActions();
        meta.textContent = `${t("update.current", { version: v })} · ${t("update.unavailableHint")}`;
        break;
      case "disabled":
        // The operator switched checking off on purpose; say so instead of
        // implying the network is broken.
        line.textContent = t("update.disabled");
        hideActions();
        meta.textContent = t("update.current", { version: v });
        break;
      default:
        line.textContent = t("update.never");
        hideActions();
        meta.textContent = t("update.current", { version: v });
    }
    $("updateCheck").textContent =
      status.state === "unknown" ? t("update.check") : t("update.recheck");

    // Surface "an update exists" on the toolbar button itself, so the notice is
    // visible without opening the panel. The button also carries the running
    // version, which is the answer when everything is current.
    const badge = $("updateBadge");
    const toggle = $("updateBtn");
    const versionEl = $("updateVersion");
    const available = status.state === "update_available";
    if (versionEl && v) versionEl.textContent = `v${String(v).replace(/^v/i, "")}`;
    if (badge) badge.hidden = !available;
    if (toggle) {
      if (available) toggle.dataset.update = "available";
      else delete toggle.dataset.update;
    }
  }

  function initUpdateBox() {
    const btn = $("updateCheck");
    const applyBtn = $("updateApply");
    const toggle = $("updateBtn");
    const panel = $("updatePanel");
    const badge = $("updateBadge");
    if (!btn || !toggle || !panel) return;

    // The server is the authority on the running version (it reports it in
    // every /api/update response), so this module never hard-codes one.
    let current = "";
    let lastStatus = null;

    const ask = async (force) => {
      renderUpdate({ state: "checking", current_version: current });
      btn.disabled = true;
      try {
        const r = await api(`/api/update${force ? "?force=true" : ""}`);
        if (r && r.current_version) current = r.current_version;
        lastStatus = r || { state: "unavailable", current_version: current };
        renderUpdate(lastStatus);
      } catch {
        // A failed request is the same user-facing situation as no network.
        renderUpdate({ state: "unavailable", current_version: current });
      } finally {
        btn.disabled = false;
      }
    };

    // Poll the download while it runs. The installer replaces this process, so
    // "launched" is the last state the page will ever see.
    const watchApply = () => {
      const line = $("updateStatus");
      const hint = $("updateApplyHint");
      const tick = async () => {
        let s;
        try {
          s = await api("/api/update/apply");
        } catch {
          return; // the process is being replaced; stop polling quietly
        }
        if (!s) return;
        if (s.phase === "downloading") {
          if (line) line.textContent = t("update.applying");
        } else if (s.phase === "launching" || s.phase === "launched") {
          if (line) line.textContent = t("update.launching");
          if (hint) {
            hint.hidden = false;
            hint.textContent = t("update.uacHint");
          }
          return; // the installer owns the rest
        } else if (s.phase === "failed") {
          if (line) line.textContent = t("update.applyFailed", { err: s.error || "" });
          if (applyBtn) applyBtn.disabled = false;
          return;
        }
        setTimeout(tick, 700);
      };
      setTimeout(tick, 400);
    };

    if (applyBtn) {
      applyBtn.addEventListener("click", async () => {
        applyBtn.disabled = true;
        if (lastStatus) {
          renderUpdate({ ...lastStatus, state: "checking" });
        }
        try {
          const r = await api("/api/update/apply", { method: "POST" });
          if (!r || !r.ok) {
            const line = $("updateStatus");
            if (line) {
              line.textContent = t("update.applyFailed", {
                err: (r && r.message) || "",
              });
            }
            applyBtn.disabled = false;
            return;
          }
          watchApply();
        } catch {
          applyBtn.disabled = false;
        }
      });
    }

    const setOpen = (open) => {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      // Close on Escape and on a click outside, the way a popover is expected
      // to behave. Not a <dialog>: nothing here should trap focus or dim the
      // page for a version number.
      if (open) {
        document.addEventListener("keydown", onKey, true);
        document.addEventListener("pointerdown", onOutside, true);
      } else {
        document.removeEventListener("keydown", onKey, true);
        document.removeEventListener("pointerdown", onOutside, true);
      }
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        setOpen(false);
        toggle.focus();
      }
    };
    const onOutside = (ev) => {
      if (!panel.contains(ev.target) && !toggle.contains(ev.target)) {
        setOpen(false);
      }
    };

    toggle.addEventListener("click", () => {
      const willOpen = panel.hidden;
      setOpen(willOpen);
      // Opening is what asks. Reading the cache is cheap and unforced, so this
      // never turns into a network request more than once a day.
      if (willOpen) ask(false);
    });

    btn.addEventListener("click", () => ask(true));

    // Warm the badge from the cache the startup check filled. Deliberately not
    // forced: that would make every page load a network request.
    ask(false);
  }

  /* ------------------------------------------------------------------ init */

  async function init() {
    // Icons + adapters first: the tables need them.
    const [icons, agents] = await Promise.all([
      api("/api/icons").catch(() => ({})),
      api("/api/adapters").catch(() => []),
    ]);
    state.icons = icons || {};
    state.agents = agents || [];

    // Build the jump bar before setLang(): applyI18n() refreshes its labels, so
    // the links must exist by then. Wire the observer after the DOM is stable.
    renderSectionNav();
    initSectionSpy();

    // The canvas is painted once per render, so a light/dark flip has to
    // trigger a repaint: the palette and the icon variants both depend on it.
    watchColorScheme(() => {
      if (!state.data) return;
      renderTables(state.data);
      renderChart(state.trend || { days: [], agents: [], series: {} });
    });

    await setLang(state.lang);

    try {
      const s = await api("/api/scan/status");
      state.interval = s.interval_seconds || 30;
      $("intervalLabel").textContent = `${state.interval}s`;
    } catch { /* keep default */ }

    $("rangeNav").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-range]");
      if (!btn) return;
      state.range = btn.dataset.range;
      for (const b of $("rangeNav").querySelectorAll("button")) {
        b.setAttribute("aria-pressed", String(b === btn));
      }
      load();
    });

    $("langSwitch").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-lang]");
      if (btn && btn.dataset.lang !== state.lang) setLang(btn.dataset.lang);
    });

    $("refreshBtn").addEventListener("click", refresh);
    $("autoScan").addEventListener("change", (ev) => {
      if (ev.target.checked) startAuto(); else stopAuto();
    });

    // Settings are applied before the first data load so the page never paints
    // at the default size and then jump (the inline <head> script has already
    // done this for a cold load; this covers a same-document re-init).
    applyAllSettings();
    wireSettingsDialog();

    function startAuto() {
      stopAuto();
      state.timer = setInterval(() => {
        // The server already runs its own scan loop on this same interval
        // (`serve --interval`, reported by /api/scan/status). Triggering a
        // second scan from here made the two loops fight over the same lock:
        // one of them lost every round, which is why "previous scan still
        // running; skipped" showed up in normal operation. Just re-read.
        load();
      }, state.interval * 1000);
    }
    function stopAuto() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }
    startAuto();

    await load();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
