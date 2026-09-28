"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, IntelligenceClaim, IntelligenceResponse, LeadDetail, SalesActivity, SalesActivityType, SalesPriority, SalesStatus, SalesWorkflow } from "@/lib/api";

const NEXT_SALES_STATUS: Record<SalesStatus, SalesStatus[]> = {
  NEW: ["QUALIFIED", "CONTACTED", "LOST"], QUALIFIED: ["CONTACTED", "LOST"],
  CONTACTED: ["ENGAGED", "QUALIFIED", "LOST"], ENGAGED: ["PROPOSAL", "CONTACTED", "LOST"],
  PROPOSAL: ["NEGOTIATION", "ENGAGED", "WON", "LOST"], NEGOTIATION: ["WON", "PROPOSAL", "LOST"],
  WON: [], LOST: ["NEW", "QUALIFIED"],
};

function localDateTime(value: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function SalesWorkflowPanel({ leadId, intelligence }: { leadId: string; intelligence: IntelligenceResponse | null }) {
  const [sales, setSales] = useState<SalesWorkflow | null>(null);
  const [status, setStatus] = useState<SalesStatus>("NEW");
  const [priority, setPriority] = useState<SalesPriority>("MEDIUM");
  const [assignedTo, setAssignedTo] = useState("");
  const [followUp, setFollowUp] = useState("");
  const [lostReason, setLostReason] = useState("");
  const [activityType, setActivityType] = useState<Exclude<SalesActivityType, "STATUS_CHANGE">>("NOTE");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [initialized, setInitialized] = useState(false);

  async function reload() {
    const data = await api<SalesWorkflow>(`/api/leads/${encodeURIComponent(leadId)}/sales`);
    setSales(data); setStatus(data.sales_status); setPriority(data.priority);
    setAssignedTo(data.assigned_to || ""); setLostReason(data.lost_reason || "");
    setFollowUp(localDateTime(data.next_follow_up_at));
  }

  useEffect(() => {
    let active = true;
    api<SalesWorkflow>(`/api/leads/${encodeURIComponent(leadId)}/sales`).then((data) => {
      if (!active) return;
      setSales(data); setStatus(data.sales_status); setPriority(data.priority);
      setAssignedTo(data.assigned_to || ""); setLostReason(data.lost_reason || "");
      setFollowUp(localDateTime(data.next_follow_up_at));
    }).catch(() => { if (active) setError("Sales workflow could not be loaded."); }).finally(() => { if (active) setInitialized(true); });
    return () => { active = false; };
  }, [leadId]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api<SalesWorkflow>(`/api/leads/${encodeURIComponent(leadId)}/sales`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sales_status: status, priority, assigned_to: assignedTo.trim() || null, next_follow_up_at: followUp ? new Date(followUp).toISOString() : null, lost_reason: status === "LOST" ? lostReason.trim() || null : null }) });
      await reload();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Sales workflow could not be saved."); }
    finally { setBusy(false); }
  }

  async function addActivity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api<SalesActivity>(`/api/leads/${encodeURIComponent(leadId)}/activities`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ activity_type: activityType, note }) });
      setNote(""); await reload();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Activity could not be saved."); }
    finally { setBusy(false); }
  }

  const input = "mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm";
  return <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-bold text-ink">Sales workflow</h2><p className="mt-1 text-sm text-slate-500">Manage pipeline state and activity. Intelligence remains read-only and independent.</p></div>{sales && <span className="rounded-full bg-teal/10 px-3 py-1 text-xs font-semibold text-teal">{sales.sales_status} · {sales.priority}</span>}</div>
    {error && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {!initialized ? <p className="mt-4 text-sm text-slate-500">Loading sales workflow…</p> : sales && <>
      <form onSubmit={save} className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-xs font-semibold text-slate-600">Sales status<select className={input} value={status} onChange={(event) => setStatus(event.target.value as SalesStatus)}><option value={sales.sales_status}>{sales.sales_status}</option>{NEXT_SALES_STATUS[sales.sales_status].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label className="text-xs font-semibold text-slate-600">Priority<select className={input} value={priority} onChange={(event) => setPriority(event.target.value as SalesPriority)}>{["LOW", "MEDIUM", "HIGH", "URGENT"].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="text-xs font-semibold text-slate-600">Assigned owner<input className={input} maxLength={120} value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)} placeholder="Unassigned" /></label>
        <label className="text-xs font-semibold text-slate-600">Next follow-up<input className={input} type="datetime-local" value={followUp} onChange={(event) => setFollowUp(event.target.value)} /></label>
        {status === "LOST" && <label className="text-xs font-semibold text-slate-600 sm:col-span-2">Lost reason<textarea className={input} maxLength={1000} required value={lostReason} onChange={(event) => setLostReason(event.target.value)} /></label>}
        <div className="flex items-end"><button disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Saving…" : "Save sales details"}</button></div>
      </form>
      <div className="mt-5 rounded-lg border border-teal/15 bg-teal/5 p-4">
        <h3 className="text-xs font-bold uppercase tracking-wide text-teal">Read-only intelligence context</h3>
        {intelligence ? <div className="mt-2 flex flex-wrap gap-x-6 gap-y-2 text-sm text-slate-700"><span>Deterministic score: <strong>{intelligence.score.value ?? "—"}</strong></span><span>Opportunities: <strong>{intelligence.opportunities.length}</strong></span><span>Personalization: <strong>{intelligence.personalization_available ? "Available" : "Unavailable"}</strong></span><span>Recommended services: <strong>{intelligence.recommendations.length ? intelligence.recommendations.map((item) => item.service_name).join(", ") : "None"}</strong></span></div> : <p className="mt-2 text-sm text-slate-500">Intelligence context is unavailable; sales workflow updates do not change intelligence.</p>}
      </div>
      {sales.lost_reason && status === "LOST" && <p className="mt-3 text-sm text-slate-600"><span className="font-semibold">Lost reason:</span> {sales.lost_reason}</p>}
      <div className="mt-6 grid gap-6 lg:grid-cols-[.8fr_1.2fr]">
        <form onSubmit={addActivity} className="rounded-lg bg-slate-50 p-4"><h3 className="font-semibold text-ink">Add activity</h3><label className="mt-3 block text-xs font-semibold text-slate-600">Type<select className={input} value={activityType} onChange={(event) => setActivityType(event.target.value as typeof activityType)}>{["NOTE", "CALL", "EMAIL", "WHATSAPP", "MEETING", "FOLLOW_UP", "PROPOSAL", "OTHER"].map((value) => <option key={value}>{value}</option>)}</select></label><label className="mt-3 block text-xs font-semibold text-slate-600">Note<textarea className={input} required maxLength={5000} value={note} onChange={(event) => setNote(event.target.value)} /></label><button disabled={busy} className="mt-3 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold disabled:opacity-50">Add activity</button></form>
        <div><h3 className="font-semibold text-ink">Activity timeline</h3>{!sales.recent_activities.length ? <p className="mt-3 text-sm text-slate-500">No sales activities recorded.</p> : <ol className="mt-3 space-y-3">{sales.recent_activities.map((activity) => <li key={activity.id} className={`rounded-lg border p-3 ${activity.activity_type === "STATUS_CHANGE" ? "border-teal/20 bg-teal/5" : "border-slate-200"}`}><div className="flex justify-between gap-3"><span className="text-xs font-bold text-slate-700">{activity.activity_type === "STATUS_CHANGE" ? "System status change" : activity.activity_type.replaceAll("_", " ")}</span><time className="text-xs text-slate-400">{date(activity.occurred_at)}</time></div><p className="mt-1 whitespace-pre-wrap text-sm text-slate-600">{activity.note}</p></li>)}</ol>}</div>
      </div>
    </>}
  </section>;
}

