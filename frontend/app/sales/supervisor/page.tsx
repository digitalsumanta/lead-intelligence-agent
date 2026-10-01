"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, SalesSupervisorRun, SalesSupervisorHistory, SupervisorTaskExecutionSummary, TaskAgentExecution, TaskAgentExecutionHistory, OutreachPreparation, OutreachPreparationChannel, OutreachApproval, AgentOutreachExecution, EmailDeliveryEvent, approveOutreachPreparation, executeApprovedOutreach, getOutreachApproval, getPreparationExecutions, getEmailDeliveryEvents } from "@/lib/api";

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
  const [preparations, setPreparations] = useState<OutreachPreparation[]>([]);
  const [approvals, setApprovals] = useState<Record<string, OutreachApproval | null>>({});
  const [outreachExecutions, setOutreachExecutions] = useState<Record<string, AgentOutreachExecution | null>>({});
  const [deliveryEvents, setDeliveryEvents] = useState<Record<string, EmailDeliveryEvent[]>>({});
  const [deliveryEventErrors, setDeliveryEventErrors] = useState<Record<string, string>>({});
  const [preparing, setPreparing] = useState("");
  const [approving, setApproving] = useState("");
  const [sending, setSending] = useState("");
  const [preparationChannels, setPreparationChannels] = useState<Record<string, OutreachPreparationChannel>>({});
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const loadOutreachState = useCallback(async (supervisorRun: SalesSupervisorRun | null) => {
    const findings = supervisorRun?.result?.findings || [];
    const leadIds = [...new Set(findings.map((finding) => finding.lead_id))];
    const histories = await Promise.all(leadIds.map(async (leadId) => {
      try {
        return await api<OutreachPreparation[]>(`/api/agent/outreach/preparations/${encodeURIComponent(leadId)}/history?limit=20`);
      } catch {
        return [];
      }
    }));
    const drafts = [...new Map(histories.flat().map((item) => [item.id, item])).values()];
    setPreparations(drafts);
    const emailDrafts = drafts.filter((item) => item.channel === "EMAIL");
    const states = await Promise.all(emailDrafts.map(async (item) => {
      try {
        const approval = await getOutreachApproval(item.id);
        const executions = approval ? await getPreparationExecutions(item.id) : { items: [] };
        return [item.id, approval, executions.items[0] || null] as const;
      } catch {
        return [item.id, null, null] as const;
      }
    }));
    setApprovals(Object.fromEntries(states.map(([id, approval]) => [id, approval])));
    setOutreachExecutions(Object.fromEntries(states.map(([id, , execution]) => [id, execution])));
    const timelines = await Promise.all(states.map(async ([, , execution]) => {
      if (!execution) return null;
      try {
        return [execution.id, (await getEmailDeliveryEvents(execution.id)).items, ""] as const;
      } catch {
        return [execution.id, [] as EmailDeliveryEvent[], "Delivery history could not be loaded."] as const;
      }
    }));
    const availableTimelines = timelines.filter((item) => item !== null);
    setDeliveryEvents(Object.fromEntries(availableTimelines.map(([id, events]) => [id, events])) as Record<string, EmailDeliveryEvent[]>);
    setDeliveryEventErrors(Object.fromEntries(availableTimelines.filter(([, , error]) => error).map(([id, , error]) => [id, error])));
  }, []);

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
      await loadOutreachState(current);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Sales Supervisor data could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [loadOutreachState]);

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
      await loadOutreachState(created);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Sales Supervisor review could not be completed.");
    } finally {
      setRunning(false);
    }
  }

  async function openHistoricalRun(runId: string) {
    setError("");
    try {
      const historical = await api<SalesSupervisorRun>(`/api/sales-supervisor/history/${encodeURIComponent(runId)}`);
      setRun(historical);
      await loadOutreachState(historical);
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

  async function prepareOutreach(finding: NonNullable<SalesSupervisorRun["result"]>["findings"][number]) {
    if (!run || !finding.action_type) return;
    setPreparing(finding.finding_id);
    setError("");
    try {
      const channel = preparationChannels[finding.finding_id]
        || (finding.action_type === "CALL_PROSPECT" ? "CALL_SCRIPT" : "EMAIL");
      const draft = await api<OutreachPreparation>("/api/agent/outreach/prepare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lead_id: finding.lead_id, source_type: "AI_SALES_SUPERVISOR",
          source_id: run.id, source_action_id: finding.finding_id,
          action_type: finding.action_type, channel,
        }),
      });
      setPreparations((previous) => [draft, ...previous.filter((item) => item.id !== draft.id)]);
      if (draft.channel === "EMAIL") {
        const approval = await getOutreachApproval(draft.id).catch(() => null);
        const history = approval ? await getPreparationExecutions(draft.id).catch(() => ({ items: [] })) : { items: [] };
        setApprovals((previous) => ({ ...previous, [draft.id]: approval }));
        setOutreachExecutions((previous) => ({ ...previous, [draft.id]: history.items[0] || null }));
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Outreach preparation could not be completed.");
    } finally {
      setPreparing("");
    }
  }

  async function approveEmail(preparation: OutreachPreparation) {
    setApproving(preparation.id);
    setError("");
    try {
      const approval = await approveOutreachPreparation(preparation.id);
      setApprovals((previous) => ({ ...previous, [preparation.id]: approval }));
      const history = await getPreparationExecutions(preparation.id);
      setOutreachExecutions((previous) => ({ ...previous, [preparation.id]: history.items[0] || null }));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "The email could not be approved.");
    } finally {
      setApproving("");
    }
  }

  async function sendApprovedEmail(preparation: OutreachPreparation, approval: OutreachApproval) {
    setSending(preparation.id);
    setError("");
    try {
      const execution = await executeApprovedOutreach(approval.id);
      setOutreachExecutions((previous) => ({ ...previous, [preparation.id]: execution }));
      try {
        const history = await getEmailDeliveryEvents(execution.id);
        setDeliveryEvents((previous) => ({ ...previous, [execution.id]: history.items }));
        setDeliveryEventErrors((previous) => { const next = { ...previous }; delete next[execution.id]; return next; });
      } catch {
        setDeliveryEventErrors((previous) => ({ ...previous, [execution.id]: "Delivery history could not be loaded." }));
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "The approved email execution could not be completed.");
    } finally {
      setSending("");
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

      <section className={`${card} mt-6`} aria-labelledby="outreach-preparation-title">
        <div>
          <h2 id="outreach-preparation-title" className="text-lg font-bold text-ink">Outreach Preparation</h2>
          <p className="mt-1 text-sm font-semibold text-amber-800">Draft only — no message has been sent. Every draft requires human review.</p>
          <p className="mt-1 text-sm text-slate-600">The provider selects validated evidence and approved wording options. The server renders the draft and never sends or initiates contact.</p>
        </div>
        {!run?.result && <p className="mt-4 text-sm text-slate-500">Run a current Supervisor review to prepare outreach from a persisted recommendation.</p>}
        {run?.result && !run.is_current && <p role="status" className="mt-4 text-sm text-amber-800">This Supervisor run is stale. Refresh it before preparing a draft.</p>}
        {run?.result?.findings.filter((finding) => finding.action_type && finding.policy_decision !== "DENY").map((finding) => {
          const channel = preparationChannels[finding.finding_id]
            || (finding.action_type === "CALL_PROSPECT" ? "CALL_SCRIPT" : "EMAIL");
          const channels: OutreachPreparationChannel[] = finding.action_type === "CALL_PROSPECT"
            ? ["CALL_SCRIPT"]
            : finding.action_type === "FOLLOW_UP"
              ? ["EMAIL", "WHATSAPP", "CALL_SCRIPT"]
              : ["EMAIL", "WHATSAPP"];
          return <div key={finding.finding_id} className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-4">
            <div><p className="font-semibold">{finding.action_type?.replaceAll("_", " ")}</p><p className="text-xs text-slate-500">Lead {finding.lead_id.slice(0, 8)} · Recommendation {finding.finding_id.slice(0, 12)}</p></div>
            <div className="flex items-center gap-2">
              <label className="sr-only" htmlFor={`prep-channel-${finding.finding_id}`}>Preparation channel</label>
              <select id={`prep-channel-${finding.finding_id}`} value={channel} onChange={(event) => setPreparationChannels((previous) => ({ ...previous, [finding.finding_id]: event.target.value as OutreachPreparationChannel }))} className="rounded-lg border border-slate-300 px-3 py-2 text-sm">
                {channels.map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
              </select>
              <button onClick={() => void prepareOutreach(finding)} disabled={Boolean(preparing) || !run.is_current} className="rounded-lg bg-ink px-3 py-2 text-sm font-semibold text-white disabled:opacity-50">
                {preparing === finding.finding_id ? "Preparing…" : "Prepare draft"}
              </button>
            </div>
          </div>;
        })}
        {preparations.length > 0 && <div className="mt-5 space-y-3" aria-label="Prepared outreach history">
          {preparations.map((item) => {
            const approval = approvals[item.id] || null;
            const execution = outreachExecutions[item.id] || null;
            const events = execution ? deliveryEvents[execution.id] || [] : [];
            const preparationDisplay = item.status === "STALE_CONTEXT" ? "STALE"
              : ["REJECTED", "INVALID_REQUEST", "POLICY_DENIED"].includes(item.status) ? "BLOCKED" : "DRAFT";
            const displayStatus = execution?.status === "EXECUTING" ? "SENDING"
              : execution?.status || approval?.status || (item.channel === "EMAIL" ? preparationDisplay : item.status);
            const canApprove = item.channel === "EMAIL" && !approval && !execution
              && ["READY_FOR_HUMAN_REVIEW", "REQUIRES_HUMAN_APPROVAL"].includes(item.status)
              && item.policy_decision !== "DENY";
            const canSend = approval?.status === "APPROVED" && !execution;
            return <article key={item.id} className="rounded-lg border border-slate-200 bg-slate-50 p-4">
            <div className="flex flex-wrap justify-between gap-2"><h3 className="font-semibold">{item.channel.replaceAll("_", " ")} · {item.purpose.replaceAll("_", " ")}</h3><span className="text-xs font-semibold">{displayStatus.replaceAll("_", " ")}</span></div>
            {item.subject && <p className="mt-2 text-sm font-semibold">Subject: {item.subject}</p>}
            {item.body && <p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{item.body}</p>}
            {item.call_script && <pre className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{JSON.stringify(item.call_script, null, 2)}</pre>}
            <dl className="mt-3 grid gap-2 border-t border-slate-200 pt-3 text-xs text-slate-600 sm:grid-cols-2">
              <div><dt className="font-semibold">Policy</dt><dd>{item.policy_decision || "Not evaluated"} · human review {item.review_required ? "required" : "not required"}</dd></div>
              <div><dt className="font-semibold">Provider</dt><dd>{item.provider || "Unavailable"} / {item.model || "Unavailable"}</dd></div>
              <div><dt className="font-semibold">Evidence</dt><dd className="break-all font-mono">{item.evidence_ids.join(" · ") || "None"}</dd></div>
              <div><dt className="font-semibold">Source recommendation</dt><dd className="break-all font-mono">{item.source_type} · {item.source_action_id}</dd></div>
              <div className="sm:col-span-2"><dt className="font-semibold">Context fingerprint</dt><dd className="break-all font-mono">{item.context_fingerprint}</dd></div>
              <div><dt className="font-semibold">Created</dt><dd>{new Date(item.created_at).toLocaleString()}</dd></div>
              {approval && <><div><dt className="font-semibold">Recipient</dt><dd className="break-all">{approval.recipient}</dd></div><div><dt className="font-semibold">Approver</dt><dd>{approval.approved_by_user_id} · {new Date(approval.approved_at).toLocaleString()}</dd></div><div><dt className="font-semibold">Approval policy</dt><dd>{approval.policy_decision}</dd></div></>}
              {execution && <><div><dt className="font-semibold">Execution policy</dt><dd>{execution.policy_decision}</dd></div><div><dt className="font-semibold">Email provider</dt><dd>{execution.provider}</dd></div><div><dt className="font-semibold">Provider message ID</dt><dd className="break-all">{execution.provider_message_id || "Unavailable"}</dd></div>{execution.failure_reason && <div className="sm:col-span-2"><dt className="font-semibold">Execution result</dt><dd>{execution.failure_code}: {execution.failure_reason}</dd></div>}</>}
            </dl>
            {item.channel !== "EMAIL" && <p className="mt-2 text-xs font-semibold text-amber-800">Draft only — Phase 10E does not execute this channel.</p>}
            {item.channel === "EMAIL" && !execution && approval?.status === "APPROVED" && <p className="mt-2 text-xs font-semibold text-amber-800">Approved only — the email has not been sent.</p>}
            {item.channel === "EMAIL" && execution?.status === "UNCERTAIN" && <p role="alert" className="mt-2 text-xs font-semibold text-amber-900">Provider outcome is uncertain. Automatic retry is disabled; reconcile with the provider before taking further action.</p>}
            {execution && <section className="mt-3 rounded-lg border border-slate-200 bg-white p-3" aria-label="Email delivery event history">
              <h4 className="text-xs font-semibold text-ink">Delivery event history</h4>
              <p className="mt-1 text-[11px] text-slate-500">Append-only provider observations, separate from the current execution status.</p>
              {deliveryEventErrors[execution.id] ? <p role="status" className="mt-2 text-xs text-amber-800">{deliveryEventErrors[execution.id]}</p> : events.length === 0 ? <p className="mt-2 text-xs text-slate-500">No delivery events recorded.</p> : <ol className="mt-2 space-y-2">
                {events.map((event) => <li key={event.id} className="border-l-2 border-teal/40 pl-3 text-xs text-slate-700">
                  <p className="font-semibold">{event.event_type.replaceAll("_", " ")} <span className="font-normal text-slate-500">· {new Date(event.occurred_at).toLocaleString()}</span></p>
                  <p className="text-slate-500">Source: {event.source}{event.provider_status ? ` · Provider status: ${event.provider_status}` : ""}</p>
                  {event.provider_message_id && <p className="break-all font-mono text-slate-500">Message ID: {event.provider_message_id}</p>}
                </li>)}
              </ol>}
            </section>}
            {item.channel === "EMAIL" && <div className="mt-4 flex flex-wrap gap-2 border-t border-slate-200 pt-3">
              {canApprove && <button onClick={() => void approveEmail(item)} disabled={Boolean(approving || sending)} className="rounded-lg border border-amber-500 px-3 py-2 text-sm font-semibold text-amber-900 disabled:opacity-50">{approving === item.id ? "Approving…" : "Approve Email"}</button>}
              {canSend && <button onClick={() => void sendApprovedEmail(item, approval)} disabled={Boolean(approving || sending) || approval.status !== "APPROVED"} className="rounded-lg bg-ink px-3 py-2 text-sm font-semibold text-white disabled:opacity-50">{sending === item.id ? "Sending…" : "Send Email"}</button>}
              {approval?.status === "STALE" && <span role="status" className="self-center text-sm font-semibold text-amber-900">Stale approval — prepare and review a new draft.</span>}
              {execution?.status === "SENT" && <span className="self-center text-sm font-semibold text-emerald-800">Sent</span>}
            </div>}
          </article>})}
        </div>}
      </section>
    </main>
  );
}
