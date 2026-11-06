/* Market Signal Desk - live dashboard frontend (Upstox feed, no mock data) */
"use strict";

// ngrok's free tunnel shows an interstitial to browsers; this header skips it for API calls.
const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const headers = new Headers((init && init.headers) || {});
  headers.set("ngrok-skip-browser-warning", "1");
  return nativeFetch(input, Object.assign({}, init, { headers }));
};

const state = { data: null, selected: "NIFTY", auto: true, timer: null, research: null };

const $ = (id) => document.getElementById(id);
const fmt = (n) => n == null ? "—" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (n) => n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtCompact(n) {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e7) return (n / 1e7).toFixed(2) + " Cr";
  if (a >= 1e5) return (n / 1e5).toFixed(2) + " L";
  if (a >= 1e3) return (n / 1e3).toFixed(1) + " K";
  return String(n);
}

function istParts() {
  const s = new Date().toLocaleString("en-GB", { timeZone: "Asia/Kolkata", hour12: false });
  const [h, m] = s.split(" ")[1].split(":").map(Number);
  const wd = new Date().toLocaleDateString("en-US", { timeZone: "Asia/Kolkata", weekday: "short" });
  return { h, m, dow: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(wd) };
}

function clockTick() {
  const t = new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false });
  $("clock").textContent = `${t} IST`;
  const p = istParts();
  const open = p.dow >= 1 && p.dow <= 5 && (p.h * 60 + p.m) >= 555 && (p.h * 60 + p.m) <= 930;
  $("mktStatus").classList.toggle("open", open);
  $("mktStatus").classList.toggle("closed", !open);
  $("mktStatusText").textContent = open ? "MARKET OPEN" : "MARKET CLOSED";
}

function fmtTimeMs(ms) {
  if (!ms) return "—";
  return new Date(Number(ms)).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false }) + " IST";
}

async function load(force) {
  try {
    const r = await fetch("/api/market" + (force ? "?force=1" : ""));
    if (!r.ok) throw new Error("HTTP " + r.status);
    state.data = await r.json();
    render();
  } catch (e) {
    $("feedNotes").innerHTML = `<div class="note err">Dashboard server unreachable (${esc(e.message)}).
      Start it with <b>python server.py</b>.</div>`;
  }
}

function setAuto(on) {
  state.auto = on;
  clearInterval(state.timer);
  if (on) state.timer = setInterval(() => { if (!document.hidden) load(false); }, 5000);
}

/* ------------------------------------------------------------------ render */

function render() {
  const d = state.data;
  if (!d || !d.instruments) return;
  const items = d.instruments;
  const primes = items.filter((i) => i.group === "prime");
  const sectors = items.filter((i) => i.group === "sector");
  if (!items.find((i) => i.id === state.selected)) state.selected = "NIFTY";

  renderFeeds(d);
  $("feedNotes").innerHTML = (d.feedNotes || []).map((n) => {
    const err = /FEED DOWN|No real candles|failed/i.test(n);
    return `<div class="note ${err ? "err" : ""}">${esc(n)}</div>`;
  }).join("");

  $("primeTabs").innerHTML = primes.map((p) => {
    const cls = p.changePct == null ? "flat" : p.changePct >= 0 ? "up" : "down";
    return `<button class="tab ${p.id === state.selected ? "active" : ""}" data-id="${p.id}">
      ${p.name}<span class="tab-chg ${cls}">${p.source.candles === "ERROR" ? "no-data" : fmtPct(p.changePct)}</span></button>`;
  }).join("");
  $("primeTabs").querySelectorAll(".tab").forEach((b) => b.onclick = () => { state.selected = b.dataset.id; render(); });

  const inst = items.find((i) => i.id === state.selected);
  renderChart(inst);
  renderSignal(inst);
  renderBoard(inst);
  renderOptions(inst);
  renderRail(sectors);
}

function renderFeeds(d) {
  const pill = $("feedPill"), txt = $("feedPillText");
  const u = (d.feeds || {}).upstox || {};
  if (u.online) {
    pill.className = "feed-pill online";
    const left = u.tokenLeft;
    const t = left == null ? "" : left > 3600 ? ` · ${Math.floor(left / 3600)}h ${Math.floor((left % 3600) / 60)}m` : ` · ${Math.max(Math.floor(left / 60), 0)}m`;
    txt.textContent = "UPSTOX LIVE" + t;
  } else {
    pill.className = "feed-pill offline";
    txt.textContent = "UPSTOX OFFLINE";
    pill.title = u.error || "";
  }
}

function srcChip(el, src) {
  const cls = src === "UPSTOX" ? "chip-live"
    : src === "UPSTOX+YAHOO" ? "chip-mixed"
    : src === "YAHOO" ? "chip-yahoo" : src === "ERROR" ? "chip-error" : "chip-na";
  el.className = "chip " + cls;
  el.textContent = src;
}

/* ------------------------------------------------------------------ chart */

function istLabel(tSec, tf) {
  const d = new Date(tSec * 1000);
  return tf === "5m"
    ? d.toLocaleString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" });
}

function renderChart(inst) {
  $("chartTitle").textContent = inst.name;
  $("chartSpot").textContent = inst.spot == null ? "NO DATA" : fmt(inst.spot);
  const chg = $("chartChg");
  chg.textContent = fmtPct(inst.changePct);
  chg.className = "chg " + (inst.changePct == null ? "flat" : inst.changePct >= 0 ? "up" : "down");
  srcChip($("srcCandles"), inst.source.candles);
  $("tfChip").textContent = inst.timeframe;
  drawChart(inst);
  const last = inst.candles[inst.candles.length - 1];
  $("chartFoot").textContent =
    (last ? `${inst.candles.length} candles · ${inst.timeframe} · last bar ${istLabel(last.t, inst.timeframe)}` : "no candles")
    + ` · quote ${fmtTimeMs(inst.lastQuoteAt)}`
    + ` · updated ${state.data.istClock} IST`
    + (inst.error ? ` · feed error: ${inst.error}` : "");
}

function drawChart(inst) {
  const canvas = $("priceChart");
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const data = inst.candles.slice(-160);
  if (!data.length) {
    ctx.fillStyle = "#ef4444";
    ctx.font = "bold 15px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("No real market data for this segment", W / 2, H / 2 - 8);
    ctx.fillStyle = "#8fa0b8";
    ctx.font = "12px ui-monospace, Consolas, monospace";
    const msg = (inst.error || "feed unavailable").slice(0, 110);
    ctx.fillText(msg, W / 2, H / 2 + 16);
    return;
  }

  const sig = inst.signal;
  const plan = inst.forecast && inst.forecast.plan ? inst.forecast.plan : null;
  const padL = 8, padR = 62, padT = 10, padB = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  let lo = Infinity, hi = -Infinity;
  data.forEach((c) => { lo = Math.min(lo, c.low); hi = Math.max(hi, c.high); });
  if (sig) [sig.entry, sig.stop, sig.target1, sig.target2].forEach((v) => {
    if (v) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  });
  if (plan) [plan.entry, plan.stop, plan.target1, plan.target2, plan.target3].forEach((v) => {
    if (v) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  });
  const span = hi - lo || 1;
  lo -= span * 0.04; hi += span * 0.04;

  const X = (i) => padL + (i + 0.5) * plotW / data.length;
  const Y = (v) => padT + (hi - v) / (hi - lo) * plotH;

  ctx.font = "10px ui-monospace, Consolas, monospace";
  ctx.textAlign = "left";
  for (let g = 0; g <= 4; g++) {
    const v = lo + (hi - lo) * g / 4, y = Y(v);
    ctx.strokeStyle = "rgba(148,163,184,0.08)";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
    ctx.fillStyle = "#64748b";
    ctx.fillText(fmt(v), padL + plotW + 6, y + 3);
  }
  ctx.textAlign = "center";
  for (let g = 0; g <= 4; g++) {
    const i = Math.floor((data.length - 1) * g / 4);
    ctx.fillStyle = "#64748b";
    ctx.fillText(istLabel(data[i].t, inst.timeframe), X(i), H - 8);
  }

  const bw = Math.max(1.5, Math.min(11, plotW / data.length * 0.62));
  data.forEach((c, i) => {
    const up = c.close >= c.open;
    const col = up ? "#22c55e" : "#ef4444";
    ctx.strokeStyle = col; ctx.fillStyle = col;
    ctx.beginPath(); ctx.moveTo(X(i), Y(c.high)); ctx.lineTo(X(i), Y(c.low)); ctx.stroke();
    const top = Y(Math.max(c.open, c.close));
    const h = Math.max(1, Math.abs(Y(c.open) - Y(c.close)));
    ctx.fillRect(X(i) - bw / 2, top, bw, h);
  });

  if (!sig) return;
  const lv = plan || sig;
  const lines = [
    { v: lv.entry, c: "#cbd5e1", t: "E " },
    { v: lv.stop, c: "#ef4444", t: "SL " },
    { v: lv.target1, c: "#22c55e", t: "T1 " },
    { v: lv.target2, c: "#16a34a", t: "T2 " },
    { v: lv.target3, c: "#15803d", t: "T3 " },
  ];
  ctx.setLineDash([5, 4]);
  lines.forEach((l) => {
    if (!l.v) return;
    const y = Y(l.v);
    ctx.strokeStyle = l.c;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
    ctx.fillStyle = l.c; ctx.textAlign = "left";
    ctx.font = "bold 10px ui-monospace, Consolas, monospace";
    ctx.fillText(l.t + fmt(l.v), padL + 6, y - 4);
  });
  ctx.setLineDash([]);
}

/* ------------------------------------------------------------------ signal card */

