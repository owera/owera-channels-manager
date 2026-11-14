import { useFeatures, useAuthMe } from "./api";
import Login from "./pages/Login";
import App from "./App";

/** When SPA Google login is on, require session before the app shell loads. */
export default function AuthGate() {
  const { data: features, isLoading: fLoading } = useFeatures();
  const spaLogin = features?.spa_google_login === true;
  const { data: me, isLoading: mLoading, isError, error } = useAuthMe(spaLogin);

  if (fLoading || (spaLogin && mLoading)) {
    return (
      <div className="min-h-dvh flex items-center justify-center text-fog-400 font-mono text-xs uppercase tracking-widest">
        Loading…
      </div>
    );
  }

  if (spaLogin && (isError || !me)) {
    const code = error?.message === "login_ambiguous_owner"
      ? "login_ambiguous_owner" : new URLSearchParams(window.location.search).get("login_error");
    return <Login errorCode={code} />;
  }

  return <App spaLogin={spaLogin} userEmail={me?.email} />;
}
