// Where the frontend sends its API calls.
//
// Leave this as "" when the frontend and backend are served from the
// SAME place (e.g. running `python app.py` locally, or a single
// Render/Railway deploy that serves both static/ and the API).
//
// Set it to the backend's URL when the frontend is hosted separately
// from the backend — e.g. this frontend on Netlify, backend on
// Render/Railway. Example:
//
//   window.OMIXA_API_BASE = "https://omixa-backend.onrender.com";
//
window.OMIXA_API_BASE = "";

// Auth is a server-side session cookie, set automatically — there is
// nothing to configure here and no key or secret is ever read from
// this file. See the backend README for cross-origin deploy notes.
