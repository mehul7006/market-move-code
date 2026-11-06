/* Market Signal Desk - live dashboard frontend (Upstox feed, no mock data) */
"use strict";

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
          first. The <b>Proof</b> panel shows the stricter T1-before-SL win rate. Not a guarantee of future moves.</div>
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
      <div class="level"><div class="k">Measured T1 win${measured != null ? " · n=" + f.auditSamples : ""}</div><div class="v ${measured >= 75 ? "pos" : ""}">${measured == null ? "—" : measured + "%"}</div></div>
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
      <div class="row2"><span style="font-size:10.5px;color:var(--muted)">${s.signal ? `entry ${fmt(s.signal.entry)} · sl ${fmt(s.signal.stop)}` : "feed error"}</span>
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
  const primes = items.filter((i) => i.group === "prime");
  const sectors = items.filter((i) => i.group === "sector");
  const pick = (i) => `<label class="pick checked"><input type="checkbox" value="${i.id}" checked>
      <span>${esc(i.name)}</span></label>`;
  $("researchBody").innerHTML = `
    <div class="pick-group"><div class="lbl">Indices</div><div class="pick-grid">${primes.map(pick).join("")}</div></div>
    <div class="pick-group"><div class="lbl">Segments / sectors</div><div class="pick-grid">${sectors.map(pick).join("")}</div></div>
    <div class="modal-actions">
      <button class="btn ghost" id="pickAll">Select all</button>
      <button class="btn ghost" id="pickNone">Clear</button>
      <span class="spacer"></span>
      <span class="pick-note" id="pickNote">${items.length} segments available</span>
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
      <div class="opt-cell"><div class="k">Threshold</div><div class="v">${r.minMove}–${r.maxMove} pts</div></div>
      <div class="opt-cell"><div class="k">Capture odds / timing</div><div class="v">${r.captureProb == null ? "—" : r.captureProb + "%"} · ${esc(r.timing || "—")}</div></div>
      <div class="opt-cell"><div class="k">Entry</div><div class="v">${fmt(p.entry ?? r.entry)}</div></div>
      <div class="opt-cell"><div class="k">SL</div><div class="v neg">${fmt(p.stop ?? r.stop)}</div></div>
      <div class="opt-cell"><div class="k">T1 / T2 / T3 · pts</div><div class="v pos">${fmt(p.target1 ?? r.target1)} (+${p.target1Move ?? "—"}) / ${fmt(p.target2 ?? r.target2)} (+${p.target2Move ?? "—"}) / ${fmt(p.target3 ?? r.target3)} (+${p.target3Move ?? "—"})</div></div>
    </div>`;
  };

  $("researchBody").innerHTML = `
    ${notes}
    <div class="verdict">
      <div class="vh">Scan verdict · ${esc(res.istClock)} IST · ${res.scanned} segments ·
        ${res.qualifiedCount} qualifying</div>
      ${esc(res.verdict)}
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
      minimum ${near.minMove} (${esc(near.forecastStatus || "")}, ${esc(String(near.timing || "").toLowerCase())} timing).
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
        <div><span>T1-before-SL win</span><b class="${a.winRate >= 75 ? "pos" : "neg"}">${a.winRate}%</b></div>
        <div><span>Worst month</span><b class="${(a.worstMonth ?? 0) >= 60 ? "pos" : "neg"}">${a.worstMonth == null ? "—" : a.worstMonth + "%"}</b></div>
        <div><span>Min move reached</span><b>${a.reachRate}%</b></div>
        <div><span>Avg bars to T1</span><b>${a.avgBarsToT1 ?? "—"}</b></div>
        <div><span>Avg risk (stop)</span><b>${a.avgRisk ?? "—"} pts</b></div>
        <div><span>R:R at T1</span><b class="${(a.rr1 ?? 0) >= 1 ? "pos" : "neg"}">${a.rr1 ?? "—"}</b></div>
        <div><span>Expectancy</span><b class="${(a.expectancyPts ?? 0) >= 0 ? "pos" : "neg"}">${a.expectancyPts == null ? "—" : (a.expectancyPts >= 0 ? "+" : "") + a.expectancyPts} pts</b></div>
      </div>
      <div class="pf-note">Min move ${a.minMove} pts · ${a.unresolved} unresolved at the horizon · window ${a.lookbackBars} bars (all the feed returned).
        "Win" = T1 hit before the stop (stop counted first if one candle touches both).
        "Reach" = the min move was touched at any point in the horizon, even after a stop.
        "Expectancy" = (wins × min move − losses × avg risk) ÷ resolved trades, before costs.
        ${a.winRate >= 75 ? "" : `<b class="neg">This profile is below its 75% floor, so the desk refuses new calls until it recovers.</b>`}</div>
      ${a.lowSample ? `<div class="pf-note neg">Fewer than 15 setups — indicative only, not proven.</div>` : ""}
      ${ex ? `<div class="table-wrap"><table class="research pf-table">
        <thead><tr><th>Bar time IST</th><th>Side</th><th>Entry</th><th>T1 / SL</th>
          <th>Outcome</th></tr></thead><tbody>${ex}</tbody></table></div>` : ""}
    </div>`;
  }).join("");

  let cmp = "";
  for (const iid of ["NIFTY", "SENSEX"]) {
    const v = p.validation[iid];
    if (!v || !v.comparison) continue;
    const rows = Object.entries(v.comparison).map(([k, r]) => `<tr class="${k === v.selected ? "sel" : ""}">
        <td class="name">${esc(r.profile)}${k === v.selected ? ' <span class="chip chip-live" style="font-size:9px">IN USE</span>' : ""}
          <div class="fcs">${esc(r.horizonLabel)} · stop ${esc(r.stopRule || (r.stopAtr + "×ATR"))}</div></td>
        <td class="num">${r.samples}</td>
        <td class="num ${r.winRate >= 75 ? "pos" : "neg"}">${r.winRate}%${r.samples ? `<div class="fcs">${r.lowSample ? "small sample" : "worst mo " + (r.worstMonth ?? "—") + "%"}</div>` : ""}</td>
        <td class="num">${r.reachRate}%</td>
        <td class="num ${(r.expectancyPts ?? 0) >= 0 ? "pos" : "neg"}">${r.expectancyPts == null ? "—" : (r.expectancyPts >= 0 ? "+" : "") + r.expectancyPts}</td>
      </tr>`).join("");
    const name = (p.provenance.find((x) => x.id === iid) || {}).name || iid;
    cmp += `<div class="proof-card">
      <div class="pf-h">${esc(name)} — 75% precision vs balanced 69%, same six months of 5m bars</div>
      <div class="table-wrap"><table class="research pf-table">
        <thead><tr><th>Profile</th><th>Setups</th><th>Win rate</th><th>Reach</th><th>Exp (pts)</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="pf-note">The 75% profile requires EMA 9/21, MACD, Supertrend and an extreme RSI to all agree,
        an ATR-expected move of at least 4× the minimum-move target, entry before 14:30 IST, one trade at a
        time with a 15-minute cooldown, a 3-hour horizon and a stop at 3.4× the target. That wide stop is
        what buys the higher hit rate: R:R drops to ≈0.29 and expectancy is near zero before costs — check
        the Exp column. It may only call a trade while its measured win rate is ≥75%; below that the desk
        says NO CALL rather than fall back to the lower-hit-rate profile.</div>
    </div>`;
  }

  const method = Object.entries(p.method || {}).map(([k, v]) => {
    const label = { candles: "Candles", noLookahead: "No look-ahead", outcome: "Outcome rule",
      optionHistory: "Options history", reproduce: "Reproduce" }[k] || k;
    return `<li><b>${esc(label)}:</b> ${esc(v)}</li>`;
  }).join("");

  $("proofBody").innerHTML = `
    <div class="verdict">
      <div class="vh">Provenance at ${esc(p.istClock)} IST</div>
      Every value below is fetched from Upstox (live) or Yahoo (real, delayed). The desk has no mock path:
      if a source fails the cell says ERROR and no number is invented. <b>UPSTOX+YAHOO</b> means the 5m series
      is Upstox data with the just-finished session filled from Yahoo's real 5m bars while Upstox EOD history
      catches up overnight.
    </div>
    <div class="prov-grid">${provCards}</div>
    ${auditCards}
    ${cmp}
    <div class="proof-card">
      <div class="pf-h">How to judge this yourself</div>
      <ul class="pf-method">${method}</ul>
      <div class="pf-cmd">Independent check — pulls Upstox and Yahoo again, diffs every overlapping bar,
        recomputes the audits and exits non-zero if anything disagrees:<br><code>python verify.py</code></div>
    </div>`;
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
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeResearch(); closeProof(); }
});

setInterval(clockTick, 1000);
clockTick();
setAuto(true);
load(true);
