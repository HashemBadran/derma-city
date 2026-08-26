'use strict';

const state = {
  boot: null,
  customers: [],
  totals: null,
  bandLabels: {},
  sort: { key: 'aged_total', dir: -1 },
  selected: null,
};

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

const fmt = (n) => money.format(n || 0);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// --------------------------------------------------------------- filters / query

function filterParams() {
  const p = new URLSearchParams();
  if (state.companyId) p.set('company_id', state.companyId);
  p.set('threshold', $('threshold').value || 270);
  p.set('scope', state.scope);
  p.set('scheme', state.scheme);
  const q = $('f-search').value.trim();
  if (q) p.set('q', q);
  if ($('f-status').value) p.set('status', $('f-status').value);
  if ($('f-band').value) p.set('band', $('f-band').value);
  if ($('f-term').value) p.set('term', $('f-term').value);
  if ($('f-area').value) p.set('area', $('f-area').value);
  if ($('f-agency').value) p.set('agency', $('f-agency').value);
  if ($('f-min').value) p.set('min', $('f-min').value);
  if ($('f-owner').value.trim()) p.set('owner', $('f-owner').value.trim());
  if ($('f-salesperson').value.trim()) p.set('salesperson', $('f-salesperson').value.trim());
  if ($('f-hide-credits').checked) p.set('hide_credits', '1');
  if ($('f-hide-settled').checked) p.set('hide_settled', '1');
  if ($('f-due').checked) p.set('due_only', '1');
  if ($('f-overdue').checked) p.set('overdue_only', '1');
  return p;
}

// Cool green through amber to red — position on the ladder, not arbitrary hues.
// Ramp by index rather than a fixed per-key map so any aging scheme (whatever
// its band keys are) gets a sensible gradient, not one flat color.
const COLOR_RAMP = ['#8bbf3f', '#d4b026', '#e39a22', '#e07a1f', '#d55b26', '#c43d2f', '#a82a33', '#7d1d2c'];
function bandColor(b, idx, total) {
  if (b === 'Not Due') return '#2f9e6b';
  const n = Math.max(total, 1);
  const pos = n <= 1 ? 1 : idx / (n - 1);
  const i = Math.min(COLOR_RAMP.length - 1, Math.max(0, Math.round(pos * (COLOR_RAMP.length - 1))));
  return COLOR_RAMP[i];
}

// --------------------------------------------------------------- data loading

async function bootstrap() {
  const r = await fetch('/api/bootstrap');
  state.boot = await r.json();

  document.title = `${state.boot.company} — Odoo`;
  $('company').textContent = state.boot.company;
  state.companyId = String(state.boot.company_id || '');
  renderCompanySwitch();
  $('threshold').value = state.boot.threshold;
  setScope(state.boot.scope || 'all', false);

  $('scheme-switch').innerHTML = (state.boot.schemes || [])
    .map((s) => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join('');
  setScheme(state.boot.scheme || 'standard', false);

  const sel = $('f-status');
  sel.innerHTML = '<option value="">All statuses</option>' +
    state.boot.statuses.map((s) => `<option value="${s.key}">${esc(s.label)}</option>`).join('');

  updateSubtitle();
}

function renderCompanySwitch() {
  const wrap = $('company-switch');
  const companies = state.boot.companies || [];
  // A single-company deployment has nothing to switch between — a "Both
  // companies" toggle next to the one company would just be redundant UI.
  if (companies.length <= 1) {
    wrap.innerHTML = '';
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  const options = [{ id: '', label: 'Both companies' }]
    .concat(companies.map((c) => ({ id: String(c.id), label: c.label })));
  wrap.innerHTML = options.map((o) => `
    <button type="button" class="seg ${o.id === state.companyId ? 'active' : ''}"
      data-company="${o.id}">${esc(o.label)}</button>`).join('');
  wrap.querySelectorAll('[data-company]').forEach((b) => {
    b.addEventListener('click', () => selectCompany(b.dataset.company));
  });
}

function selectCompany(id) {
  state.companyId = String(id || '');
  renderCompanySwitch();
  // Remembered server-side so both tabs, and every export, agree on the scope.
  fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ company_id: state.companyId }),
  }).then(() => {
    coll.data = null;
    load();
    if (!$('view-collections').classList.contains('hidden')) loadCollections();
  });
}

function setScope(scope, persist = true) {
  state.scope = scope;
  document.querySelectorAll('.seg[data-scope]').forEach((b) => {
    b.classList.toggle('active', b.dataset.scope === scope);
  });
  $('threshold-wrap').classList.toggle('hidden', scope !== 'aged');
  if (persist) {
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope }),
    }).then(load);
  }
}

function setScheme(scheme, persist = true) {
  state.scheme = scheme;
  $('scheme-switch').value = scheme;
  if (persist) {
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scheme }),
    }).then(load);
  }
}

function updateSubtitle() {
  const last = state.boot.last_sync;
  const t = $('threshold').value;
  const label = state.companyId
    ? (state.boot.companies.find((c) => String(c.id) === state.companyId) || {}).label
    : 'both companies';
  const parts = [label, state.scope === 'all'
    ? 'all open receivables'
    : `${t}+ days overdue`];
  if (last) {
    const when = last.synced_at.replace('T', ' ');
    parts.push(`synced ${when}`);
  } else {
    parts.push('not synced yet');
  }
  if (state.totals) parts.push(`aged as of ${state.totals.as_of}`);
  if (state.settled && state.settled.count && $('f-hide-settled').checked) {
    parts.push(`${state.settled.count} settled customers hidden`);
  }
  if (state.agency && state.agency.count) {
    const mode = $('f-agency').value;
    parts.push(`${state.agency.count} with the agency${
      mode === 'hide' ? ' (excluded)' : mode === 'only' ? ' (shown alone)' : ''}`);
  }
  $('subtitle').textContent = parts.join(' · ');
}

