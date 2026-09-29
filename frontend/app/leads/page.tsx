"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, DashboardStats, LeadListItem } from "@/lib/api";

const PAGE_SIZES = [10, 25, 50, 100];

function displayDate(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function Score({ value }: { value: number | null }) {
  if (value === null) return <span className="text-slate-400">—</span>;
  return (
    <div className="min-w-24" aria-label={`Intelligence score ${value} out of 100`}>
      <div className="flex items-center gap-2 font-semibold">
        <span>{value}</span><span className="text-xs text-slate-400">/ 100</span>
      </div>
      <div className="mt-1 h-1.5 rounded-full bg-slate-100">
        <div className="h-1.5 rounded-full bg-teal" style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

export default function LeadsDashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [leads, setLeads] = useState<LeadListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [minScore, setMinScore] = useState("");
  const [maxScore, setMaxScore] = useState("");
  const [intelligenceStatus, setIntelligenceStatus] = useState("");
  const [personalization, setPersonalization] = useState("");
  const [hasOpportunity, setHasOpportunity] = useState("");
  const [salesStatus, setSalesStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [followUpDue, setFollowUpDue] = useState("");
  const [appliedFilters, setAppliedFilters] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    const filterParams = new URLSearchParams(appliedFilters);
    filterParams.forEach((value, key) => query.set(key, value));
    try {
      const [summary, rows] = await Promise.all([
        api<DashboardStats>("/api/dashboard/stats"),
        api<LeadListItem[]>(`/api/leads?${query.toString()}`),
      ]);
      setStats(summary);
      setLeads(rows);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Unable to load leads. Check the API connection and try again.");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, appliedFilters]);

  useEffect(() => { void load(); }, [load]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (minScore && maxScore && Number(minScore) > Number(maxScore)) {
      setError("Minimum score must not exceed maximum score.");
      return;
    }
    const params = new URLSearchParams();
    if (minScore) params.set("min_score", minScore);
    if (maxScore) params.set("max_score", maxScore);
    if (intelligenceStatus) params.set("intelligence_status", intelligenceStatus);
    if (personalization) params.set("personalization_available", personalization);
    if (hasOpportunity) params.set("opportunity_present", hasOpportunity);
    if (salesStatus) params.set("sales_status", salesStatus);
    if (priority) params.set("priority", priority);
    if (followUpDue) params.set("follow_up_due", followUpDue);
    setPage(1);
    setAppliedFilters(params.toString());
  }

  function clearFilters() {
    setMinScore(""); setMaxScore(""); setIntelligenceStatus("");
    setPersonalization(""); setHasOpportunity(""); setPage(1); setAppliedFilters("");
    setSalesStatus(""); setPriority(""); setFollowUpDue("");
  }

  const withIntelligence = leads.filter((lead) => lead.intelligence_status === "available").length;
  const withOpportunities = leads.filter((lead) => lead.opportunity_count > 0).length;
  const metrics = [
    { label: "Total leads", value: stats?.total_leads ?? "—", note: "Across your workspace" },
    { label: "With intelligence", value: loading ? "—" : withIntelligence, note: "On this page" },
    { label: "Awaiting enrichment", value: stats?.imported ?? "—", note: "Imported status" },
    { label: "With opportunities", value: loading ? "—" : withOpportunities, note: "On this page" },
    { label: "Average score", value: stats?.average_lead_score ?? "—", note: "Current lead scores" },
  ];

  return (
    <main className="mx-auto min-h-screen max-w-[1500px] px-4 py-6 sm:px-8 lg:py-10">
      <header className="flex flex-col gap-5 border-b border-slate-200 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <Link href="/" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Lead intelligence</h1>
          <p className="mt-1 text-slate-500">Review evidence-backed opportunities and prioritize accounts.</p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <Link href="/leads/tasks" className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-semibold text-teal hover:bg-slate-50">Sales tasks</Link>
          <span className="rounded-full bg-white px-3 py-1.5 text-slate-500 ring-1 ring-slate-200">API-backed · read-only</span>
          <button onClick={() => void load()} disabled={loading} className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-semibold text-ink hover:bg-slate-50 disabled:opacity-50">
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      <section className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5" aria-label="Lead metrics">
        {metrics.map((metric) => (
          <article key={metric.label} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
            <p className="text-sm text-slate-500">{metric.label}</p>
            <p className="mt-2 text-2xl font-bold text-ink">{metric.value}</p>
            <p className="mt-1 text-xs text-slate-400">{metric.note}</p>
          </article>
        ))}
      </section>

      <section className="mt-7 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-200 px-5 py-4">
          <h2 className="font-bold text-ink">Filter leads</h2>
          <p className="mt-1 text-sm text-slate-500">Filters are applied by the API to the current page query.</p>
          <form onSubmit={applyFilters} className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
            <label className="text-xs font-semibold text-slate-600">Minimum score
              <input type="number" min="0" max="100" value={minScore} onChange={(event) => setMinScore(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="0" />
            </label>
            <label className="text-xs font-semibold text-slate-600">Maximum score
              <input type="number" min="0" max="100" value={maxScore} onChange={(event) => setMaxScore(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="100" />
            </label>
            <label className="text-xs font-semibold text-slate-600">Intelligence
              <select value={intelligenceStatus} onChange={(event) => setIntelligenceStatus(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
                <option value="">Any status</option><option value="available">Available</option><option value="unavailable">Unavailable</option>
              </select>
            </label>
            <label className="text-xs font-semibold text-slate-600">Personalization
              <select value={personalization} onChange={(event) => setPersonalization(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
                <option value="">Any</option><option value="true">Available</option><option value="false">Unavailable</option>
              </select>
            </label>
            <label className="text-xs font-semibold text-slate-600">Opportunities
              <select value={hasOpportunity} onChange={(event) => setHasOpportunity(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
                <option value="">Any</option><option value="true">Has opportunities</option><option value="false">None</option>
              </select>
            </label>
            <label className="text-xs font-semibold text-slate-600">Sales status
              <select value={salesStatus} onChange={(event) => setSalesStatus(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"><option value="">Any status</option>{["NEW", "QUALIFIED", "CONTACTED", "ENGAGED", "PROPOSAL", "NEGOTIATION", "WON", "LOST"].map((value) => <option key={value}>{value}</option>)}</select>
            </label>
            <label className="text-xs font-semibold text-slate-600">Priority
              <select value={priority} onChange={(event) => setPriority(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"><option value="">Any priority</option>{["LOW", "MEDIUM", "HIGH", "URGENT"].map((value) => <option key={value}>{value}</option>)}</select>
            </label>
            <label className="text-xs font-semibold text-slate-600">Follow-up
              <select value={followUpDue} onChange={(event) => setFollowUpDue(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"><option value="">Any</option><option value="true">Due now</option><option value="false">Not due</option></select>
            </label>
            <div className="flex items-end gap-2">
              <button type="submit" className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700">Apply</button>
              <button type="button" onClick={clearFilters} className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-50">Clear</button>
            </div>
          </form>
        </div>

        {error && <div role="alert" className="m-5 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">{error}</div>}
        {loading ? (
          <div className="p-12 text-center text-sm text-slate-500" aria-live="polite">Loading leads…</div>
        ) : leads.length === 0 ? (
          <div className="p-12 text-center">
            <h3 className="font-semibold text-ink">{error ? "Leads could not be loaded" : appliedFilters ? "No leads match these filters" : "No leads yet"}</h3>
            <p className="mt-1 text-sm text-slate-500">{error ? "Refresh after checking the API connection." : "Import leads through the existing dashboard to begin enrichment."}</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1120px] text-left text-sm">
              <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500">
                <tr>{["Business / Lead", "Location", "Intelligence score", "Opportunities", "Recommended service", "Personalization", "Enrichment", "Sales status", "Priority", "Next follow-up", "Last enriched", "Action"].map((heading) => <th key={heading} className="px-4 py-3 font-semibold">{heading}</th>)}</tr>
              </thead>
              <tbody>
                {leads.map((lead) => (
                  <tr key={lead.id} className="border-t border-slate-100 hover:bg-slate-50/70">
                    <td className="px-4 py-4"><Link href={`/leads/${lead.id}`} className="font-semibold text-ink hover:text-teal">{lead.company_name}</Link><div className="mt-1 max-w-52 truncate text-xs text-slate-400">{lead.website || lead.industry || "Business profile"}</div></td>
                    <td className="px-4 py-4 text-slate-600">{[lead.city, lead.state, lead.country].filter(Boolean).join(", ") || "—"}</td>
                    <td className="px-4 py-4"><Score value={lead.intelligence_score} /></td>
                    <td className="px-4 py-4"><span className={lead.opportunity_count ? "rounded-full bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-800" : "text-slate-400"}>{lead.opportunity_count || "None"}</span></td>
                    <td className="px-4 py-4 text-slate-700">{lead.recommended_services?.[0] || (lead.recommendation_count ? `${lead.recommendation_count} recommendation${lead.recommendation_count === 1 ? "" : "s"}` : "—")}</td>
                    <td className="px-4 py-4"><span className={lead.personalization_available ? "text-emerald-700" : "text-slate-400"}>{lead.personalization_available ? "Available" : "—"}</span></td>
                    <td className="px-4 py-4"><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${lead.intelligence_status === "available" ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-600"}`}>{lead.intelligence_status === "available" ? "Ready" : "Awaiting"}</span></td>
                    <td className="px-4 py-4 text-xs font-semibold text-slate-700">{lead.sales_status}</td>
                    <td className="px-4 py-4 text-xs text-slate-600">{lead.priority}</td>
                    <td className="whitespace-nowrap px-4 py-4 text-xs text-slate-500">{displayDate(lead.next_follow_up_at)}</td>
                    <td className="whitespace-nowrap px-4 py-4 text-slate-500">{displayDate(lead.latest_enrichment_at)}</td>
                    <td className="px-4 py-4"><Link href={`/leads/${lead.id}`} className="font-semibold text-teal hover:underline">Review</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <footer className="flex flex-col gap-3 border-t border-slate-200 px-5 py-4 text-sm sm:flex-row sm:items-center sm:justify-between">
          <div className="text-slate-500">Page {page} · {leads.length} shown{leads.length === pageSize ? "+" : ""} · API order: newest leads first</div>
          <div className="flex items-center gap-3">
            <label className="text-slate-500">Rows
              <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }} className="ml-2 rounded-md border border-slate-300 bg-white px-2 py-1 text-ink">
                {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
              </select>
            </label>
            <button onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={page === 1 || loading} className="rounded-md border border-slate-300 px-3 py-1.5 font-medium disabled:cursor-not-allowed disabled:opacity-40">Previous</button>
            <button onClick={() => setPage((current) => current + 1)} disabled={loading || leads.length < pageSize} className="rounded-md border border-slate-300 px-3 py-1.5 font-medium disabled:cursor-not-allowed disabled:opacity-40">Next</button>
          </div>
        </footer>
      </section>

      <p className="mt-4 text-xs text-slate-400">Search, custom sorting, and total matching-result counts are not currently provided by the API. List filters and pagination are applied server-side.</p>
    </main>
  );
}
