/**
 * Visitor sessions: a random id, signed with SESSION_SECRET and kept in an
 * HttpOnly cookie. The id names both the visitor's chat agent and their
 * vehicle container, so holding a valid cookie is what grants access to them.
 */

export const SESSION_COOKIE = "peykan_session";
export const SESSION_TTL_S = 2 * 60 * 60;

const encoder = new TextEncoder();

async function hmac(secret: string, value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(value));
  return btoa(String.fromCharCode(...new Uint8Array(sig)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** `<id>.<expiry>.<signature>` */
export async function issueSession(secret: string): Promise<{ id: string; cookie: string }> {
  const id = crypto.randomUUID();
  const expires = Math.floor(Date.now() / 1000) + SESSION_TTL_S;
  const payload = `${id}.${expires}`;
  const value = `${payload}.${await hmac(secret, payload)}`;
  const cookie = [
    `${SESSION_COOKIE}=${value}`,
    "Path=/",
    `Max-Age=${SESSION_TTL_S}`,
    "HttpOnly",
    "Secure",
    "SameSite=Lax"
  ].join("; ");
  return { id, cookie };
}

/** The session id from a request's cookie, or null if missing, forged or expired. */
export async function readSession(request: Request, secret: string): Promise<string | null> {
  const header = request.headers.get("Cookie") ?? "";
  const match = header
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${SESSION_COOKIE}=`));
  if (!match) return null;
  const [id, expires, signature] = match.slice(SESSION_COOKIE.length + 1).split(".");
  if (!id || !expires || !signature) return null;
  if (Number(expires) < Date.now() / 1000) return null;
  const expected = await hmac(secret, `${id}.${expires}`);
  return timingSafeEqual(signature, expected) ? id : null;
}