function renderSignal(inst) {
  const s = inst.signal;
  if (!s) {
    $("signalCard").innerHTML = `
      <div class="card-head"><h3>Final signal — ${esc(inst.name)}</h3></div>
      <div class="error-state">
        <div class="big">SIGNAL UNAVAILABLE — NO REAL DATA</div>
        <div class="why">${esc(inst.error || "candle feed returned nothing")}</div>
        <div class="fix">The desk does not fabricate data. Fix the feed and it will recompute.<br>
          • Upstox token expired → <b>python upstox_login.py</b><br>
          • Check network / Upstox status page, then hit Refresh.</div>
      </div>`;
    return;
  }
  const bear = s.direction === "BEARISH";
  if (!inst.tradeable) {
    $("signalCard").innerHTML = `
      <div class="card-head"><h3>Chart context — ${esc(inst.name)}</h3>
        <span class="chip chip-tf">${s.timeframe}</span></div>
      <span class="dir-pill dir-${s.direction}">${s.direction} · CONTEXT ONLY</span>
      <div class="conf-block">
        <div class="conf-head"><span>Aggregate conviction</span><b>${s.confidence}%</b></div>
        <div class="conf-meter"><div class="conf-fill ${bear ? "bear" : ""}" style="width:${s.confidence}%"></div></div>
      </div>
      <div class="advice">This sector index is context only — it never generates calls. Entry, stop and
        T1/T2/T3 calls are issued only for Sensex, Nifty, Bank Nifty, Fin Nifty, Midcap Select and
        Next 50, each through its own six-month ≥75% audit. Use this panel for breadth and relative strength.</div>`;
    return;
  }
  const dirWord = s.direction === "BULLISH" ? "LONG" : bear ? "SHORT" : "NO TRADE";
  const hr = s.hitRate;
  const f = inst.forecast;
  const plan = f && f.plan ? f.plan : null;
  const entry = plan ? plan.entry : s.entry;
  const stop = plan ? plan.stop : s.stop;
  const t1 = plan ? plan.target1 : s.target1;
  const t2 = plan ? plan.target2 : s.target2;
  const t3 = plan ? plan.target3 : null;
  const riskPerUnit = plan ? plan.riskPerUnit : s.riskPerShare;
  const rrT1 = plan ? plan.rr1 : null;
  const measured = f && f.auditWinRate != null ? f.auditWinRate : null;
  const quality = f && f.techConviction != null ? f.techConviction : null;

  let advice;
  if (s.direction === "NEUTRAL") {
    advice = `Signals cancel out (score ${(s.score * 100).toFixed(0)}). Stand aside until the ${inst.timeframe} `
      + `structure resolves; ±18 score flips the bias.`;
  } else if (f && !f.qualified) {
    const late = f.timing === "IN PROGRESS" || f.timing === "EXTENDED";
    advice = `${s.direction} structure, but no qualifying trade: ${f.status} — expected `
      + `${fmt(f.expectedMove)} pts vs the ${f.minMove} pt minimum over ${f.horizonLabel}.`
      + (late ? " The move is already underway, so entering now is late." : "");
  } else {
    advice = `${bear ? "Short" : "Long"} from ${fmt(entry)}. Invalidation at SL ${fmt(stop)}. `
      + `Targets: T1 +${fmt(plan ? plan.target1Move : Math.abs(t1 - entry))} pts at ${fmt(t1)}, `
      + `T2 +${fmt(plan ? plan.target2Move : Math.abs(t2 - entry))} pts at ${fmt(t2)}, `
      + `T3 +${fmt(plan && plan.target3Move != null ? plan.target3Move : Math.abs(t3 - t1) * 2)} pts at ${fmt(t3)}. `
      + `Risk ${fmt(riskPerUnit)} pts/unit (R:R 1:${plan ? plan.rr1 : s.riskReward} at T1). `
      + `Levels apply to ${s.timeframe} candles.`
      + (inst.source.candles === "YAHOO" ? " Note: candle feed is the Yahoo fallback (real, delayed)." : "");
  }

  let fcHtml = "";
  if (f) {
    const est = (k) => f.estimators && f.estimators[k] != null ? fmt(f.estimators[k]) + " pts" : "—";
    fcHtml = `
      <div class="forecast ${f.qualified ? "fc-ok" : "fc-wait"}">
        <div class="fc-head">
          <span class="fc-title">Move forecast · ${esc(f.horizonLabel)}</span>
          <span class="fc-badge">${f.qualified ? "OPPORTUNITY · PRE-MOVE" : esc(f.status)}</span>
        </div>
        <div class="fc-main">
          <div class="fc-big">${fmt(f.expectedMove)}<small> pts expected</small></div>
          <div class="fc-need">minimum ${f.minMove} · target zone ${f.minMove}–${f.maxMove} pts</div>
        </div>
        <div class="fc-grid">
          <div><div class="k">Timing</div><div class="v">${esc(f.timing)}</div></div>
          <div><div class="k">Capture odds*</div><div class="v">${f.captureProb == null ? "—" : f.captureProb + "%"}
            ${f.captureSamples ? `<small>n=${f.captureSamples}</small>` : ""}</div></div>
          <div><div class="k">ATR est</div><div class="v">${est("atr")}</div></div>
          <div><div class="k">IV est</div><div class="v">${est("optionImplied")}</div></div>
        </div>
        <div class="fc-note">Estimator range ${fmt(f.estimateLow)}–${fmt(f.estimateHigh)} pts.</div>
        <ul class="fc-reasons">${(f.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <div class="fc-foot">*Real measured statistic on this instrument's own candles: share of past same-direction
          setups that covered at least ${f.minMove} pts at some point in the horizon — the stop may still be hit
          first. The <b>Proof</b> panel shows the stricter T1-before-stop win rate over <b>all calls</b>
          (a timeout closed at 15:25 counts as a non-win). Not a guarantee of future moves.</div>
      </div>`;
  }

  const oi = s.oiRange || {};
  $("signalCard").innerHTML = `
    <div class="card-head"><h3>Final signal — ${esc(inst.name)}</h3>
      <div style="display:flex;gap:6px"><span class="chip chip-tf">${s.timeframe}</span></div></div>
    <span class="dir-pill dir-${s.direction}">${s.direction} · ${dirWord}</span>
    <div class="conf-block">
      <div class="conf-head"><span>Aggregate conviction</span><b>${s.confidence}%</b></div>
      <div class="conf-meter"><div class="conf-fill ${bear ? "bear" : ""}" style="width:${s.confidence}%"></div></div>
    </div>
    <div class="levels">
      <div class="level entry-l"><div class="k">Entry</div><div class="v">${fmt(entry)}</div></div>
      <div class="level sl-l"><div class="k">Stop loss${plan ? " · " + esc(plan.stopRule || "") : ""}</div><div class="v">${fmt(stop)}</div></div>
      <div class="level t1-l"><div class="k">T1 · ${plan ? plan.target1Move + " pts" : "1R"}${rrT1 ? " · 1:" + rrT1 : ""}</div><div class="v">${fmt(t1)}</div></div>
      <div class="level t2-l"><div class="k">T2 · ${plan ? plan.target2Move + " pts · 1:" + plan.rr2 : "2R"}</div><div class="v">${fmt(t2)}</div></div>
      <div class="level t3-l"><div class="k">T3 · ${plan ? plan.target3Move + " pts · 1:" + plan.rr3 : "3R"}</div><div class="v">${fmt(t3)}</div></div>
      <div class="level"><div class="k">Risk / unit</div><div class="v">${fmt(riskPerUnit)}</div></div>
      <div class="level"><div class="k">Setup quality</div><div class="v ${quality >= 75 ? "pos" : ""}">${quality == null ? "—" : quality + "%"}</div></div>
      <div class="level"><div class="k">Measured T1 win · all calls${measured != null ? " · n=" + f.auditSamples : ""}</div><div class="v ${measured >= 75 ? "pos" : ""}">${measured == null ? "—" : measured + "%"}</div></div>
      <div class="level"><div class="k">Profile</div><div class="v pf-b-sm">${esc(f ? f.profile : "—")}</div></div>
    </div>
    <div class="advice">${advice}</div>
    ${fcHtml}
    ${oi.support || oi.resistance ? `<div class="range-chips">
        ${oi.support ? `<span class="rc sup">OI support ${fmt(oi.support)}</span>` : ""}
        ${oi.resistance ? `<span class="rc res">OI resistance ${fmt(oi.resistance)}</span>` : ""}
      </div>` : ""}
    <div class="hitrow"><span>Walk-forward hit-rate*</span>
      <span>${hr ? `<b>${hr.hitRate}%</b> over ${hr.trades} signals (${hr.horizon})` : "not enough history"}</span></div>
    <div class="hitrow"><span>Sizing idea (1% risk)</span>
      <span>₹1,00,000 capital → max loss ₹1,000 → <b>${riskPerUnit ? Math.floor(1000 / riskPerUnit) : "—"} units</b></span></div>`;
}

/* ------------------------------------------------------------------ strategy board */

function renderBoard(inst) {
  if (!inst.signal) {
    $("strategyBoard").innerHTML = `<thead><tr><th>Strategy</th><th>Reading</th><th>Vote</th><th>Weight</th></tr></thead>
      <tbody><tr><td colspan="4" class="detail">No signal — real candle data unavailable for ${esc(inst.name)}.</td></tr></tbody>`;
    return;
  }
  const rows = inst.signal.strategies.map((x) => {
    const v = x.vote > 0 ? `<span class="vote buy">▲ BUY</span>`
      : x.vote < 0 ? `<span class="vote sell">▼ SELL</span>`
      : `<span class="vote neutral">— FLAT</span>`;
    const w = x.weight ? `<span class="wbar" style="width:${x.weight * 2}px"></span> ${x.weight}` : "info";
    return `<tr><td>${esc(x.label)}</td><td class="detail">${esc(x.detail)}</td><td>${v}</td><td class="detail">${w}</td></tr>`;
  }).join("");
  $("strategyBoard").innerHTML =
    `<thead><tr><th>Strategy</th><th>Reading</th><th>Vote</th><th>Weight</th></tr></thead><tbody>${rows}</tbody>`;
}

/* ------------------------------------------------------------------ options */

