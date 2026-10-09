(async function () {
  const { esc, api, fmtDate, fmtDay } = Omixa;
  const $ = id => document.getElementById(id);
  const m = await Omixa.me();
  if (!m.user) { Omixa.goLogin(); return; }
  const u = m.user;

  $('who').textContent = (u.name ? u.name + ' · ' : '') + (u.email || '');
  $('signOutBtn').addEventListener('click', async () => { await api('/api/auth/logout', { method: 'POST', body: {} }); window.location.href = '/'; });

  const pay = new URLSearchParams(window.location.search).get('payment');
  const banners = {
    success: ['alert-success', 'Payment received. Omixa Pro is now active on your account.'],
    failed: ['alert-error', 'The payment did not go through. You have not been charged for Pro.'],
    pending: ['alert-info', 'Your payment is still being confirmed. This page will update shortly.'],
    unknown: ['alert-error', 'We could not match that payment to your account.'],
  };
  if (pay && banners[pay]) { const b = $('payBanner'); b.className = 'alert ' + banners[pay][0]; b.textContent = banners[pay][1]; }

  const label = { pro_monthly: 'Pro Monthly', pro_annual: 'Pro Annual', free: 'Free' }[u.plan] || 'Free';
  $('planBadge').textContent = label;
  $('planBadge').className = 'badge ' + (u.is_pro ? 'badge-plan-pro' : 'badge-plan-free');
  let text = 'Free: clean files with the full cleaning engine. Pro adds repeatable data-quality standards, reports and history.';
  if (u.is_pro) text = (u.renews ? 'Renews on ' : 'Access until ') + fmtDay(u.subscription_expires) + (u.renews ? '.' : ' (it will not renew).');
  else if (u.status === 'expired') text = 'Your Pro subscription expired on ' + fmtDay(u.subscription_expires) + '. Your saved profiles and history are kept and return when you resubscribe.';
  else if (u.status === 'pending') text = 'Waiting for payment confirmation.';
  if (u.payment_failed && u.is_pro) text += ' Your last renewal payment failed, update your card to keep Pro.';
  $('planText').textContent = text;

  const actions = $('planActions');
  if (u.is_pro) {
    actions.innerHTML = '<button class="btn btn-secondary btn-sm" id="manageBtn" type="button">Manage subscription</button>';
    $('manageBtn').addEventListener('click', async () => {
      const r = await api('/api/billing/manage', { method: 'POST', body: {} });
      if (r.ok && r.json.url) window.open(r.json.url, '_blank', 'noopener'); else alert(r.json.error || 'Could not open subscription management.');
    });
  } else {
    actions.innerHTML = '<a class="btn btn-primary btn-sm" href="/pricing">See Omixa Pro</a>';
  }

  if (!u.is_pro) {
    $('profiles').innerHTML = '<p class="hint">This feature is available with Omixa Pro. Save a standard like "Research Dataset Standard" and check every file against it. <a href="/pricing">See pricing</a></p>';
    $('history').innerHTML = '<p class="hint">Processing history, quality reports and change logs are available with Omixa Pro. <a href="/pricing">See pricing</a></p>';
    $('usage').innerHTML = '<div class="stat-card"><div class="stat-value">' + esc((document.getElementById('planCard') || {dataset: {}}).dataset.freeMb || '') + ' MB</div><div class="stat-label">Free upload limit</div></div>';
    return;
  }

  $('newProfileBtn').classList.remove('hidden');
  const [pr, hr] = await Promise.all([api('/api/profiles/'), api('/api/sessions/')]);
  const profiles = (pr.json.profiles || []), sessions = (hr.json.sessions || []);
  const monthStart = new Date(); monthStart.setDate(1); monthStart.setHours(0, 0, 0, 0);
  const thisMonth = sessions.filter(s => s.created_at >= monthStart.getTime() / 1000).length;
  const avg = sessions.length ? Math.round(sessions.reduce((a, s) => a + (s.score_after || 0), 0) / sessions.length) : '–';
  $('usage').innerHTML = [[thisMonth, 'Files this month'], [sessions.length, 'Saved sessions'], [profiles.length, 'Quality Profiles'], [avg, 'Avg score after']]
    .map(([v, l]) => `<div class="stat-card"><div class="stat-value">${esc(v)}</div><div class="stat-label">${esc(l)}</div></div>`).join('');

  $('profiles').innerHTML = profiles.length ? `<table><thead><tr><th>Name</th><th>Updated</th><th></th></tr></thead><tbody>${profiles.map(p =>
    `<tr><td><a href="/profiles/${esc(p.id)}">${esc(p.name)}</a><div class="hint">${esc(p.description || '')}</div></td><td>${fmtDay(p.updated_at)}</td>
     <td class="cell-actions"><a class="btn btn-ghost btn-sm" href="/profiles/${esc(p.id)}">Edit</a>
     <button class="btn btn-ghost btn-sm" data-dup="${esc(p.id)}" type="button">Duplicate</button>
     <button class="btn btn-ghost btn-sm" data-del="${esc(p.id)}" type="button">Delete</button></td></tr>`).join('')}</tbody></table>`
    : '<p class="empty-state">No profiles yet. Create one to check datasets against your own standard.</p>';
  document.querySelectorAll('[data-dup]').forEach(b => b.addEventListener('click', async () => { await api('/api/profiles/' + b.dataset.dup + '/duplicate', { method: 'POST', body: {} }); location.reload(); }));
  document.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => { if (confirm('Delete this profile? Past reports that used it are kept.')) { await api('/api/profiles/' + b.dataset.del, { method: 'DELETE' }); location.reload(); } }));

  const cls = s => s >= 85 ? 'badge-success' : s >= 60 ? 'badge-warning' : 'badge-danger';
  $('history').innerHTML = sessions.length ? `<table><thead><tr><th>Dataset</th><th>Date</th><th>Score</th><th>Profile</th><th>Issues</th><th>Status</th></tr></thead><tbody>${sessions.map(s =>
    `<tr><td><a href="/sessions/${esc(s.id)}">${esc(s.dataset_name)}</a></td><td>${fmtDate(s.created_at)}</td>
     <td><span class="badge ${cls(s.score_after)}">${esc(s.score_before)} → ${esc(s.score_after)}</span></td>
     <td>${esc(s.profile_name || '–')}${s.compliance != null ? ' (' + esc(s.compliance) + '%)' : ''}</td><td>${esc(s.issue_count)}</td><td>${esc(s.status)}</td></tr>`).join('')}</tbody></table>`
    : '<p class="empty-state">No sessions yet. Clean a file while signed in and it appears here.</p>';
})();
