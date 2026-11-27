import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import api from "./api";
import { useStream } from "./hooks/useStream";
import LiveTab from "./components/LiveTab";
import AwbTab from "./components/AwbTab";
import MatrixTab from "./components/MatrixTab";
import { BoxIcon, PrinterIcon, SettingsIcon, TableIcon, TagIcon, VideoIcon } from "./icons";
import AwbDetailPage from "./components/AwbDetailPage";
import LabelsTab from "./components/LabelsTab";
import { warehousePath } from "./routes";
import SettingsTab from "./components/SettingsTab";
import UsersTab from "./components/UsersTab";
const Dashboard = lazy(() => import("./components/Dashboard"));

const TABS = [
  { path: "/dashboard", label: "Dashboard", icon: <BoxIcon /> },
  { path: "/live", label: "Xray", icon: <VideoIcon /> },
  { path: "/awb", label: "Queue AWB", icon: <TagIcon /> },
  { path: "/matrix", label: "Matrix", icon: <TableIcon /> },
  { path: "/labels", label: "Print Label", icon: <PrinterIcon /> },
  { path: "/users", label: "Manajemen User", icon: <SettingsIcon /> },
  { path: "/settings", label: "Setting", icon: <SettingsIcon /> },
];

const PAGE_TITLES = { "/dashboard": "Dashboard", "/live": "Xray", "/awb": "Queue AWB", "/matrix": "Matrix", "/labels": "Print Label", "/settings": "Setting", "/users": "Manajemen User" };

/** Router ringan berbasis History API — URL ikut berubah ke /namahalaman. */
export function usePathRoute() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = (to) => {
    if (to === window.location.pathname) return;
    window.history.pushState({}, "", to);
    setPath(to);
  };
  return [path, navigate];
}

