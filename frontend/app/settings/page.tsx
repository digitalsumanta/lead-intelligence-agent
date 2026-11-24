"use client";

import Link from "next/link";
import { FormEvent, ReactNode, useCallback, useEffect, useState } from "react";
import {
  AdminUser,
  ApiError,
  CurrentUser,
  ROLE_LABELS,
  Suppression,
  SuppressionPage,
  SuppressionReason,
  SystemStatus,
  USER_ROLES,
  UserRole,
  addSuppression,
  changeOwnPassword,
  createUser,
  getCurrentUser,
  getSystemStatus,
  listSuppressions,
  listUsers,
  removeSuppression,
  resetUserPassword,
  signOutUser,
  updateUser,
} from "@/lib/api";

type Tab = "account" | "users" | "suppressions" | "system";

const card = "rounded-xl border border-slate-200 bg-white p-5 shadow-sm";
const input = "w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none";
const button = "rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50";
const subtle = "rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50";

const isManager = (role?: string) => role === "sales_manager" || role === "admin";

function errorText(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong. Is the API running?";
}

function when(value: string | null) {
  if (!value) return "Never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function Notice({ kind, children }: { kind: "error" | "ok"; children: ReactNode }) {
  const style = kind === "error" ? "bg-red-50 text-red-700" : "bg-emerald-50 text-emerald-700";
  return <p role={kind === "error" ? "alert" : "status"} className={`rounded-md px-3 py-2 text-sm ${style}`}>{children}</p>;
}

// ---------------------------------------------------------------------------
// My account
// ---------------------------------------------------------------------------