async function load() {
  const p = filterParams();
  const r = await fetch('/api/customers?' + p.toString());
  const data = await r.json();
  if (data.error) { banner(data.error, true); return; }

  state.customers = data.customers;
  state.totals = data.totals;
  state.grand = data.grand_totals;
  state.bandLabels = data.band_labels;
  state.attention = data.attention;
  state.statusSummary = data.status_summary;
  state.terms = data.terms;
  state.settled = data.settled;
  state.agency = data.agency;

  syncBandFilter(data.grand_totals.bands);
  syncTermFilter(data.terms);
  syncAreaFilter(data.areas);
  syncSalespersonList(data.salespeople);
  $('btn-export').href = '/api/export.xlsx?' + p.toString();

  renderKpis();
  renderAgingStrip();
  renderAttention();
  renderTable();
  updateSubtitle();
}

function syncAreaFilter(areas) {
  if (!areas) return;
  const sel = $('f-area');
  const current = sel.value;
  const wanted = areas.map((a) => a.area).join('|');
  if (sel.dataset.areas === wanted) return;
  sel.dataset.areas = wanted;
  sel.innerHTML = '<option value="">All areas</option>' + areas.map((a) =>
    `<option value="${esc(a.area)}">${esc(a.area)} (${a.count})</option>`).join('');
  if (areas.some((a) => a.area === current)) sel.value = current;
}

function syncSalespersonList(names) {
  if (!names) return;
  const dl = $('f-salesperson-list');
  const wanted = names.join('|');
  if (dl.dataset.names === wanted) return;
  dl.dataset.names = wanted;
  dl.innerHTML = names.map((n) => `<option value="${esc(n)}">`).join('');
}

function syncTermFilter(terms) {
  const sel = $('f-term');
  const current = sel.value;
  const wanted = terms.map((t) => t.term).join('|');
  if (sel.dataset.terms === wanted) return;
  sel.dataset.terms = wanted;
  sel.innerHTML = '<option value="">All credit terms</option>' +
    terms.map((t) => `<option value="${esc(t.term)}">${esc(t.term)} (${t.count})</option>`).join('');
  if (terms.some((t) => t.term === current)) sel.value = current;
}

function syncBandFilter(bands) {
  const sel = $('f-band');
  const current = sel.value;
  const wanted = ['', ...bands].join('|');
  if (sel.dataset.bands === wanted) return;
  sel.dataset.bands = wanted;
  sel.innerHTML = '<option value="">All age bands</option>' +
    bands.map((b) => `<option value="${b}">${esc(state.bandLabels[b] || b)} (${b}d)</option>`).join('');
  if (bands.includes(current)) sel.value = current;
}

// --------------------------------------------------------------- rendering

function renderKpis() {
  const t = state.totals;
  const g = state.grand;
  const all = state.scope === 'all';
  const filtered = t.customers !== g.customers || Math.abs(t.aged_total - g.aged_total) > 0.01;
  const overdueShare = t.aged_total ? Math.round((t.overdue_total / t.aged_total) * 100) : 0;

  const tiles = [`
    <div class="kpi primary">
      <div class="label">${all ? 'Total Receivable' : `Overdue ${t.threshold}+ Days`}</div>
      <div class="value ${t.aged_total < 0 ? 'neg' : ''}">${fmt(t.aged_total)}</div>
      <div class="meta">${state.boot.currency}${filtered ? ` · ${fmt(g.aged_total)} unfiltered` : ''}</div>
    </div>`, `
    <div class="kpi">
      <div class="label">Customers</div>
      <div class="value">${t.customers}</div>
      <div class="meta">${t.documents} documents${filtered ? ` · of ${g.customers}` : ''}</div>
    </div>`];

  if (all) {
    tiles.push(`
      <div class="kpi">
        <div class="label">Within Terms</div>
        <div class="value">${compact.format(t.not_due_total)}</div>
        <div class="meta">not yet due</div>
      </div>`, `
      <div class="kpi">
        <div class="label">Overdue</div>
        <div class="value ${t.overdue_total > 0 ? 'neg' : ''}">${compact.format(t.overdue_total)}</div>
        <div class="meta">${overdueShare}% of the book</div>
      </div>`);
  } else {
    tiles.push(`
      <div class="kpi">
        <div class="label">Total Open</div>
        <div class="value">${compact.format(t.total_open)}</div>
        <div class="meta">these customers, all ages</div>
      </div>`);
  }

  const promised = state.statusSummary.promised || { count: 0, amount: 0 };
  tiles.push(`
    <div class="kpi">
      <div class="label">Promised to Pay</div>
      <div class="value">${compact.format(promised.amount)}</div>
      <div class="meta">${promised.count} customers</div>
    </div>`);

  $('kpis').innerHTML = tiles.join('');
}

function renderAgingStrip() {
  const t = state.totals;
  const bands = state.grand.bands;
  // Widths come from magnitude, so credit balances still occupy visible space.
  const magnitudes = t.band_totals.map((v) => Math.abs(v));
  const span = magnitudes.reduce((a, b) => a + b, 0);
  if (!span) { $('aging-strip').innerHTML = ''; return; }

  const active = $('f-band').value;
  const notDueOffset = bands[0] === 'Not Due' ? 1 : 0;
  const overdueCount = bands.length - notDueOffset;
  const segments = bands.map((b, i) => {
    const pct = (magnitudes[i] / span) * 100;
    if (pct <= 0) return '';
    const amount = t.band_totals[i];
    return `<span style="width:${pct}%;background:${bandColor(b, i - notDueOffset, overdueCount)};opacity:${
      active && active !== b ? .3 : 1}" title="${esc(state.bandLabels[b] || b)}: ${fmt(amount)}"
      data-band="${esc(b)}"></span>`;
  }).join('');

  const legend = bands.map((b, i) => {
    const amount = t.band_totals[i];
    if (!amount) return '';
    const pct = t.aged_total ? Math.round((amount / t.aged_total) * 100) : 0;
    return `<span class="item ${active === b ? 'active' : ''}" data-band="${esc(b)}">
      <i class="swatch" style="background:${bandColor(b, i - notDueOffset, overdueCount)}"></i>
      ${esc(state.bandLabels[b] || b)}
      <b class="amt ${amount < 0 ? 'neg' : ''}">${compact.format(amount)}</b>
      <em style="font-style:normal;opacity:.6">${pct}%</em>
    </span>`;
  }).join('');

  $('aging-strip').innerHTML = `
    <div class="head">
      <h3>Aging distribution</h3>
      <div class="split">${state.scope === 'all'
        ? `<b>${fmt(t.not_due_total)}</b> within terms · <b>${fmt(t.overdue_total)}</b> overdue`
        : `<b>${fmt(t.aged_total)}</b> across ${t.documents} documents`}</div>
    </div>
    <div class="bar">${segments}</div>
    <div class="bar-legend">${legend}</div>`;

  // Clicking a band anywhere in the strip filters the table to it.
  $('aging-strip').querySelectorAll('[data-band]').forEach((el) => {
    el.addEventListener('click', () => {
      const b = el.dataset.band;
      $('f-band').value = $('f-band').value === b ? '' : b;
      load();
    });
  });
}

