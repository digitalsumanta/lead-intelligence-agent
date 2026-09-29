"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, RevenueConversion, RevenuePipelineStage, RevenueSources, RevenueSummary, RevenueTarget, SalesCollection } from "@/lib/api";

const CURRENCIES = ["INR", "USD", "EUR", "GBP", "AED", "AUD", "CAD", "SGD", "JPY", "CHF", "CNY", "NZD", "ZAR"];

function money(value: string | null | undefined, currency: string) {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.startsWith("-");
  const [whole = "0", fraction = "00"] = value.replace("-", "").split(".");
  const integer = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(BigInt(whole || "0"));
  const symbol = new Intl.NumberFormat("en-IN", { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).formatToParts(0).find((part) => part.type === "currency")?.value || currency;
  return `${negative ? "-" : ""}${symbol}${integer}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

function percent(value: string | null) {
  return value === null ? "—" : `${value}%`;
}

export default function RevenuePage() {
  const [currency, setCurrency] = useState("INR");
  const [summary, setSummary] = useState<RevenueSummary | null>(null);
  const [pipeline, setPipeline] = useState<RevenuePipelineStage[]>([]);
  const [conversion, setConversion] = useState<RevenueConversion | null>(null);
  const [sources, setSources] = useState<RevenueSources | null>(null);
  const [collections, setCollections] = useState<SalesCollection[]>([]);
  const [targets, setTargets] = useState<RevenueTarget[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [label, setLabel] = useState("Revenue target");
  const [targetAmount, setTargetAmount] = useState("");
  const [periodStart, setPeriodStart] = useState(() => `${new Date().getFullYear()}-01-01`);
  const [periodEnd, setPeriodEnd] = useState(() => `${new Date().getFullYear()}-12-31`);
  const [editingTargetId, setEditingTargetId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const query = `?currency=${currency}`;
      const [summaryData, pipelineData, conversionData, sourceData, collectionData, targetData] = await Promise.all([
        api<RevenueSummary>(`/api/revenue/summary${query}`),
        api<RevenuePipelineStage[]>(`/api/revenue/pipeline${query}`),
        api<RevenueConversion>(`/api/revenue/conversion${query}`),
        api<RevenueSources>(`/api/revenue/sources${query}`),
        api<SalesCollection[]>(`/api/revenue/collections${query}&limit=100`),
        api<RevenueTarget[]>(`/api/revenue/targets${query}`),
      ]);
      setSummary(summaryData); setPipeline(pipelineData); setConversion(conversionData);
      setSources(sourceData); setCollections(collectionData); setTargets(targetData);
      const active = targetData.find((target) => target.active);
      if (active && !editingTargetId) {
        setEditingTargetId(active.id); setLabel(active.label); setTargetAmount(active.target_amount);
        setPeriodStart(active.period_start); setPeriodEnd(active.period_end);
      } else if (!active && !editingTargetId) {
        setTargetAmount(""); setLabel("Revenue target");
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Revenue data could not be loaded.");
    } finally { setLoading(false); }
  }, [currency, editingTargetId]);

  useEffect(() => { void load(); }, [load]);

  async function saveTarget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const body = { label: label.trim(), target_amount: targetAmount, currency, period_start: periodStart, period_end: periodEnd, active: true };
      if (editingTargetId) await api(`/api/revenue/targets/${editingTargetId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      else await api("/api/revenue/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      await load();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Revenue target could not be saved."); }
    finally { setBusy(false); }
  }

  async function deactivateTarget(target: RevenueTarget) {
    setBusy(true); setError("");
    try { await api(`/api/revenue/targets/${target.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: false }) }); setEditingTargetId(null); await load(); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Revenue target could not be deactivated."); }
    finally { setBusy(false); }
  }

  const cardValues = summary ? [
    ["Target", summary.target_amount ? money(summary.target_amount, currency) : "Not configured", "Configured target period"],
    ["Collected", money(summary.target_collected ?? summary.actual_collected, currency), summary.target_id ? "During target period" : "All recorded collections"],
    ["Remaining", summary.target_remaining === null ? "—" : money(summary.target_remaining, currency), "Cannot fall below zero"],
    ["Pipeline", money(summary.pipeline_value, currency), "Current opportunity value"],
    ["Proposals", money(summary.proposal_value, currency), "Current PROPOSAL stage"],
    ["Negotiations", money(summary.negotiation_value, currency), "Current NEGOTIATION stage"],
    ["Won", money(summary.won_value, currency), "Won deal value"],
  ] : [];
  const progress = summary?.achievement_percentage ? Math.max(0, Math.min(100, Number(summary.achievement_percentage))) : 0;

  return <main className="mx-auto min-h-screen max-w-7xl px-4 py-6 sm:px-8 lg:py-10">
    <header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-6"><div><Link href="/leads" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link><h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Revenue Command Center</h1><p className="mt-1 text-slate-500">Deterministic pipeline, collections, and conversion reporting. No AI financial calculations.</p></div><div className="flex items-center gap-3"><label className="text-xs font-semibold text-slate-600">Currency<select className="ml-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={currency} onChange={(event) => { setCurrency(event.target.value); setEditingTargetId(null); }} aria-label="Currency">{CURRENCIES.map((code) => <option key={code}>{code}</option>)}</select></label><Link href="/outreach" className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">Outreach</Link><Link href="/leads" className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">Leads</Link></div></header>
    {error && <p role="alert" className="mt-5 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading && !summary ? <p className="mt-8 text-center text-sm text-slate-500">Loading revenue data…</p> : <>
      <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7" aria-label="Revenue summary cards">{cardValues.map(([title, value, note]) => <article key={title} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><p className="text-xs text-slate-500">{title.toUpperCase()}</p><p className="mt-2 text-xl font-bold text-ink">{value}</p><p className="mt-1 text-xs text-slate-400">{note}</p></article>)}</section>

      <div className="mt-6 grid gap-5 lg:grid-cols-[1.1fr_.9fr]">
        <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-bold text-ink">Target progress</h2>{summary?.target_id ? <><div className="mt-3 flex flex-wrap justify-between gap-2 text-sm"><span>{summary.target_label} · {summary.target_amount && money(summary.target_amount, currency)}</span><strong>{percent(summary.achievement_percentage)}</strong></div><div className="mt-3 h-3 rounded-full bg-slate-100"><div className="h-3 rounded-full bg-teal transition-all" style={{ width: `${progress}%` }} /></div><div className="mt-3 flex justify-between text-xs text-slate-500"><span>Collected: {money(summary.target_collected, currency)}</span><span>Remaining: {money(summary.target_remaining, currency)}</span></div><p className="mt-2 text-xs text-slate-400">Achievement can exceed 100% when collections exceed the configured target.</p></> : <p className="mt-3 text-sm text-slate-500">No active target covers today. Configure one below to show period progress.</p>}
          <form onSubmit={saveTarget} className="mt-5 grid gap-3 border-t border-slate-100 pt-4 sm:grid-cols-2"><h3 className="font-semibold text-ink sm:col-span-2">{editingTargetId ? "Update active target" : "Configure revenue target"}</h3><label className="text-xs font-semibold text-slate-600">Label<input required maxLength={200} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" value={label} onChange={(event) => setLabel(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Target amount<input required type="number" min="0" step="0.01" className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" value={targetAmount} onChange={(event) => setTargetAmount(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Period start<input required type="date" className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" value={periodStart} onChange={(event) => setPeriodStart(event.target.value)} /></label><label className="text-xs font-semibold text-slate-600">Period end<input required type="date" className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" value={periodEnd} onChange={(event) => setPeriodEnd(event.target.value)} /></label><button disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Saving…" : "Save target"}</button>{editingTargetId && <button type="button" disabled={busy} onClick={() => { const target = targets.find((item) => item.id === editingTargetId); if (target) void deactivateTarget(target); }} className="rounded-lg border border-rose-300 px-4 py-2 text-sm font-semibold text-rose-800">Deactivate target</button>}</form>
        </section>
        <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-bold text-ink">Conversion funnel</h2>{conversion && <><div className="mt-4 grid grid-cols-2 gap-3 text-sm"><p>Tracked leads <strong className="float-right">{conversion.tracked_lead_count}</strong></p><p>Overall tracked win rate <strong className="float-right">{percent(conversion.overall_conversion_percentage)}</strong></p><p>Won deals <strong className="float-right">{conversion.won_deal_count}</strong></p><p>Lost deals <strong className="float-right">{conversion.lost_deal_count}</strong></p><p>Open opportunities <strong className="float-right">{conversion.open_opportunity_count}</strong></p><p>Average won value <strong className="float-right">{money(conversion.average_won_deal_value, currency)}</strong></p><p className="col-span-2">Average proposal value <strong className="float-right">{money(conversion.average_proposal_value, currency)}</strong></p></div><ol className="mt-4 divide-y divide-slate-100">{conversion.steps.map((step) => <li key={`${step.from_status}-${step.to_status}`} className="flex items-center justify-between gap-3 py-2 text-xs"><span>{step.from_status} → {step.to_status}</span><span>{step.converted_count}/{step.source_count} · <strong>{percent(step.conversion_percentage)}</strong></span></li>)}</ol><p className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-900">{conversion.history_limitation}</p></>}</section>
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-2"><section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-bold text-ink">Pipeline by stage</h2><div className="mt-4 overflow-x-auto"><table className="w-full min-w-[600px] text-left text-sm"><thead className="text-xs uppercase text-slate-400"><tr>{["Stage", "Leads", "Current opportunity", "Won value", "Collected"].map((heading) => <th key={heading} className="px-2 py-2">{heading}</th>)}</tr></thead><tbody>{pipeline.map((row: RevenuePipelineStage) => <tr key={row.sales_status} className="border-t border-slate-100"><td className="px-2 py-2 font-semibold">{row.sales_status}</td><td className="px-2 py-2">{row.lead_count}</td><td className="px-2 py-2">{money(row.current_opportunity_value, currency)}</td><td className="px-2 py-2">{money(row.won_value, currency)}</td><td className="px-2 py-2">{money(row.collected_value, currency)}</td></tr>)}</tbody></table></div><p className="mt-3 text-xs text-slate-400">Current opportunity value uses estimated value in early stages, proposal value in PROPOSAL, and negotiation value in NEGOTIATION. These are stage views, not additive deal values.</p></section>
        <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-bold text-ink">Revenue by source</h2>{sources && !sources.available ? <p className="mt-3 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">{sources.reason}</p> : <p className="mt-3 text-sm text-slate-500">No source attribution data.</p>}<h3 className="mt-6 font-semibold text-ink">Collection progress</h3><p className="mt-1 text-xs text-slate-500">Actual collections are stored once as individual payment records.</p><ul className="mt-3 max-h-80 divide-y divide-slate-100 overflow-y-auto">{collections.map((item) => <li key={item.id} className="flex justify-between gap-3 py-2 text-sm"><span><Link href={`/leads/${item.lead_id}`} className="font-semibold text-teal hover:underline">{item.lead_company_name || "Lead"}</Link><span className="ml-2 text-xs text-slate-400">{new Date(item.collection_date).toLocaleDateString()}</span></span><strong>{money(item.amount, item.currency)}</strong></li>)}</ul>{collections.length === 0 && <p className="mt-3 text-sm text-slate-500">No collections recorded for {currency}.</p>}</section></div>
    </>}
  </main>;
}
