// Pro batch UI. Every decision (who is Pro, limits, status) comes from the server; this file only
// renders what /api/batches returns. Progress numbers are the server's counts, never estimated here.
(function () {
  const app = document.getElementById('batchApp');
  if (!app) return;
  const $ = id => document.getElementById(id), esc = Omixa.esc;
  const MB = 1024 * 1024, maxFiles = +app.dataset.maxFiles, maxFileMb = +app.dataset.maxFileMb;
  const ok = n => /\.(csv|xlsx|xls)$/i.test(n);
  let chosen = [], batchId = null, timer = null;

  function size(b) { return b >= MB ? (b / MB).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB'; }
  function renderChosen() {
    $('batchList').innerHTML = chosen.map(f => {
      const bad = !ok(f.name) ? 'Unsupported type' : f.size > maxFileMb * MB ? 'Too large' : '';
      return '<li>' + esc(f.name) + ' · ' + size(f.size) + (bad ? ' · <strong>' + bad + '</strong>' : '') + '</li>';
    }).join('');
    $('startBatch').disabled = !chosen.length;
  }
  $('batchFiles').addEventListener('change', e => {
    chosen = Array.from(e.target.files).slice(0, maxFiles);
    $('batchMsg').textContent = e.target.files.length > maxFiles ? 'Only the first ' + maxFiles + ' files were kept.' : '';
    renderChosen();
  });

  const LABEL = { queued: 'Waiting', processing: 'Processing…', completed: '✓', failed: '✕', cancelled: 'Cancelled', expired: 'Expired' };
  function render(b) {
    const c = b.counts;
    $('batchHeadline').textContent = c.completed + ' of ' + c.total + ' files completed';
    const parts = [];
    if (c.processing) parts.push(c.processing + ' processing');
    if (c.queued) parts.push(c.queued + ' waiting');
    if (c.failed) parts.push(c.failed + ' failed');
    if (c.cancelled) parts.push(c.cancelled + ' cancelled');
    if (c.skipped) parts.push(c.skipped + ' skipped');
    $('batchSub').textContent = parts.join(' · ');
    $('batchFiles2').innerHTML = b.files.map(f => {
      let line = '<li>' + (LABEL[f.status] || f.status) + ' ' + esc(f.filename);
      if (f.status === 'failed') line += ' — ' + esc(f.error || 'Could not process file');
      if (f.status === 'completed' && f.summary) {
        line += ' — quality ' + esc(f.summary.quality_before) + ' → ' + esc(f.summary.quality_after);
        line += ' · <a href="' + esc(f.download_url) + '">Download</a>';
      }
      return line + '</li>';
    }).join('');
    $('batchSkipped').innerHTML = (b.skipped || []).length ? 'Skipped before processing: ' + b.skipped.map(s => esc(s.filename) + ' (' + esc(s.reason) + ')').join('; ') : '';
    $('dlAll').classList.toggle('hidden', !b.download_all_url);
    if (b.download_all_url) $('dlAll').href = b.download_all_url;
    $('retryBtn').classList.toggle('hidden', !(b.finished && c.failed));
    $('cancelBtn').classList.toggle('hidden', b.finished);
    $('newBatch').classList.toggle('hidden', !b.finished);
  }
  async function poll() {
    const r = await Omixa.api('/api/batches/' + batchId);
    if (!r.ok) { $('batchSub').textContent = r.json.error || 'Could not read batch status.'; return; }
    render(r.json);
    if (!r.json.finished) timer = setTimeout(poll, 2000);
  }

  $('startBatch').addEventListener('click', async () => {
    const fd = new FormData();
    chosen.filter(f => ok(f.name)).forEach(f => fd.append('files', f, f.name));
    const opts = { has_header: $('optHeader').checked };
    if ($('optColumnNames').checked) opts.standardize_column_names = true;
    fd.append('options', JSON.stringify(opts));
    $('startBatch').disabled = true; $('batchMsg').textContent = 'Uploading…';
    let res, json = {};
    try { res = await fetch(Omixa.base + '/api/batches/', { method: 'POST', body: fd, credentials: 'include' }); json = await res.json(); }
    catch (e) { $('batchMsg').textContent = 'Network error. Please try again.'; $('startBatch').disabled = false; return; }
    if (res.status === 402) { window.location.href = '/batch'; return; }  // server says not Pro: show the upgrade state
    if (res.status === 401) { Omixa.goLogin(); return; }
    if (!res.ok) { $('batchMsg').textContent = json.error || 'Could not start the batch.'; $('startBatch').disabled = false; return; }
    batchId = json.batch_id;
    $('batchSetup').classList.add('hidden'); $('batchProgress').classList.remove('hidden');
    render(json); poll();
  });
  $('cancelBtn').addEventListener('click', async () => { await Omixa.api('/api/batches/' + batchId + '/cancel', { method: 'POST', body: {} }); poll(); });
  $('retryBtn').addEventListener('click', async () => { await Omixa.api('/api/batches/' + batchId + '/retry', { method: 'POST', body: {} }); poll(); });
  $('newBatch').addEventListener('click', () => { clearTimeout(timer); chosen = []; $('batchFiles').value = ''; renderChosen(); $('batchProgress').classList.add('hidden'); $('batchSetup').classList.remove('hidden'); });
})();
