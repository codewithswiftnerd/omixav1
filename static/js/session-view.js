(async function () {
  const sid = document.getElementById('sBody').dataset.sid;
  const r = await Omixa.api('/api/sessions/' + encodeURIComponent(sid));
  const msg = document.getElementById('sMsg');
  if (r.status === 401) { Omixa.goLogin(); return; }
  if (!r.ok) {
    msg.className = 'alert alert-info';
    msg.innerHTML = r.status === 402 ? 'Saved reports are available with Omixa Pro. <a href="/pricing">See pricing</a>' : Omixa.esc(r.json.error || 'Report not found.');
    return;
  }
  const s = r.json.session;
  document.getElementById('sTitle').textContent = s.dataset_name;
  document.getElementById('sMeta').textContent = Omixa.fmtDate(s.created_at) + (s.profile_name ? ' · ' + s.profile_name : '');
  Omixa.renderPro(document.getElementById('sBody'), s.report, s.id);
})();
