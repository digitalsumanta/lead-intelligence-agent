"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, CommercialRecord, IntelligenceClaim, IntelligenceResponse, LeadDetail, OutreachArtifact, OutreachChannel, OutreachContent, OutreachExecution, OutreachReadResponse, SalesActivity, SalesActivityOutcome, SalesActivityType, SalesCollection, SalesPriority, SalesStatus, SalesTask, SalesTaskCreate, SalesTaskStatus, SalesTaskType, SalesWorkflow } from "@/lib/api";
import SeoSection from "./seo-section";
import AISalesIntelligence from "./ai-sales-intelligence";
import AISalesCopilot from "./ai-sales-copilot";

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

function money(value: string | null | undefined, currency: string) {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.startsWith("-");
  const [whole = "0", fraction = "00"] = value.replace("-", "").split(".");
  const integer = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(BigInt(whole || "0"));
  const symbol = new Intl.NumberFormat("en-IN", { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).formatToParts(0).find((part) => part.type === "currency")?.value || currency;
  return `${negative ? "-" : ""}${symbol}${integer}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

function SalesWorkflowPanel({ leadId, intelligence, refreshKey, newOutcomeActivity, onTasksChanged }: { leadId: string; intelligence: IntelligenceResponse | null; refreshKey: number; newOutcomeActivity: SalesActivity | null; onTasksChanged: () => void }) {
  const [sales, setSales] = useState<SalesWorkflow | null>(null);
  const [status, setStatus] = useState<SalesStatus>("NEW");
  const [priority, setPriority] = useState<SalesPriority>("MEDIUM");
  const [assignedTo, setAssignedTo] = useState("");
  const [followUp, setFollowUp] = useState("");
  const [lostReason, setLostReason] = useState("");
  const [activityType, setActivityType] = useState<Exclude<SalesActivityType, "STATUS_CHANGE">>("NOTE");
  const [note, setNote] = useState("");
  const [activityOutcome, setActivityOutcome] = useState<SalesActivityOutcome | "">("");
  const [nextActionActivity, setNextActionActivity] = useState<SalesActivity | null>(null);
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
  }, [leadId, refreshKey]);

  useEffect(() => {
    if (newOutcomeActivity?.suggested_task_type) setNextActionActivity(newOutcomeActivity);
  }, [newOutcomeActivity]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api<SalesWorkflow>(`/api/leads/${encodeURIComponent(leadId)}/sales`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sales_status: status, priority, assigned_to: assignedTo.trim() || null, next_follow_up_at: followUp ? new Date(followUp).toISOString() : null, lost_reason: status === "LOST" ? lostReason.trim() || null : null }) });
      await reload(); onTasksChanged();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Sales workflow could not be saved."); }
    finally { setBusy(false); }
  }

  async function addActivity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const activity = await api<SalesActivity>(`/api/leads/${encodeURIComponent(leadId)}/activities`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ activity_type: activityType, note, outcome: activityOutcome || null }) });
      setNote(""); setActivityOutcome(""); setNextActionActivity(activity); await reload();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Activity could not be saved."); }
    finally { setBusy(false); }
  }

  const input = "mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm";
  return <section id="sales-workflow" className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
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
        <form onSubmit={addActivity} className="rounded-lg bg-slate-50 p-4"><h3 className="font-semibold text-ink">Record activity / outcome</h3><label className="mt-3 block text-xs font-semibold text-slate-600">Type<select className={input} value={activityType} onChange={(event) => setActivityType(event.target.value as typeof activityType)}>{["NOTE", "CALL", "EMAIL", "WHATSAPP", "MEETING", "FOLLOW_UP", "PROPOSAL", "OTHER"].map((value) => <option key={value}>{value}</option>)}</select></label><label className="mt-3 block text-xs font-semibold text-slate-600">Outcome<select className={input} value={activityOutcome} onChange={(event) => setActivityOutcome(event.target.value as SalesActivityOutcome | "")}><option value="">No structured outcome</option>{["NO_RESPONSE", "CONNECTED", "INTERESTED", "NOT_INTERESTED", "CALLBACK_REQUESTED", "MEETING_BOOKED", "PROPOSAL_REQUESTED", "PROPOSAL_SENT", "NEGOTIATION_STARTED", "WON", "LOST", "INVALID_CONTACT"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label><label className="mt-3 block text-xs font-semibold text-slate-600">Note<textarea className={input} required maxLength={5000} value={note} onChange={(event) => setNote(event.target.value)} /></label><button disabled={busy} className="mt-3 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold disabled:opacity-50">Record activity</button><p className="mt-2 text-xs text-slate-500">Outcome rules may suggest a next task; a task is created only by an explicit human action.</p>{nextActionActivity?.suggested_task_type && <div className="mt-3 rounded-md border border-teal/20 bg-white p-3 text-sm"><p>Suggested next action: <strong>{nextActionActivity.suggested_task_type.replaceAll("_", " ")}</strong></p><button type="button" disabled={busy} onClick={() => { void (async () => { setBusy(true); try { await api(`/api/leads/${encodeURIComponent(leadId)}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_type: nextActionActivity.suggested_task_type, title: `${nextActionActivity.suggested_task_type!.replaceAll("_", " ")} — ${nextActionActivity.note.slice(0, 100)}`, due_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(), priority, assigned_to: assignedTo.trim() || null, related_activity_id: nextActionActivity.id }) }); setNextActionActivity(null); onTasksChanged(); } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Task could not be created."); } finally { setBusy(false); } })(); }} className="mt-2 rounded-md bg-ink px-3 py-2 text-xs font-semibold text-white">Create suggested task</button></div>}</form>
        <div><h3 className="font-semibold text-ink">Activity timeline</h3>{!sales.recent_activities.length ? <p className="mt-3 text-sm text-slate-500">No sales activities recorded.</p> : <ol className="mt-3 space-y-3">{sales.recent_activities.map((activity) => <li key={activity.id} className={`rounded-lg border p-3 ${activity.activity_type === "STATUS_CHANGE" ? "border-teal/20 bg-teal/5" : "border-slate-200"}`}><div className="flex justify-between gap-3"><span className="text-xs font-bold text-slate-700">{activity.activity_type === "STATUS_CHANGE" ? "System status change" : activity.activity_type.replaceAll("_", " ")}</span><time className="text-xs text-slate-400">{date(activity.occurred_at)}</time></div>{activity.outcome && <p className="mt-1 text-xs font-semibold text-teal">Recorded outcome: {activity.outcome.replaceAll("_", " ")}</p>}<p className="mt-1 whitespace-pre-wrap text-sm text-slate-600">{activity.note}</p>{activity.outreach_execution_id && <p className="mt-1 text-[11px] text-slate-400">Outreach execution <code>{activity.outreach_execution_id.slice(0, 8)}</code></p>}</li>)}</ol>}</div>
      </div>
    </>}
  </section>;
}

