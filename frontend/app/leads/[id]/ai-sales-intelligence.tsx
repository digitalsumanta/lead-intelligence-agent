"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, AISalesActionType, AISalesClaim, AISalesIntelligenceArtifact, AISalesRecommendedAction, ApiError, SalesPriority } from "@/lib/api";

const PRIORITIES: SalesPriority[] = ["LOW", "MEDIUM", "HIGH", "URGENT"];
const ACTION_TITLES: Record<AISalesActionType, string> = {
  REVIEW_SEO_OPPORTUNITY: "Review SEO findings",
  CALL_PROSPECT: "Call prospect",
  FOLLOW_UP: "Follow up with prospect",
  REVIEW_OUTREACH: "Review outreach history",
  PREPARE_PROPOSAL: "Review proposal next step",
  INVESTIGATE_MISSING_INFORMATION: "Investigate missing information",
  REVIEW_COMMERCIAL_CONTEXT: "Review commercial context",
};

function defaultDueAt() {
  const due = new Date(Date.now() + 24 * 60 * 60 * 1000);
  due.setMinutes(due.getMinutes() - due.getTimezoneOffset());
  return due.toISOString().slice(0, 16);
}

function shownTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function claimText(claim: AISalesClaim, evidenceById: Map<string, string>) {
  if (claim.kind === "VERIFIED_FACT") return claim.evidence_refs.map((id) => evidenceById.get(id) || `Evidence ${id}`).join("; ");
  return claim.text || "";
}

function Claim({ claim, evidenceById }: { claim: AISalesClaim; evidenceById: Map<string, string> }) {
  const colors: Record<AISalesClaim["kind"], string> = {
    VERIFIED_FACT: "border-sky-200 bg-sky-50 text-sky-950",
    SUPPORTED_INFERENCE: "border-amber-200 bg-amber-50 text-amber-950",
    SUGGESTED_LANGUAGE: "border-teal/20 bg-teal/5 text-ink",
    UNCERTAINTY: "border-slate-200 bg-slate-50 text-slate-700",
  };
  return <article className={`rounded-lg border p-3 ${colors[claim.kind]}`}>
    <p className="text-[10px] font-bold uppercase tracking-wide opacity-70">{claim.kind.replaceAll("_", " ")}</p>
    <p className="mt-1 whitespace-pre-wrap text-sm leading-6">{claimText(claim, evidenceById)}</p>
    {claim.evidence_refs.length > 0 && <ul className="mt-2 space-y-1">{claim.evidence_refs.map((ref) => <li key={ref} className="break-all text-[11px] opacity-70"><span className="font-mono">{ref}</span>{evidenceById.has(ref) && <span> · {evidenceById.get(ref)}</span>}</li>)}</ul>}
  </article>;
}

