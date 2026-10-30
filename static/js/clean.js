const $ = (id) => document.getElementById(id);
const drop = $('drop'), fileInput = $('fileInput'), fileNameEl = $('fileName');
const runBtn = $('runBtn'), downloadBtn = $('downloadBtn'), resetBtn = $('resetBtn');
const stage = $('stage'), results = $('results'), stats = $('stats'), detailBlocks = $('detailBlocks');
const reportCard = $('reportCard'), scoreBadge = $('scoreBadge'), scoreNum = $('scoreNum');
const reportSub = $('reportSub'), findingsEl = $('findings'), noFindings = $('noFindings');
const recommendationsCard = $('recommendationsCard');
const recSafeList = $('recSafeList'), recSafeEmpty = $('recSafeEmpty');
const recAmbiguousList = $('recAmbiguousList'), recAmbiguousEmpty = $('recAmbiguousEmpty');
const hasHeaderCheck = $('hasHeaderCheck');
const workCard = $('workCard'), uploadCard = $('uploadCard');
const changeFileBtn = $('changeFileBtn'), reportTitle = $('reportTitle');

// The home page is a single-screen tool: while a file is loaded the
// hero/upload card/marketing sections are hidden (body.has-file) and
// workCard moves through phases that CSS keys off:
//   idle -> working (upload + analysis) -> review -> done  (error -> review/idle)
function setPhase(phase){
  if(workCard) workCard.dataset.phase = phase;
}
function showWorkspace(on){
  document.body.classList.toggle('has-file', on);
  if(workCard) workCard.classList.toggle('hidden', !on);
  if(uploadCard) uploadCard.classList.toggle('hidden', on);
  if(on) window.scrollTo({ top: 0, behavior: 'smooth' });
}

function hasHeader(){
  return hasHeaderCheck ? hasHeaderCheck.checked : true;
}

// Mirrors the titles already in the static rule checklist below, so a
// recommendation card and its underlying checkbox always agree on
// what a rule is called.
const RULE_TITLES = {
  column_names: 'Standardize column names',
  formatting: 'Fix formatting',
  missing_token_normalization: 'Recognize blank/N-A style values',
  numeric_text_cleaning: 'Clean numeric text',
  gender_standardization: 'Standardize gender',
  country_standardization: 'Standardize countries',
  boolean_standardization: 'Standardize booleans',
  currency_label_standardization: 'Standardize currency labels',
  case_standardization: 'Fix capitalisation',
  categorical_standardization: 'Standardize categories',
  email_cleaning: 'Clean emails',
  phone_cleaning: 'Clean phone formatting',
  date_standardization: 'Standardize dates',
  missing_values: 'Fill gaps',
  duplicates: 'Remove duplicates',
};

function ruleCheckbox(ruleId){
  return document.querySelector(`.rule input[value="${CSS.escape(ruleId)}"]`);
}
const stepEls = {
  upload: document.querySelector('.step[data-step="upload"]'),
  review: document.querySelector('.step[data-step="review"]'),
  clean: document.querySelector('.step[data-step="clean"]'),
  download: document.querySelector('.step[data-step="download"]'),
};

let selectedFile = null;
let lastJobId = null;
let preCleanScore = null;

function base(){
  const configured = (window.OMIXA_API_BASE || '').trim().replace(/\/$/, '');
  return configured || window.location.origin;
}

// Auth is the browser's session cookie (sent automatically via
// credentials:'include' on every fetch below) — nothing to attach
// here. Kept as a passthrough so call sites don't need to change.
function apiHeaders(extra){
  return extra || {};
}

function setStep(name, state){
  // state: 'active' | 'done' | 'err' | '' (reset)
  Object.values(stepEls).forEach(el => el && el.classList.remove('active','done','err'));
  const order = ['upload','review','clean','download'];
  const idx = order.indexOf(name);
  order.forEach((key, i) => {
    const el = stepEls[key];
    if(!el) return;
    if(i < idx) el.classList.add('done');
    if(i === idx && state) el.classList.add(state);
  });
}

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); fileInput.click(); } });
['dragover','dragenter'].forEach(evt => drop.addEventListener(evt, e => { e.preventDefault(); drop.classList.add('drag'); }));
['dragleave','drop'].forEach(evt => drop.addEventListener(evt, e => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', e => { if(e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', e => { if(e.target.files[0]) setFile(e.target.files[0]); });
if(hasHeaderCheck){
  hasHeaderCheck.addEventListener('change', () => { if(selectedFile) setFile(selectedFile); });
}

// Queue mode: the API answers 202 and a dedicated worker does the heavy work, so the page polls
// the job instead of holding one long request open. Polling backs off (1s -> 5s) so thousands of
// open tabs stay cheap for the server, and obeys Retry-After if rate limited.
const sleep = ms => new Promise(res => setTimeout(res, ms));
async function pollJob(jobId, isDone){
  const started = Date.now();
  let delay = 1000;
  for(;;){
    const r = await fetch(base() + '/api/jobs/' + jobId, { credentials:'include', headers: apiHeaders() });
    if(r.status === 429){
      await sleep((parseInt(r.headers.get('Retry-After') || '3', 10) || 3) * 1000);
      continue;
    }
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || 'status check failed');
    if(j.status === 'failed') throw new Error(j.error || 'processing failed');
    if(isDone(j)) return j;
    if(j.status === 'queued'){
      setStage(j.queue_position ? 'Waiting in queue (position ' + j.queue_position + ') …' : 'Queued …', 'active');
    }
    if(Date.now() - started > 15 * 60 * 1000) throw new Error('This is taking longer than expected. Please try again.');
    await sleep(delay);
    delay = Math.min(Math.round(delay * 1.4), 5000);
  }
}

async function setFile(f){
  selectedFile = f;
  lastJobId = null;
  preCleanScore = null;
  fileNameEl.textContent = f.name + '  ·  ' + (f.size >= 1048576 ? (f.size/1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(f.size/1024)) + ' KB');
  showWorkspace(true);
  setPhase('working');
  runBtn.disabled = true;
  reportCard.classList.add('hidden');
  recommendationsCard.classList.add('hidden');
  results.classList.add('hidden');
  downloadBtn.classList.add('hidden'); resetBtn.classList.add('hidden');
  setStage('');

  try{
    setStep('upload', 'active');
    setStage('Uploading your file …', 'active');
    const form = new FormData();
    form.append('file', f);
    const upRes = await fetch(base() + '/api/upload/', { method:'POST', credentials:'include', headers: apiHeaders(), body: form });
    const upJson = await upRes.json();
    if(!upRes.ok) throw upgradeAwareError(upRes, upJson, 'upload failed');
    lastJobId = upJson.job_id;

    setStep('review', 'active');
    setStage('Checking your data …', 'active');
    const repRes = await fetch(base() + '/api/report/' + lastJobId + '?has_header=' + hasHeader(), { credentials:'include', headers: apiHeaders() });
    let repJson = await repRes.json();
    if(!repRes.ok) throw upgradeAwareError(repRes, repJson, 'analysis failed');
    if(repRes.status === 202){
      if(repJson.message) setStage(repJson.message, 'active');
      const wantHeader = hasHeader();
      const done = await pollJob(lastJobId, j => j.status === 'uploaded' && j.analysis && j.analysis.has_header === wantHeader);
      repJson = { report: done.analysis.report, recommendations: done.analysis.recommendations, validation: done.analysis.validation };
    }

    renderReport(repJson.report);
    renderRecommendations(repJson.recommendations);
    validationConfig = { column_types: {} };
    renderValidation(repJson.validation);
    prepareReview(repJson.validation);
    prepareMapping();
    setStage('');
    setPhase('review');
    runBtn.disabled = false;
  }catch(err){
    setStage(err.message, 'error');
    setStep(lastJobId ? 'review' : 'upload', 'err');
    setPhase('error');
  }
}

