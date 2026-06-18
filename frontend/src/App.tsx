import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Channels from "./pages/Channels";
import Board from "./pages/Board";
import Review from "./pages/Review";
import Profiles from "./pages/Profiles";
import Settings from "./pages/Settings";
import Trends from "./pages/Trends";

const NAV = [
  { to: "/", label: "Overview", end: true, code: "00" },
  { to: "/board", label: "Queue Board", code: "01" },
  { to: "/channels", label: "Channels", code: "02" },
  { to: "/trends", label: "Trends", code: "03" },
  { to: "/profiles", label: "Render Profiles", code: "04" },
  { to: "/settings", label: "Settings", code: "05" },
];

function Sidebar() {
  return (
    <aside className="w-60 shrink-0 border-r border-ink-line bg-ink-800/60 flex flex-col">
      <div className="px-5 pt-6 pb-5 border-b border-ink-line">
        <div className="flex items-center gap-2">
          <span className="w-2.5 h-2.5 bg-signal rounded-sm shadow-glow" />
          <span className="font-display font-extrabold text-fog-50 text-base tracking-tight leading-tight">
            Owera <span className="text-signal">Channels Manager</span>
          </span>
        </div>
        <div className="label mt-1.5 pl-[18px]">channel ops console</div>
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
    </aside>
  );
}

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/board" element={<Board />} />
          <Route path="/board/:channelId" element={<Board />} />
          <Route path="/channels" element={<Channels />} />
          <Route path="/trends" element={<Trends />} />
          <Route path="/review/:videoId" element={<Review />} />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
