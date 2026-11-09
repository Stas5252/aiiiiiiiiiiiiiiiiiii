/* LeadHunter Mini App — три экрана, одна шторка, минимум кнопок. */
const tg = window.Telegram?.WebApp;
const initData = tg?.initData || new URLSearchParams(location.search).get("initData") || "";
const inTelegram = Boolean(tg?.initData);
if (tg) {
  tg.ready(); tg.expand();
  // Строго ч/б: тему задаёт colorScheme Telegram, а не цвета темы
  const applyTheme = () => { const c = tg.colorScheme === "dark" ? "dark" : "light", hex = c === "dark" ? "#000000" : "#ffffff";
    document.documentElement.dataset.theme = c;
    try { tg.setHeaderColor?.(hex); tg.setBackgroundColor?.(hex); tg.setBottomBarColor?.(hex); } catch (_) {} };
  try { tg.disableVerticalSwipes?.(); } catch (_) {}
  applyTheme(); tg.onEvent?.("themeChanged", applyTheme);
}

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const SRC = { telegram: "Telegram", threads: "Threads", instagram: "Instagram", yandex_maps: "Яндекс.Карты", site_audit: "Аудит сайта", "hh.ru": "hh.ru", vk: "VK", kwork: "Kwork", weblancer: "Weblancer", "freelance.ru": "Freelance.ru", web: "Поиск" };
const STATUS = { new: "новый", taken: "в работе", contacted: "написали", replied: "ответил", deal: "сделка", lost: "отказ", spam: "не лид", hot: "горячий" };
const SERVICE = { "сайт": "Сайт", "бот": "Бот", "автоматизация": "Автоматизация", "мультик": "Видео" };
const srcName = (s) => SRC[s.split(":")[0]] || (s.startsWith("rss:") ? s.slice(4) : s);
const fmtTime = (iso) => { const d = new Date(iso), now = new Date(); const diff = (now - d) / 60000;
  if (diff < 1) return "только что"; if (diff < 60) return `${Math.floor(diff)} мин`; if (diff < 24 * 60) return `${Math.floor(diff / 60)} ч`;
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }); };

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Init-Data": initData } });
  if (!r.ok) { const e = await r.json().catch(() => ({ error: r.statusText })); throw new Error(e.error || r.statusText); }
  return r.json();
}
let toastTimer;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 2000); }
const haptic = { tap: () => tg?.HapticFeedback?.impactOccurred("light"), ok: () => tg?.HapticFeedback?.notificationOccurred("success"), err: () => tg?.HapticFeedback?.notificationOccurred("error"), sel: () => tg?.HapticFeedback?.selectionChanged() };

