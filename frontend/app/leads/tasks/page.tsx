"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, LeadListItem, SalesPriority, SalesTask, SalesTaskStatus, SalesTaskSummary, SalesTaskType } from "@/lib/api";

const TASK_TYPES: SalesTaskType[] = ["FOLLOW_UP", "CALL", "EMAIL", "WHATSAPP", "MEETING", "PROPOSAL", "REVIEW_OUTREACH", "OTHER"];
const PRIORITIES: SalesPriority[] = ["LOW", "MEDIUM", "HIGH", "URGENT"];
const STATUSES: SalesTaskStatus[] = ["OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"];

function displayDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

export default function TasksPage() {
  const [summary, setSummary] = useState<SalesTaskSummary | null>(null);
  const [filteredTasks, setFilteredTasks] = useState<SalesTask[] | null>(null);
  const [leads, setLeads] = useState<LeadListItem[]>([]);
  const [status, setStatus] = useState("");
  const [taskType, setTaskType] = useState("");
  const [priority, setPriority] = useState("");
  const [assignedTo, setAssignedTo] = useState("");
  const [leadId, setLeadId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const filtered = Boolean(status || taskType || priority || assignedTo.trim() || leadId);
  const queryString = useMemo(() => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (taskType) params.set("task_type", taskType);
    if (priority) params.set("priority", priority);
    if (assignedTo.trim()) params.set("assigned_to", assignedTo.trim());
    if (leadId) params.set("lead_id", leadId);
    return params.toString();
  }, [status, taskType, priority, assignedTo, leadId]);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      if (filtered) {
        const rows = await api<SalesTask[]>(`/api/tasks?${queryString}`);
        setFilteredTasks(rows); setSummary(null);
      } else {
        const data = await api<SalesTaskSummary>("/api/tasks/summary");
        setSummary(data); setFilteredTasks(null);
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Tasks could not be loaded.");
    } finally { setLoading(false); }
  }, [filtered, queryString]);

  useEffect(() => {
    void api<LeadListItem[]>("/api/leads?page=1&page_size=100").then(setLeads).catch(() => setLeads([]));
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function updateTask(task: SalesTask, action: "complete" | "cancel") {
    setBusy(true); setError("");
    try {
      await api(`/api/tasks/${task.id}/${action}`, { method: "POST" });
      await load();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Task could not be updated."); }
    finally { setBusy(false); }
  }

  let today: SalesTask[] = summary?.today || [];
  let overdue: SalesTask[] = summary?.overdue || [];
  let upcoming: SalesTask[] = summary?.upcoming || [];
  let completed: SalesTask[] = [];
  let cancelled: SalesTask[] = [];
  if (filteredTasks) {
    const now = Date.now();
    completed = filteredTasks.filter((task) => task.status === "COMPLETED");
    cancelled = filteredTasks.filter((task) => task.status === "CANCELLED");
    overdue = filteredTasks.filter((task) => task.is_overdue);
    today = filteredTasks.filter((task) => task.status !== "COMPLETED" && task.status !== "CANCELLED" && !task.is_overdue && new Date(task.due_at).getTime() < new Date().setHours(24, 0, 0, 0));
    upcoming = filteredTasks.filter((task) => task.status !== "COMPLETED" && task.status !== "CANCELLED" && new Date(task.due_at).getTime() >= now && new Date(task.due_at).getTime() <= now + 7 * 24 * 60 * 60 * 1000 && !today.some((item) => item.id === task.id));
  }

  function taskRow(task: SalesTask) {
    const actionable = task.status === "OPEN" || task.status === "IN_PROGRESS";
    return <article key={task.id} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><div className="flex flex-wrap items-start justify-between gap-3"><div><Link href={`/leads/${task.lead_id}`} className="text-xs font-semibold text-teal hover:underline">{task.lead_company_name || "Open lead"}</Link><h3 className="mt-1 font-bold text-ink">{task.title}</h3><p className="mt-1 text-xs text-slate-500">{task.task_type.replaceAll("_", " ")} · {task.status.replaceAll("_", " ")} · {task.priority}</p></div><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${task.is_overdue ? "bg-rose-50 text-rose-800" : "bg-slate-100 text-slate-600"}`}>{task.is_overdue ? "Overdue" : displayDate(task.due_at)}</span></div><p className="mt-3 text-xs text-slate-500">Assigned to {task.assigned_to || "Unassigned"}{task.related_outreach_artifact_id ? " · Outreach-linked" : ""}</p>{task.description && <p className="mt-2 text-sm text-slate-600">{task.description}</p>}{actionable && <div className="mt-3 flex gap-2"><button disabled={busy} onClick={() => void updateTask(task, "complete")} className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Complete</button><button disabled={busy} onClick={() => void updateTask(task, "cancel")} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold disabled:opacity-50">Cancel</button></div>}</article>;
  }

  const groups = [
    ["Today's tasks", today], ["Overdue", overdue], ["Upcoming next 7 days", upcoming], ["Completed", completed], ["Cancelled", cancelled],
  ] as const;

  return <main className="mx-auto min-h-screen max-w-6xl px-4 py-6 sm:px-8 lg:py-10"><header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-6"><div><Link href="/leads" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link><h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Sales task workspace</h1><p className="mt-1 text-slate-500">Daily execution view for human-managed sales actions. Nothing is sent automatically.</p></div><Link href="/leads" className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">All leads</Link></header>
    <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-bold text-ink">Filter tasks</h2><div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5"><label className="text-xs font-semibold text-slate-600">Status<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">Any status</option>{STATUSES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Task type<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={taskType} onChange={(event) => setTaskType(event.target.value)}><option value="">Any type</option>{TASK_TYPES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Priority<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={priority} onChange={(event) => setPriority(event.target.value)}><option value="">Any priority</option>{PRIORITIES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Assigned to<input className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)} placeholder="Owner label" /></label><label className="text-xs font-semibold text-slate-600">Lead<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={leadId} onChange={(event) => setLeadId(event.target.value)}><option value="">All leads</option>{leads.map((lead) => <option key={lead.id} value={lead.id}>{lead.company_name}</option>)}</select></label></div></section>
    {error && <p role="alert" className="mt-5 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading ? <p className="mt-8 text-center text-sm text-slate-500">Loading sales tasks…</p> : <div className="mt-6 space-y-6">{groups.map(([name, tasks]) => <section key={name}><div className="mb-3 flex items-center justify-between"><h2 className="text-lg font-bold text-ink">{name}</h2><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">{tasks.length}</span></div>{tasks.length ? <div className="grid gap-3 md:grid-cols-2">{tasks.map(taskRow)}</div> : <p className="rounded-xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">No tasks in this section.</p>}</section>)}</div>}
  </main>;
}
