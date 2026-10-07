(async function () {
  const slot = document.getElementById('navAuth');
  if (!slot || !window.Omixa) return;
  try {
    const m = await Omixa.me();
    if (m.user) {
      slot.textContent = 'Dashboard';
      slot.href = '/dashboard';
    } else if (m.accounts_available === false) {
      slot.remove();
    }
  } catch (e) { /* Free tool keeps working without accounts */ }
})();
