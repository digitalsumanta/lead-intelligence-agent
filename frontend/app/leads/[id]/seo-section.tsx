"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, LatestSeo, LeadSeoOpportunities, SeoAuditRun, SeoFinding, SeoRunDetail, SeoRunList, SeoSalesOpportunity } from "@/lib/api";

const SEVERITIES = ["HIGH", "MEDIUM", "LOW", "INFO"] as const;

export default function SeoSection({ leadId, website }: { leadId: string; website: string | null }) {
  const [latest, setLatest] = useState<LatestSeo | null>(null);
  const [runs, setRuns] = useState<SeoAuditRun[]>([]);
  const [detail, setDetail] = useState<SeoRunDetail | null>(null);
  const [opportunities, setOpportunities] = useState<SeoSalesOpportunity[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [summary, history, sales] = await Promise.all([
        api<LatestSeo>(`/api/leads/${encodeURIComponent(leadId)}/seo`),
        api<SeoRunList>(`/api/leads/${encodeURIComponent(leadId)}/seo/runs?page=1&page_size=20`),
        api<LeadSeoOpportunities>(`/api/leads/${encodeURIComponent(leadId)}/seo/opportunities`),
      ]);
      setLatest(summary);
      setRuns(history.items);
      setOpportunities(sales.items);
      setDetail(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "SEO audit information could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [leadId]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function startAudit() {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/leads/${encodeURIComponent(leadId)}/seo/runs`, { method: "POST" });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "SEO audit could not be queued.");
    } finally {
      setBusy(false);
    }
  }

  async function showRun(id: string) {
    setError(null);
    try { setDetail(await api<SeoRunDetail>(`/api/seo/runs/${encodeURIComponent(id)}`)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "SEO audit details could not be loaded."); }
  }

  const active = runs.find((run) => run.status === "QUEUED" || run.status === "RUNNING");
  const findings: SeoFinding[] = detail?.findings ?? latest?.findings ?? [];
  const run = detail?.run ?? latest?.audit;
  const observation = (type: string) => detail?.observations.find((item) => item.observation_type === type)?.observed_value;

  return <section className="mt-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="font-bold text-ink">SEO &amp; Website Intelligence</h2><p className="mt-1 text-sm text-slate-500">Bounded technical observations from independent, versioned website audits.</p></div>
      <div className="flex gap-2"><button onClick={() => void refresh()} disabled={loading} className="rounded-lg border border-slate-200 px-4 py-2 text-sm font-semibold text-ink disabled:opacity-50">Refresh</button><button onClick={startAudit} disabled={!website || busy || Boolean(active)} className="rounded-lg bg-teal px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">{busy ? "Queueing…" : active ? `Audit ${active.status.toLowerCase()}` : "Start SEO audit"}</button></div>
    </div>
    {!website && <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">Add a website to this lead before starting an SEO audit.</p>}
    {loading && <p className="mt-4 text-sm text-slate-500">Loading SEO audit history…</p>}
    {error && <div role="alert" className="mt-4 flex items-center justify-between gap-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800"><span>{error}</span><button onClick={() => void refresh()} className="font-semibold underline">Retry</button></div>}
    {active && <p className="mt-4 rounded-lg border border-sky-100 bg-sky-50 p-3 text-sm text-sky-900">The newest audit is {active.status.toLowerCase()}. Refresh to check its status; this page does not poll automatically.</p>}
    {!loading && run && <>
      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        <div className="rounded-lg bg-slate-50 p-3"><p className="text-xs uppercase text-slate-400">Selected audit</p><p className="mt-1 font-semibold">{run.status} · {run.audit_version}</p><p className="text-xs text-slate-500">{run.completed_at ? new Date(run.completed_at).toLocaleString() : new Date(run.created_at).toLocaleString()}</p></div>
        <div className="rounded-lg bg-slate-50 p-3"><p className="text-xs uppercase text-slate-400">Crawl</p><p className="mt-1 font-semibold">{run.pages_crawled} pages crawled</p><p className="text-xs text-slate-500">{run.pages_discovered} discovered within bounds</p></div>
        <div className="rounded-lg bg-slate-50 p-3"><p className="text-xs uppercase text-slate-400">Deterministic findings</p><p className="mt-1 font-semibold">{run.findings_count}</p><p className="text-xs text-slate-500">No composite SEO score is calculated.</p></div>
      </div>
      {run.status === "FAILED" && <p className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{run.error_message || "This audit failed."}</p>}
      {run.status === "COMPLETED" && <>
        <h3 className="mt-5 font-semibold">Findings by severity and category</h3>
        {findings.length === 0 ? <p className="mt-2 text-sm text-slate-500">No findings were triggered by the deterministic checks in this run.</p> : SEVERITIES.map((severity) => {
          const rows = findings.filter((item) => item.severity === severity);
          return rows.length ? <div key={severity} className="mt-3"><h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">{severity} · {rows.length}</h4><div className="mt-2 grid gap-2 md:grid-cols-2">{rows.map((item) => <article key={item.id} className="rounded-lg border border-slate-200 p-3"><div className="flex justify-between gap-3"><h5 className="text-sm font-semibold">{item.title}</h5><span className="text-xs text-slate-400">{item.category}</span></div><p className="mt-1 text-sm text-slate-600">{item.description}</p>{item.page_url && <p className="mt-2 break-all text-xs text-teal">{item.page_url}</p>}</article>)}</div></div> : null;
        })}
        <div className="mt-6 border-t border-slate-100 pt-5">
          <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-bold text-ink">SEO Findings → Sales Opportunities</h3><p className="mt-1 text-sm text-slate-500">Grouped deterministically from the latest completed audit only. Creating a task requires an explicit action in the Sales Action Center.</p></div><Link href="/leads/tasks" className="rounded-lg border border-teal/40 px-3 py-2 text-xs font-semibold text-teal">Open Sales Action Center</Link></div>
          {!opportunities.length ? <p className="mt-3 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">No actionable grouped SEO opportunities are available for the current completed audit.</p> : <div className="mt-3 grid gap-3 md:grid-cols-2">{opportunities.map((item) => <article key={item.opportunity_id} className="rounded-lg border border-slate-200 p-4"><div className="flex items-start justify-between gap-3"><div><h4 className="font-semibold text-ink">{item.title}</h4><p className="mt-1 text-sm text-slate-600">{item.summary}</p></div><span className="rounded-full bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-800">{item.severity}</span></div><p className="mt-2 text-xs text-slate-500">{item.finding_count} findings · {item.affected_page_count} affected pages · task {item.current_task_state.replaceAll("_", " ")}</p><ul className="mt-2 space-y-1">{item.evidence.slice(0, 3).map((evidence) => <li key={evidence.finding_id} className="text-xs text-slate-600">{evidence.finding_code}: {evidence.title}{evidence.page_url ? <span className="block break-all text-slate-400">{evidence.page_url}</span> : null}</li>)}</ul></article>)}</div>}
        </div>
        <div className="mt-5 grid gap-3 md:grid-cols-2">
          {(["robots", "sitemap", "crawl_summary", "pagespeed_status", "pagespeed_performance_score", "pagespeed_seo_score"] as const).map((key) => {
            const value = observation(key);
            return value === undefined ? null : <div key={key} className="rounded-lg bg-slate-50 p-3"><h4 className="text-xs font-bold uppercase text-slate-500">{key.replaceAll("_", " ")}</h4><pre className="mt-2 whitespace-pre-wrap break-words text-xs text-slate-700">{JSON.stringify(value, null, 2)}</pre></div>;
          })}
        </div>
      </>}
    </>}
    {!loading && !run && !error && <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">No completed SEO audit is available yet. Start an audit to create the first historical run.</p>}
    {runs.length > 0 && <div className="mt-5 border-t border-slate-100 pt-4"><h3 className="font-semibold">Audit history</h3><ul className="mt-2 divide-y divide-slate-100">{runs.map((item) => <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"><span>{new Date(item.created_at).toLocaleString()} · {item.status} · {item.findings_count} findings</span><button onClick={() => void showRun(item.id)} className="font-semibold text-teal underline">View run</button></li>)}</ul></div>}
    <p className="mt-4 text-xs leading-5 text-slate-400">Crawl limits: 25 pages, depth 2, at most 500 discovered URLs, five sitemap documents, and a 180-second crawler deadline. These observations are not a full-site audit or a ranking guarantee.</p>
  </section>;
}
