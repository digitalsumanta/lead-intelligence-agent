"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  DimensionScoreExplanation,
  getLeadQualification,
  LeadQualificationResult,
  qualifyLead,
} from "@/lib/api";

interface LeadQualificationPanelProps {
  leadId: string;
  onQualified?: () => void;
}

export default function LeadQualificationPanel({
  leadId,
  onQualified,
}: LeadQualificationPanelProps) {
  const [data, setData] = useState<LeadQualificationResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setError(null);
      const res = await getLeadQualification(leadId);
      setData(res);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setData(null);
      } else {
        setError(err instanceof ApiError ? err.message : "Failed to load lead qualification.");
      }
    } finally {
      setLoading(false);
    }
  }, [leadId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleQualify = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await qualifyLead(leadId);
      setData(res);
      if (onQualified) onQualified();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to run lead qualification.");
    } finally {
      setBusy(false);
    }
  };

  const getTierBadge = (classification: string) => {
    switch (classification) {
      case "HOT":
        return (
          <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-rose-100 text-rose-800 border border-rose-300">
            🔥 HOT (80–100) · Immediate Human Review
          </span>
        );
      case "WARM":
        return (
          <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-amber-100 text-amber-800 border border-amber-300">
            ⚡ WARM (60–79) · Commercial Opportunity Verified
          </span>
        );
      case "NURTURE":
        return (
          <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-blue-100 text-blue-800 border border-blue-300">
            🌱 NURTURE (40–59) · High Fit / Needs Discovery
          </span>
        );
      case "LOW_PRIORITY":
      default:
        return (
          <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-slate-100 text-slate-700 border border-slate-300">
            💤 LOW PRIORITY (0–39) · Unobserved / Deprioritize
          </span>
        );
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case "EVALUATED":
        return (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
            EVALUATED
          </span>
        );
      case "PARTIALLY_EVALUATED":
        return (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200">
            PARTIAL
          </span>
        );
      case "UNAVAILABLE":
        return (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200">
            UNAVAILABLE
          </span>
        );
      default:
        return (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-slate-50 text-slate-500 border border-slate-200">
            {status}
          </span>
        );
    }
  };

  return (
    <section
      id="lead-qualification-readiness"
      className="mt-6 rounded-xl border border-slate-200 bg-white p-6 shadow-sm"
      aria-label="Lead Qualification and Sales Readiness"
    >
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-slate-100 pb-5">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold uppercase tracking-wider text-teal">
              Sales Qualification & Readiness Engine
            </span>
            <span className="rounded bg-teal-50 px-2 py-0.5 text-[10px] font-medium text-teal-800 border border-teal-200">
              v1.0
            </span>
          </div>
          <h2 className="mt-1 text-xl font-bold text-ink">Commercial Qualification & Sales Readiness</h2>
          <p className="mt-1 text-sm text-slate-500">
            Evidence-grounded qualification evaluating problem severity, commercial fit, intent, and reachability.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={handleQualify}
            disabled={busy || loading}
            className="inline-flex items-center gap-2 rounded-lg bg-teal px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-teal-700 disabled:opacity-50 transition"
          >
            {busy ? (
              <>
                <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                </svg>
                Evaluating…
              </>
            ) : (
              <>⚡ Requalify Lead</>
            )}
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="mt-4 rounded-lg bg-rose-50 border border-rose-200 p-4 text-sm text-rose-800">
          <strong>Evaluation Error:</strong> {error}
        </div>
      )}

      {loading ? (
        <div className="py-12 text-center text-sm text-slate-500">
          Calculating evidence-grounded lead qualification…
        </div>
      ) : !data ? (
        <div className="py-10 text-center">
          <p className="text-sm text-slate-500">
            No qualification record exists yet for this lead. Click &quot;Requalify Lead&quot; to compute transparent scores.
          </p>
        </div>
      ) : (
        <div className="mt-6 space-y-6">
          {/* Top Score Summary Banner */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 p-5 rounded-xl bg-slate-50 border border-slate-200">
            {/* Qualification Score */}
            <div className="space-y-1">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Qualification Score
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-3xl font-extrabold text-ink">{data.qualification_score}</span>
                <span className="text-sm font-medium text-slate-400">/ 100</span>
              </div>
              <div className="w-full bg-slate-200 rounded-full h-2 mt-2">
                <div
                  className={`h-2 rounded-full ${
                    data.qualification_score >= 80
                      ? "bg-rose-500"
                      : data.qualification_score >= 60
                      ? "bg-amber-500"
                      : data.qualification_score >= 40
                      ? "bg-blue-500"
                      : "bg-slate-400"
                  }`}
                  style={{ width: `${Math.min(100, Math.max(0, data.qualification_score))}%` }}
                />
              </div>
              <div className="pt-2">{getTierBadge(data.classification)}</div>
            </div>

            {/* Scoring Coverage (FINAL-02) */}
            <div className="space-y-1 border-t md:border-t-0 md:border-l border-slate-200 md:pl-4">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Scoring Coverage
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-3xl font-extrabold text-ink">
                  {Math.round(data.scoring_coverage * 100)}%
                </span>
                <span className="text-xs font-medium text-slate-400">evidence presence</span>
              </div>
              <p className="text-xs text-slate-500 mt-2 leading-relaxed">
                Independent metric measuring observed vs. unobserved criteria. Low coverage limits confidence and comparability.
              </p>
            </div>

            {/* Recommended Sales Action (RQ-04, RQ-05) */}
            <div className="space-y-1 border-t md:border-t-0 md:border-l border-slate-200 md:pl-4">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Advisory Next Action
              </div>
              <div className="mt-1">
                <span className="inline-block px-3 py-1.5 rounded-lg bg-teal-50 text-teal-800 border border-teal-200 font-semibold text-xs">
                  {data.recommended_sales_action.replace(/_/g, " ")}
                </span>
              </div>
              <p className="text-xs text-slate-500 mt-2 leading-relaxed">
                Advisory recommendation for sales rep review. Operational status is preserved.
              </p>
            </div>

            {/* Recommended Priority */}
            <div className="space-y-1 border-t md:border-t-0 md:border-l border-slate-200 md:pl-4">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Recommended Priority
              </div>
              <div className="mt-1">
                <span
                  className={`inline-block px-3 py-1.5 rounded-lg font-semibold text-xs ${
                    data.recommended_priority === "HIGH"
                      ? "bg-rose-50 text-rose-700 border border-rose-200"
                      : data.recommended_priority === "MEDIUM"
                      ? "bg-amber-50 text-amber-700 border border-amber-200"
                      : "bg-slate-100 text-slate-700 border border-slate-200"
                  }`}
                >
                  {data.recommended_priority} PRIORITY
                </span>
              </div>
              <div className="text-[11px] text-slate-400 mt-2">
                Evaluated: {new Date(data.evaluated_at).toLocaleString()}
              </div>
            </div>
          </div>

          {/* Qualification Reasoning Narrative */}
          {data.qualification_reasoning && (
            <div className="p-4 rounded-lg bg-white border border-slate-200">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                Qualification Assessment & Reasoning
              </h3>
              <p className="mt-2 text-sm text-slate-700 leading-relaxed">
                {data.qualification_reasoning}
              </p>
            </div>
          )}

          {/* 7-Dimension Score Breakdown (RQ-07) */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-bold text-ink text-sm sm:text-base">
                Seven-Dimension Qualification Breakdown
              </h3>
              <span className="text-xs text-slate-400">Deterministic · 100 max points total</span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {Object.entries(data.dimension_breakdown || {}).map(([key, dim]: [string, DimensionScoreExplanation]) => {
                const percent =
                  dim.maximum_points > 0
                    ? Math.round((dim.awarded_points / dim.maximum_points) * 100)
                    : 0;

                return (
                  <div
                    key={key}
                    className="p-4 rounded-xl border border-slate-200 bg-white hover:border-slate-300 transition flex flex-col justify-between"
                  >
                    <div>
                      {/* Top title & status */}
                      <div className="flex items-start justify-between gap-2">
                        <h4 className="font-bold text-ink text-sm leading-tight">
                          {dim.dimension_name}
                        </h4>
                        {getStatusBadge(dim.dimension_status)}
                      </div>

                      {/* Points badge & bar */}
                      <div className="mt-3 flex items-baseline justify-between text-xs">
                        <span className="font-bold text-ink text-base">
                          {dim.awarded_points}
                          <span className="text-slate-400 font-normal text-xs"> / {dim.maximum_points} pts</span>
                        </span>
                        <span className="text-slate-500 font-medium">{percent}%</span>
                      </div>
                      <div className="w-full bg-slate-100 rounded-full h-1.5 mt-1.5">
                        <div
                          className="bg-teal h-1.5 rounded-full"
                          style={{ width: `${percent}%` }}
                        />
                      </div>

                      {/* Explanation */}
                      <p className="mt-3 text-xs text-slate-600 leading-relaxed">
                        {dim.explanation}
                      </p>

                      {/* Criteria Met */}
                      {dim.criteria_met.length > 0 && (
                        <div className="mt-3 pt-3 border-t border-slate-100">
                          <span className="text-[10px] font-bold uppercase tracking-wider text-emerald-700">
                            Criteria Met:
                          </span>
                          <ul className="mt-1 space-y-1">
                            {dim.criteria_met.map((item, i) => (
                              <li key={i} className="text-xs text-slate-700 flex items-start gap-1.5">
                                <span className="text-emerald-500 font-bold shrink-0">✓</span>
                                <span>{item}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {/* Missing Information (without penalty) */}
                      {dim.missing_information.length > 0 && (
                        <div className="mt-3 pt-2 border-t border-slate-100">
                          <span className="text-[10px] font-bold uppercase tracking-wider text-amber-700">
                            Missing Information (No Penalty):
                          </span>
                          <ul className="mt-1 space-y-1">
                            {dim.missing_information.map((item, i) => (
                              <li key={i} className="text-xs text-slate-500 flex items-start gap-1.5">
                                <span className="text-amber-500 font-bold shrink-0">?</span>
                                <span>{item}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Supporting Evidence and Limitations Collapsibles */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Supporting Evidence */}
            <div className="p-4 rounded-xl border border-slate-200 bg-slate-50">
              <h4 className="text-xs font-bold uppercase tracking-wider text-slate-600 flex items-center justify-between">
                <span>Supporting Evidence Provenance</span>
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] font-semibold text-slate-700">
                  {data.supporting_evidence.length} points
                </span>
              </h4>
              {data.supporting_evidence.length === 0 ? (
                <p className="mt-2 text-xs text-slate-500">No explicit evidence items logged.</p>
              ) : (
                <ul className="mt-2 space-y-1.5 text-xs text-slate-700 max-h-48 overflow-y-auto pr-1">
                  {data.supporting_evidence.map((item, idx) => (
                    <li key={idx} className="flex items-start gap-2">
                      <span className="text-teal font-bold shrink-0">•</span>
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Score Limitations & Missing Information (FINAL-02) */}
            <div className="p-4 rounded-xl border border-slate-200 bg-slate-50">
              <h4 className="text-xs font-bold uppercase tracking-wider text-slate-600 flex items-center justify-between">
                <span>Score Limitations & Unobserved Data</span>
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                  {data.score_limitations.length} limits
                </span>
              </h4>
              {data.score_limitations.length === 0 ? (
                <p className="mt-2 text-xs text-slate-500">Full evidence coverage observed.</p>
              ) : (
                <ul className="mt-2 space-y-1.5 text-xs text-slate-600 max-h-48 overflow-y-auto pr-1">
                  {data.score_limitations.map((item, idx) => (
                    <li key={idx} className="flex items-start gap-2">
                      <span className="text-amber-500 font-bold shrink-0">⚠️</span>
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
