(async function () {
  const { esc, api } = Omixa;
  const $ = id => document.getElementById(id);
  const form = $('pForm'), pid = form.dataset.pid;
  const m = await Omixa.me();
  if (!m.user) { Omixa.goLogin(); return; }
  if (!m.user.is_pro) { $('pMsg').className = 'alert alert-info'; $('pMsg').innerHTML = 'Quality Profiles are available with Omixa Pro. <a href="/pricing">See pricing</a>'; form.classList.add('hidden'); return; }

  const TYPES = ['text', 'integer', 'number', 'date', 'email', 'boolean'];
  const list = v => String(v || '').split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  function row(container, html) { const d = document.createElement('div'); d.className = 'inline-row'; d.innerHTML = html + '<button type="button" class="btn btn-ghost btn-sm" aria-label="Remove">×</button>'; d.querySelector('button').addEventListener('click', () => d.remove()); container.appendChild(d); }
  const addType = (c = '', t = 'text') => row($('typeRows'), `<input class="tc grow-2 grow-2" placeholder="column" value="${esc(c)}"><select class="tt grow-1">${TYPES.map(x => `<option ${x === t ? 'selected' : ''}>${x}</option>`).join('')}</select>`);
  const addMiss = (c = '', p = '') => row($('missRows'), `<input class="mc grow-2" placeholder="column" value="${esc(c)}"><input class="mp grow-1" type="number" min="0" max="100" step="0.1" placeholder="%" value="${esc(p)}">`);
  const addAllow = (c = '', v = '') => row($('allowRows'), `<input class="ac grow-1" placeholder="column" value="${esc(c)}"><input class="av grow-2" placeholder="values, separated, by commas" value="${esc(v)}">`);
  $('addType').onclick = () => addType(); $('addMiss').onclick = () => addMiss(); $('addAllow').onclick = () => addAllow();

  if (pid) {
    $('pTitle').textContent = 'Edit Quality Profile';
    const r = await api('/api/profiles/' + pid);
    if (!r.ok) { $('pMsg').className = 'alert alert-error'; $('pMsg').textContent = 'Profile not found.'; form.classList.add('hidden'); return; }
    const p = r.json.profile;
    ['name', 'description', 'max_missing_pct', 'max_duplicate_pct', 'date_min', 'date_max', 'min_quality_score'].forEach(k => { if (p[k] != null) $('f_' + k).value = p[k]; });
    ['required_columns', 'required_fields', 'duplicate_key_columns', 'date_columns', 'email_columns'].forEach(k => $('f_' + k).value = (p[k] || []).join(', '));
    $('f_no_edge_whitespace').checked = !!p.no_edge_whitespace;
    Object.entries(p.expected_types || {}).forEach(([c, t]) => addType(c, t));
    Object.entries(p.column_missing_pct || {}).forEach(([c, v]) => addMiss(c, v));
    Object.entries(p.allowed_values || {}).forEach(([c, v]) => addAllow(c, v.join(', ')));
  } else { addType(); }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const body = {
      name: $('f_name').value, description: $('f_description').value,
      required_columns: list($('f_required_columns').value), required_fields: list($('f_required_fields').value),
      max_missing_pct: $('f_max_missing_pct').value, max_duplicate_pct: $('f_max_duplicate_pct').value,
      duplicate_key_columns: list($('f_duplicate_key_columns').value), date_columns: list($('f_date_columns').value),
      date_min: $('f_date_min').value || null, date_max: $('f_date_max').value || null,
      email_columns: list($('f_email_columns').value), no_edge_whitespace: $('f_no_edge_whitespace').checked,
      min_quality_score: $('f_min_quality_score').value,
      expected_types: {}, column_missing_pct: {}, allowed_values: {},
    };
    document.querySelectorAll('#typeRows > div').forEach(d => { const c = d.querySelector('.tc').value.trim(); if (c) body.expected_types[c] = d.querySelector('.tt').value; });
    document.querySelectorAll('#missRows > div').forEach(d => { const c = d.querySelector('.mc').value.trim(), p = d.querySelector('.mp').value; if (c && p !== '') body.column_missing_pct[c] = p; });
    document.querySelectorAll('#allowRows > div').forEach(d => { const c = d.querySelector('.ac').value.trim(), v = list(d.querySelector('.av').value); if (c && v.length) body.allowed_values[c] = v; });
    $('saveBtn').disabled = true;
    const r = await api(pid ? '/api/profiles/' + pid : '/api/profiles/', { method: pid ? 'PUT' : 'POST', body });
    if (r.ok) { window.location.href = '/dashboard'; return; }
    $('pMsg').className = 'alert alert-error'; $('pMsg').textContent = r.json.error || 'Could not save the profile.'; $('saveBtn').disabled = false; window.scrollTo(0, 0);
  });
})();