function renderOptions(inst) {
  srcChip($("srcOptions"), inst.source.options);
  const o = inst.options;
  const panel = $("optionsPanel");

  if (!o) {
    if (inst.optionsError) {
      panel.innerHTML = `<div class="error-state"><div class="big">OPTION FEED ERROR</div>
        <div class="why">${esc(inst.optionsError)}</div></div>`;
    } else {
      panel.innerHTML = `<div class="opt-foot">No exchange-listed options for this segment
        (sector indices have no option chain) — the signal here is technical-only.</div>`;
    }
    return;
  }

  const needle = Math.max(0, Math.min(1, (o.pcr - 0.5) / 1.3)) * 100;
  const maxOI = Math.max(...o.rows.map((r) => Math.max(r.ceOI, r.peOI)), 1);
  const rows = o.rows.map((r) => `
    <tr class="${r.strike === o.atm ? "atm" : ""}">
      <td><span class="oibar pe" style="width:${(r.peOI / maxOI * 46).toFixed(1)}px"></span>${fmtCompact(r.peOI)}</td>
      <td class="${r.peChg >= 0 ? "pos" : "neg"}">${fmtCompact(r.peChg)}</td>
      <td>${fmt(r.peLTP)}</td>
      <td class="strike">${fmt(r.strike).replace(".00", "")}</td>
      <td class="${r.ceChg >= 0 ? "pos" : "neg"}">${fmtCompact(r.ceChg)}</td>
      <td>${fmtCompact(r.ceOI)}<span class="oibar ce" style="width:${(r.ceOI / maxOI * 46).toFixed(1)}px"></span></td>
      <td>${fmt(r.ceLTP)}</td>
    </tr>`).join("");

  const g = o.greeks || {};
  const greekCell = (k, v, digits = 2) => `<div class="opt-cell"><div class="k">${k}</div>
      <div class="v">${v == null ? "—" : Number(v).toFixed(digits)}</div></div>`;

  panel.innerHTML = `
    <div class="opt-grid">
      <div class="opt-cell"><div class="k">PCR (OI)</div><div class="v">${o.pcr}</div>
        <div class="pcr-meter"><div class="pcr-needle" style="left:${needle}%"></div></div></div>
      <div class="opt-cell"><div class="k">Max pain</div><div class="v">${fmt(o.maxPain).replace(".00", "")}</div></div>
      <div class="opt-cell"><div class="k">ATM IV</div><div class="v">${o.iv == null ? "—" : o.iv + "%"}</div></div>
      <div class="opt-cell"><div class="k">Straddle</div><div class="v">${fmt(o.straddle)}</div></div>
      <div class="opt-cell"><div class="k">Expected move</div><div class="v">±${o.expectedMovePct == null ? "—" : o.expectedMovePct + "%"}</div></div>
      <div class="opt-cell"><div class="k">OI flow Δ(PE−CE)</div><div class="v ${o.oiFlow >= 0 ? "pos" : "neg"}">${fmtCompact(o.oiFlow)}</div></div>
      <div class="opt-cell"><div class="k">Δ skew (near-ATM)</div><div class="v opt-skew ${(o.deltaSkew ?? 0) >= 0 ? "pos" : "neg"}">${o.deltaSkew == null ? "—" : (o.deltaSkew > 0 ? "+" : "") + o.deltaSkew}</div></div>
    </div>
    <div class="opt-grid">
      ${greekCell("ATM Δ CE", g.deltaCE, 3)}
      ${greekCell("ATM Δ PE", g.deltaPE, 3)}
      ${greekCell("Γ gamma", g.gamma, 4)}
      ${greekCell("Θ theta", g.theta, 2)}
      ${greekCell("Vega", g.vega, 2)}
    </div>
    <div class="range-chips" style="margin-bottom:12px">
      ${o.supports.map((s) => `<span class="rc sup">put wall ${s}</span>`).join("")}
      ${o.resistances.map((r) => `<span class="rc res">call wall ${r}</span>`).join("")}
    </div>
    <div class="table-wrap"><table class="oi">
      <thead><tr><th>PE OI</th><th>PE Δ</th><th>PE LTP</th><th class="mid">Strike</th><th>CE Δ</th><th>CE OI</th><th>CE LTP</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div class="opt-foot">Expiry ${o.expiry} · ATM ${o.atm} · live Upstox chain (OI + Greeks).</div>`;
}

/* ------------------------------------------------------------------ segments rail */

function renderRail(sectors) {
  const rail = $("sectorRail");
  rail.innerHTML = sectors.map((s) => {
    const dir = s.signal ? s.signal.direction : "ERROR";
    const label = s.signal ? s.signal.direction.slice(0, 4) : "ERR";
    const spark = s.candles.length > 1
      ? `<canvas class="spark" data-closes="${encodeURIComponent(JSON.stringify(s.candles.slice(-48).map((c) => +c.close.toFixed(1))))}"></canvas>`
      : `<div class="spark" style="display:flex;align-items:center;color:#ef4444;font-size:10px">no data</div>`;
    return `<div class="seg-card ${s.id === state.selected ? "active" : ""}" data-id="${s.id}">
      <div class="nm">${esc(s.name)} <span class="dchip ${dir}">${label}</span></div>
      <div class="px">${s.spot == null ? "—" : fmt(s.spot)}
        <span class="chg ${s.changePct == null ? "flat" : s.changePct >= 0 ? "up" : "down"}" style="font-size:11px">${s.source.candles === "ERROR" ? "no data" : fmtPct(s.changePct)}</span></div>
      ${spark}
      <div class="row2"><span style="font-size:10.5px;color:var(--muted)">${!s.tradeable ? "context only · no calls" : s.signal ? `entry ${fmt(s.signal.entry)} · sl ${fmt(s.signal.stop)}` : "feed error"}</span>
        <span class="chip ${s.source.candles === "UPSTOX" ? "chip-live" : s.source.candles === "UPSTOX+YAHOO" ? "chip-mixed" : s.source.candles === "YAHOO" ? "chip-yahoo" : "chip-error"}" style="font-size:9px">${s.source.candles}</span></div>
    </div>`;
  }).join("");
  rail.querySelectorAll(".seg-card").forEach((c) => {
    c.onclick = () => { state.selected = c.dataset.id; render(); };
  });
  rail.querySelectorAll(".spark[data-closes]").forEach((cv) => drawSpark(cv));
}

