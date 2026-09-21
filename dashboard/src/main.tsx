import { FormEvent, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Policy = {
  live_actions_enabled: boolean;
  daily_application_limit: number;
  daily_outreach_limit: number;
};
type Job = { id: string; company: string; title: string; location?: string; status: string; url: string; first_seen_at: string };
type Source = { id: string; provider: string; name: string; board_url: string; enabled: boolean; last_polled_at?: string };
type Agent = { id: string; domain: string; name: string; status: string; generation: number; fingerprint: string; mutation_summary?: string };
type Dashboard = {
  now: string;
  live_policy: Policy;
  counts: Record<string, number>;
  agent_population: Record<string, Record<string, number>>;
  recent_jobs: Job[];
  recent_escalations: Array<{ id: string; category: string; question: string; created_at: string }>;
};

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`/api${path}`, { headers: { "Content-Type": "application/json", ...(init?.headers || {}) }, ...init });
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `${response.status} ${response.statusText}`);
  return response.json();
};

function App() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [message, setMessage] = useState<string>("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [summary, sourceRows, agentRows] = await Promise.all([api<Dashboard>("/dashboard"), api<Source[]>("/sources"), api<Agent[]>("/agents")]);
      setDashboard(summary);
      setSources(sourceRows);
      setAgents(agentRows);
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to reach control plane");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  async function addSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await api("/sources", { method: "POST", body: JSON.stringify({ provider: data.get("provider"), name: data.get("name"), board_url: data.get("board_url"), enabled: true }) });
      event.currentTarget.reset();
      await refresh();
      setMessage("Source added. Poll it to ingest public jobs.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to add source"); }
  }

  async function poll(id: string) {
    try {
      const result = await api<{ fetched: number; created: number; updated: number }>(`/sources/${id}/poll`, { method: "POST" });
      setMessage(`Poll complete: ${result.fetched} fetched, ${result.created} new, ${result.updated} refreshed.`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Poll failed"); }
  }

  async function updatePolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await api("/policy", {
        method: "PUT",
        body: JSON.stringify({
          live_actions_enabled: data.get("live") === "on",
          daily_application_limit: Number(data.get("applications")),
          daily_outreach_limit: Number(data.get("outreach")),
        }),
      });
      setMessage("Live-action policy saved.");
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to update policy"); }
  }

  async function evolve(domain: string) {
    try {
      const spec = await api<Agent>("/evolution/run", { method: "POST", body: JSON.stringify({ domain, minimum_audits: 3 }) });
      setMessage(`Created challenger ${spec.name}. It remains isolated until audited and promoted.`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Evolution run failed"); }
  }

  if (loading) return <main className="loading">Starting Career System…</main>;
  const policy = dashboard?.live_policy ?? { live_actions_enabled: false, daily_application_limit: 0, daily_outreach_limit: 0 };

  return <main>
    <header>
      <div><p className="eyebrow">AUTONOMOUS CAREER OS / V1</p><h1>Career System</h1></div>
      <div className={`safety ${policy.live_actions_enabled ? "armed" : "safe"}`}>
        <span className="dot" /> {policy.live_actions_enabled ? "Live actions enabled" : "Safe mode — no live actions"}
      </div>
    </header>
    {message && <div className="notice">{message}<button onClick={() => setMessage("")}>×</button></div>}

    <section className="metric-grid">
      <Metric value={dashboard?.counts.sources ?? 0} label="ATS sources" />
      <Metric value={dashboard?.counts.jobs ?? 0} label="canonical jobs" />
      <Metric value={dashboard?.counts.applications ?? 0} label="applications" />
      <Metric value={dashboard?.counts.outreach_messages ?? 0} label="email outreach" />
      <Metric value={dashboard?.counts.open_escalations ?? 0} label="open escalations" emphasis />
    </section>

    <section className="two-column">
      <article className="card">
        <div className="section-head"><div><p className="eyebrow">DISCOVERY</p><h2>Direct ATS sources</h2></div></div>
        <form className="source-form" onSubmit={addSource}>
          <select name="provider" aria-label="Provider"><option value="greenhouse">Greenhouse</option><option value="lever">Lever</option></select>
          <input name="name" placeholder="Company name" required />
          <input name="board_url" type="url" placeholder="https://boards.greenhouse.io/company" required />
          <button>Add source</button>
        </form>
        <div className="source-list">
          {sources.length === 0 && <p className="empty">Add a public Greenhouse or Lever board to begin continuous discovery.</p>}
          {sources.map(source => <div className="source-row" key={source.id}><div><strong>{source.name}</strong><span>{source.provider} · {source.last_polled_at ? `last polled ${new Date(source.last_polled_at).toLocaleString()}` : "not polled"}</span></div><button className="secondary" onClick={() => void poll(source.id)}>Poll</button></div>)}
        </div>
      </article>

      <article className="card">
        <p className="eyebrow">GOVERNANCE</p><h2>Live action policy</h2>
        <form className="policy-form" onSubmit={updatePolicy}>
          <label className="toggle"><input name="live" type="checkbox" defaultChecked={policy.live_actions_enabled} /><span>Enable application submission and Gmail sending</span></label>
          <label>Daily application cap <input name="applications" type="number" min="0" max="100" defaultValue={policy.daily_application_limit} /></label>
          <label>Daily outreach cap <input name="outreach" type="number" min="0" max="100" defaultValue={policy.daily_outreach_limit} /></label>
          <button>Save policy</button>
        </form>
        <p className="hint">A zero cap blocks the matching external action even when live mode is on. Unknown answers and CAPTCHAs always escalate.</p>
      </article>
    </section>

    <section className="two-column">
      <article className="card wide">
        <div className="section-head"><div><p className="eyebrow">OBSERVABILITY</p><h2>Recently discovered jobs</h2></div><button className="secondary" onClick={() => void refresh()}>Refresh</button></div>
        <div className="jobs">
          {dashboard?.recent_jobs.length === 0 && <p className="empty">No jobs yet. Add and poll a source above.</p>}
          {dashboard?.recent_jobs.map(job => <a className="job" href={job.url} target="_blank" rel="noreferrer" key={job.id}><div><strong>{job.title}</strong><span>{job.company}{job.location ? ` · ${job.location}` : ""}</span></div><span className={`badge ${job.status}`}>{job.status}</span></a>)}
        </div>
      </article>

      <article className="card">
        <p className="eyebrow">HUMAN LOOP</p><h2>Escalations</h2>
        <div className="escalations">
          {dashboard?.recent_escalations.length === 0 && <p className="empty">No workflow is waiting for you.</p>}
          {dashboard?.recent_escalations.map(item => <div key={item.id}><span className="badge blocked">{item.category}</span><p>{item.question}</p></div>)}
        </div>
      </article>
    </section>

    <section className="card">
      <div className="section-head"><div><p className="eyebrow">EVOLUTION</p><h2>Champion / challenger populations</h2></div></div>
      <div className="population">
        {(["job_finder", "response_builder", "application"] as const).map(domain => <div className="population-group" key={domain}><div className="population-title"><h3>{domain.replaceAll("_", " ")}</h3><button className="secondary" onClick={() => void evolve(domain)}>Run GEPA mutation</button></div>{agents.filter(agent => agent.domain === domain).map(agent => <div className="agent" key={agent.id}><span className={`badge ${agent.status}`}>{agent.status}</span><div><strong>{agent.name}</strong><span>generation {agent.generation} · {agent.fingerprint.slice(0, 10)}</span></div></div>)}</div>)}
      </div>
      <p className="hint">Evolution requires at least three blinded audit scores on the current champion. New candidates never replace champions automatically.</p>
    </section>
  </main>;
}

function Metric({ value, label, emphasis = false }: { value: number; label: string; emphasis?: boolean }) {
  return <article className={`metric ${emphasis ? "emphasis" : ""}`}><strong>{value}</strong><span>{label}</span></article>;
}

createRoot(document.getElementById("root")!).render(<App />);