export default function App() {
  const [auth, setAuth] = useState(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError("");
    fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" })
      .then(async (response) => {
        if (response.status === 401) return null;
        if (!response.ok) throw new Error();
        const session = await response.json();
        if (session.authenticated === false) return null;
        const user = session.user ?? session;
        if (!["cas", "bgd"].includes(user?.access)) throw new Error();
        return user;
      })
      .then((user) => { if (!cancelled) setAuth(user || false); })
      .catch(() => { if (!cancelled) setError("Tidak dapat memuat sesi. Coba lagi."); });
    return () => { cancelled = true; };
  }, [retry]);

  const [captcha, setCaptcha] = useState(null);
  const [captchaToken, setCaptchaToken] = useState("");
  const [widgetError, setWidgetError] = useState("");
  const captchaContainer = useRef(null);
  const captchaWidget = useRef(null);
  const [captchaError, setCaptchaError] = useState(false);
  const loadCaptcha = useCallback(async (signal) => {
    setCaptcha(null);
    setCaptchaToken("");
    setCaptchaError(false);
    try {
      const challenge = await api("/auth/captcha", { signal, cache: "no-store" });
      if (!signal?.aborted) setCaptcha(challenge);
    } catch {
      if (!signal?.aborted) setCaptchaError(true);
    }
  }, []);
  useEffect(() => {
    if (auth !== false) return;
    const controller = new AbortController();
    void loadCaptcha(controller.signal);
    return () => controller.abort();
  }, [auth, loadCaptcha]);

  const siteKey = auth === false ? captcha?.site_key : null;
  useEffect(() => {
    if (!siteKey) return;
    setWidgetError("");
    const script = document.createElement("script");
    const render = () => {
      if (!captchaContainer.current || !window.turnstile) return;
      captchaWidget.current = window.turnstile.render(captchaContainer.current, {
        sitekey: siteKey,
        action: "web_login",
        size: "flexible",
        callback: (token) => {
          setWidgetError("");
          setCaptchaToken(token);
        },
        "expired-callback": () => setCaptchaToken(""),
        "error-callback": (code) => {
          setCaptchaToken("");
          setWidgetError(String(code || "unknown"));
        },
      });
    };
    if (window.turnstile) render();
    else {
      script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
      script.async = true;
      script.onload = render;
      script.onerror = () => setWidgetError("script");
      document.head.appendChild(script);
    }
    return () => {
      script.onload = script.onerror = null;
      script.remove();
      if (captchaWidget.current !== null) window.turnstile?.remove(captchaWidget.current);
      captchaWidget.current = null;
    };
  }, [siteKey]);

  async function login(event) {
    event.preventDefault();
    if (!captcha || (siteKey && !captchaToken)) return;
    setError("");
    setPending(true);
    try {
      const response = await fetch("/api/auth/web-login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password, captcha_token: captchaToken }) });
      if (!response.ok) throw new Error(response.status === 422
        ? "Verifikasi Turnstile gagal atau kedaluwarsa."
        : "Login gagal. Periksa email dan password Anda.");
      setPassword("");
      setAuth(null);
      setRetry((value) => value + 1);
    } catch (error) {
      setError(error.message === "Failed to fetch" ? "Login gagal. Periksa koneksi Anda." : error.message);
      setCaptchaToken("");
      if (captchaWidget.current !== null) window.turnstile?.reset(captchaWidget.current);
    }
    finally { setPending(false); }
  }

  async function logout() {
    setError("");
    setPending(true);
    try {
      const response = await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error();
      setAuth(false);
    } catch { setError("Logout gagal. Coba lagi."); }
    finally { setPending(false); }
  }

  if (auth === null) return <div className="min-h-screen grid place-items-center p-4">{error ? <div role="alert">{error} <button className="btn-ghost" onClick={() => setRetry((value) => value + 1)}>Coba lagi</button></div> : <span role="status">Memuat...</span>}</div>;
  if (!auth) return (
    <main className="min-h-screen grid place-items-center p-4">
      <form onSubmit={login} className="panel w-full max-w-sm flex flex-col gap-4">
        <div>
          <div className="text-muted text-xs font-bold uppercase tracking-wider">Xray Cargo</div>
          <h1 className="text-2xl font-extrabold mt-2">Login akses</h1>
        </div>
        <label htmlFor="email" className="text-sm font-bold">Email</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} className="w-full rounded-xl border-2 border-[var(--border)] bg-[var(--bg-cream)] px-3 py-3 text-[var(--text)] focus:outline-2 focus:outline-[var(--cta)]" />
        <label htmlFor="password" className="text-sm font-bold">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="w-full rounded-xl border-2 border-[var(--border)] bg-[var(--bg-cream)] px-3 py-3 text-[var(--text)] focus:outline-2 focus:outline-[var(--cta)]" />
        {siteKey && <div ref={captchaContainer} />}
        {(captchaError || widgetError) && <p role="alert" className="text-sm text-red-400">
          {widgetError === "110200"
            ? `Cloudflare belum mengizinkan hostname ${window.location.hostname} (110200).`
            : widgetError && /^\d+$/.test(widgetError)
              ? `Verifikasi Cloudflare gagal (${widgetError}).`
              : "Verifikasi Cloudflare tidak dapat dimuat."}{" "}
          {widgetError !== "110200" && <button type="button" className="underline" onClick={() => {
            setWidgetError("");
            if (captchaWidget.current !== null) window.turnstile?.reset(captchaWidget.current);
            else window.location.reload();
          }}>Coba lagi</button>}
        </p>}
        {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
        <button className="btn-clay" type="submit" disabled={pending || !captcha || Boolean(siteKey && !captchaToken)}>{pending ? "Memuat..." : "Masuk"}</button>
      </form>
    </main>
  );
  return <AuthenticatedApp user={auth} onLogout={logout} logoutError={error} logoutPending={pending} />;
}