// Wraps a failed response's error message so a 402 (plan limit / file
// too large, see plans.py) also surfaces a link to upgrade, instead
// of just reading like any other error.
function upgradeAwareError(res, json, fallback){
  const msg = json.error || fallback;
  if(res.status === 402 && json.upgrade_required && window.OMIXA_UPGRADE_URL){
    return new Error(msg + ', upgrade at ' + window.OMIXA_UPGRADE_URL);
  }
  return new Error(msg);
}

// Every value that originates from the uploaded file (column names, sample
// values, free-text details) must be escaped before it is placed in innerHTML.
function escHtml(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}


// ---- score explanation (shared by the pre-clean report and the result screen) ----------------------------
function scoreClass(n){ return n >= 85 ? 'good' : n >= 60 ? 'warn' : 'bad'; }

// "How is this scored?": the one-sentence formula, the arithmetic for THIS file, and the per-column table.
function howScoredHtml(report){
  const m = report && report.score_model;
  if(!m) return '';
  const cols = (m.columns || []).slice().sort((a,b) => a.score - b.score);
  const rows = cols.map(c => `<tr>
      <td data-label="Column">${escHtml(c.column)}${c.key ? ' <span class="key-tag">key</span>' : ''}</td>
      <td data-label="Valid">${escHtml(c.valid_pct)}%</td>
      <td data-label="Main problem">${escHtml(c.main_problem)}</td></tr>`).join('');
  let math = `Average column score ${escHtml(Math.round(m.cell_score))}`;
  if(m.key_column) math += `, reduced by ${escHtml(Math.round(m.key_column.penalty_pct))}% because ${escHtml(m.key_column.column)} is ${escHtml(m.key_column.bad_pct)}% missing or invalid`;
  if(m.duplicates && m.duplicates.rows) math += `, then by ${escHtml(m.duplicates.share_pct)}% for ${escHtml(m.duplicates.rows)} duplicate row${m.duplicates.rows === 1 ? '' : 's'}`;
  math += ` = <strong>${escHtml(report.score)}</strong>.`;
  return `<details class="how-scored">
    <summary>How is this scored?</summary>
    <p>${escHtml(m.formula)}</p>
    <p class="hint">${math}</p>
    ${rows ? `<table class="how-table"><thead><tr><th>Column</th><th>Valid</th><th>Main problem</th></tr></thead><tbody>${rows}</tbody></table>` : ''}
  </details>`;
}

// Readiness for analysis: the plain answer to "can I analyse this?", with the blockers/cautions behind it.
function readinessHtml(rd){
  if(!rd) return '';
  const li = (x) => `<li>${escHtml(x.label)}</li>`;
  return `<div class="readiness readiness-${escHtml(rd.verdict)}">
    <strong>${escHtml(rd.title)}</strong>
    <p>${escHtml(rd.summary)}</p>
    ${rd.blockers.length ? `<h4>Fix before analysing</h4><ul>${rd.blockers.map(li).join('')}</ul>` : ''}
    ${rd.cautions.length ? `<h4>Keep in mind</h4><ul>${rd.cautions.map(li).join('')}</ul>` : ''}
  </div>`;
}

function reasonsHtml(reasons, title){
  if(!reasons || !reasons.length) return '';
  return `<div class="reasons"><h3>${escHtml(title)}</h3><ol>${reasons.map(r =>
    `<li><span>${escHtml(r.label)}</span>${r.key_column ? ' <span class="key-tag">key column</span>' : ''}</li>`).join('')}</ol></div>`;
}

const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 };

