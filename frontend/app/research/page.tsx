"use client";

import Link from "next/link";
import React, { useEffect, useState, useRef, useCallback } from "react";
import {
  api,
  API,
  getApiToken,
  ResearchInput,
  ResearchResultItem,
  ResearchJobState,
  ResearchProvidersStatus,
  SaveLeadsResponse,
  getResearchProvidersStatus,
  startResearchJob,
  getResearchJob,
  cancelResearchJob,
  saveResearchedLeads,
  downloadResearchCsv,
} from "@/lib/api";

const PRESET_INDUSTRIES = [
  "Interior Design",
  "Real Estate",
  "Healthcare",
  "Education",
  "E-commerce",
  "Manufacturing",
  "Hospitality",
];

const PRESET_LOCATIONS = [
  "Kolkata",
  "Howrah",
  "Hooghly",
  "Chandannagar",
  "West Bengal",
  "India",
];

export default function BusinessResearchPage() {
  // Provider status
  const [providerStatus, setProviderStatus] = useState<ResearchProvidersStatus | null>(null);

  // Form inputs
  const [industrySelect, setIndustrySelect] = useState<string>("Interior Design");
  const [customIndustry, setCustomIndustry] = useState<string>("");
  const [locationSelect, setLocationSelect] = useState<string>("Kolkata");
  const [customLocation, setCustomLocation] = useState<string>("");
  const [subCategory, setSubCategory] = useState<string>("");
  const [targetCount, setTargetCount] = useState<number>(50);
  const [researchDepth, setResearchDepth] = useState<"basic" | "standard" | "deep">("standard");

  // Job & Progress
  const [activeJob, setActiveJob] = useState<ResearchJobState | null>(null);
  const [isStarting, setIsStarting] = useState<boolean>(false);
  const [isCancelling, setIsCancelling] = useState<boolean>(false);
  const [pollingError, setPollingError] = useState<string | null>(null);

  // Results & Local edits
  const [results, setResults] = useState<ResearchResultItem[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [editingItem, setEditingItem] = useState<ResearchResultItem | null>(null);
  const [evidenceItem, setEvidenceItem] = useState<ResearchResultItem | null>(null);

  // Save state
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [saveFeedback, setSaveFeedback] = useState<SaveLeadsResponse | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState<boolean>(false);

  // Polling ref
  const pollTimerRef = useRef<NodeJS.Timeout | null>(null);

  // Load provider status
  const fetchProviderStatus = useCallback(async () => {
    try {
      const status = await getResearchProvidersStatus();
      setProviderStatus(status);
    } catch {
      // Ignore or set fallback
    }
  }, []);

  useEffect(() => {
    void fetchProviderStatus();
  }, [fetchProviderStatus]);

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, []);

  // Poll job status
  const pollJob = useCallback(async (jobId: string) => {
    try {
      const job = await getResearchJob(jobId);
      setActiveJob(job);
      if (job.status === "COMPLETED") {
        if (pollTimerRef.current) clearInterval(pollTimerRef.current);
        setResults(job.results);
        setSelectedIds(new Set(job.results.map((r) => r.item_id)));
      } else if (job.status === "FAILED" || job.status === "CANCELLED") {
        if (pollTimerRef.current) clearInterval(pollTimerRef.current);
        if (job.results.length > 0) {
          setResults(job.results);
          setSelectedIds(new Set(job.results.map((r) => r.item_id)));
        }
      }
    } catch (err: any) {
      setPollingError(err?.message || "Failed to poll research progress");
    }
  }, []);

  const handleStartResearch = async (e: React.FormEvent) => {
    e.preventDefault();
    setPollingError(null);
    setSaveFeedback(null);
    setSaveError(null);

    const industry = industrySelect === "__custom__" ? customIndustry.trim() : industrySelect;
    const location = locationSelect === "__custom__" ? customLocation.trim() : locationSelect;

    if (!industry || !location) {
      setPollingError("Industry and location are required.");
      return;
    }

    setIsStarting(true);
    try {
      const input: ResearchInput = {
        industry,
        location,
        sub_category: subCategory.trim() || null,
        target_business_count: Number(targetCount) || 50,
        research_depth: researchDepth,
      };
      const res = await startResearchJob(input);
      const initialJob = await getResearchJob(res.job_id);
      setActiveJob(initialJob);
      setResults([]);
      setSelectedIds(new Set());

      // Start polling
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      pollTimerRef.current = setInterval(() => {
        void pollJob(res.job_id);
      }, 2000);
    } catch (err: any) {
      setPollingError(err?.message || "Failed to start research job");
    } finally {
      setIsStarting(false);
    }
  };

  const handleCancelResearch = async () => {
    if (!activeJob) return;
    setIsCancelling(true);
    try {
      await cancelResearchJob(activeJob.job_id);
      await pollJob(activeJob.job_id);
    } catch (err: any) {
      setPollingError(err?.message || "Failed to cancel research job");
    } finally {
      setIsCancelling(false);
    }
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === results.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(results.map((r) => r.item_id)));
    }
  };

  const toggleSelectItem = (itemId: string) => {
    const next = new Set(selectedIds);
    if (next.has(itemId)) {
      next.delete(itemId);
    } else {
      next.add(itemId);
    }
    setSelectedIds(next);
  };

  const handleExcludeItem = (itemId: string) => {
    setResults((prev) => prev.filter((r) => r.item_id !== itemId));
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.delete(itemId);
      return next;
    });
  };

  const handleSaveEdit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingItem) return;
    setResults((prev) =>
      prev.map((r) => (r.item_id === editingItem.item_id ? editingItem : r))
    );
    setEditingItem(null);
  };

  const handleSaveSelectedLeads = async () => {
    const toSave = results.filter((r) => selectedIds.has(r.item_id));
    if (toSave.length === 0) {
      setSaveError("Please select at least one lead to save.");
      return;
    }
    setIsSaving(true);
    setSaveError(null);
    setSaveFeedback(null);
    try {
      const res = await saveResearchedLeads({ leads: toSave });
      setSaveFeedback(res);
    } catch (err: any) {
      setSaveError(err?.message || "Failed to save leads to database.");
    } finally {
      setIsSaving(false);
    }
  };

  const handleExportCsv = async () => {
    if (!activeJob) return;
    setIsExporting(true);
    try {
      const blob = await downloadResearchCsv(activeJob.job_id);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `mindartdigital_lead_research_${activeJob.job_id.slice(0, 8)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      setPollingError("CSV export failed: " + (err?.message || String(err)));
    } finally {
      setIsExporting(false);
    }
  };

  const isJobRunning = activeJob && (activeJob.status === "RUNNING" || activeJob.status === "QUEUED");

  return (
    <main className="mx-auto min-h-screen max-w-[1540px] px-4 py-6 sm:px-8 lg:py-10">
      {/* Header */}
      <header className="flex flex-col gap-5 border-b border-slate-200 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Link href="/" className="text-xs font-bold uppercase tracking-[.2em] text-teal hover:underline">
              Lead Intelligence Agent
            </Link>
            <span className="text-slate-400">/</span>
            <span className="text-xs font-bold uppercase tracking-[.2em] text-slate-500">MindArtDigital</span>
          </div>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Business Research Engine</h1>
          <p className="mt-1 text-slate-500">
            Evidence-based discovery and qualification of customer leads by industry and geographic location.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <Link href="/" className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-semibold text-ink hover:bg-slate-50">
            Dashboard
          </Link>
          <Link href="/leads" className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-semibold text-ink hover:bg-slate-50">
            Leads Database
          </Link>
          <span className="rounded-full bg-teal/10 px-3 py-1.5 font-medium text-teal ring-1 ring-teal/20">
            Evidence-Backed Discovery
          </span>
        </div>
      </header>

      {/* Provider Status Alert Banner */}
      {providerStatus && !providerStatus.is_configured && (
        <section className="mt-6 rounded-xl border border-amber-200 bg-amber-50 p-4 text-amber-900 shadow-sm" aria-label="Provider Status Notice">
          <div className="flex items-start gap-3">
            <span className="text-xl">⚠️</span>
            <div>
              <h2 className="font-bold text-amber-950">Discovery Provider Configuration Notice</h2>
              <p className="mt-1 text-sm text-amber-800">
                {providerStatus.message}
              </p>
              <p className="mt-1 text-xs text-amber-700">
                Active discovery provider is set to <span className="font-mono font-semibold">{providerStatus.active_discovery_provider}</span>. In accordance with safety rule BR-06, external queries will cleanly halt with a descriptive error rather than fabricate unverified leads.
              </p>
            </div>
          </div>
        </section>
      )}

      {/* Main Research Control Form */}
      <section className="mt-6 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="text-lg font-bold text-ink">Discover & Qualify Businesses</h2>
        <p className="mt-1 text-sm text-slate-500">
          Target businesses for MindArtDigital services (SEO, Website Maintenance, Meta/Google Ads, AI Automation).
        </p>

        <form onSubmit={handleStartResearch} className="mt-6 grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-5">
          {/* Industry */}
          <div>
            <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
              Target Industry *
            </label>
            <select
              value={industrySelect}
              onChange={(e) => setIndustrySelect(e.target.value)}
              className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-ink focus:border-teal focus:outline-none"
              disabled={Boolean(isJobRunning)}
            >
              {PRESET_INDUSTRIES.map((ind) => (
                <option key={ind} value={ind}>{ind}</option>
              ))}
              <option value="__custom__">Custom Industry...</option>
            </select>
            {industrySelect === "__custom__" && (
              <input
                type="text"
                placeholder="e.g. Legal Services"
                value={customIndustry}
                onChange={(e) => setCustomIndustry(e.target.value)}
                className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-ink focus:border-teal focus:outline-none"
                disabled={Boolean(isJobRunning)}
                required
              />
            )}
          </div>

          {/* Location */}
          <div>
            <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
              Location *
            </label>
            <select
              value={locationSelect}
              onChange={(e) => setLocationSelect(e.target.value)}
              className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-ink focus:border-teal focus:outline-none"
              disabled={Boolean(isJobRunning)}
            >
              {PRESET_LOCATIONS.map((loc) => (
                <option key={loc} value={loc}>{loc}</option>
              ))}
              <option value="__custom__">Custom Location...</option>
            </select>
            {locationSelect === "__custom__" && (
              <input
                type="text"
                placeholder="e.g. Bengaluru, Karnataka"
                value={customLocation}
                onChange={(e) => setCustomLocation(e.target.value)}
                className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-ink focus:border-teal focus:outline-none"
                disabled={Boolean(isJobRunning)}
                required
              />
            )}
          </div>

          {/* Subcategory */}
          <div>
            <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
              Subcategory (Optional)
            </label>
            <input
              type="text"
              placeholder="e.g. Dental Clinics, Luxury"
              value={subCategory}
              onChange={(e) => setSubCategory(e.target.value)}
              className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-ink focus:border-teal focus:outline-none"
              disabled={Boolean(isJobRunning)}
            />
          </div>

          {/* Target Business Count */}
          <div>
            <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
              Target Count (1 - 500)
            </label>
            <input
              type="number"
              min={1}
              max={500}
              value={targetCount}
              onChange={(e) => setTargetCount(Math.max(1, Math.min(500, Number(e.target.value))))}
              className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-ink focus:border-teal focus:outline-none"
              disabled={Boolean(isJobRunning)}
            />
          </div>

          {/* Research Depth */}
          <div>
            <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
              Research Depth
            </label>
            <select
              value={researchDepth}
              onChange={(e) => setResearchDepth(e.target.value as any)}
              className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-ink focus:border-teal focus:outline-none"
              disabled={Boolean(isJobRunning)}
            >
              <option value="basic">Basic (Discovery Only)</option>
              <option value="standard">Standard (Website Inspection)</option>
              <option value="deep">Deep (Multi-channel Evidence)</option>
            </select>
          </div>

          {/* Controls */}
          <div className="flex items-center gap-3 pt-2 md:col-span-2 lg:col-span-5">
            <button
              type="submit"
              disabled={Boolean(isJobRunning) || isStarting}
              className="rounded-lg bg-ink px-6 py-2.5 font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50"
            >
              {isStarting ? "Initializing..." : "Start Research"}
            </button>

            {isJobRunning && (
              <button
                type="button"
                onClick={handleCancelResearch}
                disabled={isCancelling}
                className="rounded-lg border border-rose-300 bg-rose-50 px-5 py-2.5 font-semibold text-rose-700 hover:bg-rose-100 disabled:opacity-50"
              >
                {isCancelling ? "Cancelling..." : "Cancel Research"}
              </button>
            )}

            {activeJob && (
              <span className="text-xs text-slate-500 font-mono">
                Job ID: {activeJob.job_id.slice(0, 8)}... | Status: <span className="font-semibold text-ink">{activeJob.status}</span>
              </span>
            )}
          </div>
        </form>

        {pollingError && (
          <div className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">
            {pollingError}
          </div>
        )}
      </section>

      {/* Live Progress Bar & Job Stats */}
      {activeJob && (
        <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Research Progress</span>
                <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
                  activeJob.status === "COMPLETED" ? "bg-emerald-100 text-emerald-800" :
                  activeJob.status === "RUNNING" ? "bg-teal/10 text-teal animate-pulse" :
                  activeJob.status === "FAILED" ? "bg-rose-100 text-rose-800" :
                  "bg-slate-100 text-slate-700"
                }`}>
                  {activeJob.status}
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-400">
                Discovered: <strong className="text-ink">{activeJob.found_count}</strong> | Analyzed: <strong className="text-ink">{activeJob.processed_count}</strong> of {activeJob.target_count} target
              </p>
            </div>
            <div className="text-right">
              <span className="text-2xl font-bold text-ink">{Math.round(activeJob.progress_percent)}%</span>
            </div>
          </div>

          {/* Progress bar */}
          <div className="mt-3 h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
            <div
              className={`h-full transition-all duration-300 ${
                activeJob.status === "FAILED" ? "bg-rose-500" : "bg-teal"
              }`}
              style={{ width: `${Math.min(100, Math.max(0, activeJob.progress_percent))}%` }}
            />
          </div>

          {activeJob.error_message && (
            <div className="mt-4 rounded-lg bg-rose-50 p-3 text-xs text-rose-800 font-mono">
              {activeJob.error_message}
            </div>
          )}
        </section>
      )}

      {/* Save Notification / Feedback */}
      {saveFeedback && (
        <section className="mt-6 rounded-xl border border-emerald-200 bg-emerald-50 p-4 shadow-sm">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="font-bold text-emerald-900">Leads Saved Successfully!</h2>
              <p className="mt-1 text-sm text-emerald-800">
                {saveFeedback.saved_count} total leads saved ({saveFeedback.created_count} newly created, {saveFeedback.updated_count} enriched without overwriting existing data).
              </p>
            </div>
            <Link
              href="/leads"
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-800"
            >
              View In Leads Dashboard →
            </Link>
          </div>
        </section>
      )}

      {saveError && (
        <section className="mt-6 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800 shadow-sm">
          {saveError}
        </section>
      )}

      {/* Results Section */}
      {results.length > 0 && (
        <section className="mt-8 rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex flex-col gap-4 border-b border-slate-200 p-5 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-lg font-bold text-ink">Researched Leads ({results.length})</h2>
              <p className="text-sm text-slate-500">
                Review verified facts, inspect evidence, edit fields, and save selected leads to database.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs text-slate-500 font-medium">
                {selectedIds.size} of {results.length} selected
              </span>
              <button
                type="button"
                onClick={handleSaveSelectedLeads}
                disabled={isSaving || selectedIds.size === 0}
                className="rounded-lg bg-teal px-4 py-2 text-sm font-semibold text-white hover:bg-teal/90 disabled:opacity-50"
              >
                {isSaving ? "Saving..." : `Save Selected (${selectedIds.size})`}
              </button>
              {activeJob && (
                <button
                  type="button"
                  onClick={handleExportCsv}
                  disabled={isExporting}
                  className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-ink hover:bg-slate-50 disabled:opacity-50"
                >
                  {isExporting ? "Exporting..." : "Export CSV"}
                </button>
              )}
            </div>
          </div>

          {/* Results Table */}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1200px] text-left text-sm">
              <thead className="bg-slate-50 text-xs font-semibold uppercase text-slate-500">
                <tr>
                  <th className="px-4 py-3 text-center">
                    <input
                      type="checkbox"
                      checked={selectedIds.size === results.length && results.length > 0}
                      onChange={toggleSelectAll}
                      className="rounded border-slate-300 text-teal focus:ring-teal"
                    />
                  </th>
                  <th className="px-4 py-3">Company Name</th>
                  <th className="px-4 py-3">Website</th>
                  <th className="px-4 py-3">Industry / Subcategory</th>
                  <th className="px-4 py-3">Location</th>
                  <th className="px-4 py-3">Decision Maker</th>
                  <th className="px-4 py-3">Contact Email / Phone</th>
                  <th className="px-4 py-3">Primary Problem</th>
                  <th className="px-4 py-3">Recommended Service</th>
                  <th className="px-4 py-3">Status / Duplicate</th>
                  <th className="px-4 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {results.map((item) => (
                  <tr
                    key={item.item_id}
                    className={`hover:bg-slate-50/80 transition-colors ${
                      item.is_duplicate ? "bg-amber-50/30" : ""
                    }`}
                  >
                    {/* Checkbox */}
                    <td className="px-4 py-3 text-center">
                      <input
                        type="checkbox"
                        checked={selectedIds.has(item.item_id)}
                        onChange={() => toggleSelectItem(item.item_id)}
                        className="rounded border-slate-300 text-teal focus:ring-teal"
                      />
                    </td>

                    {/* Company */}
                    <td className="px-4 py-3 font-semibold text-ink">
                      <div>{item.company_name}</div>
                      {item.is_duplicate && (
                        <span className="inline-block mt-0.5 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-800">
                          Existing Lead
                        </span>
                      )}
                    </td>

                    {/* Website */}
                    <td className="px-4 py-3">
                      {item.website ? (
                        <a
                          href={item.website}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-teal hover:underline max-w-[180px] truncate block"
                        >
                          {item.website.replace(/^https?:\/\//, "")}
                        </a>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>

                    {/* Industry */}
                    <td className="px-4 py-3">
                      <div className="text-slate-800 font-medium">{item.industry}</div>
                      {item.sub_category && (
                        <div className="text-xs text-slate-500">{item.sub_category}</div>
                      )}
                    </td>

                    {/* Location */}
                    <td className="px-4 py-3 text-slate-700">
                      <div>{item.city}</div>
                      <div className="text-xs text-slate-400">{item.country}</div>
                    </td>

                    {/* Decision Maker */}
                    <td className="px-4 py-3">
                      {item.decision_maker ? (
                        <div>
                          <div className="font-medium text-slate-800">{item.decision_maker}</div>
                          <div className="text-xs text-slate-500">{item.decision_maker_role || "—"}</div>
                        </div>
                      ) : (
                        <span className="text-slate-400 italic text-xs">Unverified</span>
                      )}
                    </td>

                    {/* Contact */}
                    <td className="px-4 py-3">
                      {item.business_email ? (
                        <div className="text-xs text-slate-800 truncate max-w-[160px]">{item.business_email}</div>
                      ) : null}
                      {item.phone ? (
                        <div className="text-xs text-slate-500">{item.phone}</div>
                      ) : null}
                      {!item.business_email && !item.phone && (
                        <span className="text-slate-400 text-xs">—</span>
                      )}
                    </td>

                    {/* Primary Problem */}
                    <td className="px-4 py-3 max-w-[200px] text-xs text-slate-600 line-clamp-2">
                      {item.primary_problem || "—"}
                    </td>

                    {/* Recommended MindArt Service */}
                    <td className="px-4 py-3">
                      {item.recommended_mindart_service ? (
                        <span className="inline-block rounded-md bg-teal/10 px-2 py-1 text-xs font-semibold text-teal ring-1 ring-teal/20">
                          {item.recommended_mindart_service}
                        </span>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>

                    {/* Status */}
                    <td className="px-4 py-3 text-xs">
                      <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-700">
                        {item.research_status}
                      </span>
                    </td>

                    {/* Actions */}
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => setEvidenceItem(item)}
                          className="rounded border border-slate-200 px-2.5 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-100"
                        >
                          Evidence
                        </button>
                        <button
                          type="button"
                          onClick={() => setEditingItem({ ...item })}
                          className="rounded border border-slate-200 px-2.5 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-100"
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          onClick={() => handleExcludeItem(item.item_id)}
                          className="rounded border border-rose-200 px-2 py-1 text-xs font-semibold text-rose-600 hover:bg-rose-50"
                          title="Exclude from import"
                        >
                          ✕
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* Evidence Inspection Modal */}
      {evidenceItem && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl bg-white p-6 shadow-xl">
            <div className="flex items-start justify-between border-b border-slate-200 pb-4">
              <div>
                <span className="text-xs font-bold uppercase tracking-wider text-teal">Source Evidence & Audit</span>
                <h3 className="text-xl font-bold text-ink">{evidenceItem.company_name}</h3>
                {evidenceItem.source_evidence.source_url && (
                  <a
                    href={evidenceItem.source_evidence.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-teal hover:underline"
                  >
                    {evidenceItem.source_evidence.source_url}
                  </a>
                )}
              </div>
              <button
                type="button"
                onClick={() => setEvidenceItem(null)}
                className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              >
                ✕
              </button>
            </div>

            <div className="mt-4 space-y-4">
              <div>
                <span className="text-xs text-slate-400">Observed At:</span>
                <p className="text-xs font-mono text-slate-600">
                  {new Date(evidenceItem.source_evidence.observed_at).toLocaleString()}
                </p>
              </div>

              {/* Verified Facts */}
              <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
                <h4 className="text-xs font-bold uppercase tracking-wider text-emerald-800">
                  Verified Facts (Observable on Public Web)
                </h4>
                {evidenceItem.source_evidence.verified_facts.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-slate-700">
                    {evidenceItem.source_evidence.verified_facts.map((fact, i) => (
                      <li key={i}>{fact}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-1 text-xs text-slate-400 italic">No direct facts verified on public website.</p>
                )}
              </div>

              {/* Inferred Opportunities */}
              <div className="rounded-lg border border-teal/20 bg-teal/5 p-4">
                <h4 className="text-xs font-bold uppercase tracking-wider text-teal">
                  Inferred Opportunities (MindArtDigital Services)
                </h4>
                {evidenceItem.source_evidence.inferred_opportunities.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-slate-700">
                    {evidenceItem.source_evidence.inferred_opportunities.map((opp, i) => (
                      <li key={i}>{opp}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-1 text-xs text-slate-400 italic">No specific opportunities inferred.</p>
                )}
              </div>

              {/* Unavailable / Unverified Fields */}
              <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-4">
                <h4 className="text-xs font-bold uppercase tracking-wider text-amber-800">
                  Unavailable / Unverified Fields
                </h4>
                <p className="mt-1 text-xs text-amber-700">
                  In accordance with BR-09, unverified fields are left blank and never hallucinated:
                </p>
                {evidenceItem.source_evidence.unavailable_fields.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-amber-800">
                    {evidenceItem.source_evidence.unavailable_fields.map((f, i) => (
                      <li key={i}>{f}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-1 text-xs text-slate-400 italic">All key fields were discovered.</p>
                )}
              </div>
            </div>

            <div className="mt-6 flex justify-end border-t border-slate-200 pt-4">
              <button
                type="button"
                onClick={() => setEvidenceItem(null)}
                className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit Record Modal */}
      {editingItem && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <form
            onSubmit={handleSaveEdit}
            className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-xl bg-white p-6 shadow-xl"
          >
            <div className="flex items-start justify-between border-b border-slate-200 pb-4">
              <div>
                <span className="text-xs font-bold uppercase tracking-wider text-teal">Review & Edit Lead</span>
                <h3 className="text-xl font-bold text-ink">{editingItem.company_name}</h3>
                <p className="text-xs text-slate-500">Edit fields before importing into the permanent database.</p>
              </div>
              <button
                type="button"
                onClick={() => setEditingItem(null)}
                className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              >
                ✕
              </button>
            </div>

            <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Company Name *</label>
                <input
                  type="text"
                  value={editingItem.company_name}
                  onChange={(e) => setEditingItem({ ...editingItem, company_name: e.target.value })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                  required
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Website</label>
                <input
                  type="text"
                  value={editingItem.website || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, website: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Industry</label>
                <input
                  type="text"
                  value={editingItem.industry || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, industry: e.target.value })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Subcategory</label>
                <input
                  type="text"
                  value={editingItem.sub_category || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, sub_category: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">City</label>
                <input
                  type="text"
                  value={editingItem.city || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, city: e.target.value })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Country</label>
                <input
                  type="text"
                  value={editingItem.country || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, country: e.target.value })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Decision Maker Name</label>
                <input
                  type="text"
                  value={editingItem.decision_maker || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, decision_maker: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Decision Maker Role</label>
                <input
                  type="text"
                  value={editingItem.decision_maker_role || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, decision_maker_role: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Business Email</label>
                <input
                  type="email"
                  value={editingItem.business_email || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, business_email: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Phone</label>
                <input
                  type="text"
                  value={editingItem.phone || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, phone: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">LinkedIn</label>
                <input
                  type="text"
                  value={editingItem.linkedin || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, linkedin: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold uppercase text-slate-600">Advertising Platforms</label>
                <input
                  type="text"
                  value={editingItem.advertising_platforms || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, advertising_platforms: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div className="sm:col-span-2">
                <label className="block text-xs font-bold uppercase text-slate-600">Primary Problem</label>
                <textarea
                  rows={2}
                  value={editingItem.primary_problem || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, primary_problem: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>

              <div className="sm:col-span-2">
                <label className="block text-xs font-bold uppercase text-slate-600">Recommended MindArt Service</label>
                <input
                  type="text"
                  value={editingItem.recommended_mindart_service || ""}
                  onChange={(e) => setEditingItem({ ...editingItem, recommended_mindart_service: e.target.value || null })}
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
                />
              </div>
            </div>

            <div className="mt-6 flex justify-end gap-3 border-t border-slate-200 pt-4">
              <button
                type="button"
                onClick={() => setEditingItem(null)}
                className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                className="rounded-lg bg-teal px-4 py-2 text-sm font-semibold text-white hover:bg-teal/90"
              >
                Apply Edits
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}
