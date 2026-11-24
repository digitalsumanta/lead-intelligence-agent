"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { CurrentUser, getCurrentUser, logout } from "@/lib/api";

/** Shows who is signed in, with a sign-out button. Sends visitors who are not signed in to /login. */
export default function AuthBar() {
  const pathname = usePathname();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const onLogin = pathname?.startsWith("/login");

  useEffect(() => {
    if (onLogin) return;
    let cancelled = false;
    getCurrentUser()
      .then((current) => {
        if (cancelled) return;
        if (!current) {
          window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
          return;
        }
        setUser(current);
      })
      .catch(() => undefined); // API unreachable: pages show their own error state.
    return () => {
      cancelled = true;
    };
  }, [onLogin, pathname]);

  if (onLogin || !user) return null;

  async function signOut() {
    await logout();
    window.location.assign("/login");
  }

  return (
    <div className="flex items-center justify-end gap-3 border-b border-slate-200 bg-white px-4 py-2 text-sm">
      <span className="text-slate-600">
        {user.display_name || user.email || user.user_id}
        <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">{user.role.replace("_", " ")}</span>
      </span>
      <Link href="/settings" className="rounded-md border border-slate-300 px-2.5 py-1 hover:bg-slate-50">
        Settings
      </Link>
      <button type="button" onClick={signOut} className="rounded-md border border-slate-300 px-2.5 py-1 hover:bg-slate-50">
        Sign out
      </button>
    </div>
  );
}