function drawSpark(canvas) {
  const closes = JSON.parse(decodeURIComponent(canvas.dataset.closes));
  if (closes.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 190, H = 34;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const lo = Math.min(...closes), hi = Math.max(...closes) || 1;
  const X = (i) => i * (W - 4) / (closes.length - 1) + 2;
  const Y = (v) => H - 3 - (v - lo) / ((hi - lo) || 1) * (H - 6);
  const up = closes[closes.length - 1] >= closes[0];
  ctx.strokeStyle = up ? "#22c55e" : "#ef4444";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  closes.forEach((c, i) => { i ? ctx.lineTo(X(i), Y(c)) : ctx.moveTo(X(i), Y(c)); });
  ctx.stroke();
  ctx.lineTo(X(closes.length - 1), H); ctx.lineTo(X(0), H); ctx.closePath();
  ctx.fillStyle = up ? "rgba(34,197,94,0.10)" : "rgba(239,68,68,0.10)";
  ctx.fill();
}

/* ------------------------------------------------------------------ research modal */

function openResearch() {
  $("researchModal").classList.remove("hidden");
  renderResearchPicker();
}

function closeResearch() {
  $("researchModal").classList.add("hidden");
}

function renderResearchPicker() {
  const items = (state.data && state.data.instruments) || [];
  const tracked = items.filter((i) => i.tradeable);
  const pick = (i) => `<label class="pick checked"><input type="checkbox" value="${i.id}" checked>
      <span>${esc(i.name)}</span></label>`;
  $("researchBody").innerHTML = `
    <div class="pick-group"><div class="lbl">Call-enabled index segments</div>
      <div class="pick-grid">${tracked.map(pick).join("")}</div></div>
    <div class="pf-note" style="margin:2px 0 0">Sector indices are chart context only — they never
      generate calls. Each segment above runs its own measured precision profile.</div>
    <div class="modal-actions">
      <button class="btn ghost" id="pickAll">Select all</button>
      <button class="btn ghost" id="pickNone">Clear</button>
      <span class="spacer"></span>
      <span class="pick-note" id="pickNote">${tracked.length} call-enabled segments</span>
      <button class="btn btn-accent" id="runScan">Run research</button>
    </div>`;
  const sync = () => {
    const n = $("researchBody").querySelectorAll("input:checked").length;
    $("pickNote").textContent = `${n} selected`;
    $("researchBody").querySelectorAll(".pick").forEach((l) => {
      l.classList.toggle("checked", l.querySelector("input").checked);
    });
  };
  $("researchBody").querySelectorAll("input").forEach((cb) => cb.onchange = sync);
  $("pickAll").onclick = () => { $("researchBody").querySelectorAll("input").forEach((c) => c.checked = true); sync(); };
  $("pickNone").onclick = () => { $("researchBody").querySelectorAll("input").forEach((c) => c.checked = false); sync(); };
  $("runScan").onclick = () => {
    const ids = [...$("researchBody").querySelectorAll("input:checked")].map((c) => c.value);
    if (!ids.length) { $("pickNote").textContent = "Pick at least one segment"; return; }
    runResearch(ids);
  };
}

async function runResearch(ids) {
  $("researchBody").innerHTML = `<div class="loading"><div class="spinner"></div>
    Scanning ${ids.length} segment${ids.length > 1 ? "s" : ""} on the live Upstox feed…</div>`;
  try {
    const r = await fetch("/api/research?segments=" + encodeURIComponent(ids.join(",")));
    if (!r.ok) throw new Error("HTTP " + r.status);
    state.research = await r.json();
    renderResearchResults(state.research);
  } catch (e) {
    $("researchBody").innerHTML = `<div class="error-state"><div class="big">SCAN FAILED</div>
      <div class="why">${esc(e.message)}</div>
      <div class="fix">The server could not complete the live scan. Check the Upstox token
        (<b>python upstox_login.py</b>) and retry.</div>
      <button class="btn" onclick="renderResearchPicker()">Back</button></div>`;
  }
}

function renderResearchResults(res) {
  const dirClass = (d) => d === "BULLISH" ? "bullc" : d === "BEARISH" ? "bearc" : "flatc";
  const rows = res.rows.map((r) => {
    const err = r.direction === "ERROR";
    const dir = err ? `<span class="vote sell">ERROR</span>`
      : `<span class="chip ${r.direction === "BULLISH" ? "chip-live" : r.direction === "BEARISH" ? "chip-error" : "chip-na"}">${r.direction}</span>`;
    const conf = r.confidence == null ? "—"
      : `<span class="cbar ${r.direction === "BEARISH" ? "bear" : ""}" style="width:${Math.max(6, r.confidence * 0.6)}px"></span>${r.confidence}%`;
    const forecast = r.expectedMove == null ? `<span class="fcs">—</span>` : `
      <span class="fc-mini ${r.qualified ? "ok" : ""}" title="${esc((r.reasons || []).join(" · "))}">${fmt(r.expectedMove)}
        <small>/ ${r.minMove} min</small></span>
      <div class="fcs">${r.captureProb == null ? "" : `capture ${r.captureProb}% · `}${esc(r.timing || "")}</div>`;
    const p = r.plan || {};
    return `<tr>
      <td class="name">${esc(r.name)}${r.source.candles === "YAHOO" ? ` <span class="chip chip-yahoo" style="font-size:9px">YAHOO</span>` : ""}</td>
      <td class="num">${r.spot == null ? "—" : fmt(r.spot)}</td>
      <td class="num ${r.changePct == null ? "" : r.changePct >= 0 ? "pos" : "neg"}">${fmtPct(r.changePct)}</td>
      <td>${dir}</td>
      <td class="num">${conf}</td>
      <td class="num">${forecast}</td>
      <td class="num">${err ? "—" : fmt(p.entry ?? r.entry)}</td>
      <td class="num">${err ? "—" : fmt(p.stop ?? r.stop)}</td>
      <td class="num">${err ? "—" : fmt(p.target1 ?? r.target1)}</td>
      <td class="num">${err ? "—" : fmt(p.target2 ?? r.target2)}</td>
      <td class="num">${err ? "—" : fmt(p.target3 ?? r.target3)}</td>
      <td class="num">${r.pcr == null ? "—" : r.pcr}</td>
      <td class="drivers">${err ? esc((r.error || "").slice(0, 60)) : r.drivers.join(" · ")}</td>
      <td><button class="mini-btn" data-open="${r.id}" ${err ? "disabled" : ""}>Open</button></td>
    </tr>`;
  }).join("");

  const notes = (res.feedNotes || []).map((n) => {
    const err = /FEED DOWN|No real candles|failed/i.test(n);
    return `<div class="note ${err ? "err" : ""}" style="margin-bottom:8px">${esc(n)}</div>`;
  }).join("");
  const top = res.topPick;
  const near = res.nearest;
  const pickCells = (r) => {
    const p = r.plan || {};
    return `<div class="opt-grid">
      <div class="opt-cell"><div class="k">${top ? "Top qualifying pick" : "Nearest candidate"}</div><div class="v">${esc(r.name)}</div></div>
      <div class="opt-cell"><div class="k">Direction</div><div class="v ${r.direction === "BULLISH" ? "pos" : "neg"}">${r.direction}</div></div>
      <div class="opt-cell"><div class="k">Conviction</div><div class="v">${r.confidence}%</div></div>
      <div class="opt-cell"><div class="k">Expected move</div><div class="v">±${fmt(r.expectedMove)} pts</div></div>
      <div class="opt-cell"><div class="k">Target band</div><div class="v">T1 ${r.minMove} · T2–T3 to ${r.maxMove} pts</div></div>
      <div class="opt-cell"><div class="k">Capture odds / timing</div><div class="v">${r.captureProb == null ? "—" : r.captureProb + "%"} · ${esc(r.timing || "—")}</div></div>
      <div class="opt-cell"><div class="k">Entry</div><div class="v">${fmt(p.entry ?? r.entry)}</div></div>
      <div class="opt-cell"><div class="k">SL</div><div class="v neg">${fmt(p.stop ?? r.stop)}</div></div>
      <div class="opt-cell"><div class="k">T1 / T2 / T3 · pts</div><div class="v pos">${fmt(p.target1 ?? r.target1)} (+${p.target1Move ?? "—"}) / ${fmt(p.target2 ?? r.target2)} (+${p.target2Move ?? "—"}) / ${fmt(p.target3 ?? r.target3)} (+${p.target3Move ?? "—"})</div></div>
    </div>`;
  };

  $("researchBody").innerHTML = `
    ${notes}
    <div class="verdict">
      <div class="vh">Scan verdict · ${esc(res.istClock)} IST · ${res.scanned} call-enabled segments ·
        ${res.qualifiedCount} qualifying</div>
      ${esc(res.verdict)}
      <div class="fcs" style="margin-top:6px">${esc(res.scope || "")}</div>
      <div class="breadth">
        <span class="bullc">${res.breadth.bullish} bullish</span>
        <span class="bearc">${res.breadth.bearish} bearish</span>
        <span class="flatc">${res.breadth.neutral} neutral</span>
      </div>
    </div>
    ${top ? pickCells(top) : ""}
    ${!top && near ? `<div class="noqual">
      <div class="nq-h">NO QUALIFYING PRE-MOVE TRADE</div>
      Nearest: <b>${esc(near.name)} ${near.direction}</b> — expected ±${fmt(near.expectedMove)} pts vs
      T1 ${near.minMove} (${esc(near.forecastStatus || "")}, ${esc(String(near.timing || "").toLowerCase())} timing).
      Standing aside is the correct output; wait for compression plus a fresh trigger.
    </div>` : ""}
    <div class="table-wrap" style="max-height:52vh;overflow:auto">
      <table class="research">
        <thead><tr><th>Segment</th><th>LTP</th><th>Chg</th><th>Signal</th><th>Conviction</th>
          <th>Exp move / capture</th><th>Entry</th><th>SL</th><th>T1</th><th>T2</th><th>T3</th><th>PCR</th><th>Drivers</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="modal-actions">
      <button class="btn ghost" id="rescan">Back to segment picker</button>
      <span class="spacer"></span>
      <button class="btn" id="closeRes">Close</button>
    </div>`;

  $("researchBody").querySelectorAll("[data-open]").forEach((b) => {
    b.onclick = () => { state.selected = b.dataset.open; closeResearch(); render(); };
  });
  $("rescan").onclick = renderResearchPicker;
  $("closeRes").onclick = closeResearch;
}

/* ------------------------------------------------------------------ proof modal */

function openProof() {
  $("proofModal").classList.remove("hidden");
  const body = $("proofBody");
  body.innerHTML = `<div class="loading"><span class="spinner"></span> Pulling the feed and re-running the
    walk-forward audit on six months of 5m bars…</div>`;
  fetch("/api/proof")
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(renderProof)
    .catch((e) => {
      body.innerHTML = `<div class="error-state"><div class="big">PROOF UNAVAILABLE</div>
        <div class="why">${esc(e.message)}</div>
        <div class="fix">The dashboard must be running (python server.py) and the Upstox token valid.
          The Proof panel never fabricates numbers — if a pull fails it shows this error.</div></div>`;
    });
}

function closeProof() {
  $("proofModal").classList.add("hidden");
}

function proofChip(src) {
  const cls = src === "UPSTOX" ? "chip-live" : src === "UPSTOX+YAHOO" ? "chip-mixed"
    : src === "YAHOO" ? "chip-yahoo" : src === "ERROR" ? "chip-error" : "chip-na";
  return `<span class="chip ${cls}" style="font-size:9.5px">${esc(src)}</span>`;
}

function proofTime(sec) {
  if (!sec) return "—";
  return new Date(sec * 1000).toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function proofTimeShort(sec) {
  if (!sec) return "—";
  return new Date(sec * 1000).toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  }).replace(",", "");
}

function proofOutcome(o) {
  return `<span class="pf-out pf-${o}">${o.toUpperCase()}</span>`;
}

