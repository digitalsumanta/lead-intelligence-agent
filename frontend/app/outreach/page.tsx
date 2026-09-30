"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getOutreachAnalytics,
  OutreachAnalytics,
  OutreachAnalyticsChannel,
} from "@/lib/api";

const CHANNELS: OutreachAnalyticsChannel[] = ["EMAIL", "WHATSAPP", "MANUAL"];
const input = "rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm";
const card = "rounded-xl border border-slate-200 bg-white p-5 shadow-sm";

export default function OutreachPerformancePage() {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [channel, setChannel] = useState<OutreachAnalyticsChannel | "">("");
  const [applied, setApplied] = useState({ from: "", to: "", channel: "" as OutreachAnalyticsChannel | "" });
  const [data, setData] = useState<OutreachAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await getOutreachAnalytics(applied));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Outreach analytics could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [applied]);

  useEffect(() => { void load(); }, [load]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApplied({ from, to, channel });
  }

  return (
    <main className="mx-auto min-h-screen max-w-7xl px-4 py-6 sm:px-8 lg:py-10">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-6">
        <div>
          <Link href="/leads" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Outreach Performance</h1>
          <p className="mt-1 text-slate-500">Sent executions and human-recorded outcomes, grouped by execution send date.</p>
        </div>
        <nav className="flex gap-2 text-sm">
          <Link href="/leads" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Leads</Link>
          <Link href="/revenue" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Revenue</Link>
          <Link href="/sales/supervisor" className="rounded-lg border border-slate-300 px-4 py-2 font-semibold">Sales Supervisor</Link>
        </nav>
      </header>

      <section className={`${card} mt-6`} aria-labelledby="filters-heading">
        <h2 id="filters-heading" className="font-bold text-ink">Filters</h2>
        <form onSubmit={applyFilters} className="mt-4 flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-xs font-semibold text-slate-600">From<input className={input} type="date" value={from} onChange={(event) => setFrom(event.target.value)} /></label>
          <label className="grid gap-1 text-xs font-semibold text-slate-600">To<input className={input} type="date" value={to} onChange={(event) => setTo(event.target.value)} /></label>
          <label className="grid gap-1 text-xs font-semibold text-slate-600">Channel<select className={input} value={channel} onChange={(event) => setChannel(event.target.value as OutreachAnalyticsChannel | "")}><option value="">All channels</option>{CHANNELS.map((item) => <option key={item}>{item}</option>)}</select></label>
          <button className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white">Apply filters</button>
          <button type="button" onClick={() => { setFrom(""); setTo(""); setChannel(""); setApplied({ from: "", to: "", channel: "" }); }} className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">Clear</button>
        </form>
        {data && <p className="mt-3 text-xs text-slate-500">Cohort dates are inclusive UTC calendar dates on <code>sent_at</code>.</p>}
      </section>

      {error && <div role="alert" className="mt-5 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">{error}<button onClick={() => void load()} className="ml-3 font-semibold underline">Retry</button></div>}
      {loading && <p role="status" className="mt-6 text-sm text-slate-500">Loading outreach analytics…</p>}
      {!loading && !error && data && <>
        <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="Outreach performance summary">
          <article className={card}><p className="text-sm text-slate-500">Sent executions</p><p className="mt-2 text-3xl font-bold text-ink">{data.attempts_total}</p><p className="mt-1 text-xs text-slate-400">Retries count as separate executions.</p></article>
          <article className={card}><p className="text-sm text-slate-500">Recorded outcomes</p><p className="mt-2 text-3xl font-bold text-ink">{data.recorded_outcomes_total}</p><p className="mt-1 text-xs text-slate-400">Human-recorded activity entries.</p></article>
          <article className={card}><p className="text-sm text-slate-500">Outcome coverage</p><p className="mt-2 text-3xl font-bold text-ink">{data.outcome_coverage.executions_with_outcomes} / {data.outcome_coverage.eligible_sent_executions}</p><p className="mt-1 text-xs text-slate-400">Sent executions with a recorded outcome.</p></article>
          <article className={card}><p className="text-sm text-slate-500">Leads represented</p><p className="mt-2 text-3xl font-bold text-ink">{data.leads_represented}</p><p className="mt-1 text-xs text-slate-400">Distinct leads in the execution cohort.</p></article>
        </section>

        <section className="mt-5 grid gap-5 lg:grid-cols-2">
          <div className={card}><h2 className="font-bold text-ink">Attempts by Channel</h2><ul className="mt-3 divide-y divide-slate-100">{data.attempts_by_channel.map((row) => <li key={row.channel} className="flex justify-between py-2 text-sm"><span>{row.channel}</span><strong>{row.count}</strong></li>)}</ul></div>
          <div className={card}><h2 className="font-bold text-ink">Current Execution Status</h2><p className="mt-1 text-xs text-slate-500">Current execution status, not historical status.</p><ul className="mt-2 divide-y divide-slate-100">{data.attempts_by_status.map((row) => <li key={row.status} className="flex justify-between py-2 text-sm"><span>{row.status}</span><strong>{row.count}</strong></li>)}</ul></div>
        </section>

        <section className={`${card} mt-5`}><h2 className="font-bold text-ink">Recorded Outcomes</h2><p className="mt-1 text-xs text-slate-500">Counts describe recorded activities linked to executions in this sent-date cohort.</p><div className="mt-3 overflow-x-auto"><table className="w-full min-w-[560px] text-left text-sm"><thead className="text-xs uppercase text-slate-400"><tr><th className="py-2">Outcome</th><th className="py-2">Activities</th><th className="py-2">Executions</th><th className="py-2">Leads</th></tr></thead><tbody>{data.outcomes_by_type.map((row) => <tr key={row.outcome} className="border-t border-slate-100"><td className="py-2">{row.outcome.replaceAll("_", " ")}</td><td className="py-2">{row.activity_count}</td><td className="py-2">{row.executions_with_outcome}</td><td className="py-2">{row.leads_represented}</td></tr>)}</tbody></table></div>{data.recorded_outcomes_total === 0 && <p className="mt-3 text-sm text-slate-500">No recorded outcomes for this cohort.</p>}</section>

        <section className={`${card} mt-5`}><h2 className="font-bold text-ink">Outcome Coverage</h2><dl className="mt-3 grid gap-3 text-sm sm:grid-cols-3"><div><dt className="text-slate-500">Eligible sent executions</dt><dd className="mt-1 font-bold">{data.outcome_coverage.eligible_sent_executions}</dd></div><div><dt className="text-slate-500">With recorded outcomes</dt><dd className="mt-1 font-bold">{data.outcome_coverage.executions_with_outcomes}</dd></div><div><dt className="text-slate-500">Without recorded outcomes</dt><dd className="mt-1 font-bold">{data.outcome_coverage.executions_without_outcomes}</dd></div></dl><p className="mt-3 text-xs text-slate-500">Executions without outcomes do not mean no response.</p></section>

        <aside className="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950"><h2 className="font-bold">Interpretation limits</h2><ul className="mt-2 list-disc space-y-1 pl-5">{data.limitations.map((item) => <li key={item}>{item}</li>)}</ul></aside>
      </>}
    </main>
  );
}
