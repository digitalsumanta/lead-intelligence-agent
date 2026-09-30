"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, SalesSupervisorRun, SalesSupervisorHistory, SupervisorTaskExecutionSummary, TaskAgentExecution, TaskAgentExecutionHistory } from "@/lib/api";

const card = "rounded-xl border border-slate-200 bg-white p-5 shadow-sm";
const label = "text-sm text-slate-500";

function freshness(run: SalesSupervisorRun) {
  if (run.status === "FAILED") return "Run failed";
  return run.is_current ? "Current context" : "Stale context";
}

export default function SalesSupervisorPage() {
  const [run, setRun] = useState<SalesSupervisorRun | null>(null);
  const [history, setHistory] = useState<SalesSupervisorRun[]>([]);
  const [executions, setExecutions] = useState<TaskAgentExecution[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [current, past] = await Promise.all([
        api<SalesSupervisorRun | null>("/api/sales-supervisor/current"),
        api<SalesSupervisorHistory>("/api/sales-supervisor/history?limit=20"),
      ]);
      const taskRuns = await api<TaskAgentExecutionHistory>("/api/agent/tasks/executions?limit=30");
      setRun(current);
      setHistory(past.items);
      setExecutions(taskRuns.items);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Sales Supervisor data could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function runReview() {
    setRunning(true);
    setError("");
    try {
      const created = await api<SalesSupervisorRun>("/api/sales-supervisor/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "PIPELINE" }),
      });
      setRun(created);
      const past = await api<SalesSupervisorHistory>("/api/sales-supervisor/history?limit=20");
      setHistory(past.items);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Sales Supervisor review could not be completed.");
    } finally {
      setRunning(false);
    }
  }

  async function openHistoricalRun(runId: string) {
    setError("");
    try {
      setRun(await api<SalesSupervisorRun>(`/api/sales-supervisor/history/${encodeURIComponent(runId)}`));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "That supervisor run could not be loaded.");
    }
  }

  async function executeTasks() {
    setRunning(true);
    setError("");
    try {
      await api<SupervisorTaskExecutionSummary>("/api/sales-supervisor/tasks/execute", { method: "POST" });
      const taskRuns = await api<TaskAgentExecutionHistory>("/api/agent/tasks/executions?limit=30");
      setExecutions(taskRuns.items);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Internal task execution could not complete.");
    } finally {
      setRunning(false);
    }
  }

  const summary = run?.result?.pipeline_summary;

  return (
    <main className="mx-auto min-h-screen max-w-7xl px-4 py-6 sm:px-8 lg:py-10">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-6">
        <div>
          <Link href="/leads" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">AI Sales Supervisor</h1>
          <p className="mt-1 text-slate-500">Bounded review of deterministic pipeline signals and evidence.</p>
        </div>
        <nav className="flex flex-wrap gap-2 text-sm">
          <Link href="/leads" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Leads</Link>
          <Link href="/leads/tasks" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Sales tasks</Link>
          <Link href="/outreach" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Outreach</Link>
          <Link href="/revenue" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Revenue</Link>
        </nav>
      </header>

      <section className={`${card} mt-6 flex flex-wrap items-center justify-between gap-4`}>
        <div>
          <h2 className="font-bold text-ink">Pipeline review</h2>
          <p className="mt-1 text-sm text-slate-500">Each run reviews at most ten prioritized leads. Larger pipelines are summarized separately.</p>
          {run && <p className="mt-2 text-xs text-slate-500">{freshness(run)} · Last run {new Date(run.created_at).toLocaleString()} · {run.provider} / {run.model}</p>}
        </div>
        <button onClick={() => void runReview()} disabled={running || loading} className="rounded-lg bg-ink px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">
          {running ? "Reviewing…" : "Run supervisor review"}
        </button>
      </section>

      <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        Recommendation ≠ execution. The Supervisor does not send messages, create tasks, or change lead records. Policy decisions do not execute actions.
      </p>
      {error && <div role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">{error}<button onClick={() => void load()} className="ml-3 font-semibold underline">Retry</button></div>}
      {loading && <p role="status" className="mt-6 text-sm text-slate-500">Loading supervisor history…</p>}
      {!loading && !run && !error && <section className={`${card} mt-6`}><h2 className="font-bold">No supervisor run yet</h2><p className="mt-1 text-sm text-slate-500">Run a review to inspect current pipeline signals.</p></section>}

      {!loading && run && <>
        {run.status === "FAILED" && <section role="alert" className="mt-6 rounded-xl border border-rose-200 bg-rose-50 p-5 text-rose-900"><h2 className="font-bold">Review failed</h2><p className="mt-1 text-sm">{run.error_message || "The provider did not return a valid supervisor result."} ({run.error_code || "SUPERVISOR_FAILED"})</p></section>}
        {summary && <section className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" aria-label="Deterministic pipeline summary">
          {[
            ["Pipeline leads", summary.pipeline_total_leads], ["Reviewed", `${summary.total_leads_reviewed} / ${summary.pipeline_total_leads}`],
            ["Need attention", summary.leads_needing_attention], ["Overdue follow-ups", summary.overdue_followups],
            ["Follow-ups due", summary.followups_due], ["Proposal reviews", summary.proposal_reviews],
            ["SEO reviews", summary.seo_opportunity_reviews], ["Missing information", summary.missing_information_cases],
            ["Policy blocked", summary.policy_blocked_cases], ["Human approval", summary.human_approval_cases],
          ].map(([title, value]) => <article key={String(title)} className={card}><p className={label}>{title}</p><p className="mt-2 text-2xl font-bold text-ink">{value}</p></article>)}
        </section>}

        {run.result && <>
          <section className="mt-7" aria-labelledby="findings-title">
            <div className="mb-3 flex flex-wrap items-end justify-between gap-2"><div><h2 id="findings-title" className="text-xl font-bold text-ink">Findings</h2><p className="mt-1 text-sm text-slate-500">Evidence is scoped to each lead’s current Phase 9A context.</p></div><p className="text-xs text-slate-500">Versions: {run.artifact_version} · {run.prompt_version} · {run.context_version}</p></div>
            {run.result.findings.length === 0 && <div className={card}><p className="font-semibold">No findings returned for this run.</p><p className="mt-1 text-sm text-slate-500">Deterministic summary counts remain available above.</p></div>}
            <div className="grid gap-4">
              {run.result.findings.map((finding) => <article key={finding.finding_id} className={card}>
                <div className="flex flex-wrap items-start justify-between gap-3"><div><Link href={`/leads/${encodeURIComponent(finding.lead_id)}`} className="text-xs font-semibold text-teal">Lead {finding.lead_id.slice(0, 8)}</Link><h3 className="mt-1 text-lg font-bold text-ink">{finding.title}</h3></div><div className="flex gap-2"><span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold">{finding.attention_type.replaceAll("_", " ")}</span><span className="rounded-full bg-teal/10 px-3 py-1 text-xs font-semibold text-teal">{finding.urgency}</span></div></div>
                <p className="mt-3 text-sm text-slate-700">{finding.summary}</p><p className="mt-1 text-sm text-slate-600">{finding.rationale}</p>
                <dl className="mt-4 grid gap-3 border-t border-slate-100 pt-4 text-sm sm:grid-cols-2">
                  <div><dt className="font-semibold text-slate-700">Recommendation</dt><dd className="mt-1">{finding.action_type || "No action recommended"}{finding.action_type && ` · ${finding.action_priority} priority`}</dd></div>
                  <div><dt className="font-semibold text-slate-700">Policy</dt><dd className="mt-1">{finding.policy_decision || "Not applicable"}{finding.autonomy_level && ` · ${finding.autonomy_level}`}</dd></div>
                  <div><dt className="font-semibold text-slate-700">Human review</dt><dd className="mt-1">{finding.review_required ? "Required" : "Not required by policy"}</dd></div>
                  <div><dt className="font-semibold text-slate-700">Execution</dt><dd className="mt-1">Not performed</dd></div>
                  <div className="sm:col-span-2"><dt className="font-semibold text-slate-700">Evidence references</dt><dd className="mt-1 break-all font-mono text-xs text-slate-600">{finding.evidence_ids.join(" · ")}</dd></div>
                  {finding.missing_information.length > 0 && <div className="sm:col-span-2"><dt className="font-semibold text-slate-700">Missing information</dt><dd className="mt-1 break-all font-mono text-xs text-slate-600">{finding.missing_information.join(" · ")}</dd></div>}
                </dl>
              </article>)}
            </div>
          </section>
          <section className={`${card} mt-6`}><h2 className="font-bold">Review notes</h2><ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-600">{run.result.review_notes.map((note) => <li key={note}>{note}</li>)}</ul><p className="mt-3 text-xs text-slate-500">Context fingerprint: <code className="break-all">{run.context_fingerprint}</code></p></section>
        </>}
      </>}

      <section className="mt-8" aria-labelledby="history-title"><h2 id="history-title" className="text-lg font-bold text-ink">Run history</h2>{history.length === 0 ? <p className="mt-2 text-sm text-slate-500">No saved runs.</p> : <ul className="mt-3 divide-y divide-slate-200 rounded-xl border border-slate-200 bg-white">{history.map((item) => <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 p-4 text-sm"><span>{new Date(item.created_at).toLocaleString()} · {item.status} · {freshness(item)}</span><button onClick={() => void openHistoricalRun(item.id)} className="font-mono text-xs text-teal underline">Inspect {item.id.slice(0, 8)}</button></li>)}</ul>}</section>

      <section className={`${card} mt-8`} aria-labelledby="task-agent-title">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 id="task-agent-title" className="text-lg font-bold text-ink">Task Agent</h2><p className="mt-1 text-sm text-slate-600">This creates internal sales tasks only. It does not contact prospects.</p></div><button onClick={() => void executeTasks()} disabled={running || loading || !run?.result} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{running ? "Processing…" : "Run approved internal tasks"}</button></div>
        <p className="mt-2 text-xs text-slate-500">Each source action is revalidated against current evidence and policy. Denied, stale, and human-approval-required actions do not create tasks.</p>
        {executions.length === 0 ? <p className="mt-4 text-sm text-slate-500">No Task Agent executions recorded.</p> : <ul className="mt-4 divide-y divide-slate-100">{executions.map((item) => <li key={item.execution_id} className="grid gap-1 py-3 text-sm sm:grid-cols-[1fr_auto]"><div><p className="font-semibold text-ink">{item.action_type.replaceAll("_", " ")} · {item.execution_status.replaceAll("_", " ")}</p><p className="text-xs text-slate-500">Policy: {item.policy_decision || "Not evaluated"} · Execution: {item.created ? "Internal task created" : "No task created"}</p>{item.task_id && <p className="font-mono text-xs text-slate-500">Task {item.task_id}</p>}{item.execution_status === "REQUIRES_HUMAN_APPROVAL" && <p className="text-xs text-amber-700">Human approval required — task not created.</p>}{item.execution_status === "DENIED" && <p className="text-xs text-rose-700">Policy denied — task not created.</p>}{item.execution_status === "STALE_CONTEXT" && <p className="text-xs text-amber-700">Context changed — refresh supervisor recommendation.</p>}<p className="text-xs text-slate-500">{item.reason}</p></div><Link href={`/leads/${encodeURIComponent(item.lead_id)}`} className="text-xs font-semibold text-teal underline">Open lead</Link></li>)}</ul>}
      </section>
    </main>
  );
}