function renderAttention() {
  const { broken_promises: broken, due_actions: due } = state.attention;
  if (!broken.length && !due.length) { $('attention').classList.add('hidden'); return; }

  const list = (items, cls) => {
    const shown = items.slice(0, 5);
    return shown.map((i) => `
      <li data-pid="${i.partner_id}">
        <span class="who" dir="auto">${esc(i.name)}</span>
        <span class="when">${i.date} · ${compact.format(i.amount)}</span>
      </li>`).join('') +
      (items.length > 5 ? `<div class="more">+ ${items.length - 5} more</div>` : '');
  };

  let html = '';
  if (broken.length) {
    html += `<div class="alert danger"><h3>Broken promises (${broken.length})</h3>
      <ul>${list(broken)}</ul></div>`;
  }
  if (due.length) {
    html += `<div class="alert"><h3>Follow-ups due (${due.length})</h3>
      <ul>${list(due)}</ul></div>`;
  }
  $('attention').innerHTML = html;
  $('attention').classList.remove('hidden');
  $('attention').querySelectorAll('li[data-pid]').forEach((li) => {
    li.addEventListener('click', () => openDrawer(Number(li.dataset.pid)));
  });
}

const COLUMNS = () => {
  const bands = state.grand.bands;
  const all = state.scope === 'all';
  return [
    { key: '_rank', label: '#', cls: 'center', sortable: false },
    { key: 'name', label: 'Customer', cls: 'left' },
    { key: 'area', label: 'Area', cls: 'center' },
    { key: 'salesperson', label: 'Salesperson', cls: 'left' },
    { key: 'term_days', label: 'Terms', cls: 'center' },
    { key: 'aged_docs', label: 'Docs', cls: 'center' },
    { key: 'oldest_days', label: 'Oldest', cls: 'center' },
    ...bands.map((b, i) => ({ key: `b${i}`, label: b, band: i })),
    { key: 'aged_total', label: all ? 'Total Open' : 'Total Overdue' },
    { key: 'overdue_total', label: 'Overdue' },
    // What the customer actually owes across every document, at any age. In the
    // overdue view this is the column that catches an old invoice already
    // cancelled by a credit: the band shows a number, this shows zero.
    ...(all ? [] : [{ key: 'total_open', label: 'Total Balance' }]),
    { key: 'status', label: 'Status', cls: 'center' },
    { key: 'owner', label: 'Owner', cls: 'left' },
    { key: 'promise_date', label: 'Promise', cls: 'center' },
    { key: 'next_action_date', label: 'Next Action', cls: 'center' },
    { key: 'notes', label: 'Notes', cls: 'center' },
  ];
};

function agePill(days) {
  // Zero or negative means the oldest item has not reached its due date yet.
  if (days <= 0) {
    return `<span class="age-pill current">${days === 0 ? 'due today' : `${-days}d left`}</span>`;
  }
  return `<span class="age-pill ${days >= 546 ? 'hot' : ''}">${days}d</span>`;
}

function sortValue(c, key) {
  if (key.startsWith('b') && /^b\d+$/.test(key)) return c.buckets[Number(key.slice(1))];
  const v = c[key];
  return typeof v === 'string' ? v.toLowerCase() : (v ?? 0);
}

