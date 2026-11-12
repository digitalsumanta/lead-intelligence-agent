"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, ProposalIntelligence, ProposalItem, SalesPriority, SalesTask } from "@/lib/api";

const PRIORITIES: SalesPriority[] = ["LOW", "MEDIUM", "HIGH", "URGENT"];

function tomorrowLocal() {
  const date = new Date(Date.now() + 24 * 60 * 60 * 1000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

export default function AIProposalIntelligence({ leadId, onTaskCreated }: { leadId: string; onTaskCreated?: () => void }) {
  const [artifact, setArtifact] = useState<ProposalIntelligence | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showTaskForm, setShowTaskForm] = useState(false);
  const [dueAt, setDueAt] = useState(tomorrowLocal());
  const [priority, setPriority] = useState<SalesPriority>("MEDIUM");
  const [message, setMessage] = useState("");
  const evidence = useMemo(() => new Map((artifact?.evidence_catalog || []).map((entry) => [entry.id, entry])), [artifact]);

  const load = useCallback(async () => {
    try { setArtifact(await api<ProposalIntelligence | null>(`/api/leads/${encodeURIComponent(leadId)}/ai-proposal`)); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Proposal brief could not be loaded."); }
  }, [leadId]);
  useEffect(() => { void load(); }, [load]);

  async function generate() {
    setBusy(true); setError(""); setMessage("");
    try { setArtifact(await api<ProposalIntelligence>(`/api/leads/${encodeURIComponent(leadId)}/ai-proposal`, { method: "POST" })); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Proposal brief could not be generated."); }
    finally { setBusy(false); }
  }

  async function createProposalTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!artifact?.proposal_brief || !dueAt) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await api<SalesTask>(`/api/leads/${encodeURIComponent(leadId)}/tasks`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: "PROPOSAL", title: "Review AI proposal brief",
          description: `Human review requested for proposal artifact ${artifact.id}. Evidence references: ${artifact.proposal_brief.evidence_refs.join(", ")}`,
          due_at: new Date(dueAt).toISOString(), priority,
        }),
      });
      setShowTaskForm(false); setMessage("Proposal review task created through the existing SalesTask workflow.");
      onTaskCreated?.(); await load();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Proposal review task could not be created."); }
    finally { setBusy(false); }
  }

  function Items({ title, items }: { title: string; items: ProposalItem[] }) {
    if (!items.length) return null;
    return <section><h3 className="text-sm font-semibold text-ink">{title}</h3><ul className="mt-2 space-y-2">{items.map((item, index) => <li key={`${item.item_type}-${index}`} className="rounded-lg bg-slate-50 p-3"><p className="text-sm text-slate-700">{item.text}</p><Refs refs={item.evidence_refs} /></li>)}</ul></section>;
  }

  function Refs({ refs }: { refs: string[] }) {
    return <div className="mt-2 flex flex-wrap gap-2">{refs.map((ref) => <span key={ref} title={evidence.get(ref)?.display_value || ref} className="max-w-full break-all rounded bg-white px-2 py-1 font-mono text-[10px] text-teal">{evidence.get(ref)?.label || ref}</span>)}</div>;
  }

  const brief = artifact?.proposal_brief;
  return <section id="ai-proposal-intelligence" className="mt-5 rounded-xl border border-amber-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-[10px] font-bold uppercase tracking-[.18em] text-amber-800">Prepared for human review — not sent.</p><h2 className="mt-1 font-bold text-ink">AI Proposal Intelligence</h2><p className="mt-1 text-sm text-slate-500">Evidence-grounded draft material. Review and edit it before making any proposal.</p></div><button type="button" disabled={busy} onClick={() => void generate()} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Preparing…" : artifact ? "Refresh proposal brief" : "Prepare proposal brief"}</button></div>
    {busy && (
      <div className="mt-4 flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50/60 p-4 text-sm text-amber-900">
        <svg className="h-5 w-5 animate-spin text-amber-600 shrink-0" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
        </svg>
        <div>
          <p className="font-semibold">Drafting proposal brief via local AI model…</p>
          <p className="text-xs text-amber-700">CPU inference typically takes 3–5 minutes. Grounding against verified commercial and technical evidence.</p>
        </div>
      </div>
    )}
    {error && <p role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}{message && <p role="status" className="mt-4 text-sm text-emerald-700">{message}</p>}
    {artifact?.is_stale && <p className="mt-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-800">This brief uses older lead context. Refresh it before relying on the selected evidence.</p>}
    {artifact?.status === "failed" && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{artifact.error_message || "No validated proposal brief is available."}</p>}
    {brief && <><article className="mt-4 rounded-lg bg-amber-50/60 p-4"><p className="text-xs font-semibold uppercase tracking-wide text-amber-900">{brief.proposal_type.replaceAll("_", " ")}</p><p className="mt-2 text-sm leading-6 text-slate-700">{brief.executive_summary}</p></article>
      <div className="mt-5 grid gap-5 lg:grid-cols-2"><Items title="Identified needs" items={brief.identified_needs} /><section><h3 className="text-sm font-semibold text-ink">Recommended services</h3>{brief.recommended_services.length ? <ul className="mt-2 space-y-2">{brief.recommended_services.map((service) => <li key={service.service_id} className="rounded-lg bg-slate-50 p-3"><p className="font-mono text-sm text-slate-700">{service.service_id}</p><Refs refs={service.evidence_refs} /></li>)}</ul> : <p className="mt-2 text-sm text-slate-500">No supported service recommendation was selected.</p>}</section>
      <section><h3 className="text-sm font-semibold text-ink">Opportunity evidence</h3>{brief.opportunity_evidence.length ? <ul className="mt-2 space-y-2">{brief.opportunity_evidence.map((item) => <li key={item.opportunity_id} className="rounded-lg bg-slate-50 p-3"><p className="text-sm text-slate-700">{item.label}</p><Refs refs={[item.opportunity_id, ...item.evidence_refs]} /></li>)}</ul> : <p className="mt-2 text-sm text-slate-500">No current opportunity was selected.</p>}</section>
      <Items title="Proposed scope" items={brief.proposed_scope} />
      <section><h3 className="text-sm font-semibold text-ink">Deliverables for discussion</h3><ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">{brief.deliverables.map((item) => <li key={item}>{item}</li>)}</ul></section>
      <section><h3 className="text-sm font-semibold text-ink">Assumptions</h3><ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">{brief.assumptions.map((item) => <li key={item}>{item}</li>)}</ul></section>
      <section><h3 className="text-sm font-semibold text-ink">Exclusions</h3><ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">{brief.exclusions.map((item) => <li key={item}>{item}</li>)}</ul></section>
      <section><h3 className="text-sm font-semibold text-ink">Timeline guidance</h3><p className="mt-2 rounded-lg bg-slate-50 p-3 text-sm text-slate-700">{brief.timeline_guidance}</p></section>
      <section><h3 className="text-sm font-semibold text-ink">Commercial context</h3>{brief.commercial_context.length ? <ul className="mt-2 space-y-2">{brief.commercial_context.map((item) => <li key={item.evidence_ref} className="rounded-lg bg-slate-50 p-3"><p className="text-xs font-semibold text-slate-600">{item.label}</p><p className="mt-1 break-words text-sm text-slate-700">{item.display_value}</p><Refs refs={[item.evidence_ref]} /></li>)}</ul> : <p className="mt-2 text-sm text-slate-500">No persisted commercial values were selected; no price or terms are proposed.</p>}</section>
      <Items title="Missing information" items={brief.missing_information} />
      <section><h3 className="text-sm font-semibold text-ink">Review notes</h3><ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">{brief.review_notes.map((item) => <li key={item}>{item}</li>)}</ul></section></div>
      <div className="mt-5 border-t border-slate-200 pt-4"><a href={`/leads/${encodeURIComponent(leadId)}#evidence`} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-teal">Review Evidence</a>{artifact.is_stale ? <p className="mt-3 text-xs text-amber-800">Refresh the stale brief before creating a task.</p> : showTaskForm ? <form onSubmit={(event) => void createProposalTask(event)} className="mt-4 flex flex-wrap items-end gap-3"><label className="text-xs font-semibold text-slate-600">Due at<input required type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} className="mt-1 block rounded-lg border border-slate-300 px-2 py-2 text-sm" /></label><label className="text-xs font-semibold text-slate-600">Task priority<select value={priority} onChange={(event) => setPriority(event.target.value as SalesPriority)} className="mt-1 block rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm">{PRIORITIES.map((value) => <option key={value}>{value}</option>)}</select></label><button disabled={busy} className="rounded-lg bg-ink px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Confirm Proposal Task</button><button type="button" onClick={() => setShowTaskForm(false)} className="rounded-lg border border-slate-300 px-3 py-2 text-xs">Cancel</button></form> : <button type="button" onClick={() => { setDueAt(tomorrowLocal()); setShowTaskForm(true); }} disabled={busy || artifact.status !== "validated" || !artifact.is_current} className="ml-2 rounded-lg border border-amber-400 px-3 py-2 text-xs font-semibold text-amber-900 disabled:opacity-50">Create Proposal Task</button>}</div>
      <p className="mt-4 text-[10px] text-slate-400">{artifact.contract_version} · {artifact.prompt_version} · {artifact.provider} / {artifact.model} · {new Date(artifact.created_at).toLocaleString()}</p></>}
  </section>;
}