function CommercialPanel({ leadId, refreshKey }: { leadId: string; refreshKey: number }) {
  const [commercial, setCommercial] = useState<CommercialRecord | null>(null);
  const [collections, setCollections] = useState<SalesCollection[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [currency, setCurrency] = useState("INR");
  const [collectionAmount, setCollectionAmount] = useState("");
  const [collectionDate, setCollectionDate] = useState(() => localDateTime(new Date().toISOString()));
  const [paymentReference, setPaymentReference] = useState("");
  const [collectionNotes, setCollectionNotes] = useState("");
  const [editingCollection, setEditingCollection] = useState<SalesCollection | null>(null);

  async function reload() {
    const [record, rows] = await Promise.all([
      api<CommercialRecord>(`/api/leads/${encodeURIComponent(leadId)}/commercial`),
      api<SalesCollection[]>(`/api/leads/${encodeURIComponent(leadId)}/collections`),
    ]);
    setCommercial(record); setCollections(rows); setCurrency(record.currency);
    setValues({ estimated_value: record.estimated_value || "", proposal_value: record.proposal_value || "", negotiation_value: record.negotiation_value || "", won_value: record.won_value || "", expected_collection: record.expected_collection || "" });
  }

  useEffect(() => {
    let active = true;
    Promise.all([
      api<CommercialRecord>(`/api/leads/${encodeURIComponent(leadId)}/commercial`),
      api<SalesCollection[]>(`/api/leads/${encodeURIComponent(leadId)}/collections`),
    ]).then(([record, rows]) => { if (active) { setCommercial(record); setCollections(rows); setCurrency(record.currency); setValues({ estimated_value: record.estimated_value || "", proposal_value: record.proposal_value || "", negotiation_value: record.negotiation_value || "", won_value: record.won_value || "", expected_collection: record.expected_collection || "" }); } })
      .catch((cause) => { if (active) setError(cause instanceof ApiError ? cause.message : "Commercial details could not be loaded."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [leadId, refreshKey]);

  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); await reload(); setEditingCollection(null); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Commercial update could not be completed."); }
    finally { setBusy(false); }
  }

  const input = "mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm";
  const financialFields = [
    ["estimated_value", "Estimated value"], ["proposal_value", "Proposal value"],
    ["negotiation_value", "Negotiation value"], ["won_value", "Won value"],
    ["expected_collection", "Expected collection"],
  ] as const;
  const dateValue = (value: string) => localDateTime(value);

  return <section id="commercial" className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-bold text-ink">Commercial / revenue</h2><p className="mt-1 text-sm text-slate-500">Human-entered commercial values. Values are not inferred from the sales stage.</p></div><Link href="/revenue" className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold text-teal">Revenue command center</Link></div>
    {error && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading ? <p className="mt-4 text-sm text-slate-500">Loading commercial details…</p> : commercial && <>
      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{financialFields.map(([key, label]) => <article key={key} className="rounded-lg bg-slate-50 p-3"><p className="text-xs text-slate-500">{label}</p><p className="mt-1 font-bold text-ink">{money(commercial[key], commercial.currency)}</p></article>)}<article className="rounded-lg bg-emerald-50 p-3"><p className="text-xs text-emerald-700">Collected</p><p className="mt-1 font-bold text-emerald-900">{money(commercial.actual_collected, commercial.currency)}</p></article><article className="rounded-lg bg-amber-50 p-3"><p className="text-xs text-amber-800">Outstanding</p><p className="mt-1 font-bold text-amber-950">{money(commercial.outstanding_collection, commercial.currency)}</p></article><article className="rounded-lg bg-slate-50 p-3"><p className="text-xs text-slate-500">Sales status · currency</p><p className="mt-1 font-bold text-ink">{commercial.sales_status} · {commercial.currency}</p></article></div>
      <div className="mt-4 flex flex-wrap gap-2"><button disabled={busy} onClick={() => setEditing((state) => !state)} className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">{editing ? "Close value editor" : "Edit commercial values"}</button>{commercial.sales_status === "WON" && <span className="self-center text-xs text-slate-500">Collections can be recorded for won deals; no payment processing occurs here.</span>}</div>
      {editing && <form className="mt-4 grid gap-3 rounded-lg border border-slate-200 p-4 sm:grid-cols-2 lg:grid-cols-3" onSubmit={(event) => { event.preventDefault(); void perform(async () => { await api(`/api/leads/${encodeURIComponent(leadId)}/commercial`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...Object.fromEntries(financialFields.map(([key]) => [key, values[key] === "" ? null : values[key]])), currency }) }); setEditing(false); }); }}><h3 className="font-semibold text-ink sm:col-span-2 lg:col-span-3">Update financial values · {commercial.currency}</h3>{financialFields.map(([key, label]) => <label key={key} className="text-xs font-semibold text-slate-600">{label}<input className={input} type="number" min="0" step="0.01" value={values[key] || ""} onChange={(event) => setValues((current) => ({ ...current, [key]: event.target.value }))} placeholder="Not set" /></label>)}<label className="text-xs font-semibold text-slate-600">Currency<select className={input} value={currency} onChange={(event) => setCurrency(event.target.value)}>{["INR", "USD", "EUR", "GBP", "AED", "AUD", "CAD", "SGD", "JPY", "CHF", "CNY", "NZD", "ZAR"].map((code) => <option key={code}>{code}</option>)}</select></label><div className="flex items-end"><button disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white">Save values</button></div></form>}
      <section className="mt-6"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold text-ink">Collections ({collections.length})</h3><span className="text-xs text-slate-500">Partial payments are separate records and summed once.</span></div>{commercial.sales_status === "WON" && <form className="mt-3 grid gap-3 rounded-lg bg-slate-50 p-4 sm:grid-cols-2 lg:grid-cols-4" onSubmit={(event) => { event.preventDefault(); void perform(async () => { const body = { amount: collectionAmount, collection_date: new Date(collectionDate).toISOString(), payment_reference: paymentReference.trim() || null, notes: collectionNotes.trim() || null }; if (editingCollection) await api(`/api/collections/${editingCollection.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); else await api(`/api/leads/${encodeURIComponent(leadId)}/collections`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); setCollectionAmount(""); setPaymentReference(""); setCollectionNotes(""); setEditingCollection(null); }); }}><h4 className="font-semibold sm:col-span-2 lg:col-span-4">{editingCollection ? "Edit collection" : "Add collection"}</h4><label className="text-xs font-semibold text-slate-600">Amount<input className={input} type="number" min="0.01" step="0.01" required value={collectionAmount} onChange={(event) => setCollectionAmount(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Collection date<input className={input} type="datetime-local" required value={collectionDate} onChange={(event) => setCollectionDate(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Payment reference<input className={input} maxLength={250} value={paymentReference} onChange={(event) => setPaymentReference(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Notes<input className={input} maxLength={5000} value={collectionNotes} onChange={(event) => setCollectionNotes(event.target.value)} /></label><div className="flex items-end gap-2"><button disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white">{editingCollection ? "Save collection" : "Add collection"}</button>{editingCollection && <button type="button" onClick={() => setEditingCollection(null)} className="rounded-lg border px-3 py-2 text-sm">Cancel</button>}</div></form>}
        {collections.length === 0 ? <p className="mt-3 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">No collection records.</p> : <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[620px] text-left text-sm"><thead className="text-xs uppercase text-slate-400"><tr>{["Date", "Amount", "Reference", "Notes", "Actions"].map((header) => <th key={header} className="px-3 py-2">{header}</th>)}</tr></thead><tbody>{collections.map((item) => <tr key={item.id} className="border-t border-slate-100"><td className="px-3 py-3">{date(item.collection_date)}</td><td className="px-3 py-3 font-semibold">{money(item.amount, item.currency)}</td><td className="px-3 py-3">{item.payment_reference || "—"}</td><td className="px-3 py-3">{item.notes || "—"}</td><td className="px-3 py-3"><div className="flex gap-2"><button onClick={() => { setEditingCollection(item); setCollectionAmount(item.amount); setCollectionDate(dateValue(item.collection_date)); setPaymentReference(item.payment_reference || ""); setCollectionNotes(item.notes || ""); }} className="text-xs font-semibold text-teal">Edit</button><button disabled={busy} onClick={() => void perform(() => api(`/api/collections/${item.id}`, { method: "DELETE" }))} className="text-xs font-semibold text-rose-700">Delete</button></div></td></tr>)}</tbody></table></div>}</section>
    </>}
  </section>;
}

function SalesTasksPanel({ leadId, leadAssignedTo, leadPriority, refreshKey }: { leadId: string; leadAssignedTo: string | null; leadPriority: SalesPriority; refreshKey: number }) {
  const [tasks, setTasks] = useState<SalesTask[]>([]);
  const [outreach, setOutreach] = useState<OutreachArtifact | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [taskType, setTaskType] = useState<SalesTaskType>("FOLLOW_UP");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [assignedTo, setAssignedTo] = useState(leadAssignedTo || "");
  const [priority, setPriority] = useState<SalesPriority>(leadPriority);
  const [dueAt, setDueAt] = useState(() => localDateTime(new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString()));
  const [linkOutreach, setLinkOutreach] = useState(false);
  const [editing, setEditing] = useState<SalesTask | null>(null);

  async function reload() {
    const [taskRows, outreachResponse] = await Promise.all([
      api<SalesTask[]>(`/api/leads/${encodeURIComponent(leadId)}/tasks`),
      api<OutreachReadResponse>(`/api/leads/${encodeURIComponent(leadId)}/outreach`),
    ]);
    setTasks(taskRows); setOutreach(outreachResponse.artifact);
  }

  useEffect(() => {
    let active = true;
    Promise.all([
      api<SalesTask[]>(`/api/leads/${encodeURIComponent(leadId)}/tasks`),
      api<OutreachReadResponse>(`/api/leads/${encodeURIComponent(leadId)}/outreach`),
    ]).then(([taskRows, outreachResponse]) => { if (active) { setTasks(taskRows); setOutreach(outreachResponse.artifact); } })
      .catch(() => { if (active) setError("Sales actions could not be loaded."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [leadId, refreshKey]);

  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); await reload(); setEditing(null); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Sales action could not be completed."); }
    finally { setBusy(false); }
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await perform(async () => {
      const body: SalesTaskCreate = {
        task_type: taskType, title: title.trim(), description: description.trim() || null,
        assigned_to: assignedTo.trim() || null, priority,
        due_at: new Date(dueAt).toISOString(),
        related_outreach_artifact_id: linkOutreach && outreach ? outreach.id : null,
      };
      await api<SalesTask>(`/api/leads/${encodeURIComponent(leadId)}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setTitle(""); setDescription(""); setLinkOutreach(false);
    });
  }

  const active = tasks.filter((task) => task.status === "OPEN" || task.status === "IN_PROGRESS");
  const completed = tasks.filter((task) => task.status === "COMPLETED");
  const overdue = active.filter((task) => task.is_overdue);
  const upcoming = active.filter((task) => !task.is_due && new Date(task.due_at).getTime() <= Date.now() + 7 * 24 * 60 * 60 * 1000);
  const nextAction = active.filter((task) => !task.is_overdue).sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at))[0] || overdue[0];
  const input = "mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm";

  function taskCard(task: SalesTask) {
    return <article key={task.id} className="rounded-lg border border-slate-200 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2"><div><p className="font-semibold text-ink">{task.title}</p><p className="mt-1 text-xs text-slate-500">{task.task_type.replaceAll("_", " ")} · {task.status.replaceAll("_", " ")} · {task.priority}</p></div><span className={`rounded-full px-2 py-1 text-xs font-semibold ${task.is_overdue ? "bg-rose-50 text-rose-800" : "bg-slate-100 text-slate-600"}`}>{task.is_overdue ? "Overdue" : task.status === "COMPLETED" ? "Completed" : "Due " + date(task.due_at)}</span></div>
      <p className="mt-2 text-xs text-slate-500">Assigned to: {task.assigned_to || "Unassigned"}{task.related_outreach_artifact_id ? " · Linked to outreach preparation" : ""}</p>
      {task.description && <p className="mt-2 whitespace-pre-wrap text-sm text-slate-600">{task.description}</p>}
      {active.includes(task) && <div className="mt-3 flex flex-wrap gap-2"><button disabled={busy} onClick={() => { setEditing(task); setTitle(task.title); setDescription(task.description || ""); setAssignedTo(task.assigned_to || ""); setPriority(task.priority); setDueAt(localDateTime(task.due_at)); }} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-semibold">Edit</button><button disabled={busy} onClick={() => void perform(() => api(`/api/tasks/${task.id}/complete`, { method: "POST" }))} className="rounded-md bg-emerald-700 px-3 py-1.5 text-xs font-semibold text-white">Complete</button><button disabled={busy} onClick={() => void perform(() => api(`/api/tasks/${task.id}/cancel`, { method: "POST" }))} className="rounded-md border border-rose-300 px-3 py-1.5 text-xs font-semibold text-rose-800">Cancel</button></div>}
    </article>;
  }

  return <section id="sales-tasks" className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-bold text-ink">Sales actions</h2><p className="mt-1 text-sm text-slate-500">Human-managed tasks and outcomes. Prepared and approved outreach is never sent by this system.</p></div><Link href="/leads/tasks" className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold text-teal">Daily task view</Link></div>
    {error && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading ? <p className="mt-4 text-sm text-slate-500">Loading tasks…</p> : <>
      <div className="mt-4 rounded-lg bg-teal/5 p-4"><p className="text-xs font-bold uppercase tracking-wide text-teal">Next action</p>{nextAction ? <p className="mt-1 text-sm font-semibold text-ink">{nextAction.title} · {date(nextAction.due_at)} · {nextAction.assigned_to || "Unassigned"} · {nextAction.priority} · {nextAction.status}</p> : <p className="mt-1 text-sm text-slate-500">No open sales action is scheduled.</p>}</div>
      <form onSubmit={create} className="mt-5 grid gap-3 rounded-lg border border-slate-200 p-4 sm:grid-cols-2 lg:grid-cols-4"><h3 className="font-semibold text-ink sm:col-span-2 lg:col-span-4">Create task</h3><label className="text-xs font-semibold text-slate-600">Task type<select className={input} value={taskType} onChange={(event) => setTaskType(event.target.value as SalesTaskType)}>{["FOLLOW_UP", "CALL", "EMAIL", "WHATSAPP", "MEETING", "PROPOSAL", "REVIEW_OUTREACH", "OTHER"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Title<input className={input} required maxLength={250} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Next sales action" /></label><label className="text-xs font-semibold text-slate-600">Due date<input className={input} type="datetime-local" required value={dueAt} onChange={(event) => setDueAt(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Priority<select className={input} value={priority} onChange={(event) => setPriority(event.target.value as SalesPriority)}>{["LOW", "MEDIUM", "HIGH", "URGENT"].map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Assigned to<input className={input} maxLength={120} value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)} placeholder="Unassigned" /></label><label className="text-xs font-semibold text-slate-600 sm:col-span-2">Description<textarea className={input} maxLength={10000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>{outreach && <label className="flex items-center gap-2 self-end text-xs text-slate-600"><input type="checkbox" checked={linkOutreach} onChange={(event) => setLinkOutreach(event.target.checked)} />Link latest outreach ({outreach.status.replaceAll("_", " ")})</label>}<div className="flex items-end"><button disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Create task</button></div></form>
      {editing && <form className="mt-4 grid gap-3 rounded-lg border border-teal/30 bg-teal/5 p-4 sm:grid-cols-2 lg:grid-cols-4" onSubmit={(event) => { event.preventDefault(); void perform(() => api(`/api/tasks/${editing.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: title.trim(), description: description.trim() || null, assigned_to: assignedTo.trim() || null, priority, due_at: new Date(dueAt).toISOString() }) })); }}><h3 className="font-semibold sm:col-span-2 lg:col-span-4">Edit task</h3><label className="text-xs font-semibold">Title<input className={input} required maxLength={250} value={title} onChange={(event) => setTitle(event.target.value)} /></label><label className="text-xs font-semibold">Due date<input className={input} type="datetime-local" required value={dueAt} onChange={(event) => setDueAt(event.target.value)} /></label><label className="text-xs font-semibold">Assigned to<input className={input} value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)} /></label><label className="text-xs font-semibold">Priority<select className={input} value={priority} onChange={(event) => setPriority(event.target.value as SalesPriority)}>{["LOW", "MEDIUM", "HIGH", "URGENT"].map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold sm:col-span-2">Description<textarea className={input} value={description} onChange={(event) => setDescription(event.target.value)} /></label><div className="flex items-end gap-2"><button disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white">Save</button><button type="button" onClick={() => setEditing(null)} className="rounded-lg border px-4 py-2 text-sm">Cancel</button></div></form>}
      <div className="mt-5 grid gap-5 lg:grid-cols-2"><section><h3 className="font-semibold text-ink">Open tasks ({active.length})</h3><div className="mt-3 space-y-2">{active.length ? active.map(taskCard) : <p className="text-sm text-slate-500">No open tasks.</p>}</div></section><section><h3 className="font-semibold text-ink">Overdue ({overdue.length})</h3><div className="mt-3 space-y-2">{overdue.length ? overdue.map(taskCard) : <p className="text-sm text-slate-500">No overdue tasks.</p>}<h3 className="pt-4 font-semibold text-ink">Upcoming next 7 days ({upcoming.length})</h3>{upcoming.length ? <div className="mt-3 space-y-2">{upcoming.map(taskCard)}</div> : <p className="mt-2 text-sm text-slate-500">No upcoming tasks in the next seven days.</p>}</div></section></div>
      <section className="mt-5"><h3 className="font-semibold text-ink">Completed ({completed.length})</h3><div className="mt-3 grid gap-2 sm:grid-cols-2">{completed.length ? completed.map(taskCard) : <p className="text-sm text-slate-500">No completed tasks.</p>}</div></section>
    </>}
  </section>;
}

