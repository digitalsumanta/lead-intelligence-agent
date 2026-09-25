const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TOKEN = process.env.NEXT_PUBLIC_API_TOKEN || "";
export async function api<T>(path: string, init?: RequestInit): Promise<T> { const headers = new Headers(init?.headers); if (TOKEN) headers.set("Authorization", `Bearer ${TOKEN}`); const r = await fetch(`${API}${path}`, { ...init, headers, cache: "no-store" }); if (!r.ok) throw new Error(`API ${r.status}`); return r.json(); }
