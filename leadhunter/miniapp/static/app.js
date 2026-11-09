const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const initData = tg?.initData || new URLSearchParams(location.search).get("initData") || "";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ICONS = { telegram: "✈️", threads: "🧵", instagram: "📸", yandex_maps: "🗺", site_audit: "🩺", "hh.ru": "💼", vk: "🔵", rss: "📋", web: "🌐", kwork: "🟢", weblancer: "🟣", "freelance.ru": "🟠" };
const STATUS = { new: "новый", taken: "в работе", contacted: "написали", replied: "ответил", deal: "🤝 сделка", lost: "отказ", spam: "не лид", hot: "🔥 горячий" };

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Init-Data": initData, ...(opts.headers || {}) } });
  if (!r.ok) { const e = await r.json().catch(() => ({ error: r.statusText })); throw new Error(e.error || r.statusText); }
  return r.json();
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); setTimeout(() => t.classList.add("hidden"), 2200); }
function haptic(type = "light") { tg?.HapticFeedback?.impactOccurred(type); }

// ---------- tabs ----------
document.querySelectorAll("nav button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "stats") loadStats();
  if (b.dataset.tab === "settings") loadSettings();
});

// ---------- leads ----------
const state = { status: "open", service: "", q: "", mine: false, offset: 0 };
function chips(sel, key) {
  document.querySelectorAll(sel + " button").forEach((b) => b.onclick = () => {
    document.querySelectorAll(sel + " button").forEach((x) => x.classList.toggle("on", x === b));
    state[key] = b.dataset[key]; state.offset = 0; loadLeads();
  });
}
chips("#status-chips", "status"); chips("#service-chips", "service");
let qTimer; $("#q").oninput = (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = e.target.value; state.offset = 0; loadLeads(); }, 300); };
$("#mine").onchange = (e) => { state.mine = e.target.checked; state.offset = 0; loadLeads(); };
$("#more").onclick = () => { state.offset += 50; loadLeads(true); };