function OutreachPanel({ leadId, leadEmail, leadPhone, onOutcomeRecorded }: { leadId: string; leadEmail: string | null; leadPhone: string | null; onOutcomeRecorded: (activity: SalesActivity) => void }) {
  const [data, setData] = useState<OutreachReadResponse | null>(null);
  const [executions, setExecutions] = useState<OutreachExecution[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editJson, setEditJson] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [sendChannel, setSendChannel] = useState<"EMAIL" | "WHATSAPP">("EMAIL");
  const [manualMethod, setManualMethod] = useState<"EMAIL" | "WHATSAPP">("EMAIL");
  const [confirmation, setConfirmation] = useState<{ channel: "EMAIL" | "WHATSAPP"; retryId?: string } | null>(null);
  const [manualConfirmId, setManualConfirmId] = useState<string | null>(null);
  const [manualConfirmed, setManualConfirmed] = useState(false);
  const [outcomeExecutionId, setOutcomeExecutionId] = useState<string | null>(null);
  const [outcomeActivityType, setOutcomeActivityType] = useState<Exclude<SalesActivityType, "STATUS_CHANGE">>("EMAIL");
  const [outcomeValue, setOutcomeValue] = useState<SalesActivityOutcome>("CONNECTED");
  const [outcomeNote, setOutcomeNote] = useState("");
  const [outcomeOccurredAt, setOutcomeOccurredAt] = useState("");
  const [outcomeIdempotencyKey, setOutcomeIdempotencyKey] = useState("");

  async function reload() {
    const [result, rows] = await Promise.all([
      api<OutreachReadResponse>(`/api/leads/${encodeURIComponent(leadId)}/outreach`),
      api<OutreachExecution[]>(`/api/leads/${encodeURIComponent(leadId)}/outreach/executions`),
    ]);
    setData(result);
    setExecutions(rows);
    setEditing(false);
  }

  useEffect(() => {
    let active = true;
    Promise.all([
      api<OutreachReadResponse>(`/api/leads/${encodeURIComponent(leadId)}/outreach`),
      api<OutreachExecution[]>(`/api/leads/${encodeURIComponent(leadId)}/outreach/executions`),
    ])
      .then(([result, rows]) => { if (active) { setData(result); setExecutions(rows); } })
      .catch(() => { if (active) setError("Outreach preparation could not be loaded."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [leadId]);

  async function perform<T = unknown>(action: () => Promise<T>, successMessage?: string): Promise<T | null> {
    setBusy(true); setError("");
    try {
      const result = await action(); await reload();
      if (successMessage) setError(successMessage);
      setRejecting(false); setRejectReason("");
      return result;
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "The outreach action could not be completed.");
      try { await reload(); } catch { /* Keep the action error visible. */ }
      return null;
    } finally { setBusy(false); }
  }

  function beginOutcome(execution: OutreachExecution) {
    setOutcomeExecutionId(execution.id);
    setOutcomeActivityType(execution.channel === "MANUAL" ? (execution.manual_method || "OTHER") : execution.channel);
    setOutcomeValue("CONNECTED"); setOutcomeNote("");
    setOutcomeOccurredAt(localDateTime(new Date().toISOString()));
    setOutcomeIdempotencyKey(crypto.randomUUID());
  }

  async function submitOutcome(event: FormEvent<HTMLFormElement>, execution: OutreachExecution) {
    event.preventDefault();
    const recordedActivity = await perform(() => api<SalesActivity>(`/api/outreach/executions/${execution.id}/outcomes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ activity_type: outcomeActivityType, outcome: outcomeValue, note: outcomeNote, occurred_at: new Date(outcomeOccurredAt).toISOString(), idempotency_key: outcomeIdempotencyKey }),
    }));
    if (recordedActivity) {
      setOutcomeExecutionId(null); setOutcomeNote(""); setOutcomeIdempotencyKey("");
      onOutcomeRecorded(recordedActivity);
    }
  }

  function beginEdit(artifact: OutreachArtifact) {
    if (!artifact.content) return;
    setEditJson(JSON.stringify(artifact.content, null, 2)); setEditing(true); setError("");
  }

  const artifact = data?.artifact;
  const current = Boolean(artifact?.is_current && artifact.status !== "STALE");
  const approvedCurrent = Boolean(artifact?.status === "APPROVED" && current);
  const latestFailed = executions.find((execution) => execution.status === "FAILED");
  const claimSections = artifact?.content ? [
    ["Email opening", artifact.content.email.opening], ["Email body", artifact.content.email.body],
    ["Email call to action", artifact.content.email.call_to_action], ["WhatsApp opening", artifact.content.whatsapp.opening],
    ["WhatsApp message", artifact.content.whatsapp.message], ["WhatsApp call to action", artifact.content.whatsapp.call_to_action],
    ["Call opening", artifact.content.call_script.opening], ["Key problem or opportunity", artifact.content.call_script.key_problem],
    ["Objections and talking points", artifact.content.call_script.objections_and_talking_points], ["Call closing", artifact.content.call_script.closing],
  ] as const : [];

  function preview(channel: "EMAIL" | "WHATSAPP") {
    if (!artifact?.content) return { subject: null as string | null, text: "" };
    const parts = channel === "EMAIL"
      ? [artifact.content.email.opening.text, artifact.content.email.body.text, artifact.content.email.call_to_action.text]
      : [artifact.content.whatsapp.opening.text, artifact.content.whatsapp.message.text, artifact.content.whatsapp.call_to_action.text];
    return { subject: channel === "EMAIL" ? artifact.content.email.subject : null, text: parts.filter(Boolean).join("\n\n") };
  }

  async function createManualExecution() {
    await perform(() => api<OutreachExecution>(`/api/outreach/${artifact?.id}/send`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel: "MANUAL", manual_method: manualMethod, idempotency_key: crypto.randomUUID() }),
    }));
  }

  async function confirmExternalSend() {
    if (!artifact || !confirmation) return;
    const key = crypto.randomUUID();
    const action = confirmation.retryId
      ? api<OutreachExecution>(`/api/outreach/executions/${confirmation.retryId}/retry`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: key }),
        })
      : api<OutreachExecution>(`/api/outreach/${artifact.id}/send`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel: confirmation.channel, idempotency_key: key }),
        });
    await perform(() => action);
    setConfirmation(null);
  }

  return <section id="outreach" className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-bold text-ink">Outreach preparation</h2><p className="mt-1 text-sm font-semibold text-teal">Prepared for review — not sent.</p></div>{artifact && <span className={`rounded-full px-3 py-1 text-xs font-semibold ${artifact.status === "APPROVED" ? "bg-emerald-50 text-emerald-700" : artifact.status === "STALE" ? "bg-rose-50 text-rose-700" : "bg-amber-50 text-amber-800"}`}>{artifact.status.replaceAll("_", " ")} · {current ? "Current" : "Stale"}</span>}</div>
    {error && <p role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading ? <p className="mt-4 text-sm text-slate-500">Loading outreach…</p> : <>
      {data?.current_context && <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 rounded-lg bg-slate-50 p-3 text-xs text-slate-600"><span>Sales stage: <strong>{data.current_context.sales_status}</strong></span><span>Deterministic score: <strong>{data.current_context.score ?? "—"}</strong></span><span>Opportunities: <strong>{data.current_context.opportunity_count}</strong></span><span>Personalization: <strong>{data.current_context.personalization_available ? "Available" : "Unavailable"}</strong></span><span>Recommended services: <strong>{data.current_context.recommended_services.join(", ") || "None"}</strong></span></div>}
      {!artifact && <p className="mt-4 text-sm text-slate-500">No outreach draft exists for this lead. Generation uses only the selected lead’s current validated intelligence.</p>}
      {artifact && <div className="mt-4 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-500"><span>Generated: {date(artifact.generated_at)}</span><span>Run: <code>{artifact.enrichment_run_id.slice(0, 8)}</code></span><span>{artifact.contract_version} · {artifact.prompt_version}</span>{artifact.reviewed_at && <span>Reviewed: {date(artifact.reviewed_at)}</span>}</div>}
      {artifact?.generation_error && <p className="mt-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">{artifact.generation_error} No partial draft was saved.</p>}
      {!artifact?.content && artifact && <p className="mt-3 text-sm text-slate-500">This artifact has no validated outreach content.</p>}
      {artifact?.content && !editing && <div className="mt-5 space-y-5">
        <article className="rounded-lg border border-slate-200 p-4"><h3 className="font-semibold text-ink">Email draft</h3><p className="mt-2 text-sm"><span className="font-semibold">Subject:</span> {artifact.content.email.subject}</p>{claimSections.slice(0, 3).map(([title, section]) => <div key={title} className="mt-3"><p className="text-xs font-bold uppercase text-slate-400">{title}</p>{section.claims.map((claim, index) => <ClaimCard key={`${title}-${index}`} claim={claim} />)}</div>)}</article>
        <article className="rounded-lg border border-slate-200 p-4"><h3 className="font-semibold text-ink">WhatsApp draft</h3>{claimSections.slice(3, 6).map(([title, section]) => <div key={title} className="mt-3"><p className="text-xs font-bold uppercase text-slate-400">{title}</p>{section.claims.map((claim, index) => <ClaimCard key={`${title}-${index}`} claim={claim} />)}</div>)}</article>
        <article className="rounded-lg border border-slate-200 p-4"><h3 className="font-semibold text-ink">Call script</h3>{claimSections.slice(6).map(([title, section]) => <div key={title} className="mt-3"><p className="text-xs font-bold uppercase text-slate-400">{title}</p>{section.claims.map((claim, index) => <ClaimCard key={`${title}-${index}`} claim={claim} />)}</div>)}<p className="mt-4 text-xs text-slate-500">Recommended services: {artifact.content.recommended_service_ids.join(", ") || "None"}</p><ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-slate-600">{artifact.content.call_script.discovery_questions.map((question) => <li key={question}>{question}</li>)}</ul></article>
        <article className="rounded-lg border border-slate-200 p-4"><h3 className="font-semibold text-ink">Follow-up suggestions</h3><ul className="mt-3 space-y-2 text-sm text-slate-600">{artifact.content.follow_ups.map((item, index) => <li key={`${item.step}-${index}`}><strong>{item.step.replaceAll("_", " ")}</strong> · {item.timing.replaceAll("_", " ")} · {item.purpose.replaceAll("_", " ")}</li>)}</ul></article>
      </div>}
      {editing && artifact?.content && <div className="mt-5"><label className="text-xs font-semibold text-slate-600">Structured draft JSON<textarea className="mt-2 block min-h-96 w-full rounded-lg border border-slate-300 p-3 font-mono text-xs" value={editJson} onChange={(event) => setEditJson(event.target.value)} /></label><p className="mt-2 text-xs text-slate-500">Edits are checked against the current evidence, opportunities, sales stage, and deterministic services.</p><div className="mt-3 flex gap-2"><button disabled={busy} onClick={() => void perform(async () => { const content = JSON.parse(editJson) as OutreachContent; await api(`/api/leads/${encodeURIComponent(leadId)}/outreach`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) }); })} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Save edits</button><button disabled={busy} onClick={() => setEditing(false)} className="rounded-lg border border-slate-300 px-4 py-2 text-sm">Cancel</button></div></div>}
      <div className="mt-5 flex flex-wrap gap-2">
        <button disabled={busy || data?.current_context === null || data?.current_context === undefined || ["WON", "LOST"].includes(data.current_context.sales_status)} onClick={() => void perform(() => api(`/api/leads/${encodeURIComponent(leadId)}/outreach/generate`, { method: "POST" }))} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Working…" : artifact ? "Generate new draft" : "Generate outreach draft"}</button>
        {artifact?.content && current && ["DRAFT", "READY_FOR_REVIEW"].includes(artifact.status) && !editing && <button disabled={busy} onClick={() => beginEdit(artifact)} className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">Edit</button>}
        {artifact?.status === "READY_FOR_REVIEW" && current && <button disabled={busy} onClick={() => void perform(() => api(`/api/leads/${encodeURIComponent(leadId)}/outreach/approve`, { method: "POST" }))} className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Approve</button>}
        {artifact && current && ["DRAFT", "READY_FOR_REVIEW"].includes(artifact.status) && <button disabled={busy} onClick={() => setRejecting((value) => !value)} className="rounded-lg border border-rose-300 px-4 py-2 text-sm font-semibold text-rose-800">Reject</button>}
      </div>
      {artifact?.status === "STALE" && <p className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">Outreach must be regenerated/revalidated before sending.</p>}
      {approvedCurrent && artifact?.content && <section className="mt-5 rounded-lg border border-emerald-200 bg-emerald-50/40 p-4">
        <h3 className="font-semibold text-ink">Execute approved outreach</h3>
        <p className="mt-1 text-sm text-slate-600">Each send requires your explicit confirmation. Provider credentials remain on the server.</p>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="text-xs font-semibold text-slate-600">External channel<select className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={sendChannel} onChange={(event) => setSendChannel(event.target.value as "EMAIL" | "WHATSAPP")}><option value="EMAIL">Email</option><option value="WHATSAPP">WhatsApp</option></select></label>
          <button disabled={busy} onClick={() => setConfirmation({ channel: sendChannel })} className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Send…</button>
          <label className="text-xs font-semibold text-slate-600">Manual method<select className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={manualMethod} onChange={(event) => setManualMethod(event.target.value as "EMAIL" | "WHATSAPP")}><option value="EMAIL">Email</option><option value="WHATSAPP">WhatsApp</option></select></label>
          <button disabled={busy} onClick={() => void createManualExecution()} className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold disabled:opacity-50">Prepare manual send</button>
        </div>
      </section>}
      {confirmation && artifact?.content && <section role="dialog" aria-modal="true" aria-labelledby="send-confirm-title" className="mt-4 rounded-lg border-2 border-amber-300 bg-amber-50 p-4">
        <h3 id="send-confirm-title" className="font-bold text-amber-950">Confirm external send</h3>
        <p className="mt-1 text-sm text-amber-900">This action sends the approved message to the lead through the configured provider.</p>
        <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2"><div><dt className="font-semibold">Channel</dt><dd>{confirmation.channel}</dd></div><div><dt className="font-semibold">Recipient</dt><dd className="break-all">{confirmation.channel === "EMAIL" ? leadEmail || "No email on lead" : leadPhone || "No phone on lead"}</dd></div>{preview(confirmation.channel).subject && <div className="sm:col-span-2"><dt className="font-semibold">Subject</dt><dd>{preview(confirmation.channel).subject}</dd></div>}</dl>
        <div className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-amber-200 bg-white p-3 text-sm text-slate-700">{preview(confirmation.channel).text}</div>
        <div className="mt-4 flex gap-2"><button disabled={busy} onClick={() => void confirmExternalSend()} className="rounded-lg bg-amber-800 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{confirmation.retryId ? "Confirm retry" : "Confirm and send"}</button><button disabled={busy} onClick={() => setConfirmation(null)} className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm">Cancel</button></div>
      </section>}
      {executions.length > 0 && <section className="mt-5 rounded-lg border border-slate-200 p-4">
        <h3 className="font-semibold text-ink">Execution history</h3>
        <div className="mt-3 space-y-3">{executions.map((execution) => <article key={execution.id} className="rounded-lg bg-slate-50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-semibold text-ink">{execution.channel}{execution.manual_method ? ` · ${execution.manual_method} method` : ""} · {execution.status.replaceAll("_", " ")}</p><span className="text-xs text-slate-500">Attempt {execution.attempt_number} · {execution.provider}</span></div>
          <dl className="mt-2 grid gap-x-5 gap-y-1 text-xs text-slate-600 sm:grid-cols-2 lg:grid-cols-3"><div>Requested: {date(execution.requested_at)}</div><div>Sent: {date(execution.sent_at)}</div><div>Delivered: {date(execution.delivered_at)}</div><div className="break-all">Recipient: {execution.recipient || "—"}</div><div className="break-all">Message ID: {execution.provider_message_id || "—"}</div><div>Error: {execution.error_message || "—"}</div></dl>
          <details className="mt-2"><summary className="cursor-pointer text-xs font-semibold text-teal">Approved content snapshot</summary><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-white p-2 text-xs">{execution.content_snapshot.text || JSON.stringify(execution.content_snapshot.content, null, 2)}</pre></details>
          {(execution.status === "SENT" || execution.status === "DELIVERED") && execution.sent_at && <div className="mt-3">{outcomeExecutionId !== execution.id ? <button type="button" disabled={busy} onClick={() => beginOutcome(execution)} className="rounded-lg border border-teal/40 bg-white px-3 py-1.5 text-xs font-semibold text-teal disabled:opacity-50">Record outcome</button> : <form onSubmit={(event) => void submitOutcome(event, execution)} className="rounded-lg border border-teal/20 bg-white p-3"><p className="text-xs text-slate-600">Record a human-observed activity after this send. This does not attribute the outcome to the message.</p><p className="mt-2 text-xs font-semibold text-slate-700">{execution.channel}{execution.manual_method ? ` · ${execution.manual_method} method` : ""} · sent {date(execution.sent_at)} · execution <code>{execution.id.slice(0, 8)}</code></p><div className="mt-3 grid gap-3 sm:grid-cols-2"><label className="text-xs font-semibold text-slate-600">Activity type<select required className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={outcomeActivityType} onChange={(event) => setOutcomeActivityType(event.target.value as Exclude<SalesActivityType, "STATUS_CHANGE">)}>{["NOTE", "CALL", "EMAIL", "WHATSAPP", "MEETING", "FOLLOW_UP", "PROPOSAL", "OTHER"].map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Outcome<select required className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={outcomeValue} onChange={(event) => setOutcomeValue(event.target.value as SalesActivityOutcome)}>{["NO_RESPONSE", "CONNECTED", "INTERESTED", "NOT_INTERESTED", "CALLBACK_REQUESTED", "MEETING_BOOKED", "PROPOSAL_REQUESTED", "PROPOSAL_SENT", "NEGOTIATION_STARTED", "WON", "LOST", "INVALID_CONTACT"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Occurred at<input required type="datetime-local" className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={outcomeOccurredAt} onChange={(event) => setOutcomeOccurredAt(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600 sm:col-span-2">Note<textarea required maxLength={5000} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={outcomeNote} onChange={(event) => setOutcomeNote(event.target.value)} /></label></div><div className="mt-3 flex gap-2"><button disabled={busy || !outcomeNote.trim() || !outcomeOccurredAt} className="rounded-lg bg-teal px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">{busy ? "Saving…" : "Save outcome"}</button><button type="button" disabled={busy} onClick={() => setOutcomeExecutionId(null)} className="rounded-lg border border-slate-300 px-3 py-2 text-xs">Cancel</button></div></form>}</div>}
          {execution.status === "FAILED" && approvedCurrent && execution.channel !== "MANUAL" && latestFailed?.id === execution.id && <button disabled={busy} onClick={() => setConfirmation({ channel: execution.channel as "EMAIL" | "WHATSAPP", retryId: execution.id })} className="mt-3 rounded-lg border border-amber-400 bg-white px-3 py-1.5 text-xs font-semibold text-amber-900 disabled:opacity-50">Retry…</button>}
          {execution.status === "QUEUED" && execution.channel === "MANUAL" && <div className="mt-3"><div className="flex flex-wrap gap-2">{approvedCurrent && <button disabled={busy} onClick={() => { setManualConfirmId(execution.id); setManualConfirmed(false); }} className="rounded-lg border border-teal/40 bg-white px-3 py-1.5 text-xs font-semibold text-teal">Mark as manually sent…</button>}<button disabled={busy} onClick={() => void perform(() => api(`/api/outreach/executions/${execution.id}/cancel`, { method: "POST" }))} className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 disabled:opacity-50">Cancel manual preparation</button></div>{manualConfirmId === execution.id && <div className="mt-2 rounded border border-slate-200 bg-white p-3"><label className="flex items-start gap-2 text-xs text-slate-700"><input type="checkbox" checked={manualConfirmed} onChange={(event) => setManualConfirmed(event.target.checked)} /><span>I confirm I sent this approved message manually through {execution.manual_method || "my chosen channel"}.</span></label><div className="mt-2 flex gap-2"><button disabled={busy || !manualConfirmed} onClick={() => void perform(() => api(`/api/outreach/executions/${execution.id}/mark-manual`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmed: true }) }))} className="rounded bg-teal px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50">Confirm manual send</button><button onClick={() => setManualConfirmId(null)} className="rounded border px-3 py-1.5 text-xs">Cancel</button></div></div>}</div>}
        </article>)}</div>
      </section>}
      {rejecting && <form className="mt-4 flex flex-col gap-2 sm:flex-row" onSubmit={(event) => { event.preventDefault(); void perform(() => api(`/api/leads/${encodeURIComponent(leadId)}/outreach/reject`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: rejectReason }) })); }}><input required maxLength={1000} value={rejectReason} onChange={(event) => setRejectReason(event.target.value)} className="min-w-64 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="Reason for rejection" /><button disabled={busy} className="rounded-lg bg-rose-700 px-4 py-2 text-sm font-semibold text-white">Confirm rejection</button></form>}
      {data?.current_context && ["WON", "LOST"].includes(data.current_context.sales_status) && <p className="mt-3 text-xs text-slate-500">Outreach generation is disabled for WON and LOST leads.</p>}
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
  const [taskRefreshKey, setTaskRefreshKey] = useState(0);
  const [newOutcomeActivity, setNewOutcomeActivity] = useState<SalesActivity | null>(null);

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
        <article id="business-summary" className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
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

      <SalesWorkflowPanel leadId={id} intelligence={intel} refreshKey={taskRefreshKey} newOutcomeActivity={newOutcomeActivity} onTasksChanged={() => setTaskRefreshKey((key) => key + 1)} />
      <CommercialPanel leadId={id} refreshKey={taskRefreshKey} />
      <SalesTasksPanel leadId={id} leadAssignedTo={lead.assigned_to} leadPriority={lead.priority} refreshKey={taskRefreshKey} />
      <OutreachPanel leadId={id} leadEmail={lead.email} leadPhone={lead.phone} onOutcomeRecorded={(activity) => { setNewOutcomeActivity(activity); setTaskRefreshKey((key) => key + 1); }} />
      <div id="seo"><SeoSection leadId={id} website={lead.website} /></div>
      <AISalesIntelligence leadId={id} onTaskCreated={() => setTaskRefreshKey((key) => key + 1)} />
      <AISalesCopilot leadId={id} />

      {intelligenceState !== "error" && <>
      <section id="opportunities" className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
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
