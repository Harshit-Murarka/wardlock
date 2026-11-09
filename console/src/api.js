// Calls to Wardlock's /admin/api. The password lives in sessionStorage (this tab only) and is
// sent on every call; the server never asks the browser to remember it, which blocks CSRF.

const KEY = "wardlock.console.password";

export class AuthError extends Error {}

export function getPassword() {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setPassword(password) {
  try {
    if (password) sessionStorage.setItem(KEY, password);
    else sessionStorage.removeItem(KEY);
  } catch {
    // storage blocked: the password is then asked for again after a reload
  }
}

function basicAuth(password) {
  // btoa() only takes Latin-1, so encode the UTF-8 bytes first.
  const bytes = new TextEncoder().encode(`admin:${password}`);
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return "Basic " + btoa(binary);
}

export async function api(path, { method = "GET" } = {}) {
  const response = await fetch(`/admin/api${path}`, {
    method,
    headers: { Authorization: basicAuth(getPassword() || "") },
  });
  if (response.status === 401) {
    setPassword(null);
    throw new AuthError("Wrong console password.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

export function when(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}
