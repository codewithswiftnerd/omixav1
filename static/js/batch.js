// Pro batch: choose files -> Omixa checks each one -> you decide what happens -> clean -> download.
// Every decision about plans, limits and status comes from the server; this file renders what
// /api/batches returns and sends the user's choices back unchanged.
(function () {
  const app = document.getElementById('batchApp');
  if (!app) return;
  const $ = id => document.getElementById(id), esc = Omixa.esc;
  const MB = 1024 * 1024, maxFiles = +app.dataset.maxFiles, maxFileMb = +app.dataset.maxFileMb;
  const ok = n => /\.(csv|xlsx|xls)$/i.test(n);
  let chosen = [], batchId = null, timer = null, view = null;
  const analyses = {};        // job_id -> full analysis (loaded when the user customizes a file)
  const perFile = {};         // job_id -> { skip, value_actions, resolutions, leave_as_is }

  const RULES = [
    ['column_names', 'Standardize column names', '"Customer Name" → customer_name', false],
    ['formatting', 'Fix formatting', 'Trim stray spaces', true],
    ['missing_token_normalization', 'Recognize blanks', '"N/A", "null", "-" count as missing', true],
    ['numeric_text_cleaning', 'Clean numbers', 'Strip $, commas and % from text', true],
    ['gender_standardization', 'Standardize gender', 'M / MALE → male', true],
    ['country_standardization', 'Standardize countries', 'NG / nigeria → Nigeria', true],
    ['boolean_standardization', 'Standardize yes/no', 'Y / 1 / True → true', true],
    ['currency_label_standardization', 'Standardize currency labels', 'naira, ₦ → NGN', true],
    ['case_standardization', 'Fix capitalisation', 'DAVID OBI → David Obi', true],
    ['categorical_standardization', 'Merge categories', '"Male" / "MALE" / "male"', true],
    ['email_cleaning', 'Clean emails', 'Trim and lowercase', true],
    ['phone_cleaning', 'Clean phone numbers', 'Remove spaces, brackets, hyphens', true],
    ['date_standardization', 'Standardize dates', 'Clear dates → YYYY-MM-DD', true],
    ['duplicates', 'Remove duplicates', 'Keep the first occurrence', true],
    ['missing_values', 'Fill gaps with estimates', 'Median / "Unknown". Estimates, so off unless you choose it.', false],
  ];
  const PRESETS = {
    recommended: RULES.filter(r => r[3]).map(r => r[0]),
    all: RULES.map(r => r[0]).filter(r => r !== 'column_names'),
    minimal: ['formatting', 'missing_token_normalization', 'numeric_text_cleaning', 'case_standardization', 'email_cleaning'],
    none: [],
  };

  // ---- build the static controls
  $('ruleGrid').innerHTML = RULES.filter(r => r[0] !== 'column_names').map(r =>
    `<label class="rule"><input type="checkbox" value="${r[0]}"${r[3] ? ' checked' : ''}><span><strong>${esc(r[1])}</strong><em>${esc(r[2])}</em></span></label>`).join('');
  $('presets').addEventListener('click', e => {
    const b = e.target.closest('[data-preset]'); if (!b) return;
    document.querySelectorAll('#presets .tab').forEach(t => t.classList.toggle('active', t === b));
    const on = new Set(PRESETS[b.dataset.preset]);
    document.querySelectorAll('#ruleGrid input').forEach(i => { i.checked = on.has(i.value); });
  });
  $('ruleGrid').addEventListener('change', () => document.querySelectorAll('#presets .tab').forEach(t => t.classList.remove('active')));
  $('optMissing').addEventListener('change', () => $('optFillWrap').classList.toggle('hidden', $('optMissing').value !== 'fill'));

  // ---- helpers
  function setStep(name) {
    const order = ['pick', 'review', 'clean', 'download'], idx = order.indexOf(name);
    document.querySelectorAll('#bSteps .step').forEach(el => {
      const i = order.indexOf(el.dataset.step);
      el.classList.toggle('done', i < idx); el.classList.toggle('active', i === idx);
    });
  }
  function stage(text, cls) { const s = $('bStage'); s.textContent = text || ''; s.className = 'stage' + (cls ? ' ' + cls : ''); }
  function show(which) { ['batchSetup', 'batchReview', 'batchProgress'].forEach(id => $(id).classList.toggle('hidden', id !== which)); window.scrollTo({ top: 0, behavior: 'smooth' }); }
  function size(b) { return b >= MB ? (b / MB).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB'; }
  const scoreCls = n => n >= 85 ? 'good' : n >= 60 ? 'warn' : 'bad';
  const FOLD_SEV = { critical: 0, warning: 1, info: 2 };

  // ---- 1. choose
  function renderChosen() {
    $('batchList').innerHTML = chosen.map(f => {
      const bad = !ok(f.name) ? 'Unsupported type' : f.size > maxFileMb * MB ? 'Too large' : '';
      return `<li><span>${esc(f.name)} · ${size(f.size)}</span>${bad ? `<strong>${bad}</strong>` : '<span>✓</span>'}</li>`;
    }).join('');
    $('startBatch').disabled = !chosen.some(f => ok(f.name));
  }
  function addFiles(list) {
    const have = new Set(chosen.map(f => f.name + f.size));
    Array.from(list).forEach(f => { if (!have.has(f.name + f.size)) chosen.push(f); });
    $('batchMsg').textContent = chosen.length > maxFiles ? 'Only the first ' + maxFiles + ' files were kept.' : '';
    chosen = chosen.slice(0, maxFiles); renderChosen();
  }
  $('drop').addEventListener('click', () => $('batchFiles').click());
  $('drop').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('batchFiles').click(); } });
  ['dragover', 'dragenter'].forEach(ev => $('drop').addEventListener(ev, e => { e.preventDefault(); $('drop').classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev => $('drop').addEventListener(ev, e => { e.preventDefault(); $('drop').classList.remove('drag'); }));
  $('drop').addEventListener('drop', e => addFiles(e.dataTransfer.files));
  $('batchFiles').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });

  async function postForm(fd) {
    try { const res = await fetch(Omixa.base + '/api/batches/', { method: 'POST', body: fd, credentials: 'include' }); return { res, json: await res.json() }; }
    catch (e) { return { res: null, json: {} }; }
  }
  $('startBatch').addEventListener('click', async () => {
    const fd = new FormData();
    chosen.filter(f => ok(f.name)).forEach(f => fd.append('files', f, f.name));
    fd.append('options', JSON.stringify({ has_header: $('optHeader').checked }));
    fd.append('start', 'false');
    $('startBatch').disabled = true; $('startBatch').setAttribute('aria-busy', 'true'); stage('Uploading your files …', 'active');
    const { res, json } = await postForm(fd);
    $('startBatch').removeAttribute('aria-busy');
    if (!res) { stage('Network error. Please try again.', 'error'); $('startBatch').disabled = false; return; }
    if (res.status === 402) { window.location.href = '/batch'; return; }
    if (res.status === 401) { Omixa.goLogin(); return; }
    if (!res.ok) { stage(json.error || 'Could not start.', 'error'); $('startBatch').disabled = false; return; }
    batchId = json.batch_id; view = json;
    const a = await Omixa.api('/api/batches/' + batchId + '/analyze', { method: 'POST', body: { has_header: $('optHeader').checked } });
    if (!a.ok) { stage(a.json.error || 'Could not check the files.', 'error'); return; }
    setStep('review'); poll();
  });

  // ---- 2. review
  function checking(b) {
    const n = b.files.filter(f => f.stage === 'ready' || f.stage === 'failed' || f.stage === 'cancelled').length;
    stage('Checking your files … ' + n + ' of ' + b.files.length, 'active');
  }
  function reviewCard(f) {
    const sk = (perFile[f.job_id] || {}).skip;
    let side = '', meta = '';
    if (f.stage === 'ready' && f.analysis) {
      const a = f.analysis, c = a.counts || {};
      side = `<div class="score-badge ${scoreCls(a.score)}">${esc(a.score)}</div>`;
      meta = `<span class="chip">${Number(a.rows).toLocaleString()} rows · ${esc(a.columns)} columns</span>`
        + (c.critical ? `<span class="chip bad">${c.critical} critical</span>` : '')
        + (c.warning ? `<span class="chip warn">${c.warning} to fix</span>` : '')
        + (!c.critical && !c.warning ? '<span class="chip ok">Looks clean</span>' : '')
        + (a.flagged ? `<span class="chip warn">${a.flagged} flagged value${a.flagged === 1 ? '' : 's'}</span>` : '')
        + (a.ambiguous ? `<span class="chip warn">${a.ambiguous} need your call</span>` : '');
    } else if (f.stage === 'failed') meta = `<span class="chip bad">${esc(f.error || 'Could not be checked, it will still be cleaned')}</span>`;
    else if (f.stage === 'cancelled') meta = '<span class="chip">Cancelled</span>';
    else side = '<div class="bspin" aria-label="Checking"></div>';
    const choices = perFile[f.job_id] && (perFile[f.job_id].value_actions || perFile[f.job_id].resolutions) ? '<span class="chip ok">Your choices saved</span>' : '';
    return `<li class="bcard${sk ? ' is-skipped' : ''}" data-reveal><div class="bcard-side">${side}</div>
      <div class="bcard-main"><strong>${esc(f.filename)}</strong><p class="bcard-meta">${sk ? '<span class="chip">Left out of this batch</span>' : meta + choices}</p></div>
      ${f.stage === 'ready' ? `<button type="button" class="btn btn-secondary btn-sm" data-custom="${esc(f.job_id)}">Customize</button>` : ''}</li>`;
  }
  function renderReview(b) {
    const ready = b.files.filter(f => f.stage === 'ready' && f.analysis);
    const flagged = ready.reduce((n, f) => n + (f.analysis.flagged || 0), 0);
    const avg = ready.length ? Math.round(ready.reduce((n, f) => n + (f.analysis.score || 0), 0) / ready.length) : null;
    $('bSummary').textContent = b.files.length + ' file' + (b.files.length === 1 ? '' : 's') + ' checked'
      + (avg !== null ? ' · average quality ' + avg : '') + (flagged ? ' · ' + flagged + ' flagged values you can review' : '');
    $('bCards').innerHTML = b.files.map(reviewCard).join('');
    const n = b.files.filter(f => !(perFile[f.job_id] || {}).skip && f.stage !== 'cancelled').length;
    $('goCount').textContent = n; $('goBatch').disabled = !n;
  }
  async function loadProfiles() {
    try {
      const m = await Omixa.me(); if (!m.user || !m.user.is_pro) return;
      const r = await Omixa.api('/api/profiles/'); if (!r.ok) return;
      (r.json.profiles || []).forEach(p => { const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; $('profileSelect').appendChild(o); });
      if ((r.json.profiles || []).length) $('profileWrap').classList.remove('hidden');
    } catch (e) { /* optional */ }
  }

  // ---- per-file dialog: flagged values + ambiguous choices + leave out
  let dlgJob = null, dlgItems = [], dlgChoices = {};
  const vkey = (c, v) => JSON.stringify([c, v]);
  $('bCards').addEventListener('click', async e => {
    const b = e.target.closest('[data-custom]'); if (!b) return;
    dlgJob = b.dataset.custom;
    if (!analyses[dlgJob]) {
      b.setAttribute('aria-busy', 'true');
      const r = await Omixa.api('/api/batches/' + batchId + '/files/' + dlgJob + '/analysis');
      b.removeAttribute('aria-busy');
      if (!r.ok) { stage(r.json.error || 'Could not load this file.', 'error'); return; }
      analyses[dlgJob] = r.json;
    }
    openDialog();
  });
  function openDialog() {
    const a = analyses[dlgJob], saved = perFile[dlgJob] || {};
    dlgItems = ((a.flagged_values || {}).items) || []; dlgChoices = {};
    const va = saved.value_actions || {};
    Object.entries(va).forEach(([col, o]) => ['clear', 'remove_rows'].forEach(k => (o[k] || []).forEach(v => { dlgChoices[vkey(col, v)] = k; })));
    $('fdTitle').textContent = a.filename;
    $('fdSub').textContent = 'Choices here apply to this file only. Nothing changes unless you choose; your original is never touched.';
    const amb = ((a.recommendations || {}).ambiguous) || [];
    let html = `<label class="rule"><input type="checkbox" id="fdSkip"${saved.skip ? ' checked' : ''}><span><strong>Leave this file out of the batch</strong></span></label>`;
    if (amb.length) {
      html += '<p class="group-label review">Needs your call</p>' + amb.slice().sort((x, y) => FOLD_SEV[x.severity] - FOLD_SEV[y.severity]).map(f => {
        const opts = f.resolution_options || [];
        const cur = (saved.resolutions || []).find(r => r.column === f.column && r.issue === f.issue);
        const sel = opts.length ? `<select class="resolve-select" data-column="${esc(f.column || '')}" data-issue="${esc(f.issue)}"${(saved.leave_as_is || []).includes(f.column) ? ' data-touched="1"' : ''}>
          <option value="skip">Leave as is (no change)</option>${opts.map(o => `<option value="${esc(o.id)}"${cur && cur.choice === o.id ? ' selected' : ''}>${esc(o.label)}</option>`).join('')}</select>` : '';
        return `<div class="rec-item"><span class="badge ${esc(f.severity)}">${esc(f.severity)}</span><div class="rec-item-body"><strong>${f.column ? esc(f.column) : 'Whole file'}</strong><p>${esc(f.detail)}</p>${sel}</div></div>`;
      }).join('');
    }
    if (dlgItems.length) html += '<p class="group-label review">Flagged values</p><div id="fdFlagged"></div>'
      + (a.flagged_values.truncated ? '<p class="hint">Very large list: only the first values are shown.</p>' : '');
    if (!amb.length && !dlgItems.length) html += '<p class="hint">Nothing in this file needs a decision.</p>';
    $('fdBody').innerHTML = html; renderFlagged();
    $('fileDialog').showModal ? $('fileDialog').showModal() : $('fileDialog').setAttribute('open', '');
  }
  function renderFlagged() {
    const box = $('fdFlagged'); if (!box) return;
    const cols = [...new Set(dlgItems.map(i => i.column))];
    box.innerHTML = cols.map(col => `<section class="rv-col"><h3>${esc(col)}<button type="button" class="link-btn" data-clearcol="${esc(col)}">Clear all in this column</button></h3>` +
      dlgItems.filter(i => i.column === col).map(it => {
        const k = vkey(it.column, it.value), cur = dlgChoices[k] || 'keep';
        const o = (v, l) => `<option value="${v}"${cur === v ? ' selected' : ''}>${l}</option>`;
        return `<div class="rv-item"><div class="rv-what"><mark class="rv-val">${esc(it.value)}</mark><span class="rv-meta">${it.count} cell${it.count === 1 ? '' : 's'} · row${it.rows.length === 1 ? '' : 's'} ${esc(it.rows.join(', '))}</span><span class="rv-why">${esc(it.reason)}</span></div>
          <select class="rv-choice" data-key="${esc(k)}">${o('keep', 'Keep as is')}${o('clear', 'Clear it (leave cell empty)')}${o('remove_rows', 'Delete the whole row')}</select></div>`;
      }).join('') + '</section>').join('');
  }
  $('fileDialog').addEventListener('change', e => {
    const s = e.target.closest('.rv-choice');
    if (s) { if (s.value === 'keep') delete dlgChoices[s.dataset.key]; else dlgChoices[s.dataset.key] = s.value; }
    if (e.target.classList.contains('resolve-select')) e.target.dataset.touched = '1';
  });
  $('fileDialog').addEventListener('click', e => {
    const c = e.target.closest('[data-clearcol]');
    if (c) { dlgItems.filter(i => i.column === c.dataset.clearcol).forEach(i => { dlgChoices[vkey(i.column, i.value)] = 'clear'; }); renderFlagged(); }
  });
  function closeDialog() { const d = $('fileDialog'); d.close ? d.close() : d.removeAttribute('open'); renderReview(view); }
  $('fdReset').addEventListener('click', () => { delete perFile[dlgJob]; closeDialog(); });
  $('fdDone').addEventListener('click', () => {
    const o = { skip: $('fdSkip').checked };
    const va = {};
    dlgItems.forEach(it => { const c = dlgChoices[vkey(it.column, it.value)]; if (c) { ((va[it.column] = va[it.column] || {})[c] = va[it.column][c] || []).push(it.value); } });
    if (Object.keys(va).length) o.value_actions = va;
    const res = [], las = [];
    document.querySelectorAll('#fdBody .resolve-select').forEach(s => {
      if (s.value !== 'skip') res.push({ column: s.dataset.column, issue: s.dataset.issue, choice: s.value });
      else if (s.dataset.touched === '1' && s.dataset.column) las.push(s.dataset.column);
    });
    if (res.length) o.resolutions = res;
    if (las.length) o.leave_as_is = las;
    if (o.skip || o.value_actions || o.resolutions || o.leave_as_is) perFile[dlgJob] = o; else delete perFile[dlgJob];
    closeDialog();
  });

  // ---- choices -> request body
  function batchOptions() {
    const o = {};
    if ($('optDate').value !== 'auto') o.date_order = $('optDate').value;
    if ($('optNumber').value !== 'auto') o.number_format = $('optNumber').value;
    if ($('optPhone').value) o.phone_country = $('optPhone').value;
    const list = s => s.split(',').map(x => x.trim()).filter(Boolean);
    if (list($('optMarkers').value).length) o.missing_markers = list($('optMarkers').value);
    if (list($('optDupKeys').value).length) o.duplicate_keys = list($('optDupKeys').value);
    const m = $('optMissing').value;
    if (m !== 'leave') {
      const rule = { action: m };
      if (m === 'fill') rule.value = $('optFill').value.trim();
      o.missing_values = { default: rule };
    }
    return o;
  }
  $('goBatch').addEventListener('click', async () => {
    if ($('optMissing').value === 'fill' && !$('optFill').value.trim()) { stage('Type the value to fill empty cells with, or choose another option.', 'error'); return; }
    const rules = Array.from(document.querySelectorAll('#ruleGrid input:checked')).map(i => i.value);
    if ($('optColumnNames').checked) rules.unshift('column_names');
    const base = batchOptions(), pf = {};
    Object.entries(perFile).forEach(([id, p]) => {
      if (p.skip) { pf[id] = { skip: true }; return; }
      const ov = {};
      if (p.value_actions) ov.options = Object.assign({}, base, { value_actions: p.value_actions });   // keeps the batch-wide choices too
      if (p.resolutions) ov.resolutions = p.resolutions;
      if (p.leave_as_is) ov.leave_as_is = p.leave_as_is;
      if (Object.keys(ov).length) pf[id] = ov;
    });
    const body = { rules, has_header: view.has_header, options: base, per_file: pf };
    if ($('profileSelect').value) body.profile_id = $('profileSelect').value;
    $('goBatch').setAttribute('aria-busy', 'true'); stage('');
    const r = await Omixa.api('/api/batches/' + batchId + '/start', { method: 'POST', body });
    $('goBatch').removeAttribute('aria-busy');
    if (!r.ok) { stage(r.json.error || 'Could not start cleaning.', 'error'); return; }
    view = r.json; setStep('clean'); show('batchProgress'); render(view); poll();
  });
  $('discardBtn').addEventListener('click', async () => { if (batchId) await Omixa.api('/api/batches/' + batchId + '/cancel', { method: 'POST', body: {} }); reset(); });

  // ---- 3 + 4. progress and downloads
  const LABEL = { queued: 'Waiting', processing: 'Cleaning…', completed: '✓', failed: '✕', cancelled: 'Cancelled', expired: 'Expired' };
  function render(b) {
    const c = b.counts, done = b.progress.done;
    $('batchHeadline').textContent = b.finished ? (c.completed + ' of ' + c.total + ' files cleaned') : 'Cleaning ' + c.total + ' files …';
    $('bBar').style.width = (c.total ? Math.round(done / c.total * 100) : 0) + '%';
    const parts = [];
    if (c.processing) parts.push(c.processing + ' processing');
    if (c.queued) parts.push(c.queued + ' waiting');
    if (c.failed) parts.push(c.failed + ' failed');
    if (c.cancelled) parts.push(c.cancelled + ' left out');
    if (c.skipped) parts.push(c.skipped + ' skipped');
    $('batchSub').textContent = parts.join(' · ');
    $('batchFiles2').innerHTML = b.files.map(f => {
      let side = '', meta = '';
      if (f.status === 'completed' && f.summary) {
        const s = f.summary;
        side = `<div class="score-badge small ${scoreCls(s.quality_after)}">${esc(s.quality_after)}</div>`;
        meta = `<span class="chip">quality ${esc(s.quality_before)} → ${esc(s.quality_after)}</span><span class="chip">${esc(s.rows_in)} → ${esc(s.rows_out)} rows</span> <a href="${esc(f.download_url)}">Download</a>`;
      } else if (f.status === 'failed') meta = `<span class="chip bad">${esc(f.error || 'Could not process file')}</span>`;
      else if (f.status === 'processing' || f.status === 'queued') side = '<div class="bspin"></div>', meta = `<span class="chip">${LABEL[f.status]}</span>`;
      else meta = `<span class="chip">${LABEL[f.status] || esc(f.status)}</span>`;
      return `<li class="bcard"><div class="bcard-side">${side}</div><div class="bcard-main"><strong>${esc(f.filename)}</strong><p class="bcard-meta">${meta}</p></div></li>`;
    }).join('');
    $('batchSkipped').innerHTML = (b.skipped || []).length ? 'Skipped before processing: ' + b.skipped.map(s => esc(s.filename) + ' (' + esc(s.reason) + ')').join('; ') : '';
    $('dlAll').classList.toggle('hidden', !b.download_all_url);
    if (b.download_all_url) $('dlAll').href = b.download_all_url;
    $('retryBtn').classList.toggle('hidden', !(b.finished && c.failed));
    $('cancelBtn').classList.toggle('hidden', b.finished);
    $('newBatch').classList.toggle('hidden', !b.finished);
    if (b.finished) { setStep('download'); stage(''); }
  }
  async function poll() {
    clearTimeout(timer);
    const r = await Omixa.api('/api/batches/' + batchId);
    if (!r.ok) { stage(r.json.error || 'Could not read batch status.', 'error'); return; }
    view = r.json;
    if (view.draft) {
      const waiting = view.files.some(f => f.stage === 'pending' || f.stage === 'analyzing');
      if (waiting) { checking(view); timer = setTimeout(poll, 1500); return; }
      stage(''); show('batchReview'); renderReview(view); loadProfiles(); return;
    }
    render(view);
    if (!view.finished) timer = setTimeout(poll, 2000);
  }
  $('cancelBtn').addEventListener('click', async () => { await Omixa.api('/api/batches/' + batchId + '/cancel', { method: 'POST', body: {} }); poll(); });
  $('retryBtn').addEventListener('click', async () => { await Omixa.api('/api/batches/' + batchId + '/retry', { method: 'POST', body: {} }); poll(); });
  function reset() {
    clearTimeout(timer); chosen = []; batchId = null; view = null;
    Object.keys(perFile).forEach(k => delete perFile[k]); Object.keys(analyses).forEach(k => delete analyses[k]);
    renderChosen(); stage(''); setStep('pick'); show('batchSetup');
  }
  $('newBatch').addEventListener('click', reset);
})();