function renderTable() {
  const cols = COLUMNS();
  const { key, dir } = state.sort;

  $('thead').innerHTML = '<tr>' + cols.map((c) => {
    const arrow = c.key === key ? `<span class="arrow">${dir === 1 ? '▲' : '▼'}</span>` : '';
    const label = c.band !== undefined ? `${esc(c.label)}` : esc(c.label);
    return `<th class="${c.cls || ''}" data-key="${c.key}" ${c.sortable === false ? 'data-nosort="1"' : ''}>${label}${arrow}</th>`;
  }).join('') + '</tr>';

  $('thead').querySelectorAll('th:not([data-nosort])').forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.key;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : -1 };
      renderTable();
    });
  });

  const rows = [...state.customers].sort((a, b) => {
    const va = sortValue(a, key), vb = sortValue(b, key);
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return 0;
  });

  const today = state.boot.today;
  const statusLabel = Object.fromEntries(state.boot.statuses.map((s) => [s.key, s.label]));

  $('tbody').innerHTML = rows.map((c, i) => {
    const promiseLate = c.promise_date && c.promise_date < today && c.status === 'promised';
    const actionDue = c.next_action_date && c.next_action_date <= today;
    return `<tr data-pid="${c.partner_id}">
      <td class="center rank">${i + 1}</td>
      <td class="name ${c.over_limit ? 'over-limit' : ''}" dir="auto">${esc(c.name)}${
        !state.companyId && c.company ? `<span class="co-chip">${esc(c.company)}</span>` : ''}${
        c.settled ? '<span class="co-chip settled-chip" title="Owes nothing — an old invoice cancelled by an unapplied credit">settled</span>' : ''}${
        c.agency ? '<span class="co-chip agency-chip" title="Handed to a collection agency">agency</span>' : ''}</td>
      <td class="center term">${esc(c.area || '—')}</td>
      <td class="left" title="${c.salesperson_override ? `Overridden — Odoo: ${esc(c.salesperson_synced) || 'none'}` : ''}">${
        c.salesperson ? esc(c.salesperson) : '<span class="note-count">—</span>'}${
        c.salesperson_override ? ' <span class="note-count">(override)</span>' : ''}</td>
      <td class="center term">${c.term_days != null ? c.term_days + 'd'
        : (c.payment_term ? esc(c.payment_term) : '—')}</td>
      <td class="center">${c.aged_docs}</td>
      <td class="center">${agePill(c.oldest_days)}</td>
      ${c.buckets.map((v) => `<td class="${v < 0 ? 'neg' : ''}">${v ? fmt(v) : '—'}</td>`).join('')}
      <td class="total ${c.aged_total < 0 ? 'neg' : ''}">${fmt(c.aged_total)}</td>
      <td class="${c.overdue_total > 0 ? 'neg' : ''}">${c.overdue_total ? fmt(c.overdue_total) : '—'}</td>
      ${state.scope === 'all' ? '' : `<td class="${c.settled ? 'settled' : ''}">${
        c.settled ? '0.00' : fmt(c.total_open)}</td>`}
      <td class="center"><span class="chip ${esc(c.status)}">${esc(statusLabel[c.status] || c.status)}</span></td>
      <td class="left">${esc(c.owner) || '<span class="note-count">—</span>'}</td>
      <td class="center">${c.promise_date
        ? `<span class="${promiseLate ? 'chip overdue' : ''}">${c.promise_date}</span>` : '—'}</td>
      <td class="center">${c.next_action_date
        ? `<span class="${actionDue ? 'chip overdue' : ''}">${c.next_action_date}</span>` : '—'}</td>
      <td class="center note-count">${c.notes || '—'}</td>
    </tr>`;
  }).join('');

  $('tbody').querySelectorAll('tr[data-pid]').forEach((tr) => {
    tr.addEventListener('click', () => openDrawer(Number(tr.dataset.pid)));
  });

  const t = state.totals;
  $('tfoot').innerHTML = rows.length ? `<tr>
    <td class="left"></td>
    <td class="left">${t.customers} customers</td>
    <td></td>
    <td></td>
    <td></td>
    <td class="center">${t.documents}</td>
    <td></td>
    ${t.band_totals.map((v) => `<td class="${v < 0 ? 'neg' : ''}">${fmt(v)}</td>`).join('')}
    <td class="${t.aged_total < 0 ? 'neg' : ''}">${fmt(t.aged_total)}</td>
    <td class="${t.overdue_total > 0 ? 'neg' : ''}">${fmt(t.overdue_total)}</td>
    ${state.scope === 'all' ? '' : `<td>${fmt(t.total_open)}</td>`}
    <td colspan="5"></td>
  </tr>` : '';

  $('empty').classList.toggle('hidden', rows.length > 0);
}

// --------------------------------------------------------------- drawer

async function openDrawer(pid) {
  const r = await fetch(`/api/customers/${pid}?threshold=${$('threshold').value}`);
  const data = await r.json();
  if (data.error) { banner(data.error, true); return; }
  state.selected = data;
  renderDrawer();
  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  $('scrim').hidden = false;
  // Deep link, so a customer can be bookmarked or pasted to a colleague.
  if (location.hash !== `#c=${pid}`) history.replaceState(null, '', `#c=${pid}`);
}

function closeDrawer() {
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  $('scrim').hidden = true;
  state.selected = null;
  if (location.hash) history.replaceState(null, '', location.pathname);
}

function openFromHash() {
  const m = /^#c=(\d+)$/.exec(location.hash);
  if (m) openDrawer(Number(m[1]));
}

