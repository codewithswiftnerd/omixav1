// Firebase Web SDK sign-in. The browser gets a short-lived ID token from Firebase and hands it to
// our server once; the server verifies it and starts the session. Nothing secret is stored here.
import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js';
import {
  getAuth, signInWithEmailAndPassword, createUserWithEmailAndPassword, signInWithPopup,
  GoogleAuthProvider, sendPasswordResetEmail, updateProfile,
} from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js';

const $ = id => document.getElementById(id);
const msg = $('loginMsg');
let cfg = null;
try { cfg = JSON.parse(document.getElementById('fb-config').textContent); } catch (e) { cfg = null; }
if (!cfg || !cfg.apiKey) {
  msg.textContent = 'Sign-in is not configured on this server yet.';
  document.querySelectorAll('#authForm input, #authForm button, #googleBtn').forEach(el => el.disabled = true);
} else {
  const auth = getAuth(initializeApp(cfg));
  let mode = 'signin';
  const params = new URLSearchParams(window.location.search);
  const next = (() => { const n = params.get('next') || '/dashboard'; return n.startsWith('/') && !n.startsWith('//') ? n : '/dashboard'; })();

  async function finish(user) {
    const token = await user.getIdToken();
    const r = await Omixa.api('/api/auth/session', { method: 'POST', body: { idToken: token } });
    if (!r.ok) { msg.textContent = r.json.error || 'Could not sign you in.'; return; }
    window.location.href = next;
  }
  function friendly(e) {
    const c = (e && e.code) || '';
    if (c.includes('wrong-password') || c.includes('invalid-credential') || c.includes('user-not-found')) return 'Email or password is incorrect.';
    if (c.includes('email-already-in-use')) return 'An account with this email already exists. Try signing in.';
    if (c.includes('weak-password')) return 'Choose a password of at least 6 characters.';
    if (c.includes('popup-closed')) return 'Sign-in was cancelled.';
    if (c.includes('too-many-requests')) return 'Too many attempts. Please wait a moment.';
    return 'Could not sign you in. Please try again.';
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
    e.preventDefault(); msg.textContent = '';
    const email = $('email').value.trim(), pw = $('password').value;
    try {
      let cred;
      if (mode === 'signin') cred = await signInWithEmailAndPassword(auth, email, pw);
      else {
        cred = await createUserWithEmailAndPassword(auth, email, pw);
        const name = $('displayName').value.trim();
        if (name) await updateProfile(cred.user, { displayName: name });
      }
      await finish(cred.user);
    } catch (err) { msg.textContent = friendly(err); }
  });
  $('googleBtn').addEventListener('click', async () => {
    msg.textContent = '';
    try { const cred = await signInWithPopup(auth, new GoogleAuthProvider()); await finish(cred.user); }
    catch (err) { msg.textContent = friendly(err); }
  });
  $('resetLink').addEventListener('click', async e => {
    e.preventDefault();
    const email = $('email').value.trim();
    if (!email) { msg.textContent = 'Enter your email above first.'; return; }
    try { await sendPasswordResetEmail(auth, email); } catch (err) { /* same message either way */ }
    msg.textContent = 'If that email has an account, a reset link is on its way.';
  });
}
