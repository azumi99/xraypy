const BASE = "/api";

const api = (path, opt) =>
  fetch(BASE + path, opt).then((r) => {
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  });

export const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });

export const del = (path) => api(path, { method: "DELETE" });

export default api;
