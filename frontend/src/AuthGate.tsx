import { useFeatures, useAuthMe } from "./api";
import Login from "./pages/Login";
import App from "./App";

/** When SPA Google login is on, require session before the app shell loads. */
export default function AuthGate() {
  const { data: features, isLoading: fLoading } = useFeatures();
  const spaLogin = features?.spa_google_login === true;
  const { data: me, isLoading: mLoading, isError } = useAuthMe(spaLogin);

  if (fLoading || (spaLogin && mLoading)) {
    return (
      <div className="min-h-dvh flex items-center justify-center text-fog-400 font-mono text-xs uppercase tracking-widest">
        Loading…
      </div>
    );
  }

  if (spaLogin && (isError || !me)) {
    return <Login />;
  }

  return <App spaLogin={spaLogin} userEmail={me?.email} />;
}