function renderProof(p) {
  const provCards = p.provenance.map((x) => `<div class="proof-card prov-card">
      <div class="pf-h">${esc(x.name)} <span class="fc-badge">${esc(x.profile || "—")} · ${esc(x.forecastStatus || "")}</span></div>
      <div class="pf-stats pf-3">
        <div><span>Spot</span><b>${fmt(x.spot)}</b>
          <div class="fcs ${x.changePct == null ? "" : x.changePct >= 0 ? "pos" : "neg"}">${fmtPct(x.changePct)}</div></div>
        <div><span>Quote source</span><div class="pf-chiprow">${proofChip(x.quote)}</div></div>
        <div><span>Candle source</span><div class="pf-chiprow">${proofChip(x.candles)}</div></div>
        <div><span>Bars loaded</span><b>${x.bars}</b></div>
        <div><span>Last bar · close</span><b class="pf-b-sm">${proofTime(x.lastBar)}</b>
          <div class="fcs">${fmt(x.lastClose)}</div></div>
        <div><span>Option chain</span><div class="pf-chiprow">${proofChip(x.options)}
          ${x.optionExpiry ? `<span class="pf-exp">${esc(x.optionExpiry)}</span>` : ""}</div></div>
        <div><span>Expected move</span><b>${x.expectedMove != null ? "±" + fmt(x.expectedMove) : "—"}</b></div>
        <div><span>Minimum move</span><b>${x.minMove} pts</b></div>
        <div><span>Timeframe</span><b>${esc(x.timeframe)}</b></div>
      </div>
    </div>`).join("");

  const recentRows = Object.entries(p.validation).map(([iid, v]) => {
    const r = v.recent || {};
    const name = (p.provenance.find((x) => x.id === iid) || {}).name || iid;
    if (!r.samples) {
      return `<tr><td>${esc(name)}</td><td colspan="7" class="num">no setups in the last 30 days</td></tr>`;
    }
    const rr = (x) => (x == null ? "—" : (x >= 0 ? "+" : "") + x);
    const pc = (x) => (x == null ? "—" : Number(x).toFixed(1) + "%");
    return `<tr>
      <td>${esc(name)}</td>
      <td class="num">${r.samples}</td>
      <td class="num ${r.winRate >= 75 ? "pos" : "neg"}"><b>${pc(r.winRate)}</b><div class="fcs">${r.wins} calls</div></td>
      <td class="num ${r.t2Rate >= 50 ? "pos" : "neg"}"><b>${pc(r.t2Rate)}</b><div class="fcs">${r.t2Wins} calls</div></td>
      <td class="num ${r.t3Rate >= 50 ? "pos" : "neg"}"><b>${pc(r.t3Rate)}</b><div class="fcs">${r.t3Wins} calls</div></td>
      <td class="num">${r.losses}</td>
      <td class="num">${r.timeouts ?? 0}</td>
      <td class="num ${(r.avgR ?? 0) >= 0 ? "pos" : "neg"}">${rr(r.avgR)}</td>
    </tr>`;
  }).join("");

  const auditCards = Object.entries(p.validation).map(([iid, v]) => {
    const a = v.active || {};
    const name = (p.provenance.find((x) => x.id === iid) || {}).name || iid;
    if (!a.samples) {
      return `<div class="proof-card"><div class="pf-h">${esc(name)}</div>
        <div class="pf-note">${esc(a.note || "Not enough bars to audit this profile.")}</div></div>`;
    }
    const ex = (a.examples || []).slice().reverse().map((e) => `<tr>
        <td class="num">${proofTimeShort(e.barTime)}</td>
        <td>${e.direction === "BULLISH" ? '<span class="vote buy">▲ LONG</span>' : '<span class="vote sell">▼ SHORT</span>'}</td>
        <td class="num">${fmt(e.entry)}</td>
        <td class="num"><span class="pos">${fmt(e.target1)}</span><div class="fcs neg">${fmt(e.stop)}</div></td>
        <td>${proofOutcome(e.outcome)}<div class="fcs">${e.bars == null ? "—" : e.bars + " bars"} · mfe ${fmt(e.mfe)}</div></td>
      </tr>`).join("");
    return `<div class="proof-card">
      <div class="pf-h">${esc(name)} <span class="fc-badge">${esc(a.profile)} · ${esc(a.horizonLabel)} · stop ${esc(a.stopRule || (a.stopAtr + "×ATR"))}</span></div>
      <div class="pf-stats">
        <div><span>Setups</span><b>${a.samples}</b></div>
        <div><span>Wins / losses</span><b>${a.wins} / ${a.losses}</b></div>
        <div><span>Timeouts</span><b>${a.timeouts ?? 0}</b></div>
        <div><span>T1 win · all calls</span><b class="${a.winRate >= 75 ? "pos" : "neg"}">${a.winRate}%</b></div>
        <div><span>Worst month</span><b class="${(a.worstMonth ?? 0) >= 60 ? "pos" : "neg"}">${a.worstMonth == null ? "—" : a.worstMonth + "%"}</b></div>
        <div><span>Min move reached</span><b>${a.reachRate}%</b></div>
        <div><span>Avg bars to T1</span><b>${a.avgBarsToT1 ?? "—"}</b></div>
        <div><span>Avg risk (stop)</span><b>${a.avgRisk ?? "—"} pts</b></div>
        <div><span>R:R at T1</span><b class="${(a.rr1 ?? 0) >= 1 ? "pos" : "neg"}">${a.rr1 ?? "—"}</b></div>
        <div><span>Avg R / call</span><b class="${(a.avgR ?? 0) >= 0 ? "pos" : "neg"}">${a.avgR == null ? "—" : (a.avgR >= 0 ? "+" : "") + a.avgR}</b></div>
        <div><span>Break-even win</span><b>${a.rr1 ? Math.round(100 / (1 + a.rr1)) + "%" : "—"}</b></div>
        <div><span>Expectancy / setup</span><b class="${(a.expectancyPts ?? 0) >= 0 ? "pos" : "neg"}">${a.expectancyPts == null ? "—" : (a.expectancyPts >= 0 ? "+" : "") + a.expectancyPts} pts</b></div>
      </div>
      <div class="pf-note">Min move ${a.minMove} pts · ${a.unresolved ?? 0} unresolved · window ${a.lookbackBars} bars.
        Outcomes are session-bounded: the window closes at 15:25 IST on the entry day, so a call that hits
        neither T1 nor the stop is a <b>timeout</b> closed at the last price and counts as a non-win
        (${a.timeouts ?? 0} here). A candle touching both levels is scored as a stop first.
        "Reach" = the min move was touched at any point, even after a stop.
        "Avg R" and expectancy use the stop distance as 1R, before costs.
        ${a.winRate >= 75 ? "" : `<b class="neg">This profile is below its 75% floor, so the desk refuses new calls until it recovers.</b>`}</div>
      ${a.lowSample ? `<div class="pf-note neg">Fewer than 15 setups — indicative only, not proven.</div>` : ""}
      ${ex ? `<div class="table-wrap"><table class="research pf-table">
        <thead><tr><th>Bar time IST</th><th>Side</th><th>Entry</th><th>T1 / SL</th>
          <th>Outcome</th></tr></thead><tbody>${ex}</tbody></table></div>` : ""}
    </div>`;
  }).join("");

  const method = Object.entries(p.method || {}).map(([k, v]) => {
    const label = { candles: "Candles", noLookahead: "No look-ahead",
      sessionOutcome: "Session-bounded outcome", profileGate: "Per-segment gate",
      selectionBias: "Selection bias", risk: "Risk / break-even", outcome: "Outcome rule",
      recentTargets: "Last 30 days / all targets", optionHistory: "Options history", reproduce: "Reproduce" }[k] || k;
    return `<li><b>${esc(label)}:</b> ${esc(v)}</li>`;
  }).join("");

  const totalCalls = Object.values(p.validation)
    .reduce((n, v) => n + ((v.recent || {}).samples || 0), 0);

  $("proofBody").innerHTML = `
    <div class="verdict">
      <div class="vh">Provenance at ${esc(p.istClock)} IST · six call-enabled segments</div>
      Every value below is fetched from Upstox (live) or Yahoo (real, delayed). The desk has no mock path:
      if a source fails the cell says ERROR and no number is invented. <b>UPSTOX+YAHOO</b> means the 5m series
      is Upstox data with the just-finished session filled from Yahoo's real 5m bars while Upstox EOD history
      catches up overnight. Sector indices are chart context only — they are not audited for calls.
    </div>
    <div class="prov-grid">${provCards}</div>
    <div class="proof-card">
      <div class="pf-h">Last 30 days — T1, T2 and all three targets before the stop</div>
      <div class="pf-note">Same profile and every live gate, replayed on the last 30 calendar days of real
        5m bars. T1 = the minimum move, T2 = 2× and T3 = 3× that move. A target counts only if it was
        touched before the stop; a candle touching both is scored as a stop first, and a timeout is not a
        win. Small windows: ${Object.values(p.validation).map((v) => (v.recent || {}).samples || 0).join(" / ")}
        calls by segment, so read these as a recent sample, not a guarantee.</div>
      <div class="table-wrap"><table class="research pf-table">
        <thead><tr><th>Segment</th><th>Calls</th><th>T1 before SL</th><th>T2 before SL</th>
          <th>T3 (all) before SL</th><th>Stops</th><th>Timeouts</th><th>Avg R (T1)</th></tr></thead>
        <tbody>${recentRows}</tbody></table></div>
    </div>
    <div class="proof-card">
      <div class="pf-h">All ${totalCalls} calls — entry, T1/T2/T3 and SL</div>
      <div class="pf-note">Every call behind the table above, newest first, straight from the same
        no-lookahead replay. Entry is the close of the signal bar, T1/T2/T3 are 1×/2×/3× the
        segment's minimum move, SL is the published stop. Exit is the level that was touched (or
        the last price on a timeout). Targets are shaded only when they were reached before the
        stop. Times are IST.</div>
      <div class="pf-all-actions">
        <div class="pf-filters" id="callFilters">
          <button class="btn active" data-filter="all">All</button>
          <button class="btn" data-filter="t1">T1 hit</button>
          <button class="btn" data-filter="t2">T2 hit</button>
          <button class="btn" data-filter="t3">T3 hit</button>
          <button class="btn" data-filter="sl">Stops</button>
          <button class="btn" data-filter="to">Timeouts</button>
        </div>
        <button class="btn btn-accent" id="allCallsBtn">Load all ${totalCalls} calls</button>
        <a class="btn" id="allCallsCsv" href="/api/calls?days=30&amp;format=csv">Download CSV</a>
      </div>
      <div id="allCallsBody"></div>
    </div>
    ${auditCards}
    <div class="proof-card">
      <div class="pf-h">How to judge this yourself</div>
      <ul class="pf-method">${method}</ul>
      <div class="pf-cmd">Independent check — pulls Upstox again (Yahoo where the index has a
        symbol, Upstox intraday for the rest), diffs every overlapping bar, recomputes the
        audits and exits non-zero if anything disagrees:<br><code>python verify.py</code></div>
    </div>`;
  const allBtn = $("allCallsBtn");
  if (allBtn) allBtn.addEventListener("click", () => loadAllCalls(callFilter));
  document.querySelectorAll("#callFilters [data-filter]").forEach((b) => {
    b.addEventListener("click", () => setCallFilter(b.dataset.filter));
  });
}

let callFilter = "all";

function setCallFilter(f) {
  callFilter = f;
  document.querySelectorAll("#callFilters [data-filter]").forEach((b) => {
    b.classList.toggle("active", b.dataset.filter === f);
  });
  const csv = $("allCallsCsv");
  if (csv) csv.href = `/api/calls?days=30&filter=${f}&format=csv`;
  loadAllCalls(f);
}

function loadAllCalls(f) {
  const filter = f || "all";
  const btn = $("allCallsBtn"), body = $("allCallsBody");
  if (!body) return;
  if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }
  body.innerHTML = `<div class="loading"><span class="spinner"></span> Replaying the
    ${filter === "all" ? "whole" : filter.toUpperCase() + " set of"} calls from the same 5m bars…</div>`;
  fetch(`/api/calls?days=30&filter=${filter}`)
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(renderAllCalls)
    .catch((e) => {
      body.innerHTML = `<div class="error-state"><div class="big">CALL LIST UNAVAILABLE</div>
        <div class="why">${esc(e.message)}</div>
        <div class="fix">Nothing is filled in when the audit fails — restart the server with a
          valid Upstox token and try again.</div></div>`;
      if (btn) { btn.disabled = false; btn.textContent = "Retry"; }
    });
}

function renderAllCalls(res) {
  const t = res.totals || {};
  const rows = (res.calls || []).map((c) => {
    const hit2 = (c.targetsHit || 0) >= 2, hit3 = (c.targetsHit || 0) >= 3;
    const rCls = (c.r ?? 0) >= 0 ? "pos" : "neg";
    const done = c.outcome === "t1" ? "T1" : c.outcome === "stop" ? "SL"
      : c.outcome === "timeout" ? "TO" : "OPEN";
    return `<tr>
      <td class="num">${esc((c.date || "").slice(5))}<div class="fcs">${esc(c.time || "")}</div></td>
      <td>${esc(c.segment)}</td>
      <td>${c.direction === "BULLISH" ? '<span class="vote buy">▲ L</span>' : '<span class="vote sell">▼ S</span>'}</td>
      <td class="num">${fmt(c.entry)}</td>
      <td class="num pos">${fmt(c.target1)}</td>
      <td class="num ${hit2 ? "pos" : ""}">${fmt(c.target2)}</td>
      <td class="num ${hit3 ? "pos" : ""}">${fmt(c.target3)}</td>
      <td class="num neg">${fmt(c.stop)}</td>
      <td><span class="pf-out pf-${c.outcome}">${done}</span>
        <div class="fcs">${c.bars == null ? "—" : c.bars + "b"}</div></td>
      <td class="num">${fmt(c.exit)}<div class="fcs">${c.minutes == null ? "" : c.minutes + " min"}</div></td>
      <td class="num">${fmt(c.mfe)}</td>
      <td class="num ${rCls}">${c.r == null ? "—" : (c.r >= 0 ? "+" : "") + c.r}</td>
    </tr>`;
  }).join("");
  const flt = res.filter || "all";
  const label = { all: "all calls", t1: "T1 hit before SL", t2: "T2 hit before SL",
    t3: "T3 (all three targets) hit before SL", sl: "stops", to: "timeouts" }[flt] || flt;
  const rr = t.avgR == null ? "—" : (t.avgR >= 0 ? "+" : "") + t.avgR;
  const head = flt === "all"
    ? `${res.days} calendar days · ${t.calls} calls ·
       <b class="pos">${t.winRate}% T1</b> · ${t.t2Rate}% T2 · ${t.t3Rate}% T3 ·
       ${t.stops} stops · ${t.timeouts} timeouts · avg ${rr}R (T1 exit)`
    : `Filter: <b>${esc(label)}</b> · ${t.calls} of ${res.filteredFrom} calls · avg ${rr}R ·
       ${t.stops} stops · ${t.timeouts} timeouts`;
  $("allCallsBody").innerHTML = `
    <div class="pf-note">${head}. Generated ${esc(res.istClock)} IST.</div>
    ${t.calls ? `<div class="table-wrap pf-all-wrap"><table class="research pf-table">
      <thead><tr><th>Date · IST</th><th>Seg</th><th>Side</th><th>Entry</th><th>T1</th>
        <th>T2</th><th>T3</th><th>SL</th><th>Outcome</th><th>Exit</th><th>MFE</th>
        <th>R</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="pf-note neg">No call in the last ${res.days} days matches this filter.</div>`}`;
  const btn = $("allCallsBtn");
  if (btn) { btn.disabled = false; btn.textContent = "Reload " + t.calls + (t.calls === 1 ? " call" : " calls"); }
}

