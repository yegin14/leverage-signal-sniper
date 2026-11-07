"use strict";

/*
 * Read-only panel. Every value comes from /api/state. Headlines and agent text are untrusted, so
 * text enters the page only through textContent. If the server stops answering, the page says so
 * and dims the old numbers instead of presenting them as current.
 */

const STATE_POLL_MS = 5000;
const CURVE_POLL_MS = 60000;
const STALE_QUOTE_SECONDS = 90;
const SVG_NS = "http://www.w3.org/2000/svg";

const TABS = [
  ["genel", "Genel"], ["pozisyonlar", "Pozisyonlar"], ["islemler", "İşlemler"], ["arbitraj", "Arbitraj"],
  ["haberler", "Haberler"], ["masa", "Masa"], ["giderler", "Giderler"], ["sistem", "Sistem"],
];

let state = null;
let curve = null;
let lastOk = 0;
let market = "ALL";
let activeTab = (location.hash || "#genel").slice(1);
if (!TABS.some(([key]) => key === activeTab)) activeTab = "genel";
const openSessions = new Set();
const chartCache = { key: "", node: null };

// ---------------------------------------------------------------- helpers

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  for (const kid of kids) {
    if (kid === null || kid === undefined) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}

const numberTr = (digits) => new Intl.NumberFormat("tr-TR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const n2 = numberTr(2);
const n3 = numberTr(3);
const n1 = numberTr(1);
const priceFormat = new Intl.NumberFormat("tr-TR", { maximumSignificantDigits: 6 });
const dateFormat = new Intl.DateTimeFormat("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

function money(x) {
  if (x === null || x === undefined) return "yok";
  const sign = x > 0 ? "+" : x < 0 ? "−" : "";
  return `${sign}${n2.format(Math.abs(x))} $`;
}
function plain(x, fmt = n2) { return x === null || x === undefined ? "yok" : `${fmt.format(x)} $`; }
function price(x) { return x === null || x === undefined ? "yok" : priceFormat.format(x); }
function bps(x) {
  if (x === null || x === undefined) return "yok";
  return `${x > 0 ? "+" : x < 0 ? "−" : ""}${n1.format(Math.abs(x))}`;
}
function when(ts) { return ts ? dateFormat.format(new Date(ts * 1000)) : "yok"; }
function duration(seconds) {
  if (seconds === null || seconds === undefined) return "yok";
  if (seconds < 90) return `${Math.round(seconds)} sn`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} dk`;
  if (seconds < 172800) return `${n1.format(seconds / 3600)} sa`;
  return `${n1.format(seconds / 86400)} gün`;
}
function ago(ts) { return ts ? `${duration(Date.now() / 1000 - ts)} önce` : "hiç"; }
function signClass(x) { return x > 0 ? "pos" : x < 0 ? "neg" : ""; }
function direction(d) { return d === "LONG" ? "▲ LONG" : d === "SHORT" ? "▼ SHORT" : d; }
const sourceIndex = (key) => state.sources.findIndex((s) => s.key === key);

function table(columns, rows, emptyText) {
  if (!rows.length) return el("p", { class: "empty", text: emptyText });
  const head = el("tr", null, ...columns.map((c) => el("th", { class: c.num ? "num" : null, scope: "col", text: c.h })));
  const body = rows.map((row) => el("tr", null, ...columns.map((c) => {
    const value = c.v(row);
    const cell = el("td", { class: [c.num ? "num" : "", c.wrap ? "wrap" : "", c.cls ? c.cls(row) : ""].join(" ").trim() || null });
    cell.append(value instanceof Node ? value : document.createTextNode(String(value)));
    return cell;
  })));
  return el("div", { class: "table-wrap", tabindex: "0" }, el("table", null, el("thead", null, head), el("tbody", null, ...body)));
}

function card(title, note, ...kids) {
  const head = el("div", { class: "card-head" }, el("h2", { text: title }));
  return el("div", { class: "card" }, head, note ? el("p", { class: "note", text: note }) : null, ...kids);
}

function matchesMarket(row) { return market === "ALL" || row.kind === market; }

function marketFilter() {
  const options = [["ALL", "Hepsi"], ["CEX", `CEX (${state.venues.cex || "yok"})`], ["DEX", `DEX (${state.venues.dex})`]];
  return el("div", { class: "filters" }, el("span", { class: "label", text: "Piyasa" }),
    el("div", { class: "segmented", role: "group", "aria-label": "Piyasa seçimi" }, ...options.map(([key, label]) => el("button", {
      type: "button", text: label, "aria-pressed": String(market === key),
      onclick: () => { market = key; renderActive(); },
    }))));
}

// ---------------------------------------------------------------- Genel

function renderGenel(root) {
  const books = state.books.filter(matchesMarket);
  const net = books.reduce((sum, b) => sum + b.total_net, 0);
  const initial = books.reduce((sum, b) => sum + b.initial, 0);
  const equity = books.reduce((sum, b) => sum + b.equity, 0);
  const trades = books.reduce((sum, b) => sum + b.trades, 0);
  const wins = books.reduce((sum, b) => sum + b.wins, 0);
  const open = books.reduce((sum, b) => sum + b.open, 0);
  const fees = books.reduce((sum, b) => sum + b.costs.taker_fee + b.costs.maker_fee, 0);

  const tile = (label, value) => el("div", { class: "tile" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }));
  const hero = el("div", { class: "card" },
    el("div", { class: "muted", text: "Toplam net kâr/zarar" }),
    el("div", { class: `hero-value ${signClass(net)}`, text: money(net) }),
    el("p", { class: "muted", text: `${books.length} ayrı kasa, ${plain(initial, numberTr(0))} başlangıç. Açık pozisyonlar ve tüm giderler dahil.` }));
  const tiles = el("div", { class: "card tiles" },
    tile("Özsermaye", plain(equity)),
    tile("Açık pozisyon", String(open)),
    tile("Kapanan işlem", String(trades)),
    tile("Kazanma oranı", trades ? `%${n1.format(wins / trades * 100)}` : "yok"),
    tile("Ödenen komisyon", plain(fees)));

  const chart = chartBlock();

  const columns = [
    { h: "Kasa", v: (b) => b.source_label },
    { h: "Piyasa", v: (b) => `${b.kind} (${b.venue})` },
    { h: "Net", num: true, v: (b) => money(b.total_net), cls: (b) => signClass(b.total_net) },
    { h: "Bakiye", num: true, v: (b) => plain(b.balance) },
    { h: "Açık K/Z", num: true, v: (b) => money(b.unrealized), cls: (b) => signClass(b.unrealized) },
    { h: "Açık", num: true, v: (b) => b.open },
    { h: "İşlem", num: true, v: (b) => b.trades },
    { h: "Kazanma", num: true, v: (b) => (b.trades ? `%${n1.format(b.win_rate)}` : "yok") },
    { h: "Kâr faktörü", num: true, v: (b) => (!b.trades ? "yok" : b.profit_factor === null ? "kayıp yok" : n2.format(b.profit_factor)) },
    { h: "En büyük düşüş", num: true, v: (b) => plain(b.max_drawdown) },
    { h: "Reddedilen", num: true, v: (b) => Object.values(b.rejected).reduce((a, c) => a + c, 0) },
  ];
  root.replaceChildren(marketFilter(), el("div", { class: "summary" }, hero, tiles), chart.node,
    card("Kasalar", "Her kaynak CEX ve DEX'te ayrı 100 $ ile başlar, sonuçlar birbirine karışmaz.", table(columns, books, "Kasa yok.")));
  if (chart.fresh) renderChart();
}

// The chart redraws only when its data or filter changes. Rebuilding it on every state poll would
// close the tooltip someone is reading and collapse the table view.
function chartBlock() {
  const points = curve ? curve.points : [];
  const key = `${market}|${points.length}|${points.length ? points[points.length - 1].t : 0}`;
  if (chartCache.node && chartCache.key === key) return { node: chartCache.node, fresh: false };
  const legend = el("ul", { class: "legend" }, ...state.sources.map((s, i) => el("li", null, el("span", { class: `line-key k${i}` }), s.label)));
  const host = el("div", { class: "chart-host", id: "chart" });
  const node = card("Kasaların kâr/zarar seyri", null, legend, host, chartTwin());
  node.querySelector(".card-head").append(el("span", { class: "muted", text: "Her 5 dakikada bir ölçülür, tüm giderler dahil" }));
  chartCache.node = node;
  chartCache.key = key;
  return { node, fresh: true };
}

function buildSeries() {
  if (!curve || !curve.points || curve.points.length < 2) return null;
  const times = curve.points.map((p) => p.t);
  const series = state.sources.map((source, index) => {
    const names = Object.keys(curve.books).filter((name) => curve.books[name].source === source.key && (market === "ALL" || curve.books[name].kind === market));
    if (!names.length) return null;
    const values = curve.points.map((p) => {
      let total = 0;
      for (const name of names) {
        if (p.e[name] === undefined) return null;
        total += p.e[name] - curve.books[name].initial;
      }
      return total;
    });
    return { index, label: source.label, values };
  }).filter(Boolean);
  return { times, series };
}

function chartTwin() {
  const data = buildSeries();
  if (!data) return null;
  const rows = data.times.map((t, i) => ({ t, i })).slice(-24).reverse();
  const columns = [{ h: "Zaman", v: (r) => when(r.t) }, ...data.series.map((s) => ({ h: s.label, num: true, v: (r) => money(s.values[r.i]) }))];
  return el("details", { class: "table-twin" }, el("summary", { text: "Grafiği tablo olarak göster (son 24 ölçüm)" }), table(columns, rows, ""));
}

function niceStep(range) {
  const rough = range / 4;
  const power = Math.pow(10, Math.floor(Math.log10(rough)));
  const scaled = rough / power;
  return (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10) * power;
}

function renderChart() {
  const host = document.getElementById("chart");
  if (!host) return;
  host.replaceChildren();
  const data = buildSeries();
  if (!data) {
    host.append(el("p", { class: "empty", text: "Grafik için en az iki ölçüm gerekir. İlk ölçümler birkaç dakika içinde gelir." }));
    return;
  }
  const W = Math.max(host.clientWidth, 280);
  const H = 300;
  const m = { l: 62, r: 14, t: 10, b: 26 };
  const all = data.series.flatMap((s) => s.values.filter((v) => v !== null));
  let lo = Math.min(0, ...all);
  let hi = Math.max(0, ...all);
  if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
  const step = niceStep(hi - lo);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const t0 = data.times[0];
  const t1 = data.times[data.times.length - 1];
  const x = (t) => m.l + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * (W - m.l - m.r);
  const y = (v) => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);
  const fractionDigits = step < 1 ? (step < 0.1 ? 2 : 1) : 0;
  const tickFormat = numberTr(fractionDigits);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", tabindex: "0",
    "aria-label": `Kasaların kâr/zarar seyri. Değerler aşağıdaki tabloda da var. Sol ve sağ ok tuşlarıyla gezilebilir.` });
  for (let v = lo; v <= hi + step / 2; v += step) {
    const isZero = Math.abs(v) < step / 1000;
    root.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: isZero ? "zero-line" : "grid-line" }));
    const label = svg("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end", class: "tick" });
    label.textContent = `${v > 0 ? "+" : v < 0 ? "−" : ""}${tickFormat.format(Math.abs(v))} $`;
    root.append(label);
  }
  const tickCount = Math.min(W < 520 ? 3 : 6, data.times.length);
  for (let i = 0; i < tickCount; i++) {
    const t = t0 + (t1 - t0) * (tickCount === 1 ? 0 : i / (tickCount - 1));
    const anchor = i === 0 ? "start" : i === tickCount - 1 ? "end" : "middle";
    const label = svg("text", { x: x(t), y: H - 6, "text-anchor": anchor, class: "tick" });
    label.textContent = when(t);
    root.append(label);
  }
  for (const s of data.series) {
    let d = "";
    s.values.forEach((v, i) => {
      if (v === null) return;
      d += `${d ? "L" : "M"}${x(data.times[i]).toFixed(1)} ${y(v).toFixed(1)}`;
    });
    root.append(svg("path", { d, class: `series s${s.index}` }));
    const last = s.values[s.values.length - 1];
    if (last !== null) root.append(svg("circle", { cx: x(t1), cy: y(last), r: 4, class: `end-dot d${s.index}` }));
  }
  const cross = svg("line", { y1: m.t, y2: H - m.b, class: "cross", visibility: "hidden" });
  root.append(cross);
  host.append(root);

  const tip = el("div", { class: "tooltip", hidden: "hidden" });
  host.append(tip);
  let current = null;

  function show(index) {
    current = index;
    const t = data.times[index];
    const px = x(t);
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    cross.setAttribute("visibility", "visible");
    const rows = data.series
      .map((s) => ({ s, v: s.values[index] }))
      .sort((a, b) => (b.v ?? -Infinity) - (a.v ?? -Infinity))
      .map(({ s, v }) => el("div", { class: "row" }, el("span", { class: `line-key k${s.index}` }), el("strong", { text: money(v) }), el("span", { class: "name", text: s.label })));
    tip.replaceChildren(el("div", { class: "when", text: when(t) }), ...rows);
    tip.hidden = false;
    const scale = host.clientWidth / W;
    const room = host.clientWidth - tip.offsetWidth - 8;
    const wanted = px * scale + 12;
    tip.style.left = `${wanted > room ? Math.max(0, px * scale - tip.offsetWidth - 12) : wanted}px`;
  }
  function hide() {
    current = null;
    cross.setAttribute("visibility", "hidden");
    tip.hidden = true;
  }
  function nearest(clientX) {
    const rect = root.getBoundingClientRect();
    const target = (clientX - rect.left) * (W / rect.width);
    let best = 0;
    let bestGap = Infinity;
    data.times.forEach((t, i) => {
      const gap = Math.abs(x(t) - target);
      if (gap < bestGap) { bestGap = gap; best = i; }
    });
    return best;
  }
  root.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
  root.addEventListener("pointerleave", hide);
  root.addEventListener("blur", hide);
  root.addEventListener("focus", () => show(data.times.length - 1));
  root.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const start = current === null ? data.times.length - 1 : current;
    show(Math.min(data.times.length - 1, Math.max(0, start + (e.key === "ArrowRight" ? 1 : -1))));
  });
}

// ---------------------------------------------------------------- Pozisyonlar and İşlemler

function renderPozisyonlar(root) {
  const rows = state.positions.filter(matchesMarket);
  const columns = [
    { h: "Kasa", v: (p) => `${p.source_label} / ${p.kind}` },
    { h: "Parite", v: (p) => p.symbol },
    { h: "Yön", v: (p) => direction(p.direction) },
    { h: "Şimdi kapansa", num: true, v: (p) => money(p.net_now), cls: (p) => signClass(p.net_now) },
    { h: "Büyüklük", num: true, v: (p) => plain(p.notional) },
    { h: "Kaldıraç", num: true, v: (p) => `${p.leverage}x` },
    { h: "Teminat", num: true, v: (p) => plain(p.margin) },
    { h: "Giriş", num: true, v: (p) => price(p.entry) },
    { h: "Anlık", num: true, v: (p) => {
      if (p.quote_age === null || p.quote_age > STALE_QUOTE_SECONDS) return el("span", { class: "warn-cell", text: `⚠ ${price(p.mark)}, eski` });
      return price(p.mark);
    } },
    { h: "Stop", num: true, v: (p) => price(p.stop) },
    { h: "Hedef", num: true, v: (p) => (p.target === null ? "izleyen stop" : price(p.target)) },
    { h: "Tasfiye", num: true, v: (p) => price(p.liquidation) },
    { h: "Fonlama", num: true, v: (p) => money(-p.funding) },
    { h: "Süre", num: true, v: (p) => duration(Date.now() / 1000 - p.opened_at) },
    { h: "Neden", wrap: true, v: (p) => p.reason },
  ];
  root.replaceChildren(marketFilter(), card("Açık pozisyonlar",
    "\"Şimdi kapansa\" giriş komisyonunu, tahmini çıkış komisyonunu ve fonlamayı düşer. Sahte paradır.",
    table(columns, rows, "Açık pozisyon yok.")));
}

function renderIslemler(root) {
  const rows = state.trades.filter(matchesMarket);
  const columns = [
    { h: "Kapanış", v: (t) => when(t.closed_at) },
    { h: "Kasa", v: (t) => `${t.source_label} / ${t.kind}` },
    { h: "Parite", v: (t) => t.symbol },
    { h: "Yön", v: (t) => direction(t.direction) },
    { h: "Net", num: true, v: (t) => money(t.net), cls: (t) => signClass(t.net) },
    { h: "Çıkış nedeni", v: (t) => (t.liquidated ? "⚠ TASFİYE" : t.exit_reason) },
    { h: "Giriş", num: true, v: (t) => price(t.entry) },
    { h: "Çıkış", num: true, v: (t) => price(t.exit) },
    { h: "Brüt", num: true, v: (t) => money(t.gross) },
    { h: "Komisyon", num: true, v: (t) => plain(t.fees, n3) },
    { h: "Fonlama", num: true, v: (t) => money(-t.funding) },
    { h: "Kayma*", num: true, v: (t) => plain(t.slippage, n3) },
    { h: "Süre", num: true, v: (t) => duration(t.closed_at - t.opened_at) },
    { h: "Neden", wrap: true, v: (t) => t.reason },
  ];
  root.replaceChildren(marketFilter(), card("Kapanan işlemler",
    `Toplam ${state.trades_total} işlem, en yeni ${state.trades.length} tanesi gösteriliyor. *Kayma fiyatların içindedir, netten ayrıca düşülmez.`,
    table(columns, rows, "Henüz kapanan işlem yok.")));
}

// ---------------------------------------------------------------- Arbitraj

function renderArbitraj(root) {
  const a = state.arbitrage;
  const hours = (Date.now() / 1000 - a.started_at) / 3600;
  const status = (k) => {
    if (k.unavailable) return el("span", { class: "warn-cell", text: `⚠ ${k.unavailable}` });
    if (k.last_error) return el("span", { class: "warn-cell", text: `⚠ ${k.last_error.detail}` });
    return k.last_pair || "";
  };
  const columns = [
    { h: "Tür", v: (k) => k.label },
    { h: "Şu an brüt", num: true, v: (k) => bps(k.last_gross_bps) },
    { h: "Şu an net", num: true, v: (k) => bps(k.last_net_bps), cls: (k) => signClass(k.last_net_bps) },
    { h: "En iyi net", num: true, v: (k) => bps(k.best_net_bps), cls: (k) => signClass(k.best_net_bps) },
    { h: "Tarama", num: true, v: (k) => k.scans },
    { h: "Kârlı tarama", num: true, v: (k) => k.positive },
    { h: "İşlem", num: true, v: (k) => k.trades },
    { h: "Net", num: true, v: (k) => money(k.net_usd), cls: (k) => signClass(k.net_usd) },
    { h: "Son tarama", num: true, v: (k) => ago(k.last_scan_at) },
    { h: "Şu anki en iyi çift", wrap: true, v: status },
  ];
  const history = a.kinds.flatMap((k) => k.history.map((t) => ({ ...t, label: k.label }))).sort((x, y) => y.at - x.at);
  const historyColumns = [
    { h: "Zaman", v: (t) => when(t.at) }, { h: "Tür", v: (t) => t.label }, { h: "Çift", v: (t) => t.symbol },
    { h: "Alış", v: (t) => t.buy_at }, { h: "Satış", v: (t) => t.sell_at },
    { h: "Net", num: true, v: (t) => money(t.net), cls: (t) => signClass(t.net) },
    { h: "Brüt", num: true, v: (t) => money(t.gross) }, { h: "Komisyon", num: true, v: (t) => plain(t.fees, n3) },
    { h: "Gaz ve transfer", num: true, v: (t) => plain(t.other, n3) },
  ];
  root.replaceChildren(
    card("Arbitraj ölçümü",
      `${n1.format(hours)} saattir ölçülüyor, işlem başına ${n2.format(a.notional)} $. bps: yüzde 0,01. ` +
      `Net, tüm komisyon, gaz ve tahmini transfer ücretinden sonradır. Net ${n1.format(a.min_net_bps)} bps altındayken işlem açılmaz, çünkü o işlem baştan zarardır.`,
      table(columns, a.kinds, "Veri yok.")),
    card("Arbitraj işlemleri", null, table(historyColumns, history, "Henüz masraf sonrası kârlı bir fırsat çıkmadı.")));
}

// ---------------------------------------------------------------- Haberler

function actionText(item) {
  if (item.actions.length) {
    return item.actions.map((a) => {
      const base = `${a.symbol} ${direction(a.direction)}`;
      const opened = a.opened.length ? `açıldı: ${a.opened.join(", ")}` : null;
      const rejected = Object.entries(a.rejected).map(([venue, why]) => `${venue} açılmadı (${why})`).join("; ");
      return [base, opened, rejected || null].filter(Boolean).join(" · ");
    }).join("  |  ");
  }
  if (item.sentiment === 0) return "İşlem yok: yön net değil.";
  if (!item.symbols.length) return "İşlem yok: izlenen bir parite geçmiyor.";
  return "İşlem yok: haber 30 dakikadan eski kaldı.";
}

function renderHaberler(root) {
  const items = state.news.items;
  const list = items.length ? el("ul", { class: "news-list" }, ...items.map((item) => {
    const label = item.sentiment > 0 ? "▲ Olumlu" : item.sentiment < 0 ? "▼ Olumsuz" : "Nötr";
    const title = /^https?:\/\//.test(item.link)
      ? el("a", { href: item.link, target: "_blank", rel: "noopener noreferrer", text: item.title })
      : item.title;
    return el("li", null,
      el("div", { class: "news-title" }, title),
      el("div", { class: "news-meta" }, el("span", { class: "chip", text: label }), el("span", { text: item.source }), el("span", { text: ago(item.published) }),
        item.symbols.length ? el("span", { text: item.symbols.map((s) => s.replace("USDT", "")).join(", ") }) : null),
      el("div", { class: "news-action", text: actionText(item) }));
  })) : el("p", { class: "empty", text: "Henüz haber alınamadı." });
  const feeds = Object.entries(state.news.feeds).map(([name, f]) => `${f.ok ? "✓" : "⚠"} ${name}: ${f.detail}`).join("  ·  ");
  root.replaceChildren(card("Haberler",
    "Olumlu haber long, olumsuz haber short açar. Yönü anahtar kelimeler belirler, bu yöntem kabadır ve bağlamı kaçırabilir. Başlıkların doğruluğu garanti değildir. Son 30 dakikadaki haberler işleme girer.",
    el("p", { class: "muted", text: `Son kontrol: ${ago(state.news.last_fetch)}. ${feeds}` }), list));
}

// ---------------------------------------------------------------- Masa

function renderMasa(root) {
  const d = state.desk;
  const statusText = d.block ? `Çalışmıyor: ${d.block}` : "Hazır";
  const facts = el("p", { class: "note", text:
    `Durum: ${statusText}. Model: ${d.model}. Bugün ${d.sessions_today} oturum, ${n3.format(d.spent_today)} $ / ${n2.format(d.budget_usd)} $ bütçe. ` +
    `Toplam ${d.sessions_total} oturum, ${n3.format(d.spent_total)} $. Bu harcama gerçek paradır, sahte değildir.` });
  const hint = d.has_key ? null : el("p", { class: "banner", text: "Masa kapalı çünkü API anahtarı girilmedi. Render ayarlarına ANTHROPIC_API_KEY eklenince kendiliğinden başlar. Kaldıraç, haber ve arbitraj bölümleri anahtarsız çalışır." });
  const sessions = d.sessions.length ? d.sessions.map((s) => {
    const details = el("details", { class: "session", open: openSessions.has(s.id) ? "open" : null,
      ontoggle: (e) => { if (e.target.open) openSessions.add(s.id); else openSessions.delete(s.id); } },
      el("summary", null, `${s.symbol}: ${s.action === "NO_TRADE" ? "İşlem yok" : direction(s.action)}`, el("span", { class: "meta", text: `  ${when(s.started_at)}, ${n3.format(s.cost_usd)} $` })),
      el("p", { class: "muted", text: `Tetikleyen: ${s.label}. Sonuç: ${s.reason}` }),
      ...s.turns.map((t) => el("div", { class: "voice" },
        el("div", { class: "who", text: `${t.role}: ${t.stance}, güven ${t.conviction}/100${t.veto ? ", VETO" : ""}` }),
        el("div", { class: "say", text: t.argument }),
        t.risks.length ? el("div", { class: "risks", text: `Riskler: ${t.risks.join("; ")}` }) : null)));
    return details;
  }) : [el("p", { class: "empty", text: "Henüz masa oturumu yapılmadı." })];
  root.replaceChildren(card("Yapay zeka masası", null, facts, hint, ...sessions));
}

// ---------------------------------------------------------------- Giderler

function renderGiderler(root) {
  const books = state.books.filter(matchesMarket);
  const columns = [
    { h: "Kasa", v: (b) => b.source_label },
    { h: "Piyasa", v: (b) => `${b.kind} (${b.venue})` },
    { h: "Taker komisyon", num: true, v: (b) => plain(b.costs.taker_fee, n3) },
    { h: "Maker komisyon", num: true, v: (b) => plain(b.costs.maker_fee, n3) },
    { h: "Fonlama", num: true, v: (b) => plain(b.costs.funding, n3) },
    { h: "Transfer (tahmini)", num: true, v: (b) => plain(b.costs.transfer) },
    { h: "Kayma*", num: true, v: (b) => plain(b.costs.slippage, n3) },
    { h: "Tasfiye", num: true, v: (b) => b.liquidations },
  ];
  const d = state.desk;
  const arbFees = state.arbitrage.kinds.reduce((sum, k) => sum + k.fees_usd, 0);
  const arbOther = state.arbitrage.kinds.reduce((sum, k) => sum + k.other_usd, 0);
  root.replaceChildren(marketFilter(),
    card("Kaldıraç kasalarının giderleri", "*Kayma fiyatların içindedir, netten ayrıca düşülmez. Pozitif fonlama ödendi, negatif fonlama alındı demektir.", table(columns, books, "Veri yok.")),
    card("Diğer giderler", null,
      el("p", { class: "note", text: `Yapay zeka masası (gerçek para): ${d.calls} çağrı, ${d.input_tokens.toLocaleString("tr-TR")} giriş ve ${d.output_tokens.toLocaleString("tr-TR")} çıkış tokeni, toplam ${n3.format(d.spent_total)} $.` }),
      el("p", { class: "note", text: `Arbitraj: komisyon ${n3.format(arbFees)} $, gaz ve transfer ${n3.format(arbOther)} $.` })),
    card("Sahte işlem kuralları", null, el("p", { class: "note", text:
      `Her işlemde en çok ${n2.format(state.limits.risk_per_trade_usd)} $ risk, en çok ${state.limits.max_leverage}x kaldıraç, izole teminat. Tasfiye fiyatı stoptan en az iki kat uzakta tutulur. Giriş ve stop gerçek tahtadan, kötü taraftan dolar. Hedef limit emirdir, fiyat içinden geçmeden dolmaz. Mum hem stopa hem hedefe değerse stop sayılır. Deneme sürecinde işlem sayısı sınırı yoktur. Aynı kasada bir paritede tek pozisyon açılır, teminat bitince yeni işlem açılmaz.` })));
}

// ---------------------------------------------------------------- Sistem

function renderSistem(root) {
  const v = state.venues;
  const uptime = duration(state.generated_at - state.started_at);
  const facts = el("p", { class: "note", text:
    `Çalışma süresi: ${uptime}. CEX kasaları: ${v.cex || "erişilemiyor"}. DEX kasaları: ${v.dex}. Erişilen vadeli: ${v.perp.join(", ") || "yok"}. Erişilen spot: ${v.spot.join(", ") || "yok"}.` });
  const errors = state.errors.length
    ? el("div", { class: "banner", text: `⚠ Veri sorunu: ${state.errors.join("; ")}` })
    : el("p", { class: "muted", text: "✓ Şu an bildirilen veri sorunu yok." });
  const e = state.evidence;
  const evidenceColumns = [
    { h: "Strateji", v: (r) => r.name },
    { h: "Net", num: true, v: (r) => money(r.net_usd), cls: (r) => signClass(r.net_usd) },
    { h: "İşlem", num: true, v: (r) => r.trades },
    { h: "Dönem", v: (r) => r.period },
  ];
  const events = state.events.length ? el("ul", { class: "events" }, ...state.events.map((x) => el("li", null,
    el("time", { text: when(x.at) }), el("span", { class: "kind", text: x.kind }), el("span", { text: x.text })))) : el("p", { class: "empty", text: "Henüz olay yok." });
  root.replaceChildren(card("Sistem", null, facts, errors),
    card(`Geçmiş test (${e.measured_on})`, e.note, table(evidenceColumns, e.rows, "")),
    card("Olay günlüğü", null, events));
}

// ---------------------------------------------------------------- shell

const RENDERERS = { genel: renderGenel, pozisyonlar: renderPozisyonlar, islemler: renderIslemler, arbitraj: renderArbitraj,
  haberler: renderHaberler, masa: renderMasa, giderler: renderGiderler, sistem: renderSistem };

function renderTabs() {
  const nav = document.getElementById("tabs");
  const counts = state ? { pozisyonlar: state.positions.length, islemler: state.trades_total } : {};
  nav.replaceChildren(...TABS.map(([key, label]) => el("button", {
    type: "button", role: "tab", "aria-selected": String(activeTab === key), id: `tabbtn-${key}`,
    onclick: () => { activeTab = key; location.hash = key; showActive(); },
  }, label, counts[key] !== undefined ? el("span", { class: "count", text: counts[key] }) : null)));
}

function showActive() {
  for (const [key] of TABS) document.getElementById(`tab-${key}`).hidden = key !== activeTab;
  renderTabs();
  renderActive();
}

function renderActive() {
  if (!state) return;
  const root = document.getElementById(`tab-${activeTab}`);
  const scroll = window.scrollY;
  // A table someone has scrolled sideways keeps its place through the refresh.
  const sideways = [...root.querySelectorAll(".table-wrap")].map((wrap) => wrap.scrollLeft);
  try {
    RENDERERS[activeTab](root);
    root.querySelectorAll(".table-wrap").forEach((wrap, i) => { if (sideways[i]) wrap.scrollLeft = sideways[i]; });
  } catch (error) {
    // Partial numbers can mislead, so a rendering bug replaces the section with a plain message.
    console.error(error);
    root.replaceChildren(el("p", { class: "banner", text: "Bu bölüm gösterilemedi. Yanlış bilgi vermemek için rakamlar gizlendi." }));
  }
  window.scrollTo(0, scroll);
}

function renderFreshness() {
  const banner = document.getElementById("banner");
  const main = document.getElementById("main");
  const age = lastOk ? (Date.now() - lastOk) / 1000 : null;
  const stale = age === null || age > 20;
  main.classList.toggle("stale", stale && lastOk > 0);
  document.getElementById("freshness").textContent = lastOk ? `Son güncelleme: ${duration(age)} önce` : "Bağlanıyor…";
  banner.hidden = !(stale && lastOk > 0);
  if (!banner.hidden) banner.textContent = `⚠ Sunucuya ulaşılamıyor. Rakamlar ${duration(age)} öncesine ait, güncel değil.`;
}

async function fetchState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state = await response.json();
    lastOk = Date.now();
    renderTabs();
    renderActive();
  } catch (error) {
    console.warn("state fetch failed", error);
  }
  renderFreshness();
}

async function fetchCurve() {
  try {
    const response = await fetch("/api/curve", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    curve = await response.json();
    if (state && activeTab === "genel") renderActive();
  } catch (error) {
    console.warn("curve fetch failed", error);
  }
}

window.addEventListener("resize", () => { if (activeTab === "genel") renderChart(); });
window.addEventListener("hashchange", () => {
  const key = location.hash.slice(1);
  if (TABS.some(([k]) => k === key) && key !== activeTab) { activeTab = key; showActive(); }
});

showActive();
fetchCurve().then(fetchState);
setInterval(fetchState, STATE_POLL_MS);
setInterval(fetchCurve, CURVE_POLL_MS);
setInterval(renderFreshness, 1000);