function AccountTab({ user }: { user: CurrentUser }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (next !== confirm) return setMessage({ kind: "error", text: "The new passwords do not match." });
    setBusy(true);
    setMessage(null);
    try {
      await changeOwnPassword(current, next);
      setCurrent(""); setNext(""); setConfirm("");
      setMessage({ kind: "ok", text: "Password changed. You were signed out on your other devices." });
    } catch (err) {
      setMessage({ kind: "error", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-5 md:grid-cols-2">
      <section className={card}>
        <h2 className="font-semibold">Your account</h2>
        <dl className="mt-3 space-y-2 text-sm">
          <div><dt className="text-slate-500">E-mail</dt><dd>{user.email ?? user.user_id}</dd></div>
          {user.display_name && <div><dt className="text-slate-500">Name</dt><dd>{user.display_name}</dd></div>}
          <div><dt className="text-slate-500">Role</dt><dd>{ROLE_LABELS[user.role as UserRole] ?? user.role}</dd></div>
        </dl>
      </section>
      {user.auth_method === "session" && (
        <form onSubmit={submit} className={`${card} space-y-3`}>
          <h2 className="font-semibold">Change password</h2>
          <input className={input} type="password" autoComplete="current-password" placeholder="Current password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
          <input className={input} type="password" autoComplete="new-password" placeholder="New password (at least 12 characters)" minLength={12} required value={next} onChange={(e) => setNext(e.target.value)} />
          <input className={input} type="password" autoComplete="new-password" placeholder="Repeat new password" minLength={12} required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          {message && <Notice kind={message.kind}>{message.text}</Notice>}
          <button className={button} disabled={busy}>{busy ? "Saving…" : "Change password"}</button>
        </form>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users (admins)
// ---------------------------------------------------------------------------

function UsersTab({ me }: { me: CurrentUser }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [form, setForm] = useState({ email: "", display_name: "", role: "sales_rep" as UserRole, password: "" });

  const load = useCallback(async () => {
    try { setUsers(await listUsers()); setError(""); } catch (err) { setError(errorText(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function act(id: string, label: string, action: () => Promise<unknown>) {
    setBusy(id); setError(""); setNotice("");
    try { await action(); setNotice(label); await load(); } catch (err) { setError(errorText(err)); } finally { setBusy(""); }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    await act("new", `Created ${form.email.trim().toLowerCase()}.`, async () => {
      await createUser({ ...form, display_name: form.display_name || undefined });
      setForm({ email: "", display_name: "", role: "sales_rep", password: "" });
    });
  }

  function resetPassword(user: AdminUser) {
    const password = window.prompt(`New password for ${user.email} (at least 12 characters). Share it with them securely.`);
    if (password) void act(user.id, `Password reset for ${user.email}; their sessions were ended.`, () => resetUserPassword(user.id, password));
  }

  return (
    <div className="space-y-5">
      {error && <Notice kind="error">{error}</Notice>}
      {notice && <Notice kind="ok">{notice}</Notice>}
      <section className={`${card} overflow-x-auto`}>
        <h2 className="font-semibold">Users</h2>
        <table className="mt-3 w-full min-w-[720px] text-left text-sm">
          <thead className="text-slate-500">
            <tr><th className="py-2">User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th className="text-right">Actions</th></tr>
          </thead>
          <tbody>
            {users.map((user) => {
              const isMe = user.email === me.email;
              return (
                <tr key={user.id} className="border-t border-slate-100 align-middle">
                  <td className="py-2">
                    <div className="font-medium">{user.display_name || user.email}{isMe && <span className="ml-2 text-xs text-slate-400">(you)</span>}</div>
                    {user.display_name && <div className="text-xs text-slate-500">{user.email}</div>}
                  </td>
                  <td>
                    <select aria-label={`Role for ${user.email}`} className="rounded-md border border-slate-300 px-2 py-1" value={user.role} disabled={busy === user.id}
                      onChange={(e) => void act(user.id, `Role updated for ${user.email}; the change applies at their next sign-in.`, () => updateUser(user.id, { role: e.target.value as UserRole }))}>
                      {USER_ROLES.map((role) => <option key={role} value={role}>{ROLE_LABELS[role]}</option>)}
                    </select>
                  </td>
                  <td>
                    {user.is_active ? (user.locked ? <span className="text-amber-700">Locked (too many attempts)</span> : <span className="text-emerald-700">Active</span>) : <span className="text-slate-400">Deactivated</span>}
                    {user.active_sessions > 0 && <div className="text-xs text-slate-500">{user.active_sessions} active session{user.active_sessions === 1 ? "" : "s"}</div>}
                  </td>
                  <td className="text-slate-600">{when(user.last_login_at)}</td>
                  <td className="space-x-2 whitespace-nowrap py-2 text-right">
                    <button className={subtle} disabled={busy === user.id} onClick={() => resetPassword(user)}>Reset password</button>
                    <button className={subtle} disabled={busy === user.id || user.active_sessions === 0}
                      onClick={() => void act(user.id, `Signed ${user.email} out everywhere.`, () => signOutUser(user.id))}>Sign out</button>
                    {user.is_active ? (
                      <button className={subtle} disabled={busy === user.id || isMe}
                        onClick={() => window.confirm(`Deactivate ${user.email}? They will be signed out and unable to sign in.`) &&
                          void act(user.id, `Deactivated ${user.email}.`, () => updateUser(user.id, { is_active: false }))}>Deactivate</button>
                    ) : (
                      <button className={subtle} disabled={busy === user.id}
                        onClick={() => void act(user.id, `Reactivated ${user.email}.`, () => updateUser(user.id, { is_active: true }))}>Reactivate</button>
                    )}
                    {user.locked && user.is_active && (
                      <button className={subtle} disabled={busy === user.id}
                        onClick={() => void act(user.id, `Unlocked ${user.email}.`, () => updateUser(user.id, { is_active: true }))}>Unlock</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <form onSubmit={create} className={`${card} grid gap-3 md:grid-cols-5 md:items-end`}>
        <h2 className="font-semibold md:col-span-5">Add a user</h2>
        <label className="text-sm md:col-span-1">E-mail<input className={input} type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label>
        <label className="text-sm md:col-span-1">Name (optional)<input className={input} value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
        <label className="text-sm md:col-span-1">Role
          <select className={input} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as UserRole })}>
            {USER_ROLES.map((role) => <option key={role} value={role}>{ROLE_LABELS[role]}</option>)}
          </select>
        </label>
        <label className="text-sm md:col-span-1">Initial password<input className={input} type="password" autoComplete="new-password" minLength={12} required value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
        <button className={button} disabled={busy === "new"}>{busy === "new" ? "Adding…" : "Add user"}</button>
        <p className="text-xs text-slate-500 md:col-span-5">Share the initial password securely and ask them to change it under My account.</p>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Suppression list
// ---------------------------------------------------------------------------

const REASON_LABELS: Record<SuppressionReason, string> = {
  UNSUBSCRIBED: "Unsubscribed",
  HARD_BOUNCE: "Hard bounce",
  COMPLAINT: "Spam complaint",
  MANUAL: "Added manually",
};

function SuppressionsTab({ me }: { me: CurrentUser }) {
  const [data, setData] = useState<SuppressionPage | null>(null);
  const [page, setPage] = useState(1);
  const [reason, setReason] = useState<SuppressionReason | "">("");
  const [email, setEmail] = useState("");
  const [details, setDetails] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setData(await listSuppressions(page, reason || undefined)); setError(""); } catch (err) { setError(errorText(err)); }
  }, [page, reason]);
  useEffect(() => { void load(); }, [load]);

  async function add(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(""); setNotice("");
    try {
      const row = await addSuppression(email.trim(), details.trim());
      setNotice(`${row.email_masked ?? "Address"} will not be e-mailed.`);
      setEmail(""); setDetails("");
      await load();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }

  async function remove(row: Suppression) {
    if (!window.confirm(`Remove ${row.email_masked ?? "this address"} from the suppression list? Only do this if they explicitly asked to receive e-mail again.`)) return;
    setError(""); setNotice("");
    try { await removeSuppression(row.id); setNotice("Entry removed."); await load(); } catch (err) { setError(errorText(err)); }
  }

  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="space-y-5">
      <p className="text-sm text-slate-600">Addresses here are never e-mailed by any send path. Unsubscribes, hard bounces and spam complaints are added automatically; addresses are stored only as a hash.</p>
      {error && <Notice kind="error">{error}</Notice>}
      {notice && <Notice kind="ok">{notice}</Notice>}
      <form onSubmit={add} className={`${card} grid gap-3 md:grid-cols-4 md:items-end`}>
        <h2 className="font-semibold md:col-span-4">Add an address</h2>
        <label className="text-sm md:col-span-1">E-mail<input className={input} type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        <label className="text-sm md:col-span-2">Note (optional)<input className={input} placeholder='e.g. replied "please stop"' maxLength={500} value={details} onChange={(e) => setDetails(e.target.value)} /></label>
        <button className={button} disabled={busy}>{busy ? "Adding…" : "Add to list"}</button>
      </form>
      <section className={`${card} overflow-x-auto`}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-semibold">Suppressed addresses {data && <span className="font-normal text-slate-500">({data.total})</span>}</h2>
          <select aria-label="Filter by reason" className="rounded-md border border-slate-300 px-2 py-1 text-sm" value={reason}
            onChange={(e) => { setPage(1); setReason(e.target.value as SuppressionReason | ""); }}>
            <option value="">All reasons</option>
            {(Object.keys(REASON_LABELS) as SuppressionReason[]).map((r) => <option key={r} value={r}>{REASON_LABELS[r]}</option>)}
          </select>
        </div>
        {data && data.items.length === 0 ? <p className="mt-3 text-sm text-slate-500">No entries.</p> : (
          <table className="mt-3 w-full min-w-[640px] text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-2">Address</th><th>Reason</th><th>Source</th><th>Added</th><th /></tr></thead>
            <tbody>
              {data?.items.map((row) => (
                <tr key={row.id} className="border-t border-slate-100">
                  <td className="py-2">{row.email_masked ?? <span className="text-slate-400">hidden (via unsubscribe link)</span>}{row.details && <div className="text-xs text-slate-500">{row.details}</div>}</td>
                  <td>{REASON_LABELS[row.reason] ?? row.reason}</td>
                  <td className="text-slate-600">{row.source.replace(/_/g, " ")}</td>
                  <td className="text-slate-600">{when(row.created_at)}</td>
                  <td className="text-right">{isManager(me.role) && <button className={subtle} onClick={() => void remove(row)}>Remove</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {pages > 1 && (
          <div className="mt-3 flex items-center gap-2 text-sm">
            <button className={subtle} disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
            <span>Page {page} of {pages}</span>
            <button className={subtle} disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// System status (managers and admins, read-only)
// ---------------------------------------------------------------------------

function Status({ ok, warn, children }: { ok: boolean; warn?: boolean; children: ReactNode }) {
  const style = ok ? "bg-emerald-50 text-emerald-700" : warn ? "bg-amber-50 text-amber-800" : "bg-red-50 text-red-700";
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${style}`}>{children}</span>;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return <div className="flex items-start justify-between gap-4 border-t border-slate-100 py-2 text-sm first:border-t-0"><span className="text-slate-500">{label}</span><span className="text-right">{children}</span></div>;
}

function SystemTab() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { getSystemStatus().then(setStatus).catch((err) => setError(errorText(err))); }, []);
  if (error) return <Notice kind="error">{error}</Notice>;
  if (!status) return <p className="text-sm text-slate-500">Loading…</p>;
  const s = status;
  const suppressionTotal = Object.values(s.suppressions).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-5">
      <p className="text-sm text-slate-600">Read-only. These values come from the server&apos;s <code>.env</code>; change them there and restart the backend and workers. Secret values are never shown.</p>
      <div className="grid gap-5 md:grid-cols-2">
        <section className={card}>
          <h2 className="mb-2 font-semibold">Outreach sending</h2>
          <Row label="Live sending">{s.outreach.dispatch_enabled ? <Status ok={false} warn>On</Status> : <Status ok>Off</Status>}</Row>
          <Row label="Canary mode">{s.outreach.canary_mode ? <Status ok>On (restricted)</Status> : <Status ok={!s.outreach.dispatch_enabled}>Off</Status>}</Row>
          {s.outreach.unrestricted_live_sending && <Notice kind="error">Live sending is on without canary mode: approved e-mails go to any recipient.</Notice>}
          <Row label="E-mail provider">{s.outreach.email_provider ?? "—"} {s.outreach.email_enabled ? "" : "(disabled)"}</Row>
          <Row label="Canary allowed domains">{s.outreach.canary_allowed_domains.join(", ") || "—"}</Row>
        </section>
        <section className={card}>
          <h2 className="mb-2 font-semibold">E-mail compliance</h2>
          <Row label="Unsubscribe and address footer">{s.compliance.configured ? <Status ok>Configured</Status> : <Status ok={false}>{s.compliance.enforced ? "Missing: sends blocked" : "Missing"}</Status>}</Row>
          {s.compliance.problems.length > 0 && <ul className="mb-2 list-disc pl-5 text-xs text-red-700">{s.compliance.problems.map((p) => <li key={p}>{p}</li>)}</ul>}
          <Row label="Unsubscribe link / mailto">{s.compliance.unsubscribe_link ? "Link" : "—"} / {s.compliance.unsubscribe_mailto ? "Mailto" : "—"}</Row>
          <Row label="Suppressed addresses">{suppressionTotal}</Row>
        </section>
        <section className={card}>
          <h2 className="mb-2 font-semibold">AI and enrichment</h2>
          <Row label="Default AI provider">{s.ai.default_provider}</Row>
          <Row label="Ollama model">{s.ai.ollama_model}</Row>
          <Row label="Gemini">{s.ai.gemini_enabled ? `Enabled (${s.ai.gemini_model})` : "Disabled"}</Row>
          <Row label="OpenAI-compatible">{s.ai.openai_compatible_enabled ? "Enabled" : "Disabled"}</Row>
          <Row label="Google Places / PageSpeed">{s.enrichment.google_places_enabled ? "On" : "Off"} / {s.enrichment.pagespeed_enabled ? "On" : "Off"}</Row>
        </section>
        <section className={card}>
          <h2 className="mb-2 font-semibold">System</h2>
          <Row label="Environment">{s.environment}</Row>
          <Row label="Database schema">{s.database.up_to_date ? <Status ok>Up to date</Status> : <Status ok={false}>{s.database.schema_revision ?? "unknown"} (needs {s.database.required_revision})</Status>}</Row>
          <Row label="Workers">{s.workers.healthy ? <Status ok>Running</Status> : <Status ok={false}>{s.workers.last_heartbeat_seconds_ago === null ? "Never seen" : `Last seen ${s.workers.last_heartbeat_seconds_ago}s ago`}</Status>}</Row>
          <Row label="Stored API keys encrypted">{s.credentials.encryption_key_configured ? <Status ok={s.credentials.encrypted_keys === s.credentials.stored_keys}>{s.credentials.encrypted_keys} of {s.credentials.stored_keys}</Status> : <Status ok={false}>No encryption key</Status>}</Row>
          <Row label="Metrics / error tracking">{s.monitoring.metrics_enabled ? "On" : "Off"} / {s.monitoring.sentry_enabled ? "On" : "Off"}</Row>
          <Row label="Users">{s.users.active} active of {s.users.total} ({s.users.active_admins} admin{s.users.active_admins === 1 ? "" : "s"})</Row>
        </section>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SettingsPage() {
  const [me, setMe] = useState<CurrentUser | null>(null);
  const [tab, setTab] = useState<Tab>("account");

  useEffect(() => {
    getCurrentUser().then(setMe).catch(() => undefined);
    const requested = new URLSearchParams(window.location.search).get("tab") as Tab | null;
    if (requested) setTab(requested);
  }, []);

  const tabs: { id: Tab; label: string; visible: boolean }[] = [
    { id: "account", label: "My account", visible: true },
    { id: "users", label: "Users", visible: me?.role === "admin" },
    { id: "suppressions", label: "Suppression list", visible: true },
    { id: "system", label: "System status", visible: isManager(me?.role) },
  ];
  const visible = tabs.filter((t) => t.visible);
  const active = visible.some((t) => t.id === tab) ? tab : "account";

  function select(id: Tab) {
    setTab(id);
    window.history.replaceState(null, "", `/settings?tab=${id}`);
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <Link href="/leads" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link>
      <h1 className="mt-1 text-2xl font-semibold">Settings</h1>
      <nav className="mt-4 flex flex-wrap gap-2 border-b border-slate-200" aria-label="Settings sections">
        {visible.map((t) => (
          <button key={t.id} onClick={() => select(t.id)} aria-current={active === t.id ? "page" : undefined}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${active === t.id ? "border-slate-900 text-slate-900" : "border-transparent text-slate-500 hover:text-slate-800"}`}>
            {t.label}
          </button>
        ))}
      </nav>
      <div className="mt-5">
        {!me ? <p className="text-sm text-slate-500">Loading…</p> : (
          <>
            {active === "account" && <AccountTab user={me} />}
            {active === "users" && <UsersTab me={me} />}
            {active === "suppressions" && <SuppressionsTab me={me} />}
            {active === "system" && <SystemTab />}
          </>
        )}
      </div>
    </main>
  );
}
