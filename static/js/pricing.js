(function () {
  const msg = document.getElementById('priceMsg');
  document.querySelectorAll('[data-plan]').forEach(btn => btn.addEventListener('click', async () => {
    const plan = btn.getAttribute('data-plan');
    const m = await Omixa.me();
    if (!m.user) { window.location.href = '/login?next=' + encodeURIComponent('/pricing') + '&plan=' + plan; return; }
    btn.disabled = true; msg.textContent = 'Opening secure checkout…';
    const r = await Omixa.api('/api/billing/checkout', { method: 'POST', body: { plan } });
    if (r.ok && r.json.authorization_url) { window.location.href = r.json.authorization_url; return; }
    msg.textContent = r.json.error || 'Could not start checkout.';
    btn.disabled = false;
  }));
  const plan = new URLSearchParams(window.location.search).get('plan');
  if (plan) { const b = document.querySelector('[data-plan="' + plan + '"]'); if (b) b.focus(); }
})();