function AuthenticatedApp({ user, onLogout, logoutError, logoutPending }) {
  const [currentPath, navigateRoute] = usePathRoute();
  const readOnly = user.role === "bc";
  const admin = user.role === "admin";
  const printOnly = user.role === "label_print";
  const requestedPath = warehousePath(currentPath, user.access);
  const canonicalPath = admin ? warehousePath("/users", user.access) : printOnly ? warehousePath("/labels", user.access) : readOnly && !/^\/(?:dashboard|awb(?:\/\d+)?)$/.test(requestedPath.slice(user.access.length + 1))
    ? warehousePath("/dashboard", user.access)
    : requestedPath === warehousePath("/users", user.access) ? warehousePath("/dashboard", user.access) : requestedPath;
  const path = canonicalPath.slice(user.access.length + 1);
  const navigate = (to) => navigateRoute(`/${user.access}${to}`);
  useEffect(() => {
    if (canonicalPath !== currentPath) {
      window.history.replaceState({}, "", canonicalPath + window.location.search + window.location.hash);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }
  }, [canonicalPath, currentPath]);
  const tab = path.replace(/^\/+/, "").split("/")[0] || "live";
  const activePath = `/${tab}` in PAGE_TITLES ? `/${tab}` : "/live";
  const awbDetailMatch = path.match(/^\/awb\/(\d+)$/);
  const awbDetailId = awbDetailMatch ? Number(awbDetailMatch[1]) : null;
  const [session, setSession] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [currentPath]);
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("xray-theme") === "light" ? "light" : "dark"; }
    catch { return "dark"; }
  });
  const { frames, connected, waitingEventsAvailable, onEvent } = useStream(!printOnly && !admin);

  const refreshSession = useCallback(async () => {
    if (!printOnly && !admin) setSession(await api("/session/state").catch(() => null));
  }, [printOnly, admin]);

  useEffect(() => { refreshSession(); }, [refreshSession]);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("xray-theme", theme); } catch { /* storage may be unavailable */ }
  }, [theme]);

  // Refresh session state on parcel activity.
  useEffect(
    () =>
      onEvent((m) => {
        if (m.type?.startsWith("parcel.") || m.type?.startsWith("session.")) {
          refreshSession();
        }
      }),
    [onEvent, refreshSession],
  );

  return (
    <div className="min-h-screen md:flex">
      <aside className="bg-[var(--bg)] border-b-2 md:border-b-0 md:border-r-2 border-[var(--border)] px-4 py-4 flex flex-col gap-4 md:overflow-y-auto md:gap-6 md:w-60 md:h-screen md:sticky md:top-0 md:shrink-0">
        <div className="flex gap-3 items-center px-1.5">
          <span className="grid place-items-center w-10 h-10 rounded-xl bg-[var(--secondary)] border-3 border-[var(--text)] shadow-[4px_4px_0_var(--text)] shrink-0 text-[var(--text)]">
            <BoxIcon />
          </span>
          <div>
            <b className="block text-[16px] font-extrabold tracking-tight">Xray Cargo</b>
          </div>
          <button type="button" className="btn-ghost ml-auto min-h-11 md:hidden"
            aria-expanded={menuOpen} aria-controls="operator-menu" onClick={() => setMenuOpen((open) => !open)}>
            {menuOpen ? "Tutup menu" : "Menu"}
          </button>
        </div>
        <div id="operator-menu" className={`${menuOpen ? "flex" : "hidden"} flex-col gap-4 md:flex md:flex-1 md:gap-6`}>
        <nav aria-label="Navigasi utama" className="grid grid-cols-2 md:grid-cols-1 gap-1.5">
          {TABS.filter((tab) => admin ? tab.path === "/users" : tab.path !== "/users" && (printOnly ? tab.path === "/labels" : !readOnly || ["/dashboard", "/awb"].includes(tab.path))).map((t) => (
            <button
              key={t.path}
              className={`side-nav-item !w-full min-h-11 ${activePath === t.path ? "active" : ""}`}
              aria-current={activePath === t.path ? "page" : undefined}
              onClick={() => { setMenuOpen(false); navigate(t.path); }}
            >
              {t.icon}
              <span className="lbl">{t.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer md:mt-auto">
          <div className="sidebar-account">
            <div className="sidebar-account-heading"><span>{admin ? "ADMIN GLOBAL" : printOnly ? "PRINT LABEL" : readOnly ? "BEA CUKAI" : "AKUN OPERATOR"}</span><span className="sidebar-warehouse">{admin ? "CAS / BGD" : user.access.toUpperCase()}</span></div>
            {printOnly && user.pjt_code && <span className="sidebar-email">Kode PJT {user.pjt_code}</span>}
            <span className="sidebar-email">{user.email}</span>
            <button type="button" className="sidebar-logout" onClick={onLogout} disabled={logoutPending}>{logoutPending ? "Memuat..." : "Logout"}</button>
            {logoutError && <p role="alert" className="text-sm text-red-400">{logoutError}</p>}
          </div>
          {!printOnly && !admin && <div className="sidebar-status">
            <div className="sidebar-status-row"><span>Koneksi</span><strong className={connected ? "sidebar-connected" : ""}><span className="sidebar-status-dot" />{connected ? "Terhubung" : "Terputus"}</strong></div>
            <div className="sidebar-status-row"><span>Sesi</span><strong>{session?.running ? "Aktif" : "Tidak aktif"}</strong></div>
            <div className="sidebar-session-info">{session?.batch_id ?? "Belum ada sesi"} · {session?.parcel_count ?? 0} paket</div>
          </div>}
          <button
            type="button"
            className="theme-switch"
            role="switch"
            aria-checked={theme === "dark"}
            aria-label="Mode gelap"
            onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}
          >
            <span aria-hidden="true" className="theme-switch-track"><span className="theme-switch-thumb" /></span>
            {theme === "dark" ? "Dark" : "Light"}
          </button>
        </div>
        </div>
      </aside>

      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between px-4 sm:px-7 pt-6">
          <div>
            {awbDetailId ? (
              <button className="btn-ghost" onClick={() => navigate("/awb")}>← Kembali</button>
            ) : (
              <>
                <div className="text-muted text-xs font-bold uppercase tracking-[0.08em]">Xray Cargo</div>
                <h2 className="m-0 text-[21px] font-extrabold tracking-tight">
                  {PAGE_TITLES[activePath] ?? 'Xray Cargo'}
                </h2>
              </>
            )}
          </div>
        </div>
        <main className="p-4 sm:px-7 pb-7 w-full min-w-0">
          {activePath === "/dashboard" && <Suspense fallback={<div className="panel muted">Memuat dashboard…</div>}><Dashboard session={session} onEvent={onEvent} /></Suspense>}
          {!readOnly && !printOnly && !admin && <div hidden={activePath !== "/live"}>
            <LiveTab key={user.access} warehouse={user.access} frames={frames} session={session}
              visible={activePath === "/live"} onSessionChange={refreshSession} onEvent={onEvent}
              connected={connected} waitingEventsAvailable={waitingEventsAvailable} />
          </div>}
          {activePath === "/awb" && !awbDetailId && (
            <AwbTab warehouse={user.access} readOnly={readOnly} onChanged={refreshSession} onOpenDetail={(id) => navigate(`/awb/${id}`)} onEvent={onEvent} connected={connected} />
          )}
          {activePath === "/matrix" && <MatrixTab key={user.access} onEvent={onEvent} connected={connected} />}
          {activePath === "/labels" && <LabelsTab printOnly={printOnly} />}
          {admin && activePath === "/users" && <UsersTab />}
          {activePath === "/settings" && <SettingsTab warehouse={user.access} />}
          {awbDetailId && (
            <AwbDetailPage
              key={user.access}
              id={awbDetailId}
              warehouse={user.access}
              readOnly={readOnly}
              onBack={() => navigate("/awb")}
              onChanged={refreshSession}
            />
          )}
        </main>
      </div>
    </div>
  );
}