/* ------------------------------------------------------------------ today's calls */

function openToday() {
  $("todayModal").classList.remove("hidden");
  const body = $("todayBody");
  body.innerHTML = `<div class="loading"><span class="spinner"></span> Replaying the session's 5m bars
    through the desk's own gates for every segment…</div>`;
  fetch("/api/today")
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(renderToday)
    .catch((e) => {
      body.innerHTML = `<div class="error-state"><div class="big">TODAY'S CALLS UNAVAILABLE</div>
        <div class="why">${esc(e.message)}</div>
        <div class="fix">The dashboard must be running (python server.py) with a valid Upstox token.
          This panel replays real candles only — if the feed fails it shows this error, never a
          filled-in list.</div></div>`;
    });
}

function closeToday() {
  $("todayModal").classList.add("hidden");
}

function todayClock(sec) {
  if (!sec) return "—";
  return new Date(sec * 1000).toLocaleTimeString("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function todayDate(day) {
  if (!day) return "—";
  return new Date(day + "T00:00:00+05:30").toLocaleDateString("en-GB", {
    timeZone: "Asia/Kolkata", weekday: "long", day: "2-digit", month: "short", year: "numeric",
  });
}

function todayOutcome(c) {
  if (c.outcome === "open") return `<span class="pf-out pf-open">OPEN</span>`;
  if (c.outcome === "timeout") return `<span class="pf-out pf-timeout">TIMEOUT</span>`;
  return `<span class="pf-out pf-${c.outcome}">${c.outcome === "t1" ? "T1 HIT" : "SL HIT"}</span>`;
}

function renderToday(t) {
  const tt = t.totals || {};
  const calls = t.calls || [];
  const segs = t.segments || [];
  const failed = segs.filter((s) => s.error);

  const segRows = segs.map((s) => `<tr>
      <td class="name">${esc(s.name)}</td>
      <td><span class="fc-badge">${esc(s.profileKey || "—")}</span></td>
      <td class="num">${s.calls}</td>
      <td class="num pos">${s.t1}</td>
      <td class="num neg">${s.sl}</td>
      <td class="num">${s.timeouts ?? 0}</td>
      <td class="num">${s.open}</td>
      <td class="num ${s.winRate >= 75 ? "pos" : s.winRate != null ? "neg" : ""}">${s.winRate == null ? "—" : s.winRate + "%"}</td>
      <td class="num">${s.reach}</td>
      <td class="num">${s.avgT1Min == null ? "—" : s.avgT1Min + "m"}</td>
      <td class="num">${s.avgSLMin == null ? "—" : s.avgSLMin + "m"}</td>
      <td class="num">${s.avgMfe == null ? "—" : fmt(s.avgMfe)}</td>
    </tr>`).join("");

  const callRows = calls.map((c) => `<tr>
      <td class="num">${todayClock(c.t)}</td>
      <td class="name">${esc(c.name)}<div class="fcs">${esc(c.profileKey)}</div></td>
      <td>${c.side === "LONG" ? '<span class="pos">▲ LONG</span>' : '<span class="neg">▼ SHORT</span>'}</td>
      <td class="num">${fmt(c.entry)}</td>
      <td class="num neg">${fmt(c.stop)}<div class="fcs">${c.sd == null ? "—" : c.sd + " pts"}</div></td>
      <td class="num pos">${fmt(c.t1)}<div class="fcs">${c.m1} pts</div></td>
      <td class="num">${fmt(c.t2)}<div class="fcs">${c.m2} pts</div></td>
      <td class="num">${fmt(c.t3)}<div class="fcs">${c.m3} pts</div></td>
      <td class="num">${c.rr1 == null ? "—" : c.rr1}</td>
      <td>${todayOutcome(c)}</td>
      <td class="num">${c.minutes}m${c.outcome === "open" ? " elapsed" : c.outcome === "timeout" ? " · 15:25" : ""}
        <div class="fcs">${c.bars == null ? "pending" : c.bars + (c.bars === 1 ? " bar" : " bars")}</div></td>
      <td class="num">${fmt(c.mfe)}<div class="fcs">${c.reach ? "min move reached" : "min move not reached"}</div></td>
    </tr>`).join("");

  const method = Object.entries(t.method || {}).map(([k, v]) => {
    const label = { scope: "Scope", replay: "Replay", outcome: "Outcome rule",
      winRate: "Win-rate definition", profiles: "Profiles", open: "Open calls" }[k] || k;
    return `<li><b>${esc(label)}:</b> ${esc(v)}</li>`;
  }).join("");

  $("todayBody").innerHTML = `
    <div class="proof-card">
      <div class="pf-h">Session ${todayDate(t.day)}
        <span class="fc-badge">${t.isToday ? "today" : "last completed session"} · snapshot ${esc(t.istClock)} IST</span>
        <span class="fc-badge">market ${t.marketOpen ? "open" : "closed"}</span>
        <span class="fc-badge">last bar ${todayClock(t.asOf)}</span>
      </div>
      <div class="pf-stats">
        <div><span>Calls</span><b>${tt.calls ?? 0}</b></div>
        <div><span>T1 hit</span><b class="pos">${tt.t1 ?? 0}</b></div>
        <div><span>Stop hit</span><b class="neg">${tt.sl ?? 0}</b></div>
        <div><span>Timeouts</span><b>${tt.timeouts ?? 0}</b></div>
        <div><span>Still open</span><b>${tt.open ?? 0}</b></div>
        <div><span>Win · all calls</span><b class="${tt.winRate >= 75 ? "pos" : tt.winRate != null ? "neg" : ""}">${tt.winRate == null ? "—" : tt.winRate + "%"}</b></div>
        <div><span>Win · resolved</span><b>${tt.winRateResolved == null ? "—" : tt.winRateResolved + "%"}</b></div>
        <div><span>Avg R / call</span><b class="${(tt.avgR ?? 0) >= 0 ? "pos" : "neg"}">${tt.avgR == null ? "—" : (tt.avgR >= 0 ? "+" : "") + tt.avgR}</b></div>
        <div><span>Min move reached</span><b>${tt.reach ?? 0}</b></div>
        <div><span>Avg time to T1</span><b>${tt.avgT1Min == null ? "—" : tt.avgT1Min + "m"}</b></div>
        <div><span>Avg time to stop</span><b>${tt.avgSLMin == null ? "—" : tt.avgSLMin + "m"}</b></div>
      </div>
      <div class="pf-note">${esc(t.scope || "")}</div>
      ${failed.length ? `<div class="pf-note">Segments contributing no calls (feed error, too little
        history, or their own six-month audit below 75% — never a filled-in call):
        ${failed.map((s) => `<b>${esc(s.name)}</b> — ${esc(s.error)}`).join(" · ")}</div>` : ""}
      ${calls.length ? "" : `<div class="pf-note">No setup has passed the gates in this session yet.
        That is a valid desk output — the rules would rather stand aside than force a trade.</div>`}
    </div>
    ${segRows ? `<div class="proof-card">
      <div class="pf-h">By segment</div>
      <div class="table-wrap"><table class="research pf-table">
        <thead><tr><th>Segment</th><th>Profile</th><th>Calls</th><th>T1</th><th>SL</th><th>Timeout</th><th>Open</th>
          <th>Win% · all</th><th>Reached</th><th>Avg T1</th><th>Avg SL</th><th>Avg MFE</th></tr></thead>
        <tbody>${segRows}</tbody></table></div>
      <div class="pf-note">Win% is T1 hits ÷ <b>all</b> calls, so timeouts (closed at 15:25 with neither level)
        and still-open calls count as non-wins. A candle touching both T1 and the stop counts as a stop.
        Reached counts calls whose minimum move was touched at any point, even after a stop.</div>
    </div>` : ""}
    ${callRows ? `<div class="proof-card">
      <div class="pf-h">Every call — ${calls.length} in the session</div>
      <div class="table-wrap" style="max-height:52vh;overflow:auto"><table class="research pf-table">
        <thead><tr><th>Time</th><th>Segment</th><th>Side</th><th>Entry</th><th>Stop</th>
          <th>T1</th><th>T2</th><th>T3</th><th>R:R</th><th>Result</th><th>Duration</th><th>MFE</th></tr></thead>
        <tbody>${callRows}</tbody></table></div>
      <div class="pf-note">Entry is the 5m close at the decision bar; targets are the same
        measured levels the desk shows live. R:R is the T1 distance over the stop distance — these
        precision profiles run ≈0.25–0.4 by design, so break-even sits near 71–80%. A TIMEOUT is a
        call that hit neither level before 15:25 IST: it is closed at the last price and is not a win.
        MFE is the largest favourable excursion from entry, even after the call resolved.</div>
    </div>` : ""}
    <div class="proof-card">
      <div class="pf-h">How this list is produced</div>
      <ul class="pf-method">${method}</ul>
      <div class="pf-cmd">This is a replay of real candles, not a log of past advice — reopening the
        panel re-runs it on the latest bars. The same engine backs <code>python verify.py</code> and the
        walk-forward audit in the Proof panel.</div>
      <div class="modal-actions">
        <span class="spacer"></span>
        <button class="btn ghost" id="todayReload">Replay again</button>
        <button class="btn" id="todayCloseBtn">Close</button>
      </div>
    </div>`;

  $("todayReload").onclick = openToday;
  $("todayCloseBtn").onclick = closeToday;
}

/* ------------------------------------------------------------------ next-day / overnight */

function openOvernight() {
  $("overnightModal").classList.remove("hidden");
  const body = $("overnightBody");
  body.innerHTML = `<div class="loading"><span class="spinner"></span> Pulling ~3 years of real
    daily bars and re-running the overnight audit…</div>`;
  fetch("/api/overnight")
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(renderOvernight)
    .catch((e) => {
      body.innerHTML = `<div class="error-state"><div class="big">NEXT-DAY MODEL UNAVAILABLE</div>
        <div class="why">${esc(e.message)}</div>
        <div class="fix">This is a separate daily model. It needs the dashboard running with a valid
          Upstox token (or Yahoo for the segments that have a symbol). It never fills in invented
          daily bars — if the feed fails, this error is the output.</div></div>`;
    });
}

function closeOvernight() {
  $("overnightModal").classList.add("hidden");
}

function overnightStatusClass(s) {
  return s === "CALL" ? "call" : s === "FORMING" ? "forming"
    : s === "BLOCKED" ? "blocked" : s === "ERROR" ? "err" : "none";
}

function renderOvernight(o) {
  const a = o.audit || {};
  const older = a.older || {}, newer = a.newer || {};
  const ga = (a.grades || {}).A || {}, gb = (a.grades || {}).B || {};
  const pc = (v) => (v == null ? "—" : v + "%");
  const sg = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + v);
  const segs = o.segments || [];
  const signals = (o.signals || []).slice(0, 24);
  const calls = o.calls || [];

  const cards = segs.map((s) => {
    const c = s.current || {};
    const au = s.audit || {};
    const cls = overnightStatusClass(s.status);
    let body = "";
    if (s.error) {
      body = `<div class="on-err">${esc(s.error)}</div>`;
    } else if (s.status === "CALL" || s.status === "FORMING") {
      const p = s.plan || {};
      body = `
        <div class="on-side">▲ BUY AT CLOSE <span class="on-grade">Grade ${esc(p.grade || "—")}</span></div>
        <div class="on-plan">
          <div><span>Entry</span><b>${fmt(p.entry)}</b><div class="fcs">${esc(p.entryDate || "—")} close</div></div>
          <div><span>Exit</span><b>next close</b><div class="fcs">audited exit</div></div>
          <div><span>ATR range</span><b>±${fmt(p.range)}</b><div class="fcs">1-day guide</div></div>
          <div><span>0.5×ATR target</span><b>${fmt(p.target)}</b><div class="fcs">${fmt(p.targetMove)} pts · advisory</div></div>
          <div><span>1×ATR risk guide</span><b class="neg">${fmt(p.riskGuide)}</b><div class="fcs">${fmt(p.riskGuidePts)} pts · not audited</div></div>
        </div>
        <div class="on-now">RSI(14) <b>${c.rsi ?? "—"}</b> · 3-day <b class="${(c.ret3Pct ?? 0) < 0 ? "neg" : "pos"}">${sg(c.ret3Pct)}%</b>
          · measured next-close win <b>${pc(p.measuredWinRate)}</b> over ${p.measuredSamples ?? "—"} signals
          · avg <b>${sg(p.measuredAvgPts)} pts</b></div>
        ${s.forming ? `<div class="on-note">Forming bar only — this becomes a call after the 15:25 IST close.</div>` : ""}`;
    } else if (s.status === "BLOCKED") {
      body = `
        <div class="on-side short">▲ RULE TRIGGERED · NO CALL</div>
        <div class="on-now">RSI(14) <b>${c.rsi ?? "—"}</b> · 3-day <b>${sg(c.ret3Pct)}%</b></div>
        <div class="on-err">${esc(s.blockReason || "edge below the measured floor")}</div>`;
    } else {
      body = `
        <div class="on-side muted">NO SETUP · STAND ASIDE</div>
        <div class="on-now">RSI(14) <b>${c.rsi ?? "—"}</b> <span class="fcs">needs ≤ ${o.params ? o.params.rsiMax : 38}</span>
          · 3-day <b class="${(c.ret3Pct ?? 0) < 0 ? "neg" : "pos"}">${sg(c.ret3Pct)}%</b>
          <span class="fcs">needs ≤ ${o.params ? o.params.ret3MaxPct : -1}%</span></div>
        ${(c.reasons || []).length ? `<div class="on-note">${c.reasons.map(esc).join(" · ")}</div>` : ""}`;
    }
    return `<div class="on-card on-${cls}">
      <div class="on-head">
        <div>
          <div class="on-name">${esc(s.name)}</div>
          <div class="on-sub">${esc(s.sessionDate || "—")} · ${esc(s.dataSource || "—")} · ${s.bars || 0} daily bars</div>
        </div>
        <span class="on-status ${cls}">${esc(s.statusLabel)}</span>
      </div>
      ${body}
      <div class="on-audit">
        <span>3-yr next-close win <b class="${(au.winRate ?? 0) >= 55 ? "pos" : "neg"}">${pc(au.winRate)}</b> · ${au.samples ?? 0} signals</span>
        <span>older ${pc((au.older || {}).winRate)} · newest ${pc((au.newer || {}).winRate)}</span>
        <span>avg ${sg(au.avgPts)} pts · PF ${au.profitFactor ?? "—"}</span>
      </div>
    </div>`;
  }).join("");

  const signalRows = signals.map((t) => `<tr>
      <td class="num">${esc((t.date || "").slice(5))}<div class="fcs">→ ${esc((t.nextDate || "").slice(5))}</div></td>
      <td class="name">${esc(t.name)}</td>
      <td><span class="on-grade g${esc(t.grade)}">${esc(t.grade)}</span></td>
      <td class="num">${t.rsi}</td>
      <td class="num ${t.ret3Pct < 0 ? "neg" : "pos"}">${sg(t.ret3Pct)}%</td>
      <td class="num">${fmt(t.entry)}</td>
      <td class="num">${fmt(t.exit)}</td>
      <td class="num ${t.points >= 0 ? "pos" : "neg"}">${sg(t.points)}</td>
      <td class="num ${t.pct >= 0 ? "pos" : "neg"}">${sg(t.pct)}%</td>
      <td class="num ${t.gapPts >= 0 ? "pos" : "neg"}">${sg(t.gapPts)}</td>
      <td>${t.win ? '<span class="pf-out pf-t1">WIN</span>' : '<span class="pf-out pf-stop">LOSS</span>'}</td>
    </tr>`).join("");

  const segRows = segs.map((s) => {
    const au = s.audit || {};
    const od = au.older || {}, nw = au.newer || {};
    return `<tr>
      <td class="name">${esc(s.name)}</td>
      <td class="num">${au.samples ?? "—"}</td>
      <td class="num ${(au.winRate ?? 0) >= 60 ? "pos" : (au.winRate ?? 0) >= 55 ? "" : "neg"}"><b>${pc(au.winRate)}</b></td>
      <td class="num">${pc(od.winRate)} / ${pc(nw.winRate)}</td>
      <td class="num ${(au.avgPts ?? 0) >= 0 ? "pos" : "neg"}">${sg(au.avgPts)}</td>
      <td class="num">${au.profitFactor ?? "—"}</td>
      <td class="num">${pc(au.targetRate)}</td>
      <td class="num">${pc(au.gapWinRate)}</td>
      <td class="num neg">${fmt(au.medianMae)}</td>
      <td class="num neg">${sg(au.worstPts)}</td>
    </tr>`;
  }).join("");

  const method = Object.entries(o.method || {}).map(([k, v]) => {
    const label = { rule: "Rule", entryExit: "Entry / exit", noLookahead: "No look-ahead",
      longOnly: "Long only", clustering: "Clustering", data: "Data",
      risk: "Risk / gap", costs: "Costs", inSample: "In-sample",
      scope: "Scope", reproduce: "Reproduce" }[k] || k;
    return `<li><b>${esc(label)}:</b> ${esc(v)}</li>`;
  }).join("");

  $("overnightBody").innerHTML = `
    <div class="verdict">
      <div class="vh">Next-day model · as of ${esc(o.asOfDate)} · snapshot ${esc(o.istClock)} IST
        · separate from the intraday desk</div>
      ${esc(o.verdict)}
    </div>
    <div class="proof-card">
      <div class="pf-h">Three-year measured edge
        <span class="fc-badge">long only · close-to-close · no stop in the audit</span></div>
      <div class="pf-stats">
        <div><span>Signals</span><b>${a.samples ?? 0}</b><div class="fcs">${a.uniqueDates ?? 0} oversold days</div></div>
        <div><span>Next close higher</span><b class="${(a.winRate ?? 0) >= 55 ? "pos" : "neg"}">${pc(a.winRate)}</b><div class="fcs">date-level ${pc(a.dateWinRate)}</div></div>
        <div><span>Avg move / call</span><b class="${(a.avgPts ?? 0) >= 0 ? "pos" : "neg"}">${sg(a.avgPts)} pts</b><div class="fcs">${sg(a.avgPct)}%</div></div>
        <div><span>Profit factor</span><b>${a.profitFactor ?? "—"}</b></div>
        <div><span>Older 2/3</span><b>${pc(older.winRate)}</b></div>
        <div><span>Newest 1/3</span><b>${pc(newer.winRate)}</b></div>
        <div><span>Grade A</span><b>${pc(ga.winRate)}</b><div class="fcs">${ga.samples ?? 0} signals</div></div>
        <div><span>Grade B</span><b>${pc(gb.winRate)}</b><div class="fcs">${gb.samples ?? 0} signals</div></div>
        <div><span>Worst close loss</span><b class="neg">${sg(a.worstPts)} pts</b></div>
        <div><span>Positive months</span><b>${a.monthsPositive ?? 0}/${a.months ?? 0}</b></div>
      </div>
      <div class="pf-note">${esc(o.rule)} Entry is the close; the audited exit is the next session's
        close. A target/touch figure is context only — it is not the deployed exit. The rates are
        in-sample and gross of costs.</div>
    </div>
    ${calls.length ? `<div class="proof-card">
      <div class="pf-h">Tonight — ${calls.length} qualified next-day call${calls.length === 1 ? "" : "s"}</div>
      <div class="pf-note">Buy at the ${esc(o.asOfDate)} close and exit at the next trading session's
        close. If you cannot act at the close, the measured close-to-close edge does not apply to an
        entry at the next open.</div>
    </div>` : ""}
    <div class="on-grid">${cards}</div>
    <div class="proof-card">
      <div class="pf-h">By segment — three years of daily bars</div>
      <div class="table-wrap"><table class="research pf-table">
        <thead><tr><th>Segment</th><th>Signals</th><th>Next close higher</th><th>Older / newest</th>
          <th>Avg pts</th><th>Profit factor</th><th>Target at close</th><th>Favorable gap</th>
          <th>Median adverse</th><th>Worst</th></tr></thead>
        <tbody>${segRows}</tbody></table></div>
      <div class="pf-note">Win is the next close above the entry. "Target at close" is how often the
        close cleared entry + 0.5×ATR — it is a context stat, not the deployed exit. Median adverse
        is the typical next-day low below entry; the worst column is the worst close-to-close loss.
        No stop is part of the audit, so an overnight gap can exceed any risk guide.</div>
    </div>
    <div class="proof-card">
      <div class="pf-h">Recent overnight signals — entry at close, exit at the next close</div>
      <div class="table-wrap" style="max-height:48vh;overflow:auto"><table class="research pf-table">
        <thead><tr><th>Signal → exit</th><th>Segment</th><th>Grade</th><th>RSI</th><th>3-day</th>
          <th>Entry</th><th>Exit</th><th>Pts</th><th>%</th><th>Gap</th><th>Result</th></tr></thead>
        <tbody>${signalRows || `<tr><td colspan="11" class="num">No overnight signals in the last three years.</td></tr>`}</tbody>
      </table></div>
      <div class="pf-note">Signals cluster on the same oversold days, so several rows can be the
        same market bet. Grade A = RSI ≤ ${o.params ? o.params.gradeA.rsiMax : 30} and 3-day
        ≤ ${o.params ? o.params.gradeA.ret3MaxPct : -2}%.</div>
      <div class="modal-actions">
        <a class="btn" href="/api/overnight?format=csv&amp;days=365">Download signals CSV</a>
        <span class="spacer"></span>
        <button class="btn ghost" id="overnightReload">Reload</button>
        <button class="btn" id="overnightCloseBtn">Close</button>
      </div>
    </div>
    <div class="proof-card">
      <div class="pf-h">How the next-day edge is measured</div>
      <ul class="pf-method">${method}</ul>
      <div class="pf-cmd">Independent check — pulls the daily bars again, recomputes this audit and
        verifies the model is causal (truncating the newest bar changes no earlier outcome):<br>
        <code>python verify.py</code></div>
    </div>`;

  $("overnightReload").onclick = openOvernight;
  $("overnightCloseBtn").onclick = closeOvernight;
}

