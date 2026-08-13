/* Loom dashboard.
 *
 * Two rules worth keeping if you edit this:
 *  1. Never interpolate server values into innerHTML. Hostnames and job logs
 *     come from the agents, so a compromised node could otherwise inject
 *     script into the dashboard of whoever is logged in. Everything below
 *     builds nodes with the DOM API or escapes through textContent.
 *  2. Every URL is relative, so the coordinator still works when it is mounted
 *     under a path prefix behind a reverse proxy.
 */

const CSRF = document.body.dataset.csrf;
const REFRESH_MS = 4000;

let knownNodes = [];

function pct(value) {
  return value == null ? null : Math.max(0, Math.min(100, value));
}

function fmtUptime(seconds) {
  if (seconds == null) return '';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `up ${d}d ${h}h`;
  if (h) return `up ${h}h ${m}m`;
  return `up ${m}m`;
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.method && opts.method !== 'GET') {
    opts.headers['X-Loom-CSRF'] = CSRF;
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // Session expired -- bounce to the login page rather than silently
    // rendering an empty dashboard that looks like "all nodes are gone".
    window.location.reload();
    throw new Error('unauthenticated');
  }
  return res;
}

/* ---------- nodes ---------- */

function statBar(label, value, detail) {
  const wrap = document.createDocumentFragment();

  const row = document.createElement('div');
  row.className = 'stat-row';
  const l = document.createElement('span');
  l.textContent = label;
  const v = document.createElement('span');
  v.textContent = value == null ? '—' : `${value.toFixed(0)}%${detail ? ' ' + detail : ''}`;
  row.append(l, v);

  const bar = document.createElement('div');
  bar.className = 'stat-bar';
  const fill = document.createElement('div');
  fill.className = 'stat-bar-fill';
  if (value != null && value >= 90) fill.classList.add('hot');
  else if (value != null && value >= 70) fill.classList.add('warm');
  fill.style.width = `${value == null ? 0 : value}%`;
  bar.append(fill);

  wrap.append(row, bar);
  return wrap;
}

function renderNode(n) {
  const card = document.createElement('div');
  card.className = 'node-card' + (n.online ? '' : ' is-offline');

  const name = document.createElement('div');
  name.className = 'name';
  const dot = document.createElement('span');
  dot.className = `dot ${n.online ? 'online' : 'offline'}`;
  name.append(dot, document.createTextNode(n.name));
  card.append(name);

  const meta = document.createElement('div');
  meta.className = 'meta';
  const bits = [n.platform || 'unknown'];
  if (n.hostname) bits.push(n.hostname);
  if (n.online && n.uptime_seconds != null) bits.push(fmtUptime(n.uptime_seconds));
  if (!n.online && n.last_seen_age != null) {
    bits.push(`last seen ${Math.round(n.last_seen_age / 60)}m ago`);
  }
  meta.textContent = bits.join(' · ');
  card.append(meta);

  const ramDetail = (n.ram_used_gb != null && n.ram_total_gb != null)
    ? `(${n.ram_used_gb.toFixed(1)}/${n.ram_total_gb.toFixed(1)} GB)` : '';
  card.append(statBar('CPU', pct(n.cpu_percent)));
  card.append(statBar('RAM', pct(n.ram_percent), ramDetail));
  card.append(statBar('Disk', pct(n.disk_percent)));

  const tags = document.createElement('div');
  tags.className = 'tags';
  if (n.persistent) {
    const t = document.createElement('span');
    t.className = 'tag persistent';
    t.textContent = 'persistent';
    tags.append(t);
  }
  if (n.running_jobs > 0) {
    const t = document.createElement('span');
    t.className = 'tag busy';
    t.textContent = `${n.running_jobs} running`;
    tags.append(t);
  }
  (n.tags || []).forEach((tag) => {
    const t = document.createElement('span');
    t.className = 'tag';
    t.textContent = tag;
    tags.append(t);
  });
  card.append(tags);

  return card;
}

async function fetchNodes() {
  const res = await api('api/dashboard/nodes');
  const nodes = await res.json();
  knownNodes = nodes;

  const container = document.getElementById('nodes');
  container.replaceChildren(...nodes.map(renderNode));
  if (!nodes.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No nodes registered yet — run scripts/gen_api_key.py --add-device.';
    container.replaceChildren(empty);
  }
  syncTargetOptions(nodes);
}

