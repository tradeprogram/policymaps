// 해시 기반 라우터 (빌드도구 없이 동작해야 하므로 history API 대신 hash)

const routes = [];
let notFound = null;
let onNavigate = null;

export function route(pattern, handler) {
  // "/region/:sig" -> 정규식
  const names = [];
  const rx = new RegExp(
    "^" + pattern.replace(/:[A-Za-z0-9_]+/g, (m) => { names.push(m.slice(1)); return "([^/]+)"; }) + "$"
  );
  routes.push({ rx, names, handler, pattern });
}

export function setNotFound(fn) { notFound = fn; }
export function setOnNavigate(fn) { onNavigate = fn; }

export function currentPath() {
  const h = location.hash.replace(/^#/, "");
  return h && h.startsWith("/") ? h : "/dashboard";
}

export function go(path) {
  if (location.hash.replace(/^#/, "") === path) resolve();
  else location.hash = path;
}

export function resolve() {
  const path = currentPath();
  const [p, queryStr] = path.split("?");
  const query = Object.fromEntries(new URLSearchParams(queryStr || ""));
  for (const r of routes) {
    const m = r.rx.exec(p);
    if (m) {
      const params = {};
      r.names.forEach((n, i) => { params[n] = decodeURIComponent(m[i + 1]); });
      if (onNavigate) onNavigate(r.pattern, params, query);
      return r.handler(params, query);
    }
  }
  if (notFound) notFound(p);
}

export function start() {
  window.addEventListener("hashchange", resolve);
  resolve();
}