function leadCard(l) {
  const hot = l.score >= 8 ? " hot" : "";
  const when = new Date(l.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `<div class="lead" data-id="${l.id}">
    <div class="head"><span>${ICONS[l.source.split(":")[0]] || "📌"} ${esc(l.source)} · #${l.id} · ${when}</span><span class="score${hot}">${l.score}/10</span></div>
    ${l.title ? `<div class="title">${esc(l.title)}</div>` : ""}
    ${l.summary ? `<div class="text">🤖 ${esc(l.summary)}</div>` : `<div class="text">${esc(l.text)}</div>`}
    <div class="meta">
      <span class="badge ${esc(l.status)}">${STATUS[l.status] || esc(l.status)}</span>
      ${l.service ? `<span>🎯 ${esc(l.service)}</span>` : ""}${l.budget ? `<span>💰 ${esc(l.budget)}</span>` : ""}
      ${l.author ? `<span>👤 ${esc(l.author)}</span>` : ""}${l.assigned_name ? `<span>🙋 ${esc(l.assigned_name)}</span>` : ""}
    </div></div>`;
}
async function loadLeads(append = false) {
  const p = new URLSearchParams({ status: state.status, service: state.service, q: state.q, mine: state.mine ? 1 : 0, offset: state.offset, limit: 50 });
  try {
    const { leads } = await api("/api/leads?" + p);
    const list = $("#lead-list");
    const html = leads.map(leadCard).join("") || (append ? "" : `<div class="empty">Лидов нет</div>`);
    append ? list.insertAdjacentHTML("beforeend", html) : (list.innerHTML = html);
    $("#more").style.display = leads.length < 50 ? "none" : "block";
    list.querySelectorAll(".lead").forEach((el) => el.onclick = () => openLead(+el.dataset.id));
  } catch (e) { $("#lead-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function openLead(id) {
  haptic();
  const { lead: l, events } = await api("/api/leads/" + id);
  const open = ["new", "taken", "contacted", "replied"].includes(l.status);
  const btn = (s, label, cls = "") => `<button data-s="${s}" class="${cls}">${label}</button>`;
  let actions = "";
  if (l.status === "new") actions += btn("taken", "👋 Беру", "primary") + btn("hot", "🔥 Горячий") + btn("spam", "🚫 Не лид");
  else if (open) actions += btn("contacted", "✅ Написал") + btn("replied", "💬 Ответил") + btn("deal", "🤝 Сделка", "primary") + btn("lost", "❌ Отказ");
  else actions += btn("taken", "↩️ Вернуть в работу");
  $("#modal-body").innerHTML = `
    <h2>#${l.id} · ${ICONS[l.source.split(":")[0]] || "📌"} ${esc(l.source)} · ${l.score}/10 ${l.urgency === "высокая" ? "🔴" : ""}</h2>
    <div class="meta"><span class="badge ${esc(l.status)}">${STATUS[l.status] || l.status}</span> ${l.service ? "🎯 " + esc(l.service) : ""} ${l.budget ? "💰 " + esc(l.budget) : ""} ${l.assigned_name ? "🙋 " + esc(l.assigned_name) : ""}</div>
    ${l.title ? `<p><b>${esc(l.title)}</b></p>` : ""}
    ${l.summary ? `<p>🤖 ${esc(l.summary)}</p>` : ""}
    <pre>${esc(l.text)}</pre>
    <p>${l.author ? "👤 " + esc(l.author) + "<br>" : ""}${l.contact ? "📞 " + esc(l.contact) + "<br>" : ""}${l.author_bio ? "ℹ️ " + esc(l.author_bio) + "<br>" : ""}${l.chat_title ? "💬 " + esc(l.chat_title) + "<br>" : ""}${l.rule ? "🎯 правило: " + esc(l.rule) : ""}</p>
    ${l.url ? `<p><a href="${esc(l.url)}" target="_blank" rel="noopener">Открыть источник ↗</a></p>` : ""}
    <div class="actions">${actions}</div>
    ${l.draft ? `<h3>✍️ Черновик ответа <button id="copy-draft">копировать</button></h3><pre id="draft">${esc(l.draft)}</pre>` : ""}
    <div class="events">${events.map((e) => `${new Date(e.at).toLocaleString("ru-RU")} — ${STATUS[e.status] || e.status}${e.user_name ? " · " + esc(e.user_name) : ""}`).join("<br>")}</div>`;
  $("#modal").classList.remove("hidden");
  $("#modal-body").querySelectorAll(".actions button").forEach((b) => b.onclick = async () => {
    try {
      await api(`/api/leads/${id}/status`, { method: "POST", body: JSON.stringify({ status: b.dataset.s }) });
      haptic("medium"); toast("Готово"); openLead(id); loadLeads();
    } catch (e) { toast(e.message); }
  });
  const cp = $("#copy-draft");
  if (cp) cp.onclick = () => { navigator.clipboard?.writeText($("#draft").textContent); toast("Скопировано"); };
  $("#modal-body").querySelectorAll("a").forEach((a) => a.onclick = (ev) => { if (tg?.openLink) { ev.preventDefault(); tg.openLink(a.href); } });
}
$("#modal-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

// ---------- stats ----------
let days = 30;
document.querySelectorAll("#days-chips button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#days-chips button").forEach((x) => x.classList.toggle("on", x === b)); days = +b.dataset.days; loadStats();
});
function table(el, rows, label) {
  if (!rows.length) { el.innerHTML = `<div class="empty">нет данных</div>`; return; }
  el.innerHTML = `<div class="tr th"><span>${label}</span><span title="найдено">🔎</span><span title="взяли">👋</span><span title="написали">✅</span><span title="ответил">💬</span><span title="сделка">🤝</span></div>` +
    rows.map((r) => `<div class="tr"><span title="${esc(r.k)}">${esc(r.k)}</span><span>${r.found}</span><span>${r.taken}</span><span>${r.contacted}</span><span>${r.replied}</span><b>${r.deal}</b></div>`).join("");
}
async function loadStats() {
  try {
    const s = await api("/api/stats?days=" + days);
    const total = Object.values(s.by_status).reduce((a, b) => a + b, 0);
    const kpi = (v, l) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`;
    $("#kpis").innerHTML = kpi(total, "лидов") + kpi(s.by_status.deal || 0, "сделок") +
      kpi(s.response.median_min == null ? "—" : s.response.median_min.toFixed(0) + " мин", "медиана до «взял»") +
      kpi(s.response.under5_pct == null ? "—" : s.response.under5_pct + "%", "взяты за 5 мин") +
      kpi(s.by_status.new || 0, "не разобрано") + kpi(s.by_status.spam || 0, "не лид");
    const max = Math.max(1, ...s.per_day.map((d) => d.n));
    $("#chart-days").innerHTML = s.per_day.map((d) => `<div style="height:${(d.n / max) * 100}%" title="${d.day}: ${d.n} лидов, ${d.deals} сделок"></div>`).join("") || `<div class="empty">нет данных</div>`;
    table($("#funnel-source"), s.funnel_source, "источник"); table($("#funnel-service"), s.funnel_service, "услуга");
    table($("#funnel-phrase"), s.funnel_phrase, "фраза"); table($("#funnel-chat"), s.funnel_chat, "чат");
    const byUser = {}; s.team.forEach((t) => { (byUser[t.user_name] ||= {})[t.status] = t.n; });
    $("#team").innerHTML = Object.entries(byUser).map(([n, v]) => `<div class="tr"><span>${esc(n)}</span><span>—</span><span>${v.taken || 0}</span><span>${v.contacted || 0}</span><span>${v.replied || 0}</span><b>${v.deal || 0}</b></div>`).join("") || `<div class="empty">пока никто ничего не брал</div>`;
    $("#sources").innerHTML = s.sources.map((x) => `<li>${esc(x)}</li>`).join("");
  } catch (e) { toast(e.message); }
}

// ---------- settings ----------
let settings;
function renderSettings(s) {
  settings = s;
  $("#s-threshold").value = s.threshold; $("#s-paused").checked = s.paused; $("#s-geo").value = s.geo; $("#s-profile").value = s.profile;
  $("#s-phrases").value = s.phrases.join("\n");
  $("#s-rules").innerHTML = s.rules.map((r) => `<li><code>${esc(r.expr)}</code><button data-id="${r.id}">✕</button></li>`).join("") || `<li class="hint">правил нет</li>`;
  $("#s-rules").querySelectorAll("button").forEach((b) => b.onclick = async () => { renderSettings({ ...s, rules: (await api("/api/rules/" + b.dataset.id, { method: "DELETE" })).rules }); });
  const kwHtml = (kind, words) => `<p class="hint">${{ topic: "тема", intent: "намерение", negative: "стоп-слова" }[kind]}</p>` +
    words.map((w) => `<span class="kw">${esc(w)}${kind === "topic" ? " <i>" + esc(s.keywords.topic[w]) + "</i>" : ""}<button data-w="${esc(w)}">✕</button></span>`).join("");
  $("#s-keywords").innerHTML = kwHtml("topic", Object.keys(s.keywords.topic)) + kwHtml("intent", s.keywords.intent) + kwHtml("negative", s.keywords.negative);
  $("#s-keywords").querySelectorAll("button").forEach((b) => b.onclick = async () => renderSettings(await api("/api/keywords/" + encodeURIComponent(b.dataset.w), { method: "DELETE" })));
  const services = ["сайт", "бот", "автоматизация", "мультик"];
  $("#s-templates").innerHTML = services.map((sv) => `<label>${sv}<textarea data-sv="${sv}" rows="2">${esc(s.templates[sv] || "")}</textarea></label>`).join("") + `<button id="s-templates-save">Сохранить шаблоны</button>`;
  $("#s-templates-save").onclick = async () => { const t = {}; $("#s-templates").querySelectorAll("textarea").forEach((x) => { if (x.value.trim()) t[x.dataset.sv] = x.value.trim(); }); renderSettings(await api("/api/settings", { method: "POST", body: JSON.stringify({ templates: t }) })); toast("Сохранено"); };
  const ig = [...Object.entries(s.ignored.chat).map(([k, v]) => ({ k, v: "💬 " + (v || k) })), ...Object.keys(s.ignored.author).map((k) => ({ k, v: "👤 " + k }))];
  $("#s-ignored").innerHTML = ig.map((x) => `<span class="kw">${esc(x.v)}<button data-k="${esc(x.k)}">✕</button></span>`).join("") || `<p class="hint">пусто</p>`;
  $("#s-ignored").querySelectorAll("button").forEach((b) => b.onclick = async () => renderSettings(await api("/api/ignored/" + encodeURIComponent(b.dataset.k), { method: "DELETE" })));
  $("#s-team").innerHTML = s.team.map((m) => `<li>${esc(m.name)} <span class="hint">${m.user_id}</span></li>`).join("") || `<li class="hint">нажмите любую кнопку в рабочей группе, чтобы попасть в команду</li>`;
}
async function loadSettings() { try { renderSettings(await api("/api/settings")); } catch (e) { toast(e.message); } }
$("#s-save").onclick = async () => {
  try { renderSettings(await api("/api/settings", { method: "POST", body: JSON.stringify({ threshold: +$("#s-threshold").value, paused: $("#s-paused").checked, geo: $("#s-geo").value, profile: $("#s-profile").value }) })); toast("Сохранено"); haptic("medium"); } catch (e) { toast(e.message); }
};
$("#s-phrases-save").onclick = async () => {
  try { renderSettings(await api("/api/settings", { method: "POST", body: JSON.stringify({ phrases: $("#s-phrases").value.split("\n").map((x) => x.trim()).filter(Boolean) }) })); toast("Фразы сохранены"); } catch (e) { toast(e.message); }
};
$("#s-rule-add").onclick = async () => {
  try { const { rules } = await api("/api/rules", { method: "POST", body: JSON.stringify({ expr: $("#s-rule").value }) }); $("#s-rule").value = ""; renderSettings({ ...settings, rules }); } catch (e) { toast(e.message); }
};
$("#s-kw-add").onclick = async () => {
  try { renderSettings(await api("/api/keywords", { method: "POST", body: JSON.stringify({ kind: $("#s-kw-kind").value, word: $("#s-kw-word").value, category: $("#s-kw-cat").value || null }) })); $("#s-kw-word").value = ""; } catch (e) { toast(e.message); }
};

loadLeads();