function date(value: string | null | undefined) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

function safeWebsite(value: string | null) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function ClaimCard({ claim }: { claim: IntelligenceClaim }) {
  const styles = {
    verified_fact: "border-sky-200 bg-sky-50 text-sky-950",
    supported_inference: "border-amber-200 bg-amber-50 text-amber-950",
    suggested_language: "border-teal/20 bg-teal/5 text-ink",
  };
  const labels = {
    verified_fact: "Verified fact",
    supported_inference: "Supported inference",
    suggested_language: "Suggested language",
  };
  return (
    <article className={`rounded-lg border p-4 ${styles[claim.type]}`}>
      <div className="text-[11px] font-bold uppercase tracking-wide opacity-70">{labels[claim.type]}</div>
      <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{claim.text}</p>
      {claim.evidence_ids.length > 0 && <p className="mt-3 text-xs opacity-70">Evidence: {claim.evidence_ids.map((id, index) => <span key={id}>{index > 0 ? ", " : ""}<a href={`#evidence-${id}`} className="font-mono underline">{id.slice(0, 8)}</a></span>)}</p>}
    </article>
  );
}

function ClaimSection({ title, claims }: { title: string; claims: IntelligenceClaim[] }) {
  return <section>
    <h3 className="text-sm font-semibold text-ink">{title}</h3>
    <div className="mt-3 grid gap-3">{claims.map((claim, index) => <ClaimCard key={`${claim.type}-${index}`} claim={claim} />)}</div>
  </section>;
}

