// Calls to Wardlock's /admin/api. The password lives in sessionStorage (this tab only) and is
// sent on every call; the server never asks the browser to remember it, which blocks CSRF.
// User "admin" is the merchant; "viewer" is the public read-only demo (when the server allows it).

const KEY = "wardlock.console.password";
const USER_KEY = "wardlock.console.user";
export const VIEWER = "viewer";

export class AuthError extends Error {}

export function getPassword() {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function getUser() {
  try {
    return sessionStorage.getItem(USER_KEY) || "admin";
  } catch {
    return "admin";
  }
}

export function setPassword(password, user = "admin") {
  try {
    if (password) {
      sessionStorage.setItem(KEY, password);
      sessionStorage.setItem(USER_KEY, user);
    } else {
      sessionStorage.removeItem(KEY);
      sessionStorage.removeItem(USER_KEY);
    }
  } catch {
    // storage blocked: the password is then asked for again after a reload
  }
}

export function isViewer() {
  return getUser() === VIEWER;
}

// What the sign-in screen may offer (no auth needed). Returns null when the server didn't answer
// properly, which happens for a minute while a sleeping free-plan instance wakes up.
export async function consoleConfig() {
  try {
    const response = await fetch("/admin/public/config");
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

function basicAuth(password) {
  // btoa() only takes Latin-1, so encode the UTF-8 bytes first.
  const bytes = new TextEncoder().encode(`${getUser()}:${password}`);
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
