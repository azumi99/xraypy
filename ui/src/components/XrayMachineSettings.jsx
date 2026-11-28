import { useEffect, useState } from "react";

const endpoint = "/api/settings/xray-machine";
const fields = [
  ["flagGambarRpm", "checkbox"],
  ["flagRpmAlarm", "checkbox"],
  ["kodeCaraAngkut", "integer"],
  ["entryLane", "text"],
  ["entrySpeed", "decimal"],
  ["entryIdImage", "text"],
  ["entrySnImage", "text"],
  ["grossWeight", "decimal"],
  ["weightXray", "decimal"],
  ["entryGate", "text"],
  ["entrySpeedView", "text"],
];

const emptyDraft = Object.fromEntries(fields.map(([key, type]) => [key, type === "checkbox" ? false : ""]));

export default function XrayMachineSettings() {
  const [draft, setDraft] = useState(emptyDraft);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(endpoint, { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error();
        setDraft(await response.json());
      } catch {
        if (!controller.signal.aborted) setError("Data sebelumnya gagal dimuat. Kamu bisa mengisi formulir untuk menyimpan data baru.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    load();
    return () => controller.abort();
  }, []);

  async function save(event) {
    event.preventDefault();
    const payload = Object.fromEntries(fields.map(([key, type]) => [
      key, type === "integer" || type === "decimal" ? Number(draft[key]) : draft[key],
    ]));
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const response = await fetch(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error();
      setDraft(await response.json());
      setSaved(true);
    } catch {
      setError("Data belum tersimpan. Periksa isian dan coba lagi.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="dashboard-panel settings-panel" aria-labelledby="xray-machine-title">
      <header className="settings-panel-heading">
        <h3 id="xray-machine-title">Data Mesin X-ray</h3>
      </header>
      <form onSubmit={save}>
        <fieldset className="settings-xray-data" disabled={loading || saving}>
          {fields.map(([key, type]) => {
            const checkbox = type === "checkbox";
            const numeric = type === "integer" || type === "decimal";
            function change(event) {
              const value = checkbox ? event.target.checked : event.target.value;
              setDraft((previous) => ({ ...previous, [key]: value }));
              setSaved(false);
            }
            return (
              <label key={key} className={checkbox ? "flex items-center gap-2 text-xs font-bold" : "text-xs font-bold"}>
                {checkbox ? (
                  <><input type="checkbox" checked={draft[key]} onChange={change} />{key}</>
                ) : (
                  <>
                    {key}
                    <input className="field mt-1" type={numeric ? "number" : "text"}
                      value={draft[key]} onChange={change} required
                      min={numeric ? 0 : undefined} step={type === "decimal" ? "any" : undefined}
                      maxLength={numeric ? undefined : 128} />
                  </>
                )}
              </label>
            );
          })}
        </fieldset>
        <div className="settings-actions">
          <button type="submit" className="btn-clay" disabled={loading || saving}>
            {saving ? "Menyimpan…" : "Simpan"}
          </button>
          {loading && <p role="status">Memuat data mesin X-ray…</p>}
          {saved && <p role="status">Data mesin X-ray tersimpan.</p>}
        </div>
        {error && <p role="alert" className="dashboard-error">{error}</p>}
      </form>
    </section>
  );
}