function renderReport(report){
  preCleanScore = report.score;
  reportCard.classList.remove('hidden');
  scoreNum.innerHTML = escHtml(report.score) + (report.grade ? '<small>' + escHtml(report.grade) + '</small>' : '');
  scoreBadge.className = 'score-badge ' + (report.score >= 85 ? 'good' : report.score >= 60 ? 'warn' : 'bad');
  const c = report.counts || {};
  const total = (c.critical || 0) + (c.warning || 0) + (c.info || 0);
  reportTitle.textContent = total === 0
    ? 'Looks clean already'
    : total === 1 ? 'We found 1 thing to fix' : `We found ${total} things to fix`;
  reportSub.textContent = `${Number(report.row_count).toLocaleString()} rows · ${report.column_count} columns`
    + (c.critical ? ` · ${c.critical} critical` : '');

  const preWhy = $('preScoreWhy');
  if(preWhy) preWhy.innerHTML = readinessHtml(report.readiness) + (report.grade_note ? `<p class="hint" role="note">${escHtml(report.grade_note)}</p>` : '')
    + reasonsHtml(report.score_model && report.score_model.top_reasons, 'What is holding the score down') + howScoredHtml(report);

  const findings = (report.findings || []).filter(f => !f.advisory).slice().sort((a,b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
  findingsEl.innerHTML = '';
  noFindings.style.display = findings.length ? 'none' : 'block';

  findings.forEach(f => {
    const row = document.createElement('div');
    row.className = 'finding finding-' + f.severity;
    row.innerHTML = `
      <span class="badge ${escHtml(f.severity)}">${escHtml(f.severity)}</span>
      <div class="finding-body">
        <strong>${f.column ? escHtml(f.column) : 'Whole file'}</strong>
        <p>${escHtml(f.detail)}</p>
        <p class="suggestion">${escHtml(f.suggestion)}</p>
      </div>`;
    findingsEl.appendChild(row);
  });
}

// Renders the Recommendations panel from { safe, ambiguous,
// recommended_rules } (see cleaning/recommendations.py). This never
// changes which rules exist or their default-checked state in the
// .rules list below, it only surfaces, per-issue, which of those
// already-available rules the current file's analysis backs up, and
// lets the user toggle a recommendation to include/exclude that rule
// from the run. Findings with no safe rule (or "critical" severity)
// show up as read-only "needs review" items instead of a checkbox,
// since there's no automated fix to opt in or out of.
function renderRecommendations(recommendations){
  if(!recommendations){ recommendationsCard.classList.add('hidden'); return; }
  recommendationsCard.classList.remove('hidden');

  const safe = recommendations.safe || [];
  const ambiguous = recommendations.ambiguous || [];
  if(ambiguous.length){
    reportSub.textContent += ` · ${ambiguous.length} need${ambiguous.length === 1 ? 's' : ''} your review (open Customize)`;
  }

  recSafeList.innerHTML = '';
  recSafeEmpty.style.display = safe.length ? 'none' : 'block';
  safe.forEach(rec => {
    const checkbox = ruleCheckbox(rec.rule);
    const title = RULE_TITLES[rec.rule] || rec.rule;
    const evidence = rec.findings.map(f => (f.column ? f.column + ': ' : '') + f.detail).join(' · ');

    const item = document.createElement('label');
    item.className = 'rec-item safe';
    item.innerHTML = `
      <input type="checkbox" ${checkbox && checkbox.checked ? 'checked' : ''}>
      <div class="rec-item-body">
        <strong>${escHtml(title)}</strong>
        <p>${escHtml(rec.reason)}</p>
        <p class="rec-evidence">${escHtml(evidence)}</p>
      </div>`;
    const input = item.querySelector('input');
    // Two-way sync: this checkbox is a view onto the real rule
    // checkbox in .rules, so unchecking a recommendation actually
    // excludes that rule from the /api/process request, and vice
    // versa, nothing new to wire into selectedRules().
    input.addEventListener('change', () => { if(checkbox) checkbox.checked = input.checked; });
    if(checkbox) checkbox.addEventListener('change', () => { input.checked = checkbox.checked; });
    recSafeList.appendChild(item);
  });

  recAmbiguousList.innerHTML = '';
  recAmbiguousEmpty.style.display = ambiguous.length ? 'none' : 'block';
  ambiguous
    .slice()
    .sort((a,b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
    .forEach(f => {
      const item = document.createElement('div');
      item.className = 'rec-item';
      const sampleHtml = f.sample_values && f.sample_values.length
        ? `<p class="rec-evidence">e.g. ${escHtml(f.sample_values.join(', '))}</p>` : '';

      const options = f.resolution_options || [];
      let choiceHtml = '';
      if(options.length){
        const selectId = 'resolve-' + Math.random().toString(36).slice(2);
        const hasGroups = options.some(o => o.group);
        let opts;
        if(hasGroups){
          // Grouped (currently: phone country codes by region) so a
          // long list like "suspicious_phone_format"'s ~30 countries
          // doesn't read as one undifferentiated wall of options.
          const byGroup = {};
          options.forEach(o => { (byGroup[o.group] = byGroup[o.group] || []).push(o); });
          opts = Object.entries(byGroup).map(([group, opts]) =>
            `<optgroup label="${escHtml(group)}">` +
            opts.map(o => `<option value="${escHtml(o.id)}">${escHtml(o.label)}</option>`).join('') +
            `</optgroup>`
          ).join('');
        } else {
          opts = options.map(o => `<option value="${escHtml(o.id)}">${escHtml(o.label)}</option>`).join('');
        }
        choiceHtml = `
          <select class="resolve-select" id="${selectId}"
                  data-column="${escHtml(f.column || '')}" data-issue="${escHtml(f.issue)}">
            <option value="skip">Leave as is (no change)</option>${opts}
          </select>`;
      }

      // Impossible ages (below 0 or above 120) are never real values: start on "blank them" so they come out of
      // the cleaned file unless the user changes it. Every other finding still starts on "Leave as is".
      const preselect = f.issue === 'impossible_age' ? 'blank_invalid' : 'skip';
      choiceHtml = choiceHtml.replace(`<option value="${preselect}">`, `<option value="${preselect}" selected>`);

      item.innerHTML = `
        <span class="badge ${escHtml(f.severity)}">${escHtml(f.severity)}</span>
        <div class="rec-item-body">
          <strong>${f.column ? escHtml(f.column) : 'Whole file'}</strong>
          <p>${escHtml(f.detail)}</p>
          ${sampleHtml}
          <p class="rec-evidence">${escHtml(f.suggestion)}</p>
          ${choiceHtml}
        </div>`;
      recAmbiguousList.appendChild(item);
    });
}

// "Leave as is" is an explicit instruction, NOT the absence of one. Every dropdown starts on "Leave as is",
// so a column is only locked when the user actually touched that dropdown (and left or put it on "Leave as
// is"). Those columns go to the server as `leave_as_is`; the server then refuses to let any automatic rule
// change a single value in them. (Previously these were silently dropped and the defaults still ran.)
document.addEventListener('change', e => {
  if(e.target && e.target.classList && e.target.classList.contains('resolve-select')) e.target.dataset.touched = '1';
});

// Reads every resolve-select where the user picked an actual action.
function selectedResolutions(){
  return Array.from(document.querySelectorAll('.resolve-select'))
    .filter(sel => sel.value !== 'skip')
    .map(sel => ({ column: sel.dataset.column, issue: sel.dataset.issue, choice: sel.value }));
}

function selectedLeaveAsIs(){
  const cols = new Set();
  document.querySelectorAll('.resolve-select').forEach(sel => {
    if(sel.dataset.touched === '1' && sel.value === 'skip' && sel.dataset.column) cols.add(sel.dataset.column);
  });
  return Array.from(cols);
}

// ---- Column validation report (read-only; backed by /api/validate) ----
let validationConfig = { column_types: {} };
const VAL_TYPES = ['numeric','integer','currency','date','email','phone','categorical','identifier','text'];
const VAL_CATS = ['valid','invalid','suspicious','missing','unresolved'];

function renderValidation(v){
  const group = $('validationGroup'), table = $('validationTable');
  if(!group || !table) return;
  $('validationRows').innerHTML = '';
  if(!v || !v.columns || !v.columns.length){ group.classList.add('hidden'); return; }
  group.classList.remove('hidden');
  const rows = v.columns.map(c => {
    const detected = c.type_status === 'confirmed' ? 'confirmed' : (c.type_status === 'needs_confirmation' ? 'unsure' : 'detected');
    const sel = `<select class="val-type" data-col="${escHtml(c.column)}">` +
      `<option value="">${escHtml(c.effective_type)} (${detected}${c.type_status === 'confirmed' ? '' : ', ' + Math.round(c.type_confidence*100) + '%'})</option>` +
      VAL_TYPES.map(t => `<option value="${t}"${c.confirmed_type === t ? ' selected' : ''}>${t}</option>`).join('') + `</select>`;
    const cell = cat => c.counts[cat]
      ? `<button type="button" class="val-count" data-col="${escHtml(c.column)}" data-cat="${cat}">${c.counts[cat]}</button>` : '0';
    const flag = c.fully_valid ? '' : ' ⚠';
    const acts = (c.suggested_actions || []).map(a => escHtml(a.text)).join('<br>');
    return `<tr><td><strong>${escHtml(c.column)}</strong>${flag}</td><td>${sel}</td><td>${c.total}</td>` +
      VAL_CATS.map(k => `<td>${cell(k)}</td>`).join('') + `<td style="font-size:.75rem;">${acts}</td></tr>`;
  }).join('');
  table.innerHTML = `<table><thead><tr><th>Column</th><th>Type</th><th>Rows</th><th>Valid</th>` +
    `<th>Invalid</th><th>Suspicious</th><th>Missing</th><th>Unresolved</th><th>Suggested</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function revalidate(){
  const res = await fetch(base() + '/api/validate/' + lastJobId, { method:'POST', credentials:'include',
    headers: apiHeaders({'Content-Type':'application/json'}),
    body: JSON.stringify({ column_types: validationConfig.column_types, has_header: hasHeader() }) });
  const json = await res.json();
  if(res.ok) renderValidation(json.validation);
}

async function showValidationRows(column, category){
  const box = $('validationRows');
  const res = await fetch(base() + '/api/validate/' + lastJobId + '/rows', { method:'POST', credentials:'include',
    headers: apiHeaders({'Content-Type':'application/json'}),
    body: JSON.stringify({ column, category, column_types: validationConfig.column_types, has_header: hasHeader(), limit: 100 }) });
  const json = await res.json();
  if(!res.ok){ box.innerHTML = `<p class="hint">${escHtml(json.error || 'Could not load rows')}</p>`; return; }
  const body = json.rows.map(r => `<tr><td>${r.sheet_row}</td><td>${r.value === null ? '<em>(empty)</em>' : escHtml(String(r.value))}</td><td>${escHtml(r.reason)}</td></tr>`).join('');
  box.innerHTML = `<p class="hint"><strong>${escHtml(column)}</strong> · ${escHtml(category)} · ${json.total} row(s)` +
    `${json.total > json.rows.length ? ' (first ' + json.rows.length + ' shown)' : ''}. Original values, unchanged.</p>` +
    `<table><thead><tr><th>Row</th><th>Original value</th><th>Why</th></tr></thead><tbody>${body}</tbody></table>`;
}

document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('.val-count');
  if(b) showValidationRows(b.dataset.col, b.dataset.cat);
});
document.addEventListener('change', e => {
  if(!e.target.classList || !e.target.classList.contains('val-type')) return;
  if(e.target.value) validationConfig.column_types[e.target.dataset.col] = e.target.value;
  else delete validationConfig.column_types[e.target.dataset.col];
  revalidate();
});

function setStage(text, cls){
  stage.textContent = text;
  stage.className = 'stage' + (cls ? ' ' + cls : '');
}

function selectedRules(){
  // Only the rule checklist: the header toggle also lives in a .rule label.
  return Array.from(document.querySelectorAll('fieldset.rules .rule input:checked')).map(c => c.value);
}

runBtn.addEventListener('click', async () => {
  if(!selectedFile || !lastJobId) return;
  runBtn.disabled = true;
  runBtn.setAttribute('aria-busy', 'true');   // spinner while the real request runs (motion.css)
  results.classList.add('hidden');
  downloadBtn.classList.add('hidden');
  try{
    setStep('clean', 'active');
    setPhase('working');
    const rules = selectedRules();
    setStage('Cleaning your data …', 'active');
    const procRes = await fetch(base() + '/api/process/' + lastJobId, {
      method:'POST',
      credentials:'include',
      headers: apiHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({ rules, resolutions: selectedResolutions(), leave_as_is: selectedLeaveAsIs(), has_header: hasHeader(), options: reviewOptions(), cleaning_profile: mappingProfile(), profile_id: (window.OmixaPro && OmixaPro.profileId()) || undefined })
    });
    let procJson = await procRes.json();
    if(!procRes.ok) throw upgradeAwareError(procRes, procJson, 'processing failed');
    if(procRes.status === 202){
      if(procJson.message) setStage(procJson.message, 'active');
      const done = await pollJob(lastJobId, j => j.status === 'processed' && j.summary);
      procJson = { summary: done.summary };
    }

    setStep('download', '');
    setStage('');
    setPhase('done');
    renderResults(procJson.summary);
    if(window.OmixaPro) OmixaPro.showResults(procJson.summary);
    downloadBtn.classList.remove('hidden');
    resetBtn.classList.remove('hidden');
  }catch(err){
    setStage(err.message, 'error');
    setStep(lastJobId ? 'clean' : 'upload', 'err');
    setPhase(lastJobId ? 'review' : 'error');
  }finally{
    runBtn.disabled = false;
    runBtn.removeAttribute('aria-busy');
  }
});

function renderResults(summary){
  results.classList.remove('hidden');
  const c = summary.changes || {};
  const cmp = summary.quality_comparison || (summary.cleaning_summary && summary.cleaning_summary.quality_comparison) || null;
  const review = cmp && cmp.review;
  // Honest heading: "Your data is clean" only when nothing is left to review.
  const heading = $('resultsHeading'), sub = $('resultsSub');
  if(heading){
    heading.textContent = review ? review.headline : 'Cleaning finished';
    results.classList.toggle('needs-review', !!(review && !review.clean));
  }
  if(sub) sub.textContent = !review ? "Here's what changed. Your file is ready."
    : review.clean ? "Here's what changed. Your file is ready."
    : "Your file is ready. Here's what changed, and what is left for you to check.";

  const post = summary.quality_report;
  const beforeGrade = summary.quality_report_before ? summary.quality_report_before.grade : null;
  let scoreHtml = '';
  if(post){
    const cls = post.score >= 85 ? 'good' : post.score >= 60 ? 'warn' : 'bad';
    const before = preCleanScore != null ? preCleanScore : post.score;
    const delta = post.score - before;
    const deltaText = delta > 0 ? `+${delta}` : delta === 0 ? 'no change' : `${delta}`;
    const badge = (score, grade) => escHtml(score) + (grade ? '<small>' + escHtml(grade) + '</small>' : '');
    const why = cmp && cmp.overall ? cmp.overall.why_unchanged : null;
    const beforeLabel = badge(before, beforeGrade);
    const afterLabel = badge(post.score, post.grade);
    scoreHtml = `
      <div class="score-compare">
        <div class="score-compare-item">
          <div class="score-badge small ${before >= 85 ? 'good' : before >= 60 ? 'warn' : 'bad'}">${beforeLabel}</div>
          <div class="l">before</div>
        </div>
        <span class="score-arrow">→</span>
        <div class="score-compare-item">
          <div class="score-badge small ${cls}">${afterLabel}</div>
          <div class="l">after (${deltaText})</div>
        </div>
      </div>
      ${why ? `<p class="hint score-why" role="note">${escHtml(why)}</p>` : ''}`;
  }

  stats.innerHTML = scoreHtml + `
    <div class="stat"><div class="n">${summary.rows_in}</div><div class="l">rows in</div></div>
    <div class="stat"><div class="n">${summary.rows_out}</div><div class="l">rows out</div></div>
  `;
  // Only list the changes that actually happened, so the summary stays short.
  [['formatting_changed','cells reformatted'],['missing_values_changed','values filled'],['duplicates_changed','duplicates removed']]
    .forEach(([key, label]) => {
      if(c[key]) stats.innerHTML += `<div class="stat"><div class="n">${c[key]}</div><div class="l">${label}</div></div>`;
    });


  // Any other "<rule>_changed" counters (the newer standardization
  // rules) get their own small stat cards too, so nothing added to
  // the pipeline later silently fails to show up here.
  const KNOWN_STAT_KEYS = new Set(['formatting_changed','missing_values_changed','duplicates_changed']);
  const STAT_LABELS = {
    column_names_changed: 'headers cleaned',
    missing_token_normalization_changed: 'blanks recognized',
    numeric_text_cleaning_changed: 'numbers cleaned',
    gender_standardization_changed: 'genders standardized',
    country_standardization_changed: 'countries standardized',
    boolean_standardization_changed: 'booleans standardized',
    currency_label_standardization_changed: 'currency labels standardized',
    case_standardization_changed: 'capitalisation fixed',
    categorical_standardization_changed: 'categories merged',
    email_cleaning_changed: 'emails normalized',
    phone_cleaning_changed: 'phone values cleaned',
    date_standardization_changed: 'dates standardized',
  };
  Object.entries(c).forEach(([key, val]) => {
    if(KNOWN_STAT_KEYS.has(key) || !val) return;
    const label = STAT_LABELS[key] || key.replace(/_changed$/, '').replace(/_/g, ' ');
    stats.innerHTML += `<div class="stat"><div class="n">${val}</div><div class="l">${label}</div></div>`;
  });

  const whyEl = $('scoreWhy');
  if(whyEl){
    let html = readinessHtml(post && post.readiness);
    if(review && !review.clean){
      html += `<div class="review-list"><h3>${escHtml(review.headline)}</h3>` + review.items.map(it => `
        <div class="finding finding-${escHtml(it.severity)}">
          <span class="badge ${escHtml(it.severity)}">${it.optional ? 'optional' : escHtml(it.severity)}</span>
          <div class="finding-body">
            <strong>${it.column ? escHtml(it.column) : 'Whole file'} · ${escHtml(it.title)} (${escHtml(it.count)})</strong>
            <p>${escHtml(it.detail)}</p>
            ${it.suggestion ? `<p class="suggestion">${escHtml(it.suggestion)}</p>` : ''}
          </div>
        </div>`).join('') + '</div>';
    }
    if(post && post.grade_note) html += `<p class="hint" role="note">${escHtml(post.grade_note)}</p>`;
    html += reasonsHtml(post && post.score_model && post.score_model.top_reasons, 'What is holding the score down');
    html += howScoredHtml(post);
    whyEl.innerHTML = html;
  }

  detailBlocks.innerHTML = '';
  const d = summary.details || {};

  // The spec-style plain-text "DATA CLEANING SUMMARY" (see
  // cleaning/summary.py), shown first, verbatim, so it's easy to
  // copy/paste elsewhere; the tables below break the same numbers
  // down per column for anyone who wants more detail.
  if(summary.cleaning_summary && summary.cleaning_summary.text){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Cleaning summary</h3><pre class="cleaning-summary-text">${escHtml(summary.cleaning_summary.text)}</pre>`;
    detailBlocks.appendChild(block);
  }

  if(d.missing_values){
    const rows = Object.entries(d.missing_values.per_column || {});
    if(rows.length){
      const block = document.createElement('div');
      block.innerHTML = `<h3>Missing values by column</h3>` + tableFrom(
        ['column','action','missing %','filled'],
        rows.map(([col, v]) => [
          col,
          {html: `<span class="badge ${v.action === 'median_imputation' ? 'median' : v.action === 'unknown_category' ? 'unknown' : 'review'}">${escHtml(String(v.action).replace(/_/g,' '))}</span>`},
          v.missing_percentage + '%',
          v.filled ?? '-'
        ])
      );
      detailBlocks.appendChild(block);
    }
  }

  if(d.formatting && Object.keys(d.formatting.per_column_cells_changed || {}).length){
    const rows = Object.entries(d.formatting.per_column_cells_changed);
    const block = document.createElement('div');
    block.innerHTML = `<h3>Formatting by column</h3>` + tableFrom(
      ['column','cells changed'], rows.map(([col, n]) => [col, n])
    );
    detailBlocks.appendChild(block);
  }

  if(d.duplicates){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Duplicates</h3><p class="hint">Found ${d.duplicates.duplicate_rows_found}, removed ${d.duplicates.removed}, kept "${d.duplicates.kept}" occurrence.</p>`;
    detailBlocks.appendChild(block);
  }

  if(d.column_names && Object.keys(d.column_names.renamed || {}).length){
    const rows = Object.entries(d.column_names.renamed);
    const block = document.createElement('div');
    block.innerHTML = `<h3>Column names cleaned up</h3>` + tableFrom(
      ['original', 'cleaned'], rows.map(([oldName, newName]) => [oldName, newName])
    );
    detailBlocks.appendChild(block);
  }

  // Generic renderer for every other per-column standardization rule
  // (numeric text, booleans, categories, emails, phone, dates, missing
  // tokens) so a new rule shows up here automatically without needing
  // a bespoke block; they all share the same { per_column: {...} } shape.
  const GENERIC_DETAIL_TITLES = {
    missing_token_normalization: 'Blank / N-A style values recognized',
    numeric_text_cleaning: 'Numeric text cleaned',
    gender_standardization: 'Gender standardized',
    country_standardization: 'Country standardized',
    boolean_standardization: 'Booleans standardized',
    currency_label_standardization: 'Currency labels standardized',
    case_standardization: 'Capitalisation fixed',
    categorical_standardization: 'Categories standardized',
    email_cleaning: 'Emails cleaned',
    phone_cleaning: 'Phone formatting cleaned',
    date_standardization: 'Dates standardized',
  };
  Object.entries(GENERIC_DETAIL_TITLES).forEach(([ruleName, title]) => {
    const detail = d[ruleName];
    const perColumn = detail && detail.per_column;
    const rows = perColumn ? Object.entries(perColumn) : [];
    if(!rows.length && !(detail && detail.skipped_ambiguous && detail.skipped_ambiguous.length)) return;

    const block = document.createElement('div');
    let html = rows.length ? `<h3>${title}</h3>` + tableFrom(
      ['column', 'cells changed'],
      rows.map(([col, v]) => [col, typeof v === 'object' ? (v.changed ?? '-') : v])
    ) : `<h3>${title}</h3>`;
    if(detail && detail.skipped_ambiguous && detail.skipped_ambiguous.length){
      html += `<p class="hint">Skipped (format too ambiguous to guess safely): ${escHtml(detail.skipped_ambiguous.join(', '))}</p>`;
    }
    block.innerHTML = html;
    detailBlocks.appendChild(block);
  });

  if(post && post.findings && post.findings.some(f => !f.advisory)){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Still worth a look</h3>`;
    const list = document.createElement('div');
    list.className = 'findings';
    post.findings
      .slice()
      .sort((a,b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
      .forEach(f => {
        const row = document.createElement('div');
        row.className = 'finding finding-' + f.severity;
        row.innerHTML = `
          <span class="badge ${escHtml(f.severity)}">${escHtml(f.severity)}</span>
          <div class="finding-body">
            <strong>${f.column ? escHtml(f.column) : 'Whole file'}</strong>
            <p>${escHtml(f.detail)}</p>
            <p class="suggestion">${escHtml(f.suggestion)}</p>
          </div>`;
        list.appendChild(row);
      });
    block.appendChild(list);
    detailBlocks.appendChild(block);
  }

  const mapRows = mappingResultRows(summary);
  if(mapRows.length){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Value standardization</h3>` + tableFrom(
      ['column', 'type', 'changed automatically', 'decided by you', 'still undecided (left as is)'], mapRows)
      + '<p class="hint">Values that were uncertain or ambiguous are never changed without your approval. They stay exactly as uploaded.</p>';
    detailBlocks.appendChild(block);
  }

  if(summary.resolutions_applied && summary.resolutions_applied.length){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Your resolution choices</h3>` + tableFrom(
      ['column', 'issue', 'choice', 'cells changed'],
      summary.resolutions_applied.map(r => [r.column, r.issue.replace(/_/g,' '), r.choice.replace(/_/g,' '), r.changed])
    );
    detailBlocks.appendChild(block);
  }
}

// Cells are escaped by default. A caller that needs markup (e.g. a badge) must
// opt in explicitly with {html: '...'} and is then responsible for escaping any
// data it interpolates.
function tableFrom(headers, rows){
  const cellHtml = cell => (cell && typeof cell === 'object' && 'html' in cell) ? cell.html : escHtml(cell);
  const th = headers.map(h => `<th>${escHtml(h)}</th>`).join('');
  const tr = rows.map(r => `<tr>${r.map(cell => `<td>${cellHtml(cell)}</td>`).join('')}</tr>`).join('');
  return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

// Saves `blob` to disk under `filename`. On browsers that support the
// File System Access API (desktop Chrome/Edge/Opera), this opens the
// native "Save As" dialog so the user can pick the name and folder,
// same as a normal download in a desktop app. Browsers without that
// API (Firefox, Safari, and ALL mobile browsers - Chrome for Android
// and iOS Safari included - have no such dialog to offer) fall back
// to the classic silent save-to-Downloads behavior; that fallback is
// a real platform limitation, not a bug in this app.
async function saveDownloadedFile(blob, filename){
  if (window.showSaveFilePicker){
    const dotIndex = filename.lastIndexOf('.');
    const ext = dotIndex > -1 ? filename.slice(dotIndex) : '';
    try{
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: ext ? [{
          description: ext.slice(1).toUpperCase() + ' file',
          accept: { [blob.type || 'application/octet-stream']: [ext] }
        }] : undefined
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return true;
    }catch(e){
      if (e.name === 'AbortError') return false; // user cancelled the dialog
      // Fall through to the classic method for any other failure.
    }
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  return true;
}

downloadBtn.addEventListener('click', async () => {
  if(!lastJobId || downloadBtn.getAttribute('aria-busy') === 'true') return;
  downloadBtn.setAttribute('aria-busy', 'true');
  try{ await doDownload(); }
  finally{ downloadBtn.removeAttribute('aria-busy'); }
});

async function doDownload(){
  let r;
  try{
    r = await fetch(base() + '/api/download/' + lastJobId + '?format=url', { credentials:'include', headers: apiHeaders() });
  }catch(e){
    setStage('download failed: could not reach the server', 'error');
    return;
  }
  if(r.ok && (r.headers.get('Content-Type') || '').includes('application/json')){
    // Object-storage mode: the file comes straight from a short-lived signed URL (no cookies sent).
    // This must be a browser NAVIGATION, not fetch(): the page's CSP (connect-src 'self') blocks
    // cross-origin fetches to the bucket, and a plain navigation to an attachment URL is not
    // subject to it. The signed URL already carries the download filename.
    const j = await r.json();
    const a = document.createElement('a');
    a.href = j.url; a.download = j.filename || '';
    a.rel = 'noopener'; a.target = '_self';
    document.body.appendChild(a); a.click(); a.remove();
    downloadBtn.classList.add('hidden'); // the original upload is discarded after download
    return;
  }
  if(!r.ok){
    let msg = 'download failed (HTTP ' + r.status + ')';
    try{
      const j = await r.json();
      msg = 'download failed: ' + (j.detail || j.error || msg);
    }catch(e){ /* response wasn't JSON, keep the generic message */ }
    setStage(msg, 'error');
    return;
  }
  const blob = await r.blob();
  const cd = r.headers.get('Content-Disposition') || '';
  const match = cd.match(/filename="?([^"]+)"?/);
  const filename = match ? match[1] : 'cleaned_output';
  await saveDownloadedFile(blob, filename);
  downloadBtn.classList.add('hidden'); // job is deleted server-side after download
}

function resetAll(){
  selectedFile = null; lastJobId = null; preCleanScore = null;
  resetReview(); resetMapping();
  fileNameEl.textContent = ''; fileInput.value = '';
  runBtn.disabled = true;
  results.classList.add('hidden');
  reportCard.classList.add('hidden');
  recommendationsCard.classList.add('hidden');
  downloadBtn.classList.add('hidden'); resetBtn.classList.add('hidden');
  setStage('');
  setStep('upload', '');
  setPhase('idle');
  showWorkspace(false);
}
resetBtn.addEventListener('click', resetAll);
if(changeFileBtn) changeFileBtn.addEventListener('click', resetAll);

// "No file? Try one of these": build a File from the bundled sample CSV
// and push it through exactly the same path as a real upload.
document.querySelectorAll('.sample').forEach(btn => {
  btn.addEventListener('click', () => {
    const sample = (window.OMIXA_SAMPLES || {})[btn.dataset.sample];
    if(!sample) return;
    setFile(new File([sample.csv], sample.filename, { type: 'text/csv' }));
  });
});


// ---- Review pop-up: flagged values (phone = "call me", amount = "abc", ...) ----
// Lists every distinct value the validation report flagged as invalid / suspicious / unresolved, in ANY column,
// and lets the user choose per value: keep it, clear the cell (leave it empty) or delete the whole row.
// Nothing changes unless a choice is made; choices go to the server as options.value_actions.
const REVIEW_CATS = ['invalid', 'suspicious', 'unresolved'];
const reviewBtn = $('reviewBtn'), reviewDialog = $('reviewDialog');
let reviewItems = [];        // [{column, value, count, reason, rowsShown:[...], truncated}]
let reviewChoices = {};      // key -> 'keep' | 'clear' | 'remove_rows'
let reviewValidation = null;
let reviewAutoShown = false;

const reviewKey = (col, val) => JSON.stringify([col, val]);

function resetReview(){
  reviewItems = []; reviewChoices = {}; reviewValidation = null; reviewAutoShown = false;
  if(reviewBtn) reviewBtn.classList.add('hidden');
}

function flaggedTotal(v){
  if(!v || !v.columns) return 0;
  return v.columns.reduce((n, c) => n + REVIEW_CATS.reduce((m, k) => m + (c.counts[k] || 0), 0), 0);
}

function updateReviewButton(){
  if(!reviewBtn) return;
  // Only shown once the list is loaded, so the number on it is the number of values the pop-up will show.
  const total = reviewItems.reduce((n, i) => n + i.count, 0);
  const chosen = Object.values(reviewChoices).filter(c => c !== 'keep').length;
  reviewBtn.classList.toggle('hidden', !reviewItems.length);
  reviewBtn.textContent = chosen
    ? `Review flagged values (${chosen} choice${chosen === 1 ? '' : 's'} saved)`
    : `Review ${total} flagged value${total === 1 ? '' : 's'}`;
}

async function prepareReview(v){
  reviewItems = []; reviewChoices = {}; reviewValidation = v; reviewAutoShown = false;
  updateReviewButton();
  if(!flaggedTotal(v) || !reviewDialog) return;
  // Pre-load the list in the background so the pop-up opens instantly; open it once, automatically.
  try{
    await loadReviewItems(v);
    updateReviewButton();
    if(reviewItems.length && !reviewAutoShown){ reviewAutoShown = true; openReview(); }
  }catch(_){ /* the button still works and retries */ }
}

async function loadReviewItems(v){
  const jobs = [];
  v.columns.forEach(c => REVIEW_CATS.forEach(cat => { if(c.counts[cat]) jobs.push({ column: c.column, category: cat }); }));
  const results = await Promise.all(jobs.map(async j => {
    const res = await fetch(base() + '/api/validate/' + lastJobId + '/rows', { method:'POST', credentials:'include',
      headers: apiHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({ column: j.column, category: j.category, column_types: validationConfig.column_types, has_header: hasHeader(), limit: 1000 }) });
    const json = await res.json();
    if(!res.ok) throw new Error(json.error || 'Could not load flagged values');
    return { ...j, json };
  }));
  const byKey = new Map();
  results.forEach(({ column, json }) => json.rows.forEach(r => {
    if(r.value === null || r.value === undefined) return;          // empty cells are "missing", handled elsewhere
    if(r.code === 'needs_country') return;                          // a plausible number with no country: not an error
    const value = String(r.value), k = reviewKey(column, value);
    if(!byKey.has(k)) byKey.set(k, { column, value, count: 0, reason: r.reason, rows: [] });
    const it = byKey.get(k); it.count += 1; it.rows.push(r.sheet_row);
  }));
  reviewItems = Array.from(byKey.values());
  reviewItems.forEach(it => { it.truncated = false; });
  results.forEach(({ column, json }) => { if(json.total > json.rows.length) reviewItems.forEach(it => { if(it.column === column) it.truncated = true; }); });
}

function renderReview(){
  const body = $('reviewBody');
  if(!reviewItems.length){ body.innerHTML = '<p class="hint">Nothing needs a decision. Empty cells are handled separately.</p>'; return; }
  const cols = [...new Set(reviewItems.map(i => i.column))];
  body.innerHTML = cols.map(col => {
    const items = reviewItems.filter(i => i.column === col);
    const rowsHtml = items.map(it => {
      const k = reviewKey(it.column, it.value), cur = reviewChoices[k] || 'keep';
      const where = it.rows.slice(0, 4).join(', ') + (it.rows.length > 4 ? ', …' : '');
      const opt = (v, label) => `<option value="${v}"${cur === v ? ' selected' : ''}>${label}</option>`;
      return `<div class="rv-item">
        <div class="rv-what"><mark class="rv-val">${escHtml(it.value)}</mark>
          <span class="rv-meta">${it.count} cell${it.count === 1 ? '' : 's'} · row${it.rows.length === 1 ? '' : 's'} ${escHtml(where)}</span>
          <span class="rv-why">${escHtml(it.reason)}</span></div>
        <select class="rv-choice" data-key="${escHtml(k)}" aria-label="What to do with ${escHtml(it.value)} in ${escHtml(col)}">
          ${opt('keep', 'Keep as is')}${opt('clear', 'Clear it (leave cell empty)')}${opt('remove_rows', 'Delete the whole row')}
        </select></div>`;
    }).join('');
    return `<section class="rv-col"><h3>${escHtml(col)}<button type="button" class="link-btn rv-clear-col" data-col="${escHtml(col)}">Clear all in this column</button></h3>${rowsHtml}</section>`;
  }).join('') + (reviewItems.some(i => i.truncated) ? '<p class="hint">Very large list: only the first 1,000 flagged rows per column are shown.</p>' : '');
}

async function openReview(){
  if(!reviewDialog || !lastJobId) return;
  if(!reviewItems.length && flaggedTotal(reviewValidation)){
    try{ await loadReviewItems(reviewValidation); }catch(err){ setStage(err.message, 'error'); return; }
  }
  renderReview();
  if(typeof reviewDialog.showModal === 'function'){ if(!reviewDialog.open) reviewDialog.showModal(); }
  else reviewDialog.setAttribute('open', '');
}
function closeReview(){
  if(reviewDialog.open) reviewDialog.close(); else reviewDialog.removeAttribute('open');
  updateReviewButton();
}

if(reviewBtn) reviewBtn.addEventListener('click', openReview);
if(reviewDialog){
  reviewDialog.addEventListener('change', e => {
    const sel = e.target.closest && e.target.closest('.rv-choice');
    if(!sel) return;
    if(sel.value === 'keep') delete reviewChoices[sel.dataset.key]; else reviewChoices[sel.dataset.key] = sel.value;
  });
  reviewDialog.addEventListener('click', e => {
    const all = e.target.closest && e.target.closest('.rv-clear-col');
    if(all){
      reviewItems.filter(i => i.column === all.dataset.col).forEach(i => { reviewChoices[reviewKey(i.column, i.value)] = 'clear'; });
      renderReview(); return;
    }
    if(e.target.closest && e.target.closest('#reviewKeepAll')){ reviewChoices = {}; renderReview(); return; }
    if(e.target.closest && e.target.closest('#reviewDone')) closeReview();
    if(e.target.closest && e.target.closest('#reviewClose')) closeReview();
  });
  reviewDialog.addEventListener('close', updateReviewButton);
}

// {column: {clear: [...], remove_rows: [...]}} for the server; undefined when nothing was chosen.
function reviewOptions(){
  const out = {};
  reviewItems.forEach(it => {
    const c = reviewChoices[reviewKey(it.column, it.value)];
    if(c === 'clear' || c === 'remove_rows'){
      if(!out[it.column]) out[it.column] = {};
      if(!out[it.column][c]) out[it.column][c] = [];
      out[it.column][c].push(it.value);
    }
  });
  return Object.keys(out).length ? { value_actions: out } : undefined;
}


// ---- Standardize values (Pro): countries, yes/no, currency labels, categories ----------------------------
// Everything shown here comes from POST /api/cleaning/mapping/<job> (read-only). Choices are sent to
// /api/process as a cleaning_profile with one `standardize_values` rule per ticked column; the server
// re-validates all of it and decides what is safe. Nothing is applied by this dialog itself.
const mapBtn = $('mapBtn'), mapDialog = $('mapDialog');
const MAP_LABEL = { country: 'Countries', boolean: 'Yes / No', currency: 'Currency labels', category: 'Category labels' };
let mapState = {};   // key -> {column, category, enabled, output_format, formats, custom:[{from,to}], decisions:{original:{choice,value}}, preview}
const mapKey = (col, cat) => JSON.stringify([col, cat]);

function resetMapping(){ mapState = {}; if(mapBtn) mapBtn.classList.add('hidden'); }

function updateMapButton(){
  if(!mapBtn) return;
  const all = Object.values(mapState), on = all.filter(s => s.enabled).length;
  mapBtn.classList.toggle('hidden', !all.length);
  mapBtn.textContent = on ? `Standardize values (${on} column${on === 1 ? '' : 's'} chosen)` : 'Standardize values';
}

async function mapFetch(body){
  const res = await fetch(base() + '/api/cleaning/mapping/' + lastJobId, { method: 'POST', credentials: 'include',
    headers: apiHeaders({'Content-Type': 'application/json'}), body: JSON.stringify({ has_header: hasHeader(), ...body }) });
  const json = await res.json();
  if(!res.ok) throw new Error(json.error || 'Could not prepare the preview');
  return json;
}

async function prepareMapping(){
  resetMapping();
  if(!mapBtn || !lastJobId || !(window.OmixaPro && OmixaPro.isPro())) return;
  try{
    const json = await mapFetch({});
    (json.suggestions || []).forEach(s => {
      mapState[mapKey(s.column, s.category)] = { column: s.column, category: s.category, enabled: false,
        output_format: s.output_format, formats: s.output_formats, custom: [], decisions: {}, preview: s };
    });
    updateMapButton();
  }catch(_){ /* optional extra: the normal flow never depends on it */ }
}

async function mapRefresh(key){
  const st = mapState[key];
  const mappings = {}; st.custom.forEach(m => { mappings[m.from] = m.to; });
  try{
    const json = await mapFetch({ column: st.column, category: st.category, output_format: st.output_format, mappings });
    st.preview = { ...json.preview, output_formats: st.formats };
    st.error = '';
  }catch(err){ st.error = err.message; }
  renderMapping();
}

function mapItemHtml(key, it){
  const k = escHtml(key);
  if(it.status === 'safe' || it.status === 'user_defined'){
    return `<div class="rv-item mp-item-ok"><div class="rv-what"><span><mark class="rv-val">${escHtml(it.original)}</mark> → <strong>${escHtml(it.proposed)}</strong></span>
      <span class="rv-meta">${it.count} cell${it.count === 1 ? '' : 's'} · applied automatically</span><span class="rv-why">${escHtml(it.reason)}</span></div></div>`;
  }
  if(it.status === 'not_applicable') return '';
  const cur = (mapState[key].decisions[String(it.original)] || {}), c = cur.choice || 'later';
  const label = it.status === 'review' ? 'Needs your approval' : it.status === 'ambiguous' ? 'Ambiguous: not changed' : 'Not recognised: not changed';
  const opt = (v, t) => `<option value="${v}"${c === v ? ' selected' : ''}>${t}</option>`;
  const orig = escHtml(JSON.stringify(it.original));
  return `<div class="rv-item"><div class="rv-what"><span><mark class="rv-val">${escHtml(it.original)}</mark>${it.proposed ? ` → <strong>${escHtml(it.proposed)}</strong>?` : ''}</span>
      <span class="rv-meta">${it.count} cell${it.count === 1 ? '' : 's'} · ${label}</span><span class="rv-why">${escHtml(it.reason)}</span></div>
      <div><select class="rv-choice mp-choice" data-key="${k}" data-orig="${orig}" aria-label="What to do with ${escHtml(it.original)}">
        ${opt('later', 'Decide later (left unchanged)')}${opt('reject', 'Keep as is')}${it.proposed ? opt('accept', 'Use “' + escHtml(it.proposed) + '”') : ''}${opt('edit', 'Type my own…')}
      </select>${c === 'edit' ? `<input class="mp-edit" data-key="${k}" data-orig="${orig}" maxlength="200" value="${escHtml(cur.value || '')}" aria-label="Your value for ${escHtml(it.original)}">` : ''}</div></div>`;
}

function renderMapping(){
  const body = $('mapBody'), states = Object.entries(mapState), keepTop = body.scrollTop;
  if(!states.length){ body.innerHTML = '<p class="hint">No column looks like countries, yes/no flags, currency labels or inconsistent categories.</p>'; return; }
  body.innerHTML = states.map(([key, st]) => {
    const p = st.preview, sm = p.summary, k = escHtml(key);
    const fmt = Object.entries(st.formats).map(([v, t]) => `<option value="${v}"${st.output_format === v ? ' selected' : ''}>${escHtml(t)}</option>`).join('');
    const items = (p.items || []).map(it => mapItemHtml(key, it)).join('');
    const custom = st.custom.map((m, i) => `<div class="mp-custom">Your mapping: “${escHtml(m.from)}” → “${escHtml(m.to)}” (this file only)
      <button type="button" class="link-btn mp-del" data-key="${k}" data-i="${i}">remove</button></div>`).join('');
    return `<section class="rv-col mp-col"><h3>${escHtml(st.column)} · ${MAP_LABEL[st.category] || escHtml(st.category)}
        <label class="mp-bar"><input type="checkbox" class="mp-enable" data-key="${k}"${st.enabled ? ' checked' : ''}> Standardize this column</label></h3>
      <p class="rv-meta">${escHtml(p.column_fit && p.column_fit.reason || '')} ${sm.will_apply} cell${sm.will_apply === 1 ? '' : 's'} would change automatically, ${sm.needs_approval + sm.ambiguous + sm.unrecognised} need a decision or stay as they are.</p>
      ${Object.keys(st.formats).length > 1 ? `<div class="mp-bar"><label>Write values as <select class="mp-format" data-key="${k}">${fmt}</select></label></div>` : ''}
      ${st.error ? `<p class="hint" role="alert">${escHtml(st.error)}</p>` : ''}${items || '<p class="hint">Nothing to change in this column.</p>'}${p.items_truncated ? '<p class="hint">Only the most common values are listed.</p>' : ''}
      ${custom}<div class="mp-add"><input class="mp-from" data-key="${k}" maxlength="200" placeholder="Value in your file" aria-label="Value in your file">
        <input class="mp-to" data-key="${k}" maxlength="200" placeholder="Should become" aria-label="Should become"><button type="button" class="btn btn-secondary mp-addbtn" data-key="${k}">Add mapping</button></div></section>`;
  }).join('');
  body.scrollTop = keepTop;   // re-rendering must not throw the reader back to the top
}

async function openMapping(){
  if(!mapDialog) return;
  renderMapping();
  if(typeof mapDialog.showModal === 'function'){ if(!mapDialog.open) mapDialog.showModal(); } else mapDialog.setAttribute('open', '');
}
function closeMapping(){ if(mapDialog.open) mapDialog.close(); else mapDialog.removeAttribute('open'); updateMapButton(); }

if(mapBtn) mapBtn.addEventListener('click', openMapping);
if(mapDialog){
  const keyOf = el => el.dataset.key, origOf = el => JSON.parse(el.dataset.orig);
  mapDialog.addEventListener('change', e => {
    const t = e.target, key = t.dataset && t.dataset.key; if(!key || !mapState[key]) return;
    const st = mapState[key];
    if(t.classList.contains('mp-enable')){
      st.enabled = t.checked;
      // one standardization per column: choosing one kind switches the others off
      if(t.checked) Object.values(mapState).forEach(o => { if(o !== st && o.column === st.column) o.enabled = false; });
      renderMapping();
    }else if(t.classList.contains('mp-format')){ st.output_format = t.value; mapRefresh(key); }
    else if(t.classList.contains('mp-choice')){
      const o = String(origOf(t));
      if(t.value === 'later') delete st.decisions[o]; else st.decisions[o] = { choice: t.value, value: (st.decisions[o] || {}).value || '' };
      renderMapping();
    }
  });
  mapDialog.addEventListener('input', e => {
    const t = e.target; if(!t.classList || !t.classList.contains('mp-edit')) return;
    const st = mapState[keyOf(t)], o = String(origOf(t)); if(st && st.decisions[o]) st.decisions[o].value = t.value;
  });
  mapDialog.addEventListener('click', e => {
    const t = e.target.closest && e.target.closest('button'); if(!t) return;
    if(t.id === 'mapDone' || t.id === 'mapClose') return closeMapping();
    if(t.id === 'mapClear'){ Object.values(mapState).forEach(s => { s.enabled = false; s.decisions = {}; }); renderMapping(); return closeMapping(); }
    const key = t.dataset.key, st = key && mapState[key]; if(!st) return;
    if(t.classList.contains('mp-del')){ st.custom.splice(Number(t.dataset.i), 1); mapRefresh(key); }
    if(t.classList.contains('mp-addbtn')){
      const sec = t.closest('.mp-col'), from = sec.querySelector('.mp-from').value.trim(), to = sec.querySelector('.mp-to').value.trim();
      if(!from || !to){ st.error = 'Fill in both boxes. A mapping to nothing is not allowed.'; return renderMapping(); }
      st.custom = st.custom.filter(m => m.from.toLowerCase() !== from.toLowerCase()); st.custom.push({ from, to });
      st.enabled = true; mapRefresh(key);
    }
  });
  mapDialog.addEventListener('close', updateMapButton);
}

// The cleaning_profile for /api/process; undefined when no column was ticked.
function mappingProfile(){
  const columns = {}, decisions = [];
  Object.values(mapState).filter(s => s.enabled).forEach(st => {
    const rule = { type: 'standardize_values', category: st.category, output_format: st.output_format };
    const mappings = {}; st.custom.forEach(m => { mappings[m.from] = m.to; });
    if(Object.keys(mappings).length) rule.mappings = mappings;
    columns[st.column] = { rules: [rule] };
    Object.entries(st.decisions).forEach(([original, d]) => {
      if(d.choice === 'reject') decisions.push({ column: st.column, original, action: 'reject' });
      else if(d.choice === 'accept') decisions.push({ column: st.column, original, action: 'accept' });
      else if(d.choice === 'edit' && d.value && d.value.trim()) decisions.push({ column: st.column, original, action: 'accept', value: d.value.trim() });
    });
  });
  if(!Object.keys(columns).length) return undefined;
  return decisions.length ? { name: 'Standardize values', columns, decisions } : { name: 'Standardize values', columns };
}

// Rows for the result screen: straight from the server's per-column report (no figures are recomputed here).
function mappingResultRows(summary){
  const eng = summary && summary.cleaning_engine; if(!eng || !eng.columns) return [];
  const rows = [];
  Object.entries(eng.columns).forEach(([col, info]) => {
    const r = (info.rules || []).find(x => x.type === 'standardize_values' && x.status === 'ran'); if(!r) return;
    rows.push([col, MAP_LABEL[(r.metrics || {}).category] || '', r.changed, info.review_items - info.review_pending, info.review_pending]);
  });
  return rows;
}