/* ------------------------------------------------------------------ access / token */

const accessState = {
  key: localStorage.getItem("deskAdminKey") || "",
  status: null, error: null, notice: null, busy: false, draft: "",
};

function openAccess() {
  $("accessModal").classList.remove("hidden");
  accessState.status = null;
  accessState.error = null;
  accessState.notice = null;
  accessState.draft = "";
  renderAccess();
  refreshAccess();
}

function closeAccess() { $("accessModal").classList.add("hidden"); }

async function refreshAccess() {
  try {
    const r = await fetch("/api/token", {
      headers: accessState.key ? { "X-Admin-Key": accessState.key } : {},
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
    accessState.status = body;
    accessState.error = null;
  } catch (e) {
    accessState.error = e.message;
  }
  renderAccess();
}

async function saveAccess() {
  const key = ($("accessKeyInput").value || "").trim();
  const token = ($("accessTokenInput").value || "").trim();
  accessState.draft = token;
  if (!key) {
    accessState.error = "Enter the access panel key first (DESK_ADMIN_KEY in .env).";
    renderAccess();
    return;
  }
  localStorage.setItem("deskAdminKey", key);
  accessState.key = key;
  if (!token) {
    accessState.error = "Paste an Upstox access token, or the redirected URL containing code=.";
    renderAccess();
    return;
  }
  accessState.busy = true;
  accessState.error = null;
  accessState.notice = "Checking the token with Upstox…";
  renderAccess();
  try {
    const r = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Admin-Key": key },
      body: JSON.stringify({ token }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
    accessState.status = Object.assign({}, accessState.status || {}, {
      configured: true, expiresInSec: body.expiresInSec, expiresLabel: body.expiresLabel,
      feed: body.feed, publicUrl: body.publicUrl || (accessState.status || {}).publicUrl,
      adminKeyConfigured: true,
    });
    accessState.notice = body.message + (body.userName ? " Verified as " + body.userName + "." : "");
    accessState.draft = "";
    await load(true);
    await refreshAccessSilent();
  } catch (e) {
    accessState.error = e.message;
    accessState.notice = null;
  }
  accessState.busy = false;
  renderAccess();
}

async function refreshAccessSilent() {
  try {
    const r = await fetch("/api/token", { headers: { "X-Admin-Key": accessState.key } });
    if (r.ok) accessState.status = await r.json();
  } catch (e) { /* status is cosmetic; the apply already succeeded */ }
}

function renderAccess() {
  const known = !!accessState.status;
  const s = accessState.status || {};
  const feed = s.feed || {};
  const url = s.publicUrl || "";
  const tokenLine = !known ? "—" : s.configured
    ? `set · ${s.expiresLabel ? s.expiresLabel + " left" : "expiry unknown"}`
    : "no token";
  const feedStatus = !known ? "—" : feed.online ? "LIVE" : "DOWN";
  const feedClass = !known ? "" : feed.online ? "up" : "down";
  const help = accessState.error
    ? `<div class="access-msg err">${esc(accessState.error)}</div>`
    : accessState.notice ? `<div class="access-msg">${esc(accessState.notice)}</div>` : "";

  $("accessBody").innerHTML = `
    <div class="access-grid">
      <div class="access-card">
        <div class="ac-h">Upstox feed</div>
        <div class="ac-row"><span>Status</span><b class="${feedClass}">${feedStatus}</b></div>
        <div class="ac-row"><span>Token</span><b>${esc(tokenLine)}</b></div>
        <div class="ac-row"><span>Nifty quote</span><b>${!known || feed.nifty == null ? "—" : fmt(feed.nifty)}</b></div>
        ${known && feed.error ? `<div class="ac-err">${esc(feed.error)}</div>` : ""}
        <div class="ac-note">A token lives ~1 day. When it expires the desk stays honest and shows feed
          errors — paste a fresh token below and every panel reloads on the new feed.</div>
      </div>
      <div class="access-card">
        <div class="ac-h">Public link</div>
        ${url ? `<div class="ac-url"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>
          <button class="btn ghost" id="accessCopy">Copy</button></div>
          <div class="ac-note">Open this on any device. Browser login is
            <b>${s.siteLogin ? "on" : "off"}</b>${s.siteLogin ? " — use DESK_USER / DESK_PASS from .env" : ""}.</div>`
        : `<div class="ac-none">${known ? "No tunnel running." : "Locked — enter the panel key to read it."}</div>
           <div class="ac-note">In a terminal: <code>python share.py</code> — it starts ngrok, prints the
             public URL, and writes the nginx reverse-proxy config.</div>`}
      </div>
    </div>
    ${help}
    <div class="access-form">
      <label for="accessKeyInput">Access panel key <span>stored in this browser · .env → DESK_ADMIN_KEY</span></label>
      <input id="accessKeyInput" type="password" autocomplete="off" value="${esc(accessState.key)}"
             placeholder="DESK_ADMIN_KEY">
      <label for="accessTokenInput">Upstox access token <span>JWT, or the full redirected URL containing code=</span></label>
      <textarea id="accessTokenInput" rows="3" spellcheck="false"
        placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6...">${esc(accessState.draft)}</textarea>
      <div class="modal-actions">
        <button class="btn btn-accent" id="accessSave" ${accessState.busy ? "disabled" : ""}>
          ${accessState.busy ? "Checking…" : "Check & apply"}</button>
        <button class="btn ghost" id="accessDismiss">Close</button>
        <span class="spacer"></span>
        <span class="access-help">Fresh token: <code>python upstox_login.py &lt;redirect-uri&gt;</code></span>
      </div>
    </div>`;

  $("accessSave").onclick = saveAccess;
  $("accessDismiss").onclick = closeAccess;
  const copy = $("accessCopy");
  if (copy) copy.onclick = () => {
    navigator.clipboard?.writeText(url).then(() => { copy.textContent = "Copied"; },
      () => { copy.textContent = "Select manually"; });
  };
}

/* ------------------------------------------------------------------ boot */

let resizeT;
window.addEventListener("resize", () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => {
    if (state.data) {
      const inst = state.data.instruments.find((i) => i.id === state.selected);
      if (inst) drawChart(inst);
    }
  }, 150);
});

$("autoToggle").addEventListener("change", (e) => setAuto(e.target.checked));
$("refreshBtn").addEventListener("click", () => load(true));
$("researchBtn").addEventListener("click", openResearch);
$("researchClose").addEventListener("click", closeResearch);
$("researchModal").addEventListener("click", (e) => { if (e.target.id === "researchModal") closeResearch(); });
$("proofBtn").addEventListener("click", openProof);
$("proofClose").addEventListener("click", closeProof);
$("proofModal").addEventListener("click", (e) => { if (e.target.id === "proofModal") closeProof(); });
$("todayBtn").addEventListener("click", openToday);
$("todayClose").addEventListener("click", closeToday);
$("todayModal").addEventListener("click", (e) => { if (e.target.id === "todayModal") closeToday(); });
$("overnightBtn").addEventListener("click", openOvernight);
$("overnightClose").addEventListener("click", closeOvernight);
$("overnightModal").addEventListener("click", (e) => { if (e.target.id === "overnightModal") closeOvernight(); });
$("accessBtn").addEventListener("click", openAccess);
$("accessClose").addEventListener("click", closeAccess);
$("accessModal").addEventListener("click", (e) => { if (e.target.id === "accessModal") closeAccess(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeResearch(); closeProof(); closeToday(); closeOvernight(); closeAccess(); }
});

setInterval(clockTick, 1000);
clockTick();
setAuto(true);
load(true);
