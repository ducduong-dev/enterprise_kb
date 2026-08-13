/**
 * Keycloak OIDC sign-in (authorization code + PKCE).
 *
 * The portal is a public client: it holds no secret and no authority. The access token it
 * receives is what portal-api verifies, and the groups inside it are what the FilterBuilder
 * turns into an ACL — nothing here decides access, it only proves identity.
 */
import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";

/**
 * Relative by default (`/realms/kb`), resolved against wherever this page is being served
 * from. That is what lets the same bundle work on localhost, on a LAN address and through a
 * forwarded port without a rebuild — the alternative bakes one hostname in and fails with
 * "Failed to fetch" everywhere else.
 */
const configuredIssuer = import.meta.env.VITE_OIDC_ISSUER ?? "/realms/kb";
const issuer = new URL(configuredIssuer, window.location.origin).toString().replace(/\/$/, "");
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID ?? "kb-portal";

/** Dev escape hatch: a token pasted into localStorage, used when no issuer is reachable. */
const DEV_TOKEN_KEY = "kb.dev.token";

const manager = new UserManager({
  authority: issuer,
  client_id: clientId,
  redirect_uri: `${window.location.origin}/callback`,
  post_logout_redirect_uri: window.location.origin,
  response_type: "code",
  scope: "openid profile email",
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  automaticSilentRenew: true,
});

export async function getUser(): Promise<User | null> {
  try {
    return await manager.getUser();
  } catch {
    return null;
  }
}

export async function getAccessToken(): Promise<string | null> {
  const devToken = window.localStorage.getItem(DEV_TOKEN_KEY);
  if (devToken) return devToken;
  const user = await getUser();
  if (!user || user.expired) return null;
  return user.access_token;
}

export async function signIn(): Promise<void> {
  await manager.signinRedirect();
}

export async function completeSignIn(): Promise<User> {
  return manager.signinRedirectCallback();
}

export async function signOut(): Promise<void> {
  window.localStorage.removeItem(DEV_TOKEN_KEY);
  await manager.signoutRedirect();
}

export function displayName(user: User | null): string {
  const profile = user?.profile as { preferred_username?: string; name?: string } | undefined;
  return profile?.preferred_username ?? profile?.name ?? "chưa đăng nhập";
}