function renderDrawer() {
  const { customer: c, contact, notes, odoo_url: odooUrl } = state.selected;
  const statusOptions = state.boot.statuses.map((s) =>
    `<option value="${s.key}" ${s.key === c.status ? 'selected' : ''}>${esc(s.label)}</option>`).join('');

  const contactBits = [];
  if (contact.phone) contactBits.push(`<a href="tel:${esc(contact.phone)}">${esc(contact.phone)}</a>`);
  if (contact.mobile && contact.mobile !== contact.phone) contactBits.push(`<a href="tel:${esc(contact.mobile)}">${esc(contact.mobile)}</a>`);
  if (contact.email) contactBits.push(`<a href="mailto:${esc(contact.email)}">${esc(contact.email)}</a>`);
  if (contact.city) contactBits.push(esc(contact.city));
  if (contact.payment_term) contactBits.push(`Terms: ${esc(contact.payment_term)}`);
  if (contact.credit_limit) {
    contactBits.push(`<span class="${c.over_limit ? 'over-limit' : ''}">Limit: ${fmt(contact.credit_limit)}${
      c.over_limit ? ' — exceeded' : ''}</span>`);
  }
  contactBits.push(`<a href="${esc(odooUrl)}" target="_blank" rel="noopener">Open in Odoo ↗</a>`);

  $('drawer-body').innerHTML = `
    <div class="d-head">
      <h2 dir="auto">${esc(c.name)}</h2>
      <div class="d-meta">${contactBits.join('<span>·</span>')}</div>
    </div>

    <div class="d-stats">
      <div class="d-stat"><div class="l">Total Open</div><div class="v ${c.total_open < 0 ? 'neg' : ''}">${fmt(c.total_open)}</div></div>
      <div class="d-stat"><div class="l">Within Terms</div><div class="v">${fmt(c.not_due_total)}</div></div>
      <div class="d-stat"><div class="l">Overdue</div><div class="v ${c.overdue_total > 0 ? 'neg' : ''}">${fmt(c.overdue_total)}</div></div>
      <div class="d-stat"><div class="l">Oldest</div><div class="v">${
        c.oldest_days > 0 ? c.oldest_days + 'd' : 'not due'}</div></div>
      <div class="d-stat"><div class="l">Documents</div><div class="v">${c.aged_docs}</div></div>
    </div>

    <div class="card">
      <h3>Follow-up</h3>
      <div class="form-grid">
        <div class="field"><label for="d-status">Status</label>
          <select id="d-status">${statusOptions}</select></div>
        <div class="field"><label for="d-salesperson">Salesperson${
          c.salesperson_override ? ` <span class="note-count">(overridden — Odoo: ${
            esc(c.salesperson_synced) || 'none'})</span>` : ''}</label>
          <input id="d-salesperson" type="text" value="${esc(c.salesperson_override)}"
            placeholder="${esc(c.salesperson_synced) || 'Not set in Odoo'}"></div>
        <div class="field"><label for="d-owner">Owner</label>
          <input id="d-owner" type="text" value="${esc(c.owner)}" placeholder="Who is chasing this"></div>
        <div class="field"><label for="d-promise">Promised payment date</label>
          <input id="d-promise" type="date" value="${esc(c.promise_date)}"></div>
        <div class="field"><label for="d-amount">Promised amount</label>
          <input id="d-amount" type="number" step="0.01" value="${c.promise_amount || ''}"></div>
        <div class="field"><label for="d-next">Next action date</label>
          <input id="d-next" type="date" value="${esc(c.next_action_date)}"></div>
      </div>
      <div class="form-actions">
        <label class="check" style="margin-right:auto">
          <input type="checkbox" id="d-agency" ${c.agency ? 'checked' : ''}>
          With collection agency</label>
        <button class="btn" id="d-save">Save</button>
        <span class="saved" id="d-saved">Saved</span>
        ${c.updated_at ? `<span class="note-count" style="margin-left:auto">updated ${esc(c.updated_at.replace('T', ' '))}</span>` : ''}
      </div>
    </div>

    <div class="card">
      <h3>Add a note</h3>
      <div class="field">
        <textarea id="d-note" placeholder="Called the accounts office — asked for a payment plan…"></textarea>
      </div>
      <div class="form-actions">
        <button class="btn" id="d-add-note">Add note</button>
      </div>
      <ul class="timeline" id="d-notes">${renderNotes(notes)}</ul>
    </div>

    <div class="card">
      <h3>Open documents (${c.documents.length})</h3>
      <table class="docs">
        <thead><tr>
          <th class="left">Document</th><th class="left">Reference</th>
          <th>Due</th><th>Age</th><th>Original</th><th>Balance</th>
        </tr></thead>
        <tbody>${c.documents.map((d) => `
          <tr>
            <td class="left">${esc(d.doc)}</td>
            <td class="ref" dir="auto">${esc(d.ref)}</td>
            <td>${d.due_date}</td>
            <td>${agePill(d.days)}</td>
            <td>${fmt(d.original)}</td>
            <td class="${d.residual < 0 ? 'neg' : ''}">${fmt(d.residual)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  $('d-agency').addEventListener('change', async (ev) => {
    await fetch(`/api/agency/${c.partner_id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agency: ev.target.checked }),
    });
    await refreshDrawer();
    load();
  });
  // Saves on blur (native `change` behavior for a text input), same as the
  // agency checkbox above — no separate save button for a single field.
  // Clearing the field back to empty is how you revert to the Odoo value.
  $('d-salesperson').addEventListener('change', async (ev) => {
    await fetch(`/api/customers/${c.partner_id}/salesperson`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ salesperson: ev.target.value }),
    });
    await refreshDrawer();
    load();
  });
  $('d-save').addEventListener('click', saveFollowup);
  $('d-add-note').addEventListener('click', addNote);
  bindNoteDeletes();
}

function renderNotes(notes) {
  if (!notes.length) return '<li class="note-count">No notes yet.</li>';
  return notes.map((n) => `
    <li>
      <div class="body">
        <div class="who">${esc(n.author) || 'Someone'} <span class="when">· ${esc(n.created_at.replace('T', ' '))}</span></div>
        <div dir="auto">${esc(n.body)}</div>
      </div>
      <button class="del" data-note="${n.id}" title="Delete note">✕</button>
    </li>`).join('');
}

function bindNoteDeletes() {
  document.querySelectorAll('.del[data-note]').forEach((b) => {
    b.addEventListener('click', async () => {
      if (!confirm('Delete this note?')) return;
      await fetch(`/api/notes/${b.dataset.note}/delete`, { method: 'POST' });
      await refreshDrawer();
      load();
    });
  });
}

async function refreshDrawer() {
  if (!state.selected) return;
  const pid = state.selected.customer.partner_id;
  const r = await fetch(`/api/customers/${pid}?threshold=${$('threshold').value}`);
  state.selected = await r.json();
  renderDrawer();
}

async function saveFollowup() {
  const pid = state.selected.customer.partner_id;
  const btn = $('d-save');
  btn.disabled = true;
  await fetch(`/api/customers/${pid}/followup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      status: $('d-status').value,
      owner: $('d-owner').value,
      promise_date: $('d-promise').value,
      promise_amount: $('d-amount').value,
      next_action_date: $('d-next').value,
    }),
  });
  btn.disabled = false;
  $('d-saved').classList.add('show');
  setTimeout(() => $('d-saved').classList.remove('show'), 1600);
  await load();
  await refreshDrawer();
}

