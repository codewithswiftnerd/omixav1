// Firebase Web SDK sign-in. The browser gets a short-lived ID token from Firebase and hands it to
// our server once; the server verifies it and starts the session. Nothing secret is stored here.
import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js';
import {
  getAuth, signInWithEmailAndPassword, createUserWithEmailAndPassword, signInWithPopup,
  GoogleAuthProvider, sendPasswordResetEmail, updateProfile,
} from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js';

const $ = id => document.getElementById(id);
const msg = $('loginMsg');
function say(text, kind) { msg.textContent = text; msg.className = 'hint login-msg' + (kind ? ' ' + kind : ''); }
let cfg = null;
try { cfg = JSON.parse(document.getElementById('fb-config').textContent); } catch (e) { cfg = null; }
if (!cfg || !cfg.apiKey) {
  say('Sign-in is not set up on this server yet (missing Firebase web config).', 'error');
  document.querySelectorAll('#authForm input, #authForm button, #googleBtn').forEach(el => el.disabled = true);
} else {
  const auth = getAuth(initializeApp(cfg));
  let mode = 'signin';
  const params = new URLSearchParams(window.location.search);
  const next = (() => { const n = params.get('next') || '/dashboard'; return n.startsWith('/') && !n.startsWith('//') ? n : '/dashboard'; })();

  async function finish(user) {
    const token = await user.getIdToken();
    const r = await Omixa.api('/api/auth/session', { method: 'POST', body: { idToken: token } });
    if (!r.ok) { say(r.json.error || (r.status >= 500 ? 'Signed in with Google, but our server could not start your session. Please try again shortly.' : 'We could not verify your sign-in. Please try again.'), 'error'); return; }
    window.location.href = next;
  }
  function friendly(e) {
    const c = (e && e.code) || '';
    if (c.includes('wrong-password') || c.includes('invalid-credential') || c.includes('user-not-found')) return 'That email and password do not match. Check them and try again, or use "Forgot password?".';
    if (c.includes('invalid-email')) return 'That email address does not look right. Check for typos.';
    if (c.includes('missing-password')) return 'Enter your password.';
    if (c.includes('email-already-in-use')) return 'An account with this email already exists. Sign in instead, or use "Forgot password?".';
    if (c.includes('weak-password')) return 'Choose a password of at least 6 characters.';
    if (c.includes('user-disabled')) return 'This account has been disabled. Contact support if you think this is a mistake.';
    if (c.includes('account-exists-with-different-credential')) return 'This email is already registered with a different sign-in method. Sign in with your email and password instead.';
    if (c.includes('popup-closed') || c.includes('cancelled-popup-request')) return 'The Google window was closed before sign-in finished. Tap the button to try again.';
    if (c.includes('popup-blocked')) return 'Your browser blocked the Google sign-in window. Allow pop-ups for this site, then try again.';
    if (c.includes('unauthorized-domain')) return 'Google sign-in is not enabled for this website address yet. Add this domain under Firebase > Authentication > Settings > Authorized domains.';
    if (c.includes('operation-not-allowed')) return 'This sign-in method is not turned on yet. Enable it under Firebase > Authentication > Sign-in method.';
    if (c.includes('network-request-failed')) return 'Network problem. Check your connection and try again.';
    if (c.includes('too-many-requests')) return 'Too many attempts. Please wait a few minutes and try again.';
    if (c.includes('internal-error') || c.includes('web-storage-unsupported')) return 'Your browser is blocking something sign-in needs (cookies or storage). Turn off private mode or content blockers for this site, or sign in with email.';
    return 'Could not sign you in' + (c ? ' (' + c.replace('auth/', '') + ')' : '') + '. Please try again.';
  }
  function setMode(m) {
    mode = m;
    $('loginTitle').textContent = m === 'signin' ? 'Sign in' : 'Create your account';
    $('submitBtn').textContent = m === 'signin' ? 'Sign in' : 'Create account';
    $('nameField').classList.toggle('hidden', m === 'signin');
    $('switchLink').textContent = m === 'signin' ? 'New to Omixa? Create an account' : 'Have an account? Sign in';
  }
  $('switchLink').addEventListener('click', e => { e.preventDefault(); setMode(mode === 'signin' ? 'signup' : 'signin'); });
  $('authForm').addEventListener('submit', async e => {
    e.preventDefault(); say('');
    const email = $('email').value.trim(), pw = $('password').value;
    const submitBtn = $('submitBtn');
    if (submitBtn.getAttribute('aria-busy') === 'true') return;
    submitBtn.setAttribute('aria-busy', 'true');
    try {
      let cred;
      if (mode === 'signin') cred = await signInWithEmailAndPassword(auth, email, pw);
      else {
        cred = await createUserWithEmailAndPassword(auth, email, pw);
        const name = $('displayName').value.trim();
        if (name) await updateProfile(cred.user, { displayName: name });
      }
      await finish(cred.user);
    } catch (err) { say(friendly(err), 'error'); }
    finally { submitBtn.removeAttribute('aria-busy'); }
  });
  $('googleBtn').addEventListener('click', async () => {
    say('');
    const gb = $('googleBtn');
    if (gb.getAttribute('aria-busy') === 'true') return;
    gb.setAttribute('aria-busy', 'true');
    try {
      const provider = new GoogleAuthProvider();
      provider.setCustomParameters({ prompt: 'select_account' });
      const cred = await signInWithPopup(auth, provider);
      await finish(cred.user);
    } catch (err) { say(friendly(err), 'error'); }
    finally { gb.removeAttribute('aria-busy'); }
  });
  $('resetLink').addEventListener('click', async e => {
    e.preventDefault();
    const email = $('email').value.trim();
    if (!email) { say('Enter your email above first, then tap "Forgot password?".', 'error'); return; }
    // Preferred path: our server sends a branded email from support@getomixa.com (Firebase still creates the
    // single-use, expiring link). If the server cannot send email, fall back to Firebase's own reset email.
    const r = await Omixa.api('/api/auth/password-reset', { method: 'POST', body: { email } }).catch(() => ({ ok: false, status: 0, json: {} }));
    if (r.status === 400) { say(r.json.error || 'That email address does not look right.', 'error'); return; }
    if (r.status === 429) { say(r.json.error || 'Too many reset requests. Please wait a while and try again.', 'error'); return; }
    if (!r.ok) {
      try { await sendPasswordResetEmail(auth, email); } catch (err) { /* same message either way */ }
    }
    say('If that email has an account, a reset link is on its way. Check spam too.', 'ok');
  });
}
