import { useEffect, useState } from "react";
import api from "../api";

const EMPTY_USER = { email: "", access: "cas", role: "operator", active: true, password: "", pjt_code: "" };
const ROLE_LABELS = { admin: "Admin global", operator: "Operator", bc: "Bea Cukai", label_print: "Print Label" };

export default function UsersTab() {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = async () => {
    setLoading(true);
    setError("");
    try { setUsers(await api("/users", { cache: "no-store" })); }
    catch { setError("Daftar akun gagal dimuat."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  const open = (user) => {
    setEditing(Boolean(user));
    setDraft(user ? { ...user, password: "" } : EMPTY_USER);
    setError("");
    setNotice("");
  };
  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    const { email, role, access, active, password } = draft
    const pjt_code = role === "label_print" ? draft.pjt_code : undefined;;
    try {
      const response = await fetch(editing ? `/api/users/${encodeURIComponent(email)}` : "/api/users", {
        method: editing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editing
          ? { role, access, active, password: password || null, pjt_code }
          : { email, role, access, password, pjt_code }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Periksa kembali data akun.");
      setUsers((previous) => [...previous.filter((user) => user.email !== data.email), data]
        .sort((a, b) => a.email.localeCompare(b.email)));
      setNotice(editing ? "Perubahan akun tersimpan." : "Akun berhasil dibuat.");
      setDraft(null);
    } catch (err) { setError(err.message || "Akun gagal disimpan."); }
    finally { setBusy(false); }
  };

  return <div className="data-workspace grid min-w-0 gap-3">
    <section className="panel">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className="btn-clay" disabled={busy} onClick={() => open()}>Tambah akun</button>
        {notice && <p role="status" className="text-ok">{notice}</p>}
      </div>
      {error && <p role="alert" className="text-bad mt-3">{error}{" "}
        {!draft && <button type="button" className="btn-ghost" onClick={load}>Coba lagi</button>}
      </p>}
      {draft && <form className="grid gap-4 mt-4" onSubmit={save}>
        <h3>{editing ? "Edit akun" : "Tambah akun"}</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label>Email<input className="field mt-1" type="email" value={draft.email} required
            maxLength={254} readOnly={editing} onChange={(event) => setDraft({ ...draft, email: event.target.value })} /></label>
          <label>{editing ? "Password baru (opsional)" : "Password (minimal 12 karakter)"}
            <input className="field mt-1" type="password" autoComplete="new-password" value={draft.password}
              required={!editing} minLength={12} maxLength={128}
              onChange={(event) => setDraft({ ...draft, password: event.target.value })} /></label>
          <label>Warehouse<select className="field mt-1" value={draft.access}
            onChange={(event) => setDraft({ ...draft, access: event.target.value })}>
            <option value="cas">CAS</option><option value="bgd">BGD</option>
          </select></label>
          <label>Role<select className="field mt-1" value={draft.role}
            onChange={(event) => setDraft({ ...draft, role: event.target.value })}>
            {["operator", "bc", "label_print"].map((role) => <option key={role} value={role}>{ROLE_LABELS[role]}</option>)}
          </select></label>
          {draft.role === "label_print" && <label>Kode PJT<input className="field mt-1"
            value={draft.pjt_code ?? ""} required maxLength={40}
            onChange={(event) => setDraft({ ...draft, pjt_code: event.target.value.toUpperCase() })} /></label>}
        </div>
        {editing && <label className="flex items-center gap-2"><input type="checkbox" checked={draft.active}
          onChange={(event) => setDraft({ ...draft, active: event.target.checked })} /> Akun aktif</label>}
        <div className="flex flex-wrap gap-3">
          <button className="btn-clay" disabled={busy} type="submit">{busy ? "Menyimpan…" : "Simpan"}</button>
          <button className="btn-ghost" disabled={busy} type="button" onClick={() => { setError(""); setDraft(null); }}>Batal</button>
        </div>
      </form>}
    </section>
    <section className="panel">
      <div className="awb-table-scroll" role="region" aria-label="Daftar akun" tabIndex={0}>
        <table className="awb-data-table w-full">
          <thead><tr><th scope="col">Email</th><th scope="col">Warehouse</th><th scope="col">Role</th>
            <th scope="col">PJT</th><th scope="col">Status</th><th scope="col">Aksi</th></tr></thead>
          <tbody>
            {users.map((user) => <tr key={user.email}>
              <td>{user.email}</td><td>{user.role === "admin" ? "CAS dan BGD" : user.access.toUpperCase()}</td>
              <td>{ROLE_LABELS[user.role]}</td><td>{user.pjt_code || "—"}</td><td>{user.active ? "Aktif" : "Nonaktif"}</td>
              <td>{user.role !== "admin" && <button type="button" className="btn-ghost" disabled={busy}
                onClick={() => open(user)}>Edit</button>}</td>
            </tr>)}
            {!users.length && <tr><td colSpan={6}>{loading ? "Memuat akun…" : "Tidak ada akun untuk ditampilkan."}</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  </div>;
}
