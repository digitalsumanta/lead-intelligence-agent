"use client";

import { useCallback, useEffect, useState } from "react";
import { AgentPolicyEvaluation, AgentPolicyHistory, api } from "@/lib/api";

export default function AgentPolicy({ leadId }: { leadId: string }) {
  const [latest, setLatest] = useState<AgentPolicyEvaluation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const history = await api<AgentPolicyHistory>(`/api/leads/${encodeURIComponent(leadId)}/agent-policy/history?limit=1`);
      setLatest(history.items[0] || null);
    } catch {
      setError("Policy evaluation history could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [leadId]);

  useEffect(() => { void load(); }, [load]);

  const decisionStyle = latest?.decision === "DENY" ? "bg-rose-50 text-rose-800" : latest?.decision === "REQUIRE_HUMAN_APPROVAL" ? "bg-amber-50 text-amber-800" : "bg-teal/10 text-teal";
  return <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="agent-policy-title">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 id="agent-policy-title" className="font-bold text-ink">Agent policy decision</h2><p className="mt-1 text-sm text-slate-500">Read-only audit of deterministic authorization checks. This view never executes actions.</p></div>
      <button type="button" onClick={() => void load()} disabled={loading} className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold disabled:opacity-50">Refresh</button>
    </div>
    {error && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading ? <p className="mt-4 text-sm text-slate-500">Loading policy history…</p> : latest ? <div className="mt-4 rounded-lg border border-slate-100 p-4">
      <div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-ink">{latest.action_type.replaceAll("_", " ")}</span>{latest.requested_channel && <span className="text-xs text-slate-500">via {latest.requested_channel}</span>}<span className={`rounded-full px-2.5 py-1 text-xs font-bold ${decisionStyle}`}>{latest.decision.replaceAll("_", " ")}</span></div>
      <p className="mt-3 text-sm text-slate-700">{latest.reason.replaceAll("_", " ")}</p>
      <p className="mt-3 text-xs text-slate-500">Policy {latest.policy_version} · Autonomy {latest.autonomy_level.replaceAll("_", " ")} · Evaluated {new Date(latest.evaluated_at).toLocaleString()}</p>
    </div> : <p className="mt-4 text-sm text-slate-500">No policy evaluations have been recorded for this lead.</p>}
  </section>;
}