function syncTargetOptions(nodes) {
  const select = document.getElementById('job-target');
  const current = select.value;

  const options = [{ value: '', label: 'Any node (falls back to persistent)' }];
  const tags = new Set();
  nodes.forEach((n) => (n.tags || []).forEach((t) => tags.add(t)));
  [...tags].sort().forEach((t) => options.push({ value: `tag:${t}`, label: `Tag: ${t}` }));
  nodes.forEach((n) => options.push({
    value: `device:${n.id}`,
    label: `${n.name}${n.online ? '' : ' (offline)'}`,
  }));

  const signature = options.map((o) => o.value).join('|');
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;

  select.replaceChildren(...options.map((o) => {
    const el = document.createElement('option');
    el.value = o.value;
    el.textContent = o.label;
    return el;
  }));
  select.value = current;
}

/* ---------- jobs ---------- */

const TERMINAL = ['done', 'failed', 'cancelled', 'lost'];

function renderJobRow(j) {
  const tr = document.createElement('tr');

  const add = (text, className) => {
    const td = document.createElement('td');
    td.textContent = text;
    if (className) td.className = className;
    tr.append(td);
    return td;
  };

  add(j.id, 'num');
  const cmd = add(j.command, 'cmd');
  cmd.title = j.command;

  const status = document.createElement('td');
  const pill = document.createElement('span');
  pill.className = `pill ${j.status}`;
  pill.textContent = j.status;
  if (j.exit_code != null && j.exit_code !== 0) pill.title = `exit code ${j.exit_code}`;
  status.append(pill);
  tr.append(status);

  add(j.assigned_name || '—');
  add(j.created_at ? new Date(j.created_at).toLocaleString() : '');

  const actions = document.createElement('td');
  actions.className = 'actions';

  if (TERMINAL.includes(j.status)) {
    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = 'Log';
    btn.onclick = () => showLog(j.id);
    actions.append(btn);
  } else {
    const btn = document.createElement('button');
    btn.className = 'ghost danger';
    btn.textContent = 'Cancel';
    btn.onclick = () => cancelJob(j.id, btn);
    actions.append(btn);
  }
  tr.append(actions);

  return tr;
}

async function fetchJobs() {
  const res = await api('api/dashboard/jobs');
  const jobs = await res.json();
  const tbody = document.querySelector('#jobs-table tbody');
  tbody.replaceChildren(...jobs.map(renderJobRow));
}

async function cancelJob(id, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await api(`api/dashboard/jobs/${id}/cancel`, { method: 'POST' });
  } finally {
    refresh();
  }
}

async function showLog(id) {
  const modal = document.getElementById('log-modal');
  const body = document.getElementById('log-body');
  document.getElementById('log-title').textContent = `Job ${id}`;
  body.textContent = 'Loading…';
  modal.hidden = false;

  try {
    const res = await api(`api/dashboard/jobs/${id}`);
    const job = await res.json();
    const header = [
      `command:  ${job.command}`,
      `status:   ${job.status}${job.exit_code != null ? ` (exit ${job.exit_code})` : ''}`,
      `node:     ${job.assigned_name || '—'}`,
      `source:   ${job.source || 'dashboard'}`,
      `started:  ${job.started_at || '—'}`,
      `finished: ${job.finished_at || '—'}`,
      ''.padEnd(60, '─'),
      '',
    ].join('\n');
    body.textContent = header + (job.log || '(no output captured)');
  } catch (e) {
    body.textContent = `Could not load job ${id}: ${e.message}`;
  }
}

function closeLog() {
  document.getElementById('log-modal').hidden = true;
}

/* ---------- submit ---------- */

document.getElementById('job-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const commandEl = document.getElementById('job-command');
  const targetEl = document.getElementById('job-target');
  const errorEl = document.getElementById('job-error');
  errorEl.hidden = true;

  const payload = { command: commandEl.value.trim() };
  if (!payload.command) return;

  const target = targetEl.value;
  if (target.startsWith('tag:')) payload.target_tag = target.slice(4);
  else if (target.startsWith('device:')) payload.target_device_id = Number(target.slice(7));

  try {
    const res = await api('api/dashboard/jobs/submit', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      errorEl.textContent = err.error || `Submit failed (${res.status})`;
      errorEl.hidden = false;
      return;
    }
    commandEl.value = '';
    fetchJobs();
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  }
});

document.getElementById('log-close').addEventListener('click', closeLog);
document.getElementById('log-modal').addEventListener('click', (e) => {
  if (e.target.id === 'log-modal') closeLog();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeLog();
});

/* ---------- refresh loop ---------- */

function setConn(ok, message) {
  const el = document.getElementById('conn');
  el.textContent = message;
  el.className = `conn ${ok ? 'ok' : 'bad'}`;
}

async function refresh() {
  try {
    await Promise.all([fetchNodes(), fetchJobs()]);
    setConn(true, 'live');
  } catch (e) {
    if (e.message !== 'unauthenticated') setConn(false, 'coordinator unreachable');
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
