import { useHealth, useMut, useSettings } from "../api";
import { Dot, Field, SectionLabel, Toggle } from "../ui";

export default function Settings() {
  const { data: s } = useSettings();
  const { data: health } = useHealth();
  const m = useMut();
  if (!s) return <div className="p-8 font-mono text-fog-400">loading…</div>;

  const patch = (body: any) => m.updateSettings.mutate(body);

  return (
    <div className="p-8 max-w-[760px]">
      <header className="mb-8">
        <div className="label mb-2">// settings</div>
        <h1 className="font-display font-extrabold text-4xl text-fog-50 tracking-tight">Settings</h1>
      </header>

      <div className="panel p-6 mb-6">
        <SectionLabel>// engine</SectionLabel>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 font-mono text-sm">
            <Dot hex={health?.mpt_reachable ? "#c9f24e" : "#f7768e"} pulse={health?.mpt_reachable} />
            <span className={health?.mpt_reachable ? "text-fog-100" : "text-[#f7768e]"}>
              MoneyPrinterTurbo {health?.mpt_reachable ? "online" : "offline"}
            </span>
          </div>
          <span className="font-mono text-xs text-fog-400">{s.mpt_base_url}</span>
        </div>
      </div>

      <div className="panel p-6 mb-6">
        <SectionLabel>// scheduler</SectionLabel>
        <div className="flex items-center justify-between mb-5">
          <div><div className="text-sm text-fog-100">Pause all automation</div><div className="label">stops render + publish loops globally</div></div>
          <Toggle on={s.scheduler_paused} onChange={(v) => patch({ scheduler_paused: v })} />
        </div>
        <div className="grid grid-cols-2 gap-5">
          <Field label="render concurrency" hint="parallel renders driven into MPT">
            <input type="number" className="input" defaultValue={s.render_concurrency} min={1} max={4}
              onBlur={(e) => patch({ render_concurrency: Number(e.target.value) })} />
          </Field>
          <Field label="publish drip (minutes)" hint="min spacing between uploads per channel">
            <input type="number" className="input" defaultValue={s.publish_drip_minutes}
              onBlur={(e) => patch({ publish_drip_minutes: Number(e.target.value) })} />
          </Field>
        </div>
      </div>

      <div className="panel p-6">
        <SectionLabel>// topic autogen</SectionLabel>
        <div className="flex items-center justify-between mb-5">
          <div><div className="text-sm text-fog-100">Auto-refill topic queues</div><div className="label">LLM generates new topics when a channel runs low</div></div>
          <Toggle on={s.topic_autogen_enabled} onChange={(v) => patch({ topic_autogen_enabled: v })} />
        </div>
        <Field label="min pending threshold" hint="generate more when fewer than this many topics remain queued">
          <input type="number" className="input" defaultValue={s.topic_autogen_min_pending}
            onBlur={(e) => patch({ topic_autogen_min_pending: Number(e.target.value) })} />
        </Field>
      </div>
    </div>
  );
}
