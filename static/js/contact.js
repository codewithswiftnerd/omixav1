// Contact form. Validates in the browser for fast feedback; the server validates everything again.
// No secrets here: the message goes to /api/contact and the server sends the email.
(function () {
  const form = document.getElementById('contactForm');
  if (!form) return;
  const $ = id => document.getElementById(id);
  const status = $('cStatus'), btn = $('cSubmit'), msg = $('cMessage'), count = $('cCount');
  const EMAIL = /^[^\s@,;<>"()\[\]\\]+@[^\s@,;<>"()\[\]\\]+\.[^\s@,;<>"()\[\]\\]+$/;

  function show(text, kind) {
    status.textContent = text;
    status.className = 'alert alert-' + kind;
    status.classList.remove('hidden');
  }
  function clearErrors() {
    status.classList.add('hidden');
    form.querySelectorAll('.field-error').forEach(el => el.remove());
    form.querySelectorAll('[aria-invalid]').forEach(el => el.removeAttribute('aria-invalid'));
  }
  function fieldError(input, text) {
    input.setAttribute('aria-invalid', 'true');
    const p = document.createElement('div');
    p.className = 'hint field-error';
    p.style.color = 'var(--danger)';
    p.textContent = text;
    input.closest('.field').appendChild(p);
  }
  function validate(v) {
    const e = {};
    if (!v.name) e.name = 'Please enter your name.';
    if (!EMAIL.test(v.email)) e.email = 'Please enter a valid email address.';
    if (v.subject.length < 3) e.subject = 'Please add a short subject.';
    if (v.message.length < 10) e.message = 'Please write a little more (at least 10 characters).';
    return e;
  }
  const inputs = { name: $('cName'), email: $('cEmail'), subject: $('cSubject'), message: msg };

  msg.addEventListener('input', () => { count.textContent = msg.value.length; });

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    if (btn.getAttribute('aria-busy') === 'true') return;
    clearErrors();
    const v = {
      name: $('cName').value.trim(), email: $('cEmail').value.trim(),
      subject: $('cSubject').value.trim(), message: msg.value.trim(),
    };
    const errs = validate(v);
    const keys = Object.keys(errs);
    if (keys.length) {
      keys.forEach(k => fieldError(inputs[k], errs[k]));
      inputs[keys[0]].focus();
      return;
    }
    btn.setAttribute('aria-busy', 'true'); btn.disabled = true;
    try {
      const r = await Omixa.api('/api/contact', { method: 'POST', body: Object.assign(v, { website: $('cWebsite').value, token: $('cToken').value }) });
      if (r.ok) {
        form.reset(); count.textContent = '0';
        show(r.json.message || 'Thanks, your message has been sent.', 'success');
      } else if (r.status === 422 && r.json.fields) {
        Object.keys(r.json.fields).forEach(k => inputs[k] && fieldError(inputs[k], r.json.fields[k]));
        show(r.json.error || 'Please check the highlighted fields.', 'error');
      } else {
        show(r.json.error || 'Something went wrong. Please try again, or email us directly.', 'error');
      }
    } catch (err) {
      show('We could not reach the server. Check your connection and try again.', 'error');
    } finally {
      btn.removeAttribute('aria-busy'); btn.disabled = false;
    }
  });
})();
