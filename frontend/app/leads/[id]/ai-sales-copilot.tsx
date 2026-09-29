"use client";

import { FormEvent, useMemo, useState } from "react";
import { api, ApiError, CopilotInteraction, CopilotReviewActionType } from "@/lib/api";

const QUESTIONS = [
  ["What should I do next with this lead?", "What should I do next with this lead?"],
  ["Why should I follow up with this lead?", "Why should I follow up with this lead?"],
  ["What are the main opportunities?", "What are the main opportunities?"],
  ["What information is missing?", "What information is missing?"],
  ["What happened in the previous outreach?", "What happened in the previous outreach?"],
  ["What are the current SEO opportunities?", "What are the current SEO opportunities?"],
  ["What should I review before contacting this lead?", "What should I review before contacting this lead?"],
] as const;

const ACTION_LABELS: Record<CopilotReviewActionType, string> = {
  REVIEW_LEAD: "Review lead details", REVIEW_OPPORTUNITY: "Review opportunity",
  REVIEW_SEO: "Review SEO findings", REVIEW_OUTREACH: "Review outreach history",
  REVIEW_COMMERCIAL_CONTEXT: "Review commercial context", VERIFY_MISSING_INFORMATION: "Verify missing information",
};
const ACTION_HREFS: Record<CopilotReviewActionType, string> = {
  REVIEW_LEAD: "#business-summary", REVIEW_OPPORTUNITY: "#opportunities",
  REVIEW_SEO: "#seo", REVIEW_OUTREACH: "#outreach",
  REVIEW_COMMERCIAL_CONTEXT: "#commercial", VERIFY_MISSING_INFORMATION: "#business-summary",
};

export default function AISalesCopilot({ leadId }: { leadId: string }) {
  const [question, setQuestion] = useState<string>(QUESTIONS[0][0]);
  const [interaction, setInteraction] = useState<CopilotInteraction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const evidence = useMemo(() => new Map((interaction?.evidence_catalog || []).map((item) => [item.id, item])), [interaction]);

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setError(""); setInteraction(null);
    try {
      const result = await api<CopilotInteraction>(`/api/leads/${encodeURIComponent(leadId)}/ai-copilot`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }),
      });
      setInteraction(result);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Sales Copilot could not answer this question.");
    } finally { setBusy(false); }
  }

  function EvidenceList({ refs }: { refs: string[] }) {
    if (!refs.length) return <p className="text-xs text-slate-500">No matching evidence was selected.</p>;
    return <ul className="mt-2 space-y-2">{refs.map((ref) => {
      const item = evidence.get(ref);
      return <li key={ref} className="rounded-md border border-slate-200 bg-white p-2">
        <p className="break-all font-mono text-[11px] text-teal">{ref}</p>
        <p className="mt-1 text-xs font-semibold text-slate-700">{item?.label || item?.source_type || "Evidence"}</p>
        {item?.display_value && <p className="mt-1 break-words text-xs text-slate-500">{item.display_value}</p>}
      </li>;
    })}</ul>;
  }

  return <section className="mt-5 rounded-xl border border-violet-200 bg-white p-5 shadow-sm">
    <p className="text-[10px] font-bold uppercase tracking-[.18em] text-violet-700">AI-assisted decision support — review before acting.</p>
    <h2 className="mt-1 font-bold text-ink">AI Sales Copilot</h2>
    <form onSubmit={(event) => void ask(event)} className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end">
      <label className="flex-1 text-xs font-semibold text-slate-600">Question
        <select value={question} onChange={(event) => setQuestion(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
          {QUESTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <button type="submit" disabled={busy} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Reviewing current evidence…" : "Ask Copilot"}</button>
    </form>
    {error && <p role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {interaction && <div className="mt-5 border-t border-slate-200 pt-4">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500"><span>{interaction.status === "validated" ? "Evidence validated" : interaction.status === "unsupported" ? "Unsupported question" : "Generation failed safely"}</span><span>{interaction.provider} · {interaction.model}</span><span>{new Date(interaction.created_at).toLocaleString()}</span>{interaction.is_stale && <span className="font-semibold text-amber-700">Source context has changed since this response.</span>}</div>
      {interaction.status === "failed" && <p role="alert" className="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{interaction.error_message || "No partial answer was saved."} <span className="font-mono">{interaction.error_code}</span></p>}
      {interaction.response && <>
        <article className="mt-4 rounded-lg bg-violet-50 p-4"><h3 className="text-sm font-semibold text-ink">Answer</h3><p className="mt-1 text-sm leading-6 text-slate-700">{interaction.response.answer}</p></article>
        {interaction.response.intent === "UNSUPPORTED" && <p className="mt-4 text-sm text-slate-600">Choose one of the seven supported questions from the menu.</p>}
        {interaction.response.key_facts.length > 0 && <section className="mt-4"><h3 className="text-sm font-semibold text-ink">Key facts</h3><EvidenceList refs={interaction.response.key_facts} /></section>}
        {interaction.response.relevant_opportunities.length > 0 && <section id="copilot-opportunities" className="mt-4"><h3 className="text-sm font-semibold text-ink">Relevant opportunities</h3><EvidenceList refs={interaction.response.relevant_opportunities} /></section>}
        {interaction.response.recommended_review_actions.length > 0 && <section className="mt-4"><h3 className="text-sm font-semibold text-ink">Recommended review actions</h3><ul className="mt-2 space-y-2">{interaction.response.recommended_review_actions.map((item, index) => <li key={`${item.action}-${index}`} className="rounded-lg border border-slate-200 p-3"><a href={ACTION_HREFS[item.action]} className="text-sm font-semibold text-teal underline">{ACTION_LABELS[item.action]}</a><EvidenceList refs={item.evidence_refs} />{item.service_ids.length > 0 && <p className="mt-2 text-xs text-slate-500">Existing services: {item.service_ids.join(", ")}</p>}</li>)}</ul></section>}
        {interaction.response.missing_information.length > 0 && <section className="mt-4"><h3 className="text-sm font-semibold text-ink">Missing information</h3><EvidenceList refs={interaction.response.missing_information} /></section>}
        <details className="mt-4 rounded-lg bg-slate-50 p-3"><summary className="cursor-pointer text-sm font-semibold text-ink">Evidence references ({interaction.response.evidence_refs.length})</summary><EvidenceList refs={interaction.response.evidence_refs} /></details>
      </>}
    </div>}
  </section>;
}