async function addNote() {
  const body = $('d-note').value.trim();
  if (!body) return;
  const pid = state.selected.customer.partner_id;
  await fetch(`/api/customers/${pid}/notes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body, author: $('d-owner').value || state.boot.owner || '' }),
  });
  await refreshDrawer();
  load();
}

// --------------------------------------------------------------- sync

function banner(msg, isError) {
  const el = $('sync-banner');
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
  el.classList.remove('hidden');
}

function hideBanner() { $('sync-banner').classList.add('hidden'); }

async function startSync() {
  $('btn-sync').disabled = true;
  banner('Syncing from Odoo… this can take a little while.');
  try {
    const r = await fetch('/api/sync', { method: 'POST' });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      banner(body.error || `Sync failed (${r.status})`, true);
      return;
    }
    banner('Sync complete.');
    setTimeout(hideBanner, 2600);
    await bootstrap();
    await load();
  } catch (err) {
    banner('Sync failed: ' + err.message, true);
  } finally {
    $('btn-sync').disabled = false;
  }
}

// --------------------------------------------------------------- wiring

async function saveThreshold() {
  const v = Number($('threshold').value) || 0;
  await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ threshold: v }),
  });
  load();
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  const prefersDark = matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = saved || (prefersDark ? 'dark' : 'light');
  $('btn-theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('theme', next);
  });
}

function initTableResize() {
  const el = $('table-scroll');
  const saved = Number(localStorage.getItem('tableHeight'));
  if (saved) el.style.height = `${saved}px`;
  // The drag handle itself is native (CSS `resize: vertical`) — this just
  // remembers whatever height the user drags it to, across reloads and tabs.
  new ResizeObserver(debounce(() => {
    localStorage.setItem('tableHeight', Math.round(el.getBoundingClientRect().height));
  }, 300)).observe(el);
}

function init() {
  initTheme();
  initTableResize();
  $('btn-sync').addEventListener('click', startSync);
  $('threshold').addEventListener('change', saveThreshold);
  $('scheme-switch').addEventListener('change', () => setScheme($('scheme-switch').value));
  $('drawer-close').addEventListener('click', closeDrawer);
  $('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  document.querySelectorAll('.seg').forEach((b) => {
    b.addEventListener('click', () => setScope(b.dataset.scope));
  });

  const reload = debounce(load, 220);
  $('f-search').addEventListener('input', reload);
  ['f-status', 'f-band', 'f-term', 'f-area', 'f-agency', 'f-min', 'f-owner',
   'f-salesperson', 'f-hide-credits', 'f-hide-settled', 'f-due', 'f-overdue'].forEach((id) => {
    $(id).addEventListener('input', reload);
  });
  $('f-reset').addEventListener('click', () => {
    ['f-search', 'f-min', 'f-owner', 'f-salesperson'].forEach((id) => { $(id).value = ''; });
    ['f-status', 'f-band', 'f-term', 'f-area', 'f-agency'].forEach((id) => { $(id).value = ''; });
    ['f-hide-credits', 'f-due', 'f-overdue'].forEach((id) => { $(id).checked = false; });
    $('f-hide-settled').checked = true;
    load();
  });

  window.addEventListener('hashchange', openFromHash);
  bootstrap().then(load).then(openFromHash)
    .catch((e) => banner('Failed to start: ' + e.message, true));
}

init();

// =================================================================== collections

const coll = { data: null, sort: { key: 'total', dir: -1 }, receiptsOffset: 0 };
const RECEIPTS_PAGE = 100;

function cFilters() {
  const p = new URLSearchParams();
  const put = (k, v) => { if (v) p.set(k, v); };
  put('date_from', $('c-from').value);
  put('date_to', $('c-to').value);
  put('q', $('c-q').value.trim());
  put('user_id', $('c-user').value);
  put('journal', $('c-journal').value);
  put('area', $('c-area').value);
  put('agency', $('c-agency').value);
  put('applied', $('c-applied').value);
  put('basis', $('c-basis').value);
  put('company_id', state.companyId);
  return p;
}

const cTip = {
  el: null,
  show(html, ev) {
    if (!this.el) {
      this.el = document.createElement('div');
      this.el.className = 'tooltip';
      document.body.appendChild(this.el);
    }
    this.el.innerHTML = html;
    this.el.hidden = false;
    const r = this.el.getBoundingClientRect();
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
    this.el.style.left = `${Math.max(8, x)}px`;
    this.el.style.top = `${Math.max(8, y)}px`;
  },
  hide() { if (this.el) this.el.hidden = true; },
};

/* Daily collection. One series, so one hue and no legend — the card title says
   what it is. Bars are anchored to a zero baseline; collections never go negative. */
function dailyChart(rows) {
  if (!rows.length) return '<p class="empty">No collections in this range.</p>';
  const W = 1000, H = 190, padL = 64, padR = 10, padT = 12, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const max = Math.max(...rows.map((r) => r.total), 1);
  const y = (v) => padT + plotH - (v / max) * plotH;
  const bw = plotW / rows.length;
  const barW = Math.max(1.5, Math.min(26, bw - 3));

  const grid = Array.from({ length: 5 }, (_, i) => {
    const v = (max * i) / 4;
    return `<line class="grid-line" x1="${padL}" x2="${W - padR}" y1="${y(v)}" y2="${y(v)}"/>
            <text class="axis-label" x="${padL - 8}" y="${y(v) + 3.5}"
              text-anchor="end">${compact.format(v)}</text>`;
  }).join('');

  const bars = rows.map((r, i) => {
    const cx = padL + bw * i + bw / 2;
    const top = y(r.total), h = Math.max(1.5, y(0) - top);
    const t = `<div class="t-label">${r.label}</div><b>${fmt(r.total)}</b><br>
      ${r.receipts} receipt(s) · ${r.customers} customer(s)`;
    return `<rect class="bar" x="${cx - barW / 2}" y="${top}" width="${barW}"
              height="${h}" rx="3"/>
            <rect class="bar-hit" x="${cx - bw / 2}" y="${padT}" width="${bw}"
              height="${plotH}" data-ctip='${t.replace(/'/g, '&#39;')}'/>`;
  }).join('');

  const every = Math.max(1, Math.ceil(rows.length / 12));
  const labels = rows.map((r, i) => (i % every ? '' :
    `<text class="axis-label" x="${padL + bw * i + bw / 2}" y="${H - 8}"
       text-anchor="middle">${r.label.slice(5)}</text>`)).join('');

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
            style="height:${H}px" role="img" aria-label="Collections by day">
            ${grid}${bars}${labels}</svg>`;
}

function cBarTable(rows, total, onClick) {
  if (!rows.length) return '<p class="empty">Nothing to show.</p>';
  const max = Math.max(...rows.map((r) => r.total), 1);
  return `<table class="bartable">${rows.map((r, i) => `
    <tr data-key="${esc(r.key)}" data-ctip='${
      `<div class="t-label">${esc(r.label)}</div><b>${fmt(r.total)}</b><br>${r.receipts} receipts · ${r.days} active day(s)`
        .replace(/'/g, '&#39;')}'>
      <td class="rank">${i + 1}</td>
      <td class="lbl">${esc(r.label)}</td>
      <td class="barcell"><div class="track">
        <div class="fill" style="width:${(r.total / max) * 100}%"></div></div></td>
      <td class="val">${fmt(r.total)}</td>
      <td class="pct">${total ? ((r.total / total) * 100).toFixed(1) : '0.0'}%</td>
    </tr>`).join('')}</table>`;
}

async function loadCollections() {
  coll.receiptsOffset = 0;
  const p = cFilters();
  $('c-export').href = '/api/collections/export.xlsx?' + p;
  const d = await (await fetch('/api/collections?' + p)).json();
  if (d.error) { banner(d.error, true); return; }
  coll.data = d;
  loadReceipts();

  const f = d.facets || {};
  if (f.salespeople && !$('c-user').dataset.filled) {
    $('c-user').innerHTML = '<option value="">All salespeople</option>' +
      f.salespeople.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
    $('c-user').dataset.filled = '1';
  }
  if (f.areas && !$('c-area').dataset.filled) {
    $('c-area').innerHTML = '<option value="">All areas</option>' +
      f.areas.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
    $('c-area').dataset.filled = '1';
  }
  if (f.journals && !$('c-journal').dataset.filled) {
    $('c-journal').innerHTML = '<option value="">All bank / cash</option>' +
      f.journals.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
    $('c-journal').dataset.filled = '1';
  }
  if (f.range && !$('c-from').min) {
    $('c-from').min = $('c-to').min = f.range.first_date || '';
    // Bounded by today, not by the last synced date. Capping at the data's end
    // makes today unpickable whenever the cache is behind -- which is exactly
    // when someone needs to look at today and find out it is empty.
    $('c-from').max = $('c-to').max = state.boot.today;
  }

  const t = d.totals;
  // Say which days the figure covers. A total with no date beside it is the
  // thing that turned a one-day lag into an apparent mismatch with Odoo.
  const covered = cRangeLabel(f);
  const nothing = !t.receipts;
  $('c-kpis').innerHTML = [
    ['primary', 'Collected', fmt(t.total),
     nothing
       ? `${covered} · nothing received${cStaleNote(f)}`
       : `${state.boot.currency} · ${t.receipts} receipts · ${covered}${
         $('c-basis').value ? ' · filtered' : ''}${cStaleNote(f)}`],
    ['', 'Per Active Day', compact.format(t.per_day), `${t.days} days with collection`],
    ['', 'Applied to Invoices', compact.format(t.applied),
     `${t.total ? ((t.applied / t.total) * 100).toFixed(1) : 0}% traced to a salesperson`],
    ['', 'Unapplied', compact.format(t.unapplied), 'received, not yet matched'],
    ['', 'vs Opening Balances', compact.format(t.opening),
     t.opening ? 'settling migrated 2025 balances' : 'none in this view'],
    ['', 'Customers Paying', String(t.customers), `${t.salespeople} salespeople credited`],
  ].map(([cls, label, value, meta]) => `
    <div class="kpi ${cls}">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="meta">${meta}</div>
    </div>`).join('');

  $('c-body').innerHTML = `
    <div class="card">
      <h3>Collected per day</h3>
      <div class="card-sub">${state.boot.currency} · ${t.first_date || ''} → ${t.last_date || ''}</div>
      ${dailyChart(d.daily)}
    </div>
    <div class="cards2">
      <div class="card">
        <h3>By salesperson</h3>
        <div class="card-sub">credited from the invoices each receipt settles${
          t.opening > 0
            ? ` — ${fmt(t.opening)} of this settles pre-2026 balances carried over from the old
               system and is credited to whoever posted the migration. Switch to
               <b>Only settling 2026 invoices</b> for collection each salesperson actually earned.`
            : ''}</div>
        ${cBarTable(d.by_salesperson, t.total)}
      </div>
      <div class="card">
        <h3>By customer</h3>
        <div class="card-sub">top 25 payers</div>
        ${cBarTable(d.by_customer, t.total)}
      </div>
    </div>
    <div class="cards2">
      <div class="card">
        <h3>By area</h3>
        <div class="card-sub">the customer's sales region in Odoo</div>
        ${cBarTable(d.by_area || [], t.total)}
      </div>
      <div class="card">
        <h3>By bank / cash account</h3>
        ${cBarTable(d.by_journal, t.total)}
      </div>
    </div>`;

  document.querySelectorAll('[data-ctip]').forEach((el) => {
    el.addEventListener('mousemove', (ev) => cTip.show(el.dataset.ctip, ev));
    el.addEventListener('mouseleave', () => cTip.hide());
  });
  // Clicking a salesperson filters the whole view to them, including the
  // receipts list below — that's the point: see exactly what they collected.
  $('c-body').querySelectorAll('.card').forEach((card) => {
    if (!card.querySelector('h3').textContent.includes('salesperson')) return;
    card.querySelectorAll('tr[data-key]').forEach((tr) => {
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', () => {
        $('c-user').value = $('c-user').value === tr.dataset.key ? '' : tr.dataset.key;
        loadCollections();
        $('c-receipts-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  });
}

async function loadReceipts() {
  const p = cFilters();
  p.set('limit', RECEIPTS_PAGE);
  p.set('offset', coll.receiptsOffset);
  const d = await (await fetch('/api/collections/receipts?' + p)).json();
  coll.receiptsData = d;
  renderReceipts();
}

function renderReceipts() {
  const d = coll.receiptsData;
  if (!d) return;
  const salesperson = $('c-user').value;
  $('c-receipts-title').textContent = salesperson ? `Receipts — ${salesperson}` : 'Receipts';
  $('c-receipts-clear').classList.toggle('hidden', !salesperson);

  $('c-receipts-body').innerHTML = d.rows.length ? d.rows.map((r) => `
    <tr>
      <td class="left">${esc(cDay(r.date))}</td>
      <td class="left">${esc(r.doc) || '—'}</td>
      <td class="left" dir="auto">${esc(r.customer)}</td>
      <td class="left">${esc(r.salesperson) || '—'}</td>
      <td class="left">${esc(r.area) || '—'}</td>
      <td class="left">${r.invoice ? esc(r.invoice)
        : '<span class="note-count">unapplied</span>'}</td>
      <td class="left">${esc(r.journal)}</td>
      <td>${fmt(r.amount)}</td>
    </tr>`).join('')
    : '<tr><td colspan="8" class="left"><span class="note-count">No receipts match these filters.</span></td></tr>';

  const from = d.total ? coll.receiptsOffset + 1 : 0;
  const to = Math.min(coll.receiptsOffset + RECEIPTS_PAGE, d.total);
  $('c-receipts-pager').innerHTML = `
    <button class="btn btn-ghost btn-sm" id="c-receipts-prev" ${coll.receiptsOffset === 0 ? 'disabled' : ''}>← Prev</button>
    <span>${from}–${to} of ${d.total}</span>
    <button class="btn btn-ghost btn-sm" id="c-receipts-next" ${to >= d.total ? 'disabled' : ''}>Next →</button>`;
  $('c-receipts-prev').addEventListener('click', () => {
    coll.receiptsOffset = Math.max(0, coll.receiptsOffset - RECEIPTS_PAGE);
    loadReceipts();
  });
  $('c-receipts-next').addEventListener('click', () => {
    coll.receiptsOffset += RECEIPTS_PAGE;
    loadReceipts();
  });
}

/** Short form of a date: "29 Jul", or with the year when it is not this one. */
function cDay(iso) {
  if (!iso) return '';
  const d = new Date(iso + 'T00:00:00');
  const opts = { day: 'numeric', month: 'short' };
  if (d.getFullYear() !== new Date(state.boot.today + 'T00:00:00').getFullYear()) {
    opts.year = 'numeric';
  }
  return d.toLocaleDateString(undefined, opts);
}

/** Describe the days the current figure covers, in the user's terms. */
function cRangeLabel() {
  const from = $('c-from').value;
  const to = $('c-to').value;
  if (!from && !to) return 'all time';
  if (from && from === to) {
    return from === state.boot.today ? `today, ${cDay(from)}` : cDay(from);
  }
  if (from && to) return `${cDay(from)} – ${cDay(to)}`;
  return from ? `from ${cDay(from)}` : `up to ${cDay(to)}`;
}

/**
 * Warn when the requested range runs past the data.
 *
 * The tile is now honest about the dates it covers, but "0.00 today" is
 * ambiguous on its own: it could mean nobody paid, or it could mean nothing has
 * been synced yet. Only the second one is worth acting on, so they have to read
 * differently.
 */
function cStaleNote(facets) {
  const synced = facets?.range?.last_date;
  const to = $('c-to').value || state.boot.today;
  if (!synced || synced >= to) return '';
  return ` · synced to ${cDay(synced)}, refresh for anything since`;
}

function cPreset(kind) {
  // Anchored to the real calendar date, not to the last date in the data.
  // Anchoring to the data made "Today" mean "the most recent day we happen to
  // have synced": with the cache a day behind, the tile showed yesterday's
  // receipts under a label saying today, and comparing it against Odoo looked
  // like a discrepancy in the figures rather than in the date. A quiet day now
  // reads 0.00, which is the truth and is visibly different from a stale tile.
  const today = state.boot.today;
  const end = new Date(today + 'T00:00:00');
  // Built from the local Y/M/D rather than toISOString, which converts to UTC
  // and lands on the previous day for anyone east of Greenwich.
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    + `-${String(d.getDate()).padStart(2, '0')}`;
  let from = '';
  if (kind === 'today') from = today;
  else if (kind === 'week') from = iso(new Date(end.getFullYear(), end.getMonth(), end.getDate() - 6));
  else if (kind === 'mtd') from = iso(new Date(end.getFullYear(), end.getMonth(), 1));
  else if (kind === 'ytd') from = iso(new Date(end.getFullYear(), 0, 1));
  $('c-from').value = from;
  $('c-to').value = kind === 'all' ? '' : today;
  document.querySelectorAll('#c-presets .seg').forEach((b) =>
    b.classList.toggle('active', b.dataset.range === kind));
}

function switchView(view) {
  document.querySelectorAll('.vtab').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === view));
  $('view-receivables').classList.toggle('hidden', view !== 'receivables');
  $('view-collections').classList.toggle('hidden', view !== 'collections');
  // Receivables-only controls. The company switch is deliberately NOT here — it
  // scopes both tabs, so it has to stay reachable on Collections too.
  $('btn-export').classList.toggle('hidden', view !== 'receivables');
  document.querySelector('.threshold').classList.toggle('hidden', view !== 'receivables');
  $('scope-switch').classList.toggle('hidden', view !== 'receivables');
  $('scheme-switch').classList.toggle('hidden', view !== 'receivables');
  if (view === 'collections' && !coll.data) loadCollections();
  if (location.hash.slice(1) !== view) history.replaceState(null, '', `#${view}`);
}

