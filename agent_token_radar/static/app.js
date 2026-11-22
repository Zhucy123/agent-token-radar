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
    const hit = Number(totals.cache_hit || 0);
    $("v-hit").textContent = `${(hit * 100).toFixed(hit >= 0.995 ? 0 : 1)}%`;
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
   * "100%" than as "99.99%". Zero (or a missing value) renders "0%".
   */
  function pct(ratio) {
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
   */
  function fillTable(tableId, headers, rows, numericFrom, firstIsAgent = false, agentCol = -1) {
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
      ]), 1, true);

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
      ]), 4, false, 2);;

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
    // the user's browser font-size preference just like the DOM text.
    ctx.font = "0.6875rem system-ui, -apple-system, 'Segoe UI', sans-serif";
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
    // Range buttons / lang buttons keep their own pressed state.
    for (const b of $("langSwitch").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
    }
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

  /* ------------------------------------------------------------------ init */

  async function init() {
    // Icons + adapters first: the tables need them.
    const [icons, agents] = await Promise.all([
      api("/api/icons").catch(() => ({})),
      api("/api/adapters").catch(() => []),
    ]);
    state.icons = icons || {};
    state.agents = agents || [];

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
