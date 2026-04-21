import { useEffect, useMemo, useState } from "react";
import { NavLink, Route, Routes, useNavigate, useParams } from "react-router-dom";
import {
  API_BASE,
  createRun,
  downloadArtifact,
  getProtocols,
  getRun,
  getRunLog,
  getRuns,
  login,
} from "./api";

function LoginPage({ onLogin, error }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await onLogin(username, password);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="centered">
      <form className="card login" onSubmit={submit}>
        <h2>Pfizer Unified Eval Dashboard</h2>
        <p className="muted">API: {API_BASE}</p>
        <label>Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} required />
        <label>Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error ? <p className="error">{error}</p> : null}
        <button disabled={busy} type="submit">
          {busy ? "Signing in..." : "Sign in"}
        </button>
      </form>
    </div>
  );
}

function Shell({ onLogout, children }) {
  return (
    <div>
      <header className="topbar">
        <h1>Unified Eval Dashboard</h1>
        <nav>
          <NavLink to="/">Runs</NavLink>
          <NavLink to="/compare">Compare</NavLink>
        </nav>
        <button className="ghost" onClick={onLogout}>
          Logout
        </button>
      </header>
      <main className="layout">{children}</main>
    </div>
  );
}

function RunsPage({ token }) {
  const [protocols, setProtocols] = useState([]);
  const [runs, setRuns] = useState([]);
  const [study, setStudy] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  async function refresh() {
    setLoading(true);
    setErr("");
    try {
      const [p, r] = await Promise.all([getProtocols(token), getRuns(token, study)]);
      setProtocols(p.protocols || []);
      setRuns(r || []);
      if (!study && p.protocols?.length) setStudy(p.protocols[0]);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, []); // eslint-disable-line

  async function onRun() {
    if (!study) return;
    setLoading(true);
    setErr("");
    try {
      const created = await createRun(study, token);
      await refresh();
      navigate(`/runs/${created.id}`);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="card">
      <h2>Run Manager</h2>
      <div className="row">
        <label>Protocol</label>
        <select value={study} onChange={(e) => setStudy(e.target.value)}>
          <option value="">Select protocol</option>
          {protocols.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <button onClick={onRun} disabled={!study || loading}>
          Start run
        </button>
        <button className="ghost" onClick={refresh} disabled={loading}>
          Refresh
        </button>
      </div>
      {err ? <p className="error">{err}</p> : null}
      <table>
        <thead>
          <tr>
            <th>Run ID</th>
            <th>Study</th>
            <th>Status</th>
            <th>Risk Mode</th>
            <th>Started</th>
            <th>Finished</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} onClick={() => navigate(`/runs/${r.id}`)} className="clickable">
              <td>{r.id.slice(0, 8)}</td>
              <td>{r.study_id}</td>
              <td>{r.status}</td>
              <td>{r.risk_mode}</td>
              <td>{r.started_at?.slice(0, 19) || "-"}</td>
              <td>{r.finished_at?.slice(0, 19) || "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunDetailPage({ token }) {
  const { runId } = useParams();
  const [run, setRun] = useState(null);
  const [logText, setLogText] = useState("");
  const [err, setErr] = useState("");

  async function refresh() {
    if (!runId) return;
    setErr("");
    try {
      const [r, lg] = await Promise.all([getRun(runId, token), getRunLog(runId, token)]);
      setRun(r);
      setLogText(lg.log || "");
    } catch (e) {
      setErr(String(e.message || e));
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 6000);
    return () => clearInterval(id);
  }, [runId]); // eslint-disable-line

  if (!run) return <div className="card">{err ? <p className="error">{err}</p> : "Loading..."}</div>;

  return (
    <div className="grid">
      <section className="card">
        <h2>Run {run.id}</h2>
        <p>
          <strong>Study:</strong> {run.study_id}
        </p>
        <p>
          <strong>Status:</strong> {run.status}
        </p>
        <p>
          <strong>Risk mode:</strong> {run.risk_mode}
        </p>
        <h3>Metrics</h3>
        <table>
          <thead>
            <tr>
              <th>Product</th>
              <th>Metric</th>
              <th>Value</th>
              <th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {(run.metrics || []).map((m, i) => (
              <tr key={`${m.product}-${i}`}>
                <td>{m.product}</td>
                <td>{m.metric_key}</td>
                <td>{m.metric_value}</td>
                <td>{m.verdict || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="card">
        <h3>Artifacts</h3>
        <table>
          <thead>
            <tr>
              <th>Path</th>
              <th>Type</th>
              <th>Size</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(run.artifacts || []).map((a) => (
              <tr key={a.id}>
                <td>{a.relative_path}</td>
                <td>{a.artifact_type}</td>
                <td>{a.size_bytes}</td>
                <td>
                  <button onClick={() => downloadArtifact(run.id, a.id, a.relative_path.split("/").pop(), token)}>
                    Download
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <h3>Run Log</h3>
        <pre>{logText || "(no log yet)"}</pre>
      </section>
    </div>
  );
}

function ComparePage({ token }) {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    getRuns(token)
      .then(setRuns)
      .catch((e) => setErr(String(e.message || e)));
  }, [token]);

  const data = useMemo(() => {
    const byId = new Map(runs.map((r) => [r.id, r]));
    return selected.map((id) => byId.get(id)).filter(Boolean);
  }, [runs, selected]);

  function toggle(id) {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= 5 ? prev : [...prev, id]
    );
  }

  return (
    <div className="card">
      <h2>Cross-Protocol Comparison</h2>
      {err ? <p className="error">{err}</p> : null}
      <p className="muted">Select up to 5 runs:</p>
      <div className="chips">
        {runs.map((r) => (
          <label key={r.id} className="chip">
            <input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} />
            {r.study_id} · {r.id.slice(0, 8)} · {r.status}
          </label>
        ))}
      </div>
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Study</th>
            <th>Status</th>
            <th>Risk Mode</th>
          </tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td>{r.study_id}</td>
              <td>{r.status}</td>
              <td>{r.risk_mode}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [token, setToken] = useState(localStorage.getItem("evalToken") || "");
  const [authErr, setAuthErr] = useState("");

  async function onLogin(username, password) {
    setAuthErr("");
    try {
      const res = await login(username, password);
      localStorage.setItem("evalToken", res.token);
      setToken(res.token);
    } catch (e) {
      setAuthErr(String(e.message || e));
    }
  }

  function logout() {
    localStorage.removeItem("evalToken");
    setToken("");
  }

  if (!token) return <LoginPage onLogin={onLogin} error={authErr} />;

  return (
    <Shell onLogout={logout}>
      <Routes>
        <Route path="/" element={<RunsPage token={token} />} />
        <Route path="/runs/:runId" element={<RunDetailPage token={token} />} />
        <Route path="/compare" element={<ComparePage token={token} />} />
      </Routes>
    </Shell>
  );
}
