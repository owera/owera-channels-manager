import { Link } from "react-router-dom";
import { useOnboarding } from "../api";
import { Dot, Empty, SectionLabel } from "../ui";

export default function Onboarding() {
  const { data, isLoading } = useOnboarding();
  if (isLoading) return <div className="p-8 label">loading checklist…</div>;
  if (!data) return <Empty>Could not load onboarding status.</Empty>;

  return (
    <div className="p-4 md:p-8 max-w-3xl">
      <div className="label mb-2">// early access</div>
      <h1 className="font-display font-extrabold text-3xl text-fog-50 mb-2">Onboarding</h1>
      <p className="text-fog-400 text-sm mb-8 max-w-xl">
        Workspace <span className="font-mono text-fog-200">{data.workspace_slug}</span>
        {data.onboarding_complete ? " — checklist complete." : " — finish the steps below to go live."}
      </p>

      <SectionLabel>// checklist</SectionLabel>
      <ul className="mt-4 space-y-3">
        {data.steps.map((s) => (
          <li key={s.id} className="panel px-4 py-3 flex items-center gap-3">
            <Dot hex={s.done ? "#c9f24e" : "#6c7681"} pulse={s.done} />
            <span className={s.done ? "text-fog-100" : "text-fog-400"}>{s.label}</span>
          </li>
        ))}
      </ul>

      <div className="mt-8 flex gap-3">
        <Link to="/channels" className="btn btn-signal">Open channels</Link>
        <Link to="/" className="btn btn-ghost">Overview</Link>
      </div>
    </div>
  );
}