function ActionCard({ action, artifact, evidenceById, leadId, onTaskCreated }: {
  action: AISalesRecommendedAction; artifact: AISalesIntelligenceArtifact;
  evidenceById: Map<string, string>; leadId: string; onTaskCreated: () => void;
}) {
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dueAt, setDueAt] = useState(defaultDueAt);
  const [priority, setPriority] = useState<SalesPriority>("MEDIUM");
  const [assignee, setAssignee] = useState("");

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action.task_type || !dueAt) return;
    setBusy(true); setError("");
    try {
      const evidence = action.rationale.evidence_refs.join(", ");
      await api(`/api/leads/${encodeURIComponent(leadId)}/tasks`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: action.task_type, title: ACTION_TITLES[action.action_type],
          description: `${claimText(action.rationale, evidenceById)}\nAI interpretation ${artifact.id}; evidence: ${evidence}`,
          due_at: new Date(dueAt).toISOString(), priority, assigned_to: assignee.trim() || null,
        }),
      });
      setCreating(false); onTaskCreated();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Task could not be created.");
    } finally { setBusy(false); }
  }

  return <article className="rounded-lg border border-slate-200 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold text-ink">{ACTION_TITLES[action.action_type]}</h4><span className="rounded-full bg-teal/10 px-2 py-1 text-[10px] font-semibold text-teal">Human review required</span></div>
    <Claim claim={action.rationale} evidenceById={evidenceById} />
    {action.service_ids.length > 0 && <p className="mt-2 text-xs text-slate-500">Existing service references: {action.service_ids.join(", ")}</p>}
    {creating ? <form onSubmit={(event) => void createTask(event)} className="mt-3 grid gap-3 rounded-lg bg-slate-50 p-3 sm:grid-cols-2">
      <label className="text-xs font-semibold text-slate-600">Due at<input required type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm" /></label>
      <label className="text-xs font-semibold text-slate-600">Priority<select value={priority} onChange={(event) => setPriority(event.target.value as SalesPriority)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm">{PRIORITIES.map((item) => <option key={item}>{item}</option>)}</select></label>
      <label className="text-xs font-semibold text-slate-600 sm:col-span-2">Assigned to<input maxLength={120} value={assignee} onChange={(event) => setAssignee(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm" /></label>
      {error && <p role="alert" className="text-xs text-rose-700 sm:col-span-2">{error}</p>}
      <div className="flex gap-2 sm:col-span-2"><button disabled={busy} className="rounded-lg bg-ink px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">{busy ? "Creating…" : "Confirm Create Task"}</button><button type="button" disabled={busy} onClick={() => setCreating(false)} className="rounded-lg border border-slate-300 px-3 py-2 text-xs">Cancel</button></div>
    </form> : action.task_type ? <button type="button" onClick={() => setCreating(true)} className="mt-3 rounded-lg border border-teal/40 px-3 py-2 text-xs font-semibold text-teal">Create Task</button> : <p className="mt-3 text-xs text-slate-500">No task type was recommended; review the action in the Sales Action Center.</p>}
  </article>;
}

export default function AISalesIntelligence({ leadId, onTaskCreated }: { leadId: string; onTaskCreated: () => void }) {
  const [artifact, setArtifact] = useState<AISalesIntelligenceArtifact | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true); setError("");
    try { setArtifact(await api<AISalesIntelligenceArtifact | null>(`/api/leads/${encodeURIComponent(leadId)}/ai-sales-intelligence`)); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "AI sales intelligence could not be loaded."); }
    finally { setLoading(false); }
  }

  useEffect(() => { void load(); }, [leadId]);

  async function generate() {
    setBusy(true); setError("");
    try { setArtifact(await api<AISalesIntelligenceArtifact>(`/api/leads/${encodeURIComponent(leadId)}/ai-sales-intelligence`, { method: "POST" })); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "AI sales intelligence could not be generated."); }
    finally { setBusy(false); }
  }

  const evidenceById = useMemo(() => new Map((artifact?.evidence_catalog || []).map((item) => [item.id, item.display_value || item.label])), [artifact]);
  const generated = artifact?.generated;
  return <section className="mt-5 rounded-xl border border-violet-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-[10px] font-bold uppercase tracking-[.18em] text-violet-700">AI-generated interpretation</p><h2 className="mt-1 font-bold text-ink">AI Sales Intelligence</h2><p className="mt-1 text-sm text-slate-500">An interpretation of current deterministic evidence. It does not change CRM, task, outreach, or revenue state.</p></div><button type="button" disabled={busy} onClick={() => void generate()} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Generating…" : artifact ? "Regenerate Intelligence" : "Generate Intelligence"}</button></div>
    {error && <div role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800"><p>{error}</p><button type="button" onClick={() => void load()} className="mt-2 font-semibold underline">Retry load</button></div>}
    {loading ? <p className="mt-4 text-sm text-slate-500">Loading saved AI interpretation…</p> : !artifact ? <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">No AI sales intelligence has been generated for this lead. Generation is an explicit action and does not occur on page load.</p> : <>
      <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-500"><span>{artifact.status === "failed" ? "Generation failed" : artifact.is_stale ? "Stale — source context changed" : artifact.is_current ? "Current validated artifact" : "Historical artifact"}</span><span>{artifact.provider} · {artifact.model}</span><span>Generated {shownTime(artifact.generated_at)}</span><span>Contract {artifact.contract_version} · prompt {artifact.prompt_version}</span></div>
      {artifact.status === "failed" && <p role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{artifact.error_message || "Generation failed safely. No partial AI output was saved."} <span className="font-mono">{artifact.error_code}</span></p>}
      {generated && <>
        <section className="mt-5"><h3 className="text-sm font-semibold text-ink">Summary</h3><div className="mt-2 grid gap-2">{generated.summary.map((claim, index) => <Claim key={`summary-${index}`} claim={claim} evidenceById={evidenceById} />)}</div></section>
        {generated.insights.length > 0 && <section className="mt-5"><h3 className="text-sm font-semibold text-ink">Insights</h3><div className="mt-2 grid gap-2">{generated.insights.map((claim, index) => <Claim key={`insight-${index}`} claim={claim} evidenceById={evidenceById} />)}</div></section>}
        {generated.opportunities.length > 0 && <section className="mt-5"><h3 className="text-sm font-semibold text-ink">Synthesized opportunities</h3><div className="mt-2 grid gap-3 md:grid-cols-2">{generated.opportunities.map((item, index) => <article key={`${item.action_type}-${index}`} className="rounded-lg border border-amber-200 p-4"><h4 className="font-semibold text-ink">{item.title}</h4><Claim claim={item.rationale} evidenceById={evidenceById} /><p className="mt-2 break-all text-[11px] text-slate-500">Source items: {item.source_refs.join(", ")}</p>{item.service_ids.length > 0 && <p className="mt-1 text-xs text-slate-500">Services: {item.service_ids.join(", ")}</p>}</article>)}</div></section>}
        {generated.recommended_actions.length > 0 && <section className="mt-5"><h3 className="text-sm font-semibold text-ink">Recommended actions</h3><div className="mt-2 grid gap-3 md:grid-cols-2">{generated.recommended_actions.map((item, index) => <ActionCard key={`${item.action_type}-${index}`} action={item} artifact={artifact} evidenceById={evidenceById} leadId={leadId} onTaskCreated={onTaskCreated} />)}</div></section>}
        {generated.uncertainties.length > 0 && <section className="mt-5"><h3 className="text-sm font-semibold text-ink">Uncertainties / missing information</h3><div className="mt-2 grid gap-2">{generated.uncertainties.map((claim, index) => <Claim key={`uncertainty-${index}`} claim={claim} evidenceById={evidenceById} />)}</div></section>}
        <details className="mt-5 rounded-lg bg-slate-50 p-3"><summary className="cursor-pointer text-sm font-semibold text-ink">Evidence catalog ({artifact.evidence_refs.length} references)</summary><ul className="mt-3 space-y-2">{artifact.evidence_refs.map((ref) => { const item = artifact.evidence_catalog.find((entry) => entry.id === ref); return <li key={ref} className="rounded-md border border-slate-200 bg-white p-2"><p className="font-mono text-[11px] text-teal">{ref}</p><p className="mt-1 text-xs font-semibold text-slate-700">{item?.label || item?.source_type || "Supplied evidence"}</p><p className="mt-1 break-words text-xs text-slate-500">{item?.display_value || "Evidence detail is not available."}</p></li>; })}</ul></details>
      </>}
    </>}
  </section>;
}