export default function LeadDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [intelligence, setIntelligence] = useState<IntelligenceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState("");
  const [intelligenceState, setIntelligenceState] = useState<"loading" | "available" | "unavailable" | "error">("loading");
  const [intelligenceRetry, setIntelligenceRetry] = useState(0);

  useEffect(() => {
    let current = true;
    async function load() {
      setLoading(true); setError(""); setNotFound(false); setIntelligenceState("loading"); setIntelligence(null);
      try {
        const business = await api<LeadDetail>(`/api/leads/${encodeURIComponent(id)}`);
        if (!current) return;
        setLead(business);
        try {
          const detail = await api<IntelligenceResponse>(`/api/leads/${encodeURIComponent(id)}/intelligence`);
          if (current) {
            setIntelligence(detail);
            setIntelligenceState("available");
          }
        } catch (cause) {
          if (!current) return;
          if (cause instanceof ApiError && cause.code === "INTELLIGENCE_NOT_AVAILABLE") {
            setIntelligenceState("unavailable");
          } else {
            setIntelligenceState("error");
          }
        }
      } catch (cause) {
        if (!current) return;
        if (cause instanceof ApiError && cause.code === "LEAD_NOT_FOUND") setNotFound(true);
        else setError("Lead details could not be loaded. Check the API connection and try again.");
      } finally {
        if (current) setLoading(false);
      }
    }
    void load();
    return () => { current = false; };
  }, [id, intelligenceRetry]);

  if (loading) return <main className="mx-auto max-w-6xl p-6 sm:p-10" aria-live="polite"><p className="text-sm text-slate-500">Loading lead intelligence…</p></main>;
  if (notFound) return <main className="mx-auto max-w-6xl p-6 sm:p-10"><Link href="/leads" className="text-sm font-semibold text-teal">← All leads</Link><section className="mt-8 rounded-xl border bg-white p-10 text-center"><h1 className="text-xl font-bold">Lead not found</h1><p className="mt-2 text-sm text-slate-500">This lead may have been removed or the link may be incorrect.</p></section></main>;
  if (!lead) return <main className="mx-auto max-w-6xl p-6 sm:p-10"><p role="alert" className="text-sm text-rose-700">{error || "Lead details are unavailable."}</p></main>;

  const intel = intelligence;
  const websiteUrl = safeWebsite(lead.website);
  const evidenceById = new Map((intel?.evidence || []).map((item) => [item.id, item]));
  const personalization = intel?.personalization;
  const personalizationSections = personalization ? [
    ["Sales opening", personalization.sales_opening.claims],
    ["Audit summary", personalization.audit_summary.claims],
    ["Audit video script", personalization.audit_video_script.claims],
    ["Recommended package language", personalization.recommended_package_language.claims],
  ] as const : [];

  return (
    <main className="mx-auto min-h-screen max-w-6xl px-4 py-6 sm:px-8 lg:py-10">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <Link href="/leads" className="text-sm font-semibold text-teal hover:underline">← All leads</Link>
          <p className="mt-5 text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">{lead.company_name}</h1>
          <p className="mt-2 text-slate-500">{[lead.industry, lead.city, lead.state, lead.country].filter(Boolean).join(" · ") || "Business profile"}</p>
        </div>
        <span className={`rounded-full px-3 py-1.5 text-sm font-semibold ${intelligenceState === "available" ? "bg-emerald-50 text-emerald-700" : intelligenceState === "error" ? "bg-rose-50 text-rose-700" : "bg-slate-100 text-slate-600"}`}>
          {intelligenceState === "available" ? "Intelligence available" : intelligenceState === "unavailable" ? "Intelligence unavailable" : intelligenceState === "error" ? "Intelligence request failed" : "Loading intelligence"}
        </span>
      </header>

      {error && <div role="alert" className="mt-5 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">{error}</div>}
      {intelligenceState === "unavailable" && <div className="mt-5 rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-600">No completed enrichment run with valid intelligence is available yet. Business details remain available below.</div>}
      {intelligenceState === "error" && <div role="alert" className="mt-5 flex flex-col gap-3 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900 sm:flex-row sm:items-center sm:justify-between"><div><p className="font-semibold">Intelligence could not be loaded.</p><p className="mt-1">The request failed, so intelligence data is not shown. Your business details remain available.</p></div><button onClick={() => setIntelligenceRetry((attempt) => attempt + 1)} className="shrink-0 rounded-lg border border-rose-300 bg-white px-4 py-2 font-semibold hover:bg-rose-100">Retry intelligence</button></div>}

      <section className="mt-7 grid gap-4 lg:grid-cols-[1.3fr_1fr]">
        <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="font-bold text-ink">Business summary</h2>
          <dl className="mt-4 grid gap-x-6 gap-y-4 text-sm sm:grid-cols-2">
            <div><dt className="text-xs font-semibold uppercase text-slate-400">Website</dt><dd className="mt-1 break-all">{lead.website ? websiteUrl ? <a className="text-teal hover:underline" href={websiteUrl} target="_blank" rel="noreferrer">{lead.website}</a> : lead.website : "—"}</dd></div>
            <div><dt className="text-xs font-semibold uppercase text-slate-400">Business status</dt><dd className="mt-1">{lead.status}</dd></div>
            <div><dt className="text-xs font-semibold uppercase text-slate-400">Email</dt><dd className="mt-1 break-all">{lead.email || "—"}</dd></div>
            <div><dt className="text-xs font-semibold uppercase text-slate-400">Phone</dt><dd className="mt-1">{lead.phone || "—"}</dd></div>
            <div className="sm:col-span-2"><dt className="text-xs font-semibold uppercase text-slate-400">Address</dt><dd className="mt-1">{[lead.address, lead.city, lead.state, lead.country].filter(Boolean).join(", ") || "—"}</dd></div>
          </dl>
        </article>

        {intelligenceState !== "error" && <>
        <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="font-bold text-ink">Intelligence summary</h2>
          {intel ? <>
            <div className="mt-4 flex items-end gap-3"><span className="text-4xl font-bold text-ink">{intel.score.value ?? "—"}</span><span className="pb-1 text-sm text-slate-400">/ 100 · {intel.score.version}</span></div>
            {intel.score.value !== null && <div className="mt-3 h-2 rounded-full bg-slate-100"><div className="h-2 rounded-full bg-teal" style={{ width: `${intel.score.value}%` }} /></div>}
            <p className="mt-4 text-sm text-slate-600">Run {intel.run.status.toLowerCase()} · completed {date(intel.run.completed_at)}</p>
            <p className="mt-1 text-xs text-slate-400">Pipeline {intel.run.pipeline_version} · generated {date(intel.generated_at)}</p>
            <details className="mt-4 rounded-lg bg-slate-50 p-3">
              <summary className="cursor-pointer text-sm font-semibold text-ink">Score breakdown</summary>
              <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words text-xs text-slate-600">{JSON.stringify(intel.score.breakdown, null, 2)}</pre>
            </details>
          </> : <p className="mt-4 text-sm text-slate-500">No score is available for a completed enrichment run.</p>}
        </article>
        </>}
      </section>

      <SalesWorkflowPanel leadId={id} intelligence={intel} />

      {intelligenceState !== "error" && <>
      <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between gap-3"><div><h2 className="font-bold text-ink">Opportunities</h2><p className="mt-1 text-sm text-slate-500">Deterministic findings from this enrichment run.</p></div><span className="rounded-full bg-amber-50 px-3 py-1 text-xs font-semibold text-amber-800">{intel?.opportunities.length ?? 0}</span></div>
        {!intel?.opportunities.length ? <p className="mt-5 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">No opportunities are available for this run.</p> : <div className="mt-4 grid gap-3 md:grid-cols-2">
          {intel.opportunities.map((opportunity) => <article key={opportunity.id} className="rounded-lg border border-slate-200 p-4">
            <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold text-ink">{opportunity.title}</h3><span className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-semibold uppercase text-amber-800">{opportunity.severity}</span></div>
            <p className="mt-2 text-sm leading-6 text-slate-600">{opportunity.description}</p>
            <p className="mt-3 text-xs text-slate-400">{opportunity.category.replaceAll("_", " ")} · {opportunity.status}</p>
            {opportunity.evidence_ids.length > 0 && <p className="mt-2 text-xs text-slate-500">Evidence: {opportunity.evidence_ids.map((evidenceId, index) => <span key={evidenceId}>{index ? ", " : ""}<a href={`#evidence-${evidenceId}`} className="font-mono text-teal underline">{evidenceById.get(evidenceId)?.evidence_type || evidenceId.slice(0, 8)}</a></span>)}</p>}
          </article>)}
        </div>}
      </section>

      <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-bold text-ink">Recommended services</h2>
        {!intel?.recommendations.length ? <p className="mt-4 text-sm text-slate-500">No deterministic service recommendations are available.</p> : <div className="mt-4 grid gap-3 md:grid-cols-2">
          {intel.recommendations.map((item) => <article key={item.id} className="rounded-lg border border-slate-200 p-4">
            <div className="flex items-start justify-between gap-3"><div><h3 className="font-semibold text-ink">{item.service_name}</h3><p className="mt-1 text-xs text-slate-400">Service ID: <code>{item.service_id}</code> · Rank {item.rank}</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600">{Math.round(item.confidence * 100)}% confidence</span></div>
            <p className="mt-3 text-sm text-slate-600">{item.service_description}</p><p className="mt-2 text-sm leading-6 text-slate-700">{item.rationale}</p>
          </article>)}
        </div>}
      </section>

      <section id="evidence" className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-bold text-ink">Evidence</h2>
        <p className="mt-1 text-sm text-slate-500">Persisted observations supporting this run’s score and opportunities.</p>
        {!intel?.evidence.length ? <p className="mt-4 text-sm text-slate-500">No evidence is available for this run.</p> : <div className="mt-4 divide-y divide-slate-100">
          {intel.evidence.map((item) => <article id={`evidence-${item.id}`} key={item.id} className="scroll-mt-6 py-4 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold text-ink">{item.evidence_type.replaceAll("_", " ")}</h3><span className="text-xs text-slate-400">{date(item.observed_at)}</span></div>
            <p className="mt-1 text-xs text-slate-500">{item.source_type} · {item.provider_name}{item.confidence !== null ? ` · ${Math.round(item.confidence * 100)}% confidence` : ""}</p>
            <pre className="mt-3 whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-3 text-xs leading-5 text-slate-700">{typeof item.observed_value === "string" ? item.observed_value : JSON.stringify(item.observed_value, null, 2)}</pre>
            {item.source_url && <p className="mt-2 text-xs">Source origin: <a className="break-all text-teal underline" href={item.source_url} target="_blank" rel="noreferrer">{item.source_url}</a></p>}
          </article>)}
        </div>}
      </section>

      <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-bold text-ink">Evidence-grounded personalization</h2><p className="mt-1 text-sm text-slate-500">Suggested sales copy is labeled separately from facts and inferences.</p></div><span className={`rounded-full px-3 py-1 text-xs font-semibold ${personalization ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>{personalization ? "Validated" : "Not available"}</span></div>
        {!personalization ? <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">No validated personalization is available for this enrichment run.</p> : <>
          <p className="mt-3 text-xs text-slate-400">{personalization.contract_version} · {personalization.prompt_version} · confidence {Math.round(personalization.confidence_score * 100)}%</p>
          <div className="mt-5 grid gap-6 lg:grid-cols-2">{personalizationSections.map(([title, claims]) => <ClaimSection key={title} title={title} claims={claims} />)}</div>
        </>}
      </section>

      <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-bold text-ink">Processing</h2>
        <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm text-slate-600">
          <span>Status: {intel?.processing.status || lead.status}</span>
          {intel?.processing.checkpoint && <span>Checkpoint: {intel.processing.checkpoint}</span>}
          {intel?.processing.attempt_count !== null && intel?.processing.attempt_count !== undefined && <span>Attempt: {intel.processing.attempt_count}/{intel.processing.max_attempts}</span>}
          <span>Last updated: {date(intel?.processing.updated_at)}</span>
        </div>
      </section>
      </>}
    </main>
  );
}