/* ---------- навигация ---------- */
let me = null;
const screens = { today: loadToday, leads: () => loadLeads(), stats: loadStats, content: loadContent, settings: loadSettings };
let current = "today";
function show(name) {
  current = name;
  $$(".screen").forEach((s) => s.classList.toggle("active", s.id === "screen-" + name));
  $$(".tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.screen === name));
  $(".tabbar").classList.toggle("hidden", name === "settings");
  if (name === "settings") tg?.BackButton?.show(); else tg?.BackButton?.hide();
  window.scrollTo(0, 0);
  screens[name]?.();
}
$$(".tabbar button").forEach((b) => b.onclick = () => { haptic.sel(); show(b.dataset.screen); });
$("#open-settings").onclick = () => show("settings");
$$("[data-back]").forEach((b) => b.onclick = () => show("today"));
tg?.BackButton?.onClick(() => { if (!$("#sheet").classList.contains("hidden")) closeSheet(); else show("today"); });

/* ---------- карточка-строка лида ---------- */
function leadRow(l, opts = {}) {
  const cls = l.score >= 8 ? "hot" : l.score >= 6 ? "warm" : "";
  const title = l.summary || l.title || l.text;
  const sub = l.summary ? (l.title || l.text) : (l.title ? l.text : "");
  const mine = me && l.assigned_name && opts.mineLabel;
  return `<div class="lead" data-id="${l.id}">
    <div class="under"><span class="l">👋 Беру</span><span class="r">Не лид</span></div>
    <div class="inner">
      <div class="score ${cls}">${l.score}</div>
      <div class="body">
        <div class="title">${esc(title)}</div>
        ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}
        <div class="meta">
          <span>${esc(srcName(l.source))}</span>
          ${l.service ? `<span class="dot">${SERVICE[l.service] || esc(l.service)}</span>` : ""}
          ${l.budget ? `<span class="dot">${esc(l.budget)}</span>` : ""}
          <span class="dot">${fmtTime(l.created_at)}</span>
          ${l.status !== "new" ? `<span class="badge ${esc(l.status)}">${STATUS[l.status] || l.status}${l.assigned_name && !mine ? " · " + esc(l.assigned_name) : ""}</span>` : ""}
        </div>
      </div>
    </div></div>`;
}
function bindRows(container) {
  $$(".lead", container).forEach((el) => {
    el.addEventListener("click", () => { if (!el.dataset.swiped) openLead(+el.dataset.id); });
    swipe(el, +el.dataset.id);
  });
}
/* свайп вправо — взять, влево — не лид */
function swipe(el, id) {
  const inner = $(".inner", el); let x0 = null, dx = 0, moved = false;
  el.addEventListener("touchstart", (e) => { x0 = e.touches[0].clientX; dx = 0; moved = false; inner.style.transition = "none"; }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (x0 == null) return; dx = e.touches[0].clientX - x0; if (Math.abs(dx) < 8) return; moved = true;
    inner.style.transform = `translateX(${Math.max(-110, Math.min(110, dx))}px)`;
    el.classList.toggle("swipe-l", dx > 40); el.classList.toggle("swipe-r", dx < -40);
  }, { passive: true });
  el.addEventListener("touchend", async () => {
    inner.style.transition = ""; inner.style.transform = ""; el.classList.remove("swipe-l", "swipe-r");
    if (moved) { el.dataset.swiped = "1"; setTimeout(() => delete el.dataset.swiped, 400); }
    if (dx > 80) await act(id, "taken"); else if (dx < -80) await act(id, "spam");
    x0 = null;
  });
}
async function act(id, status) {
  try {
    await api(`/api/leads/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
    haptic.ok(); toast({ taken: "Лид ваш", spam: "Скрыто", contacted: "Напомню через сутки", deal: "🎉 Сделка!", replied: "Отлично", lost: "Отказ", hot: "🔥 Отмечен" }[status] || "Готово");
    refresh(); return true;
  } catch (e) { haptic.err(); toast(e.message); return false; }
}
function refresh() { screens[current]?.(); }

/* ---------- Сегодня ---------- */
async function loadToday() {
  try {
    const [stats, newLeads, mine, meResp] = await Promise.all([
      api("/api/stats?days=1"), api("/api/leads?status=new&limit=20"), api("/api/leads?status=open&mine=1&limit=20"), me ? Promise.resolve({ user: me }) : api("/api/me"),
    ]);
    me = meResp.user;
    const total = Object.values(stats.by_status).reduce((a, b) => a + b, 0);
    const kpi = (v, l, c = "") => `<div class="kpi ${c}"><b>${v}</b><span>${l}</span></div>`;
    $("#today-kpi").innerHTML = kpi(total, "лидов за сутки") + kpi(stats.by_status.new || 0, "ждут", (stats.by_status.new || 0) > 5 ? "bad" : "")
      + kpi(stats.response.median_min == null ? "—" : Math.round(stats.response.median_min) + " мин", "до «беру»", stats.response.median_min != null && stats.response.median_min <= 5 ? "good" : "");
    const broken = stats.sources.filter((s) => s.includes("⚠️"));
    $("#today-alert").classList.toggle("hidden", !broken.length);
    if (broken.length) $("#today-alert").textContent = broken.map((s) => s.split(" — ")[0]).join(", ") + (broken.length > 1 ? " не работают" : " не работает") + " — смотрите Отчёт";
    const hot = newLeads.leads.filter((l) => l.score >= 6), rest = newLeads.leads.filter((l) => l.score < 6);
    $("#today-new-count").textContent = newLeads.leads.length || "";
    $("#today-new").innerHTML = [...hot, ...rest].slice(0, 10).map((l) => leadRow(l)).join("") || `<div class="empty">Все лиды разобраны 👌</div>`;
    const my = mine.leads.filter((l) => l.status !== "new");
    $("#today-mine-count").textContent = my.length || "";
    $("#today-mine").innerHTML = my.map((l) => leadRow(l, { mineLabel: true })).join("") || `<div class="empty">Свайпните лид вправо, чтобы взять</div>`;
    bindRows($("#today-new")); bindRows($("#today-mine"));
  } catch (e) { $("#today-new").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ---------- Лиды ---------- */
const flt = { status: "open", service: "", q: "", offset: 0 };
$$("#seg-status button").forEach((b) => b.onclick = () => { $$("#seg-status button").forEach((x) => x.classList.toggle("on", x === b)); flt.status = b.dataset.status; flt.offset = 0; haptic.sel(); loadLeads(); });
$$("#chips-service button").forEach((b) => b.onclick = () => { $$("#chips-service button").forEach((x) => x.classList.toggle("on", x === b)); flt.service = b.dataset.service; flt.offset = 0; haptic.sel(); loadLeads(); });
let qT; $("#q").oninput = (e) => { clearTimeout(qT); qT = setTimeout(() => { flt.q = e.target.value.trim(); flt.offset = 0; loadLeads(); }, 300); };
$("#more").onclick = () => { flt.offset += 50; loadLeads(true); };
async function loadLeads(append = false) {
  const p = new URLSearchParams({ status: flt.status, service: flt.service, q: flt.q, offset: flt.offset, limit: 50 });
  try {
    const { leads } = await api("/api/leads?" + p);
    const list = $("#lead-list"), html = leads.map((l) => leadRow(l)).join("");
    if (append) list.insertAdjacentHTML("beforeend", html); else list.innerHTML = html || `<div class="empty">Ничего не найдено</div>`;
    $("#more-wrap").classList.toggle("hidden", leads.length < 50);
    bindRows(list);
  } catch (e) { $("#lead-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ---------- шторка ---------- */
let sheetId = null;
function closeSheet() { $("#sheet").classList.add("hidden"); sheetId = null; tg?.MainButton?.hide(); if (current !== "settings") tg?.BackButton?.hide(); }
$$("[data-close]").forEach((b) => b.addEventListener("click", closeSheet));
// свайп шторки вниз — закрыть
(() => { const body = $(".sheet-body"); let y0 = null;
  body.addEventListener("touchstart", (e) => { y0 = body.scrollTop <= 0 ? e.touches[0].clientY : null; }, { passive: true });
  body.addEventListener("touchend", (e) => { if (y0 != null && e.changedTouches[0].clientY - y0 > 90) closeSheet(); y0 = null; }); })();
function setMain(text, status) {
  if (!tg?.MainButton || !inTelegram) return false;
  tg.MainButton.offClick(mainHandler); mainHandler = async () => { if (await act(sheetId, status)) openLead(sheetId); };
  tg.MainButton.setParams({ text, is_visible: true, is_active: true }); tg.MainButton.onClick(mainHandler); return true;
}
let mainHandler = () => {};
async function openLead(id) {
  haptic.tap(); sheetId = id;
  let data; try { data = await api("/api/leads/" + id); } catch (e) { toast(e.message); return; }
  const { lead: l, events } = data;
  const open = ["new", "taken", "contacted", "replied"].includes(l.status);
  const mineLead = me && l.assigned_to === me.id;
  // одно главное действие — следующий шаг воронки
  const next = l.status === "new" ? ["taken", "👋 Беру"] : l.status === "taken" ? ["contacted", "✅ Написал клиенту"]
    : l.status === "contacted" ? ["replied", "💬 Клиент ответил"] : l.status === "replied" ? ["deal", "🤝 Сделка"] : null;
  const facts = [];
  if (l.contact) facts.push(["Контакт", linkify(l.contact)]);
  if (l.author && l.author !== l.contact) facts.push(["Автор", esc(l.author)]);
  if (l.author_bio) facts.push(["О себе", esc(l.author_bio)]);
  if (l.chat_title) facts.push(["Чат", esc(l.chat_title)]);
  if (l.budget) facts.push(["Бюджет", esc(l.budget)]);
  if (l.url) facts.push(["Источник", `<a href="${esc(l.url)}" data-ext>${esc(srcName(l.source))} ↗</a>`]);
  $("#sheet-content").innerHTML = `
    <h2>${esc(l.summary || l.title || l.text.slice(0, 90))}</h2>
    <div class="status-line">
      <span class="badge ${esc(l.status)}">${STATUS[l.status] || l.status}</span>
      <span>${l.score}/10</span>${l.urgency === "высокая" ? "<span>🔴 срочно</span>" : ""}
      ${l.service ? `<span>· ${SERVICE[l.service] || esc(l.service)}</span>` : ""}
      ${l.assigned_name ? `<span>· ${mineLead ? "у вас" : esc(l.assigned_name)}</span>` : ""}
      <span>· ${fmtTime(l.created_at)}</span>
    </div>
    ${l.summary ? `<div class="ai">${esc(l.summary)}</div>` : ""}
    ${l.title && l.summary ? `<div class="text"><b>${esc(l.title)}</b></div>` : ""}
    <div class="text clamp" id="lead-text">${esc(l.text)}</div>
    ${l.text.length > 400 ? `<button class="text" id="expand" style="padding:0">Показать полностью</button>` : ""}
    <div class="facts">${facts.map(([k, v]) => `<div><span class="k">${k}</span><span>${v}</span></div>`).join("")}</div>
    ${l.draft ? `<div class="draft"><div class="card-title">Черновик ответа <button class="text" id="copy" style="padding:0">Скопировать</button></div><pre id="draft">${esc(l.draft)}</pre></div>` : ""}
    ${next && !setMain(next[1], next[0]) ? `<button class="primary" data-s="${next[0]}">${next[1]}</button>` : ""}
    <div class="secondary">
      ${l.status === "new" ? `<button data-s="hot">🔥 Горячий</button><button data-s="spam" class="danger">Не лид</button>` : ""}
      ${open && l.status !== "new" ? `<button data-s="lost" class="danger">Отказ</button>` : ""}
      ${!open ? `<button data-s="taken">↩︎ Вернуть в работу</button>` : ""}
    </div>
    <div class="history">${events.map((e) => `${new Date(e.at).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} — ${STATUS[e.status] || e.status}${e.user_name ? " · " + esc(e.user_name) : ""}`).join("<br>")}</div>`;
  if (!next) tg?.MainButton?.hide();
  $("#sheet").classList.remove("hidden"); tg?.BackButton?.show();
  $$("#sheet-content [data-s]").forEach((b) => b.onclick = async () => { if (await act(id, b.dataset.s)) openLead(id); });
  const ex = $("#expand"); if (ex) ex.onclick = () => { $("#lead-text").classList.remove("clamp"); ex.remove(); };
  const cp = $("#copy"); if (cp) cp.onclick = () => { navigator.clipboard?.writeText($("#draft").textContent); haptic.ok(); toast("Скопировано"); };
  $$("#sheet-content a").forEach((a) => a.onclick = (ev) => { if (tg?.openLink && a.dataset.ext !== undefined) { ev.preventDefault(); tg.openLink(a.href); } else if (tg?.openTelegramLink && a.href.includes("t.me")) { ev.preventDefault(); tg.openTelegramLink(a.href); } });
}
function linkify(c) {
  const parts = c.split(/,\s*/).map((p) => {
    if (p.startsWith("@")) return `<a href="https://t.me/${esc(p.slice(1))}">${esc(p)}</a>`;
    if (/^https?:\/\//.test(p)) return `<a href="${esc(p)}" data-ext>${esc(p.replace(/^https?:\/\/(www\.)?/, ""))}</a>`;
    if (/^\+?\d[\d\s()\-]{6,}$/.test(p)) return `<a href="tel:${esc(p.replace(/[^\d+]/g, ""))}">${esc(p)}</a>`;
    if (p.includes("@")) return `<a href="mailto:${esc(p)}">${esc(p)}</a>`;
    return esc(p);
  });
  return parts.join(", ");
}

/* ---------- Отчёт ---------- */
let days = 30, funnelBy = "source", statsCache = null;
$$("#seg-days button").forEach((b) => b.onclick = () => { $$("#seg-days button").forEach((x) => x.classList.toggle("on", x === b)); days = +b.dataset.days; loadStats(); });
$$("#seg-funnel button").forEach((b) => b.onclick = () => { $$("#seg-funnel button").forEach((x) => x.classList.toggle("on", x === b)); funnelBy = b.dataset.by; if (statsCache) renderFunnel(statsCache); });
function renderFunnel(s) {
  const rows = (s["funnel_" + funnelBy] || []).slice(0, 8), max = Math.max(1, ...rows.map((r) => r.found));
  $("#funnel").innerHTML = rows.map((r) => `<div class="fr"><div><div class="k">${esc(r.k === "—" ? "без данных" : funnelBy === "source" ? srcName(r.k) : funnelBy === "service" ? (SERVICE[r.k] || r.k) : r.k)}</div>
    <div class="bar"><i style="width:${(r.found / max) * 100}%"></i><b style="width:${(r.deal / max) * 100}%"></b></div></div>
    <div class="n">${r.found} → ${r.contacted} → <b>${r.deal}</b></div></div>`).join("") || `<div class="empty">пока пусто</div>`;
}
async function loadStats() {
  try {
    const s = statsCache = await api("/api/stats?days=" + days);
    const total = Object.values(s.by_status).reduce((a, b) => a + b, 0);
    const kpi = (v, l, c = "") => `<div class="kpi ${c}"><b>${v}</b><span>${l}</span></div>`;
    $("#stats-kpi").innerHTML = kpi(total, "лидов") + kpi(s.by_status.deal || 0, "сделок", "good")
      + kpi(s.response.median_min == null ? "—" : Math.round(s.response.median_min) + " мин", "медиана до «беру»")
      + kpi(s.response.under5_pct == null ? "—" : s.response.under5_pct + "%", "взяты за 5 минут", s.response.under5_pct >= 50 ? "good" : "");
    const max = Math.max(1, ...s.per_day.map((d) => d.n));
    $("#chart-days").innerHTML = s.per_day.map((d) => `<div class="${d.deals ? "deal" : ""}" style="height:${(d.n / max) * 100}%" title="${d.day}: ${d.n}"></div>`).join("") || `<div class="empty">пока пусто</div>`;
    $("#chart-days").classList.toggle("few", s.per_day.length < 7);
    renderFunnel(s);
    const byUser = {}; s.team.forEach((t) => { (byUser[t.user_name] ||= {})[t.status] = t.n; });
    $("#team").innerHTML = Object.entries(byUser).map(([n, v]) => `<div class="row"><span>${esc(n)}</span><span class="n">взял ${v.taken || 0} · написал ${v.contacted || 0} · сделок ${v.deal || 0}</span></div>`).join("") || `<div class="empty">пока никто не брал лидов</div>`;
    $("#sources").innerHTML = s.sources.map((x) => { const [name, rest] = x.split(" — "); return `<div class="row"><span>${esc(name)}</span><span class="n">${esc(rest || "")}</span></div>`; }).join("");
  } catch (e) { toast(e.message); }
}

/* ---------- Контент-фабрика ---------- */
const C = { status: "" };
const KIND = { post: "Пост", carousel: "Карусель", reel: "Видео", magnet: "Лид-магнит" };
const CSTATUS = { draft: "черновик", scheduled: "в плане", published: "вышло", rejected: "удалено" };
$$("#c-seg button").forEach((b) => b.onclick = () => { $$("#c-seg button").forEach((x) => x.classList.toggle("on", x === b)); C.status = b.dataset.cstatus; haptic.sel(); loadContent(); });
$("#c-make").onclick = async () => {
  const topic = $("#c-topic").value.trim(); if (!topic) return toast("Напишите тему");
  $("#c-make").disabled = true; toast("Пишу…");
  try { await api("/api/content", { method: "POST", body: JSON.stringify({ kind: $("#c-kind").value, topic }) }); $("#c-topic").value = ""; haptic.ok(); loadContent(); }
  catch (e) { haptic.err(); toast(e.message); } finally { $("#c-make").disabled = false; }
};
async function cact(id, body) { try { const r = await api("/api/content/" + id, { method: "POST", body: JSON.stringify(body) }); haptic.ok(); if (r.errors?.length) toast(r.errors.join("; ")); else if (r.results) toast("Опубликовано"); loadContent(); } catch (e) { haptic.err(); toast(e.message); } }
function contentRow(it) {
  const when = it.scheduled_at && it.status === "scheduled" ? " · " + new Date(it.scheduled_at).toLocaleString("ru", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  const links = (it.published || []).map((p) => p.url ? `<a href="${esc(p.url)}" target="_blank">${esc(p.platform)}</a>` : esc(p.platform)).join(" ");
  const cover = it.images?.[0] ? `<img src="${esc(it.images[0])}" alt="">` : it.video ? `<span class="score">▶</span>` : `<span class="score">${KIND[it.kind]?.[0] || "?"}</span>`;
  const act = it.status === "published" ? "" : `<div class="c-actions">
      <button data-a="publish">${it.status === "scheduled" ? "Сейчас" : "Опубликовать"}</button>
      ${it.status === "scheduled" ? `<button data-a="draft">Снять</button>` : `<button data-a="pick">Когда</button>`}
      <button data-a="reject" class="danger">Удалить</button>
      <input type="datetime-local" class="hidden c-when"></div>`;
  return `<div class="lead c-item" data-id="${it.id}"><div class="inner">${cover}<div class="body">
    <div class="title">${esc(it.title || it.topic)}</div>
    <div class="sub">${esc((it.text || "").replace(/<[^>]+>/g, "").slice(0, 160))}</div>
    <div class="meta"><span>${KIND[it.kind] || it.kind}</span><span class="dot">${CSTATUS[it.status] || it.status}${when}</span>${links ? `<span class="dot">${links}</span>` : ""}${it.slug ? `<span class="dot"><a href="/lp/${esc(it.slug)}" target="_blank">страница</a></span>` : ""}</div>
    ${act}</div></div></div>`;
}
async function loadContent() {
  try {
    const [{ items }, ch] = await Promise.all([api("/api/content?" + new URLSearchParams({ status: C.status })), api("/api/content/channels").catch(() => ({ channels: [] }))]);
    $("#c-channels").textContent = ch.channels.length ? "→ " + ch.channels.map((c) => c.label).join(", ") : "площадки: /channels в боте";
    const list = $("#content-list");
    list.innerHTML = items.map(contentRow).join("") || `<div class="empty">Пока пусто — напишите тему выше или /post в боте</div>`;
    $$(".c-item", list).forEach((el) => {
      const id = el.dataset.id;
      $$("[data-a]", el).forEach((b) => b.onclick = (ev) => {
        ev.stopPropagation(); haptic.tap();
        const a = b.dataset.a;
        if (a === "pick") { const w = $(".c-when", el); w.classList.toggle("hidden"); w.onchange = () => cact(id, { action: "schedule", at: w.value }); return; }
        if (a === "reject" && !confirm("Удалить?")) return;
        cact(id, { action: a });
      });
      $(".title", el).onclick = () => { const t = $(".sub", el); t.classList.toggle("full"); };
    });
  } catch (e) { $("#content-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ---------- Настройки ---------- */
let S = null, saveT;
function autosave(patch) { clearTimeout(saveT); saveT = setTimeout(async () => { try { S = await api("/api/settings", { method: "POST", body: JSON.stringify(patch) }); haptic.ok(); toast("Сохранено"); } catch (e) { haptic.err(); toast(e.message); } }, 500); }
$$(".stepper button").forEach((b) => b.onclick = () => { const v = Math.max(0, Math.min(10, +$("#s-threshold").textContent + +b.dataset.step)); $("#s-threshold").textContent = v; haptic.sel(); autosave({ threshold: v }); });
$("#s-paused").onchange = (e) => autosave({ paused: e.target.checked });
$("#s-geo").onchange = (e) => autosave({ geo: e.target.value });
$("#s-profile").onchange = (e) => autosave({ profile: e.target.value });
function tag(text, onDel, cls = "") { const el = document.createElement("span"); el.className = "tag " + cls; el.innerHTML = `<code>${esc(text)}</code><button aria-label="удалить">×</button>`; $("button", el).onclick = onDel; return el; }
function renderSettings(s) {
  S = s;
  $("#s-threshold").textContent = s.threshold; $("#s-paused").checked = s.paused; $("#s-geo").value = s.geo; $("#s-profile").value = s.profile;
  const ph = $("#s-phrases"); ph.replaceChildren(...s.phrases.map((p) => tag(p, async () => renderSettings(await api("/api/settings", { method: "POST", body: JSON.stringify({ phrases: s.phrases.filter((x) => x !== p) }) })))));
  const rl = $("#s-rules"); rl.replaceChildren(...s.rules.map((r) => tag(r.expr, async () => { await api("/api/rules/" + r.id, { method: "DELETE" }); loadSettings(); })));
  if (!s.rules.length) rl.innerHTML = `<span class="hint">Правил нет — лиды отбираются словарём и ИИ</span>`;
  const kw = $("#s-keywords"); kw.innerHTML = "";
  const groups = [["topic", "Тема", Object.keys(s.keywords.topic)], ["intent", "Намерение", s.keywords.intent], ["negative", "Стоп-слова", s.keywords.negative]];
  for (const [kind, label, words] of groups) {
    const g = document.createElement("div"); g.className = "kw-group"; g.innerHTML = `<div class="hint">${label}</div><div class="tags"></div>`;
    $(".tags", g).replaceChildren(...words.map((w) => tag(kind === "topic" ? `${w} · ${s.keywords.topic[w]}` : w, async () => { await api("/api/keywords/" + encodeURIComponent(w), { method: "DELETE" }); loadSettings(); }, kind === "topic" ? "cat" : "")));
    kw.appendChild(g);
  }
  const tp = $("#s-templates"); tp.className = "tpl";
  tp.innerHTML = Object.entries(SERVICE).map(([k, v]) => `<label>${v}<textarea data-sv="${k}" rows="2" placeholder="Здравствуйте! Увидел ваш запрос…">${esc(s.templates[k] || "")}</textarea></label>`).join("");
  $$("textarea", tp).forEach((t) => t.onchange = () => autosave({ templates: { [t.dataset.sv]: t.value.trim() } }));
  const ig = $("#s-ignored");
  ig.replaceChildren(...Object.entries(s.ignored.chat).map(([k, v]) => tag("💬 " + (v || k), async () => { await api("/api/ignored/" + encodeURIComponent(k), { method: "DELETE" }); loadSettings(); })),
    ...Object.keys(s.ignored.author).map((k) => tag("👤 " + k, async () => { await api("/api/ignored/" + encodeURIComponent(k), { method: "DELETE" }); loadSettings(); })));
  if (!ig.children.length) ig.innerHTML = `<span class="hint">пусто</span>`;
  $("#s-team").innerHTML = s.team.map((m) => `<div class="row"><span>${esc(m.name)}</span><span class="n">${m.user_id}</span></div>`).join("") || `<div class="empty">пока только вы</div>`;
}
async function loadSettings() { try { renderSettings(await api("/api/settings")); } catch (e) { toast(e.message); } }
$$("[data-add]").forEach((b) => b.onclick = async () => {
  try {
    if (b.dataset.add === "phrase") { const v = $("#s-phrase-new").value.trim(); if (!v) return; $("#s-phrase-new").value = ""; renderSettings(await api("/api/settings", { method: "POST", body: JSON.stringify({ phrases: [...S.phrases, v] }) })); }
    if (b.dataset.add === "rule") { const v = $("#s-rule-new").value.trim(); if (!v) return; await api("/api/rules", { method: "POST", body: JSON.stringify({ expr: v }) }); $("#s-rule-new").value = ""; loadSettings(); }
    if (b.dataset.add === "kw") { const w = $("#s-kw-word").value.trim(); if (!w) return; const kind = $("#s-kw-kind").value; await api("/api/keywords", { method: "POST", body: JSON.stringify({ kind, word: w, category: kind === "topic" ? "другое" : null }) }); $("#s-kw-word").value = ""; loadSettings(); }
    haptic.ok();
  } catch (e) { haptic.err(); toast(e.message); }
});

show("today");