(function initCollections() {
  $('viewtabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('.vtab');
    if (b) switchView(b.dataset.view);
  });
  $('c-presets').addEventListener('click', (ev) => {
    const b = ev.target.closest('.seg');
    if (b) { cPreset(b.dataset.range); loadCollections(); }
  });
  const reload = debounce(loadCollections, 240);
  $('c-q').addEventListener('input', reload);
  ['c-user', 'c-journal', 'c-area', 'c-agency', 'c-applied', 'c-basis',
   'c-from', 'c-to'].forEach((id) =>
    $(id).addEventListener('change', () => {
      if (id.startsWith('c-from') || id.startsWith('c-to')) {
        document.querySelectorAll('#c-presets .seg').forEach((b) => b.classList.remove('active'));
      }
      loadCollections();
    }));
  $('c-receipts-clear').addEventListener('click', () => {
    $('c-user').value = '';
    loadCollections();
  });
  $('c-reset').addEventListener('click', () => {
    ['c-q', 'c-user', 'c-journal', 'c-area', 'c-agency', 'c-applied']
      .forEach((id) => { $(id).value = ''; });
    $('c-basis').value = '';
    cPreset('all');
    loadCollections();
  });
  if (location.hash === '#collections') {
    // bootstrap() has not run yet; defer until state.boot exists
    const wait = setInterval(() => {
      if (state.boot) { clearInterval(wait); switchView('collections'); }
    }, 60);
  }
})();
