import { Navigate, useLocation } from "react-router-dom";
import { useFeatures, useAuthMe, useWizard } from "./api";
import Login from "./pages/Login";
import App from "./App";

/** When SPA Google login is on, require session before the app shell loads.
 *
 * S2: after login, a workspace with 0 channels (or one that started the setup
 * wizard and hasn't finished it) is sent to the single onboarding wizard at
 * /welcome. Workspaces that already had channels never see it. Trends and
 * Settings stay hidden on that first login (until the first draft rendered). */
export default function AuthGate() {
  const { data: features, isLoading: fLoading } = useFeatures();
  const spaLogin = features?.spa_google_login === true;
  const { data: me, isLoading: mLoading, isError, error } = useAuthMe(spaLogin);
  const authed = !fLoading && (!spaLogin || (!!me && !isError));
  const { data: wizard, isLoading: wLoading } = useWizard(authed);
  const location = useLocation();

  if (fLoading || (spaLogin && mLoading) || (authed && wLoading)) {
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

  const wizardActive = wizard?.active === true;
  if (wizardActive && location.pathname !== "/welcome") {
    return <Navigate to="/welcome" replace />;
  }

  return <App spaLogin={spaLogin} userEmail={me?.email} firstLogin={wizard?.first_login === true} />;
}
