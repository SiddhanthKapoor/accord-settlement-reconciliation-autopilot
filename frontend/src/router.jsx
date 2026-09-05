/**
 * Accord router — real URLs on the History API.
 *
 * The app used to be `useState("runs")`: every tab was the same URL, the
 * browser Back button walked straight out of the site, and a run could not
 * be linked to. Reconciliation work is inherently linkable — "look at
 * record rec_812 in run_abc123" is a sentence a controller says out loud —
 * so the address bar has to mean something.
 *
 * Hand-rolled rather than react-router because the whole surface needed is
 * three exports, and scroll restoration (reset on push, restore on pop) is
 * easier to get exactly right with direct access to the history entry keys
 * than it is to configure around a library's own scroll handling.
 *
 * Exports: `navigate(to, {replace})`, `useRoute()`, `<Link to>`.
 */

import { useCallback, useMemo, useSyncExternalStore } from "react";

/* ------------------------------------------------------------------ table */

/**
 * Matched top to bottom, so anything with a literal segment must sit above
 * the pattern whose parameter would otherwise swallow it: `/app/runs/new`
 * before `/app/runs/:runId`, or "new" becomes a run id.
 *
 * `section` is the animation key for the app shell. Several routes share
 * one section on purpose — drilling from a run into a record is movement
 * *within* the runs section, not a page change. Keying the top-level
 * AnimatePresence on the full path (ids included) is what caused the blank
 * page this repo already fixed once: `mode="wait"` sat waiting on an exit
 * for a subtree that had been replaced underneath it.
 */
export const ROUTES = [
  { id: "landing", pattern: "/" },
  { id: "app.index", pattern: "/app", redirect: "/app/runs" },
  { id: "app.runs.new", pattern: "/app/runs/new", section: "new" },
  { id: "app.record", pattern: "/app/runs/:runId/records/:recordId", section: "runs" },
  { id: "app.run", pattern: "/app/runs/:runId", section: "runs" },
  { id: "app.runs", pattern: "/app/runs", section: "runs" },
  { id: "app.review", pattern: "/app/review", section: "review" },
  { id: "app.audit", pattern: "/app/audit", section: "audit" },
  { id: "app.evaluation", pattern: "/app/evaluation", section: "evaluation" },
];

const segmentsOf = (path) => path.replace(/\/+$/, "").split("/").filter(Boolean);

function matchPattern(pattern, pathname) {
  // "/" is the only pattern with no segments, so it needs the explicit case.
  if (pattern === "/") return pathname === "/" || pathname === "" ? {} : null;
  const pat = segmentsOf(pattern);
  const seg = segmentsOf(pathname);
  if (pat.length !== seg.length) return null;
  const params = {};
  for (let i = 0; i < pat.length; i += 1) {
    if (pat[i].startsWith(":")) {
      if (!seg[i]) return null;
      params[pat[i].slice(1)] = decodeURIComponent(seg[i]);
    } else if (pat[i] !== seg[i]) {
      return null;
    }
  }
  return params;
}

/** Resolve a pathname to a route descriptor. Never throws; unknown => 404. */
export function matchRoute(pathname) {
  for (const route of ROUTES) {
    const params = matchPattern(route.pattern, pathname);
    if (params) return { ...route, params };
  }
  return { id: "notfound", pattern: null, section: "notfound", params: {} };
}

/* ------------------------------------------------- history entry bookkeeping */

const isBrowser = typeof window !== "undefined";

// Each history entry carries an opaque key so a scroll offset can be parked
// against it and found again when the user comes back to that exact entry.
let keySeq = 0;
const nextKey = () => `k${Date.now().toString(36)}${(keySeq += 1)}`;
const scrollOffsets = new Map();

function currentKey() {
  return (isBrowser && window.history.state && window.history.state.__accordKey) || "root";
}

if (isBrowser) {
  // The browser's own restoration fights ours: it fires after paint with a
  // position measured against the *previous* document height.
  if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
  if (!window.history.state || !window.history.state.__accordKey) {
    window.history.replaceState({ ...window.history.state, __accordKey: nextKey() }, "");
  }

  // Record continuously rather than at navigation time: by the moment
  // `popstate` fires the scroll offset of the page being left is already
  // gone in some browsers, so it has to have been captured beforehand.
  let ticking = false;
  window.addEventListener(
    "scroll",
    () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(() => {
        ticking = false;
        scrollOffsets.set(currentKey(), window.scrollY);
      });
    },
    { passive: true }
  );
}

/**
 * Applying the offset has to wait for the new view to have laid out, or the
 * document is still the old height and the scroll clamps to the bottom of
 * whatever was there before. Two frames: one for React's commit, one for
 * layout.
 */
function applyScroll(y) {
  if (!isBrowser) return;
  if (y === 0) {
    window.scrollTo(0, 0); // top always exists; no need to wait for layout
    return;
  }
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => window.scrollTo(0, y));
  });
}

/* ---------------------------------------------------------------- the store */

const listeners = new Set();

function readHref() {
  if (!isBrowser) return "/";
  return window.location.pathname + window.location.search;
}

let href = readHref();

function emit() {
  const next = readHref();
  if (next === href) return;
  href = next;
  listeners.forEach((l) => l());
}

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// useSyncExternalStore compares snapshots by identity, so this must return
// the same string instance until the location actually changes.
const getSnapshot = () => href;
const getServerSnapshot = () => "/";

if (isBrowser) {
  window.addEventListener("popstate", () => {
    // Back/forward: put the reader back where they were on this entry.
    const target = scrollOffsets.get(currentKey()) ?? 0;
    emit();
    applyScroll(target);
  });
}

/**
 * Navigate to `to`, a root-relative path such as "/app/runs/run_abc123".
 * `replace: true` swaps the current entry instead of adding one — used for
 * redirects, so Back does not bounce off the redirect and get stuck.
 */
export function navigate(to, { replace = false } = {}) {
  if (!isBrowser) return;
  const url = String(to);
  const current = readHref();
  if (url === current && !replace) return; // no dead entries on the stack

  // Park the outgoing page's offset before the entry changes hands.
  scrollOffsets.set(currentKey(), window.scrollY);

  const state = { __accordKey: nextKey() };
  if (replace) window.history.replaceState(state, "", url);
  else window.history.pushState(state, "", url);

  emit();
  applyScroll(0); // a fresh destination starts at the top
}

/** `{ id, section, path, params, query }` for the current URL. */
export function useRoute() {
  const location = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return useMemo(() => {
    const [path, search = ""] = location.split("?");
    const route = matchRoute(path);
    return {
      id: route.id,
      section: route.section,
      redirect: route.redirect,
      path,
      params: route.params,
      query: Object.fromEntries(new URLSearchParams(search)),
    };
  }, [location]);
}

/**
 * A real anchor, so middle-click and cmd-click open a tab, the status bar
 * shows the destination, and a screen reader announces "link" rather than
 * "button". Only a plain left-click is intercepted.
 */
export function Link({ to, children, onClick, replace = false, ...rest }) {
  const handleClick = useCallback(
    (event) => {
      if (onClick) onClick(event);
      if (event.defaultPrevented) return;
      // Anything but an unmodified primary click is the browser's to handle:
      // new tab, new window, download, "open in background".
      if (event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (rest.target && rest.target !== "_self") return;
      event.preventDefault();
      navigate(to, { replace });
    },
    [to, onClick, replace, rest.target]
  );

  return (
    <a href={to} onClick={handleClick} {...rest}>
      {children}
    </a>
  );
}
