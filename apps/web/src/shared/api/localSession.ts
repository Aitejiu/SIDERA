const CSRF_COOKIE = "sidera_csrf=";
const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function csrfCookie(): string | null {
  if (typeof document === "undefined") return null;
  const values = document.cookie.split(";").map((part) => part.trim())
    .filter((part) => part.startsWith(CSRF_COOKIE));
  if (values.length !== 1) return null;
  try {
    const value = decodeURIComponent(values[0]!.slice(CSRF_COOKIE.length));
    return value.length > 0 ? value : null;
  } catch {
    return null;
  }
}

export function localSessionRequest(init: RequestInit = {}): RequestInit {
  const headers = new Headers(init.headers);
  const csrf = WRITE_METHODS.has((init.method ?? "GET").toUpperCase()) ? csrfCookie() : null;
  if (csrf === null) headers.delete("X-Sidera-CSRF");
  else headers.set("X-Sidera-CSRF", csrf);
  return { ...init, credentials: "same-origin", headers };
}
