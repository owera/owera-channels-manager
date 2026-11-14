import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Channels from "./pages/Channels";
import Board from "./pages/Board";
import Review from "./pages/Review";
import Profiles from "./pages/Profiles";
import Settings from "./pages/Settings";
import Onboarding from "./pages/Onboarding";
import Trends from "./pages/Trends";
import { HeldToasts, PolicyLinks } from "./ui";

const NAV = [
  { to: "/", label: "Overview", short: "Home", end: true, code: "00" },
  { to: "/board", label: "Review queue", short: "Queue", code: "01" },
  { to: "/channels", label: "Channels", short: "Channels", code: "02" },
  { to: "/onboarding", label: "Onboarding", short: "Start", code: "06" },
  { to: "/trends", label: "Trends", short: "Trends", code: "03" },
  { to: "/profiles", label: "Render Profiles", short: "Profiles", code: "04" },
  { to: "/settings", label: "Settings", short: "Config", code: "05" },
];

function SignOut({ email }: { email?: string }) {
  const logout = async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    window.location.href = "/";
  };
  return (
    <div className="px-5 py-4 border-t border-ink-line mt-auto">
      {email && (
        <div className="text-[10px] text-fog-500 truncate mb-2 font-mono" title={email}>
          {email}
        </div>
      )}
      <button type="button" onClick={() => void logout()} className="btn w-full text-xs">
        Sign out
      </button>
    </div>
  );
}

function Sidebar({ spaLogin, userEmail }: { spaLogin?: boolean; userEmail?: string }) {
  return (
    <aside className="hidden md:flex w-60 shrink-0 border-r border-ink-line bg-ink-800/60 flex-col">
      <div className="px-5 pt-6 pb-5 border-b border-ink-line">
        <div className="flex items-center gap-2">
          <span className="w-2.5 h-2.5 bg-signal rounded-sm shadow-glow" />
          <span className="font-display font-extrabold text-fog-50 text-base tracking-tight leading-tight">
            Owera <span className="text-signal">Channels Manager</span>
          </span>
        </div>
        <div className="text-[10px] text-fog-400 mt-1.5 pl-[18px] tracking-wide">YouTube channel studio</div>
      </div>

      <nav className="flex-1 py-4">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) =>
              `group flex items-center gap-3 px-5 py-2.5 font-mono text-xs uppercase tracking-wider transition-colors ${
                isActive
                  ? "text-signal bg-signal/5 border-l-2 border-signal"
                  : "text-fog-300 hover:text-fog-50 border-l-2 border-transparent"
              }`
            }
          >
            <span className="text-fog-400 group-hover:text-fog-200 tabular-nums">{n.code}</span>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <PolicyLinks className={`px-5 py-3 ${spaLogin ? "" : "mt-auto border-t border-ink-line"}`} />
      {spaLogin && <SignOut email={userEmail} />}
    </aside>
  );
}

function BottomNav() {
  return (
    <nav className="fixed bottom-0 left-0 right-0 md:hidden z-40 bg-ink-800/95 backdrop-blur-sm border-t border-ink-line flex">
      {NAV.map((n) => (
        <NavLink
          key={n.to}
          to={n.to}
          end={n.end}
          className={({ isActive }) =>
            `flex-1 flex flex-col items-center pt-2 pb-3 gap-0.5 font-mono text-[8px] uppercase tracking-widest transition-colors ${
              isActive ? "text-signal" : "text-fog-400"
            }`
          }
        >
          <span className="font-display font-extrabold text-[14px] leading-none tabular-nums">{n.code}</span>
          <span>{n.short}</span>
        </NavLink>
      ))}
    </nav>
  );
}

export default function App({ spaLogin, userEmail }: { spaLogin?: boolean; userEmail?: string }) {
  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar spaLogin={spaLogin} userEmail={userEmail} />
      <main className="flex-1 overflow-y-auto pb-[56px] md:pb-0">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/board" element={<Board />} />
          <Route path="/board/:channelId" element={<Board />} />
          <Route path="/channels" element={<Channels />} />
          <Route path="/onboarding" element={<Onboarding />} />
          <Route path="/trends" element={<Trends />} />
          <Route path="/review/:videoId" element={<Review />} />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
        <footer className="md:hidden px-4 py-6 border-t border-ink-line mt-8">
          <PolicyLinks />
        </footer>
      </main>
      <HeldToasts />
      <BottomNav />
    </div>
  );
}
