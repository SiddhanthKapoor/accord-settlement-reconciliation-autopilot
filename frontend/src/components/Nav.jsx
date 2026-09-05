import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { Link, useRoute } from "../router.jsx";
import { lift } from "../motion-landing.js";

/**
 * The app shell's persistent nav.
 *
 * Tab order follows how the work actually runs rather than how the code is
 * organised. An operator does not open this tool to create something; they
 * open it to see what is outstanding. So the sections read as a pipeline —
 * the workspaces in flight, then the exceptions waiting on a human, then
 * the evidence, then the assurance the engine itself is behaving — and
 * "New run" sits apart as a primary action, because starting a run is a
 * thing you do, not a place you go.
 */
const TABS = [
  { to: "/app/runs", section: "runs", label: "Runs" },
  { to: "/app/review", section: "review", label: "Review Queue" },
  { to: "/app/audit", section: "audit", label: "Audit Trail" },
  { to: "/app/evaluation", section: "evaluation", label: "Evaluation" },
];

/* ------------------------------------------------------------- AI status */

const AI_STATES = {
  AI_AVAILABLE: { label: "AI available", tone: "ok" },
  AI_FALLBACK_ACTIVE: { label: "AI fallback active", tone: "warn" },
  AI_UNAVAILABLE: { label: "AI unavailable", tone: "off" },
};

/** Distinct shapes, not just distinct colours — status must survive greyscale. */
function StatusShape({ tone }) {
  if (tone === "ok") {
    return (
      <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true" focusable="false">
        <circle cx="6" cy="6" r="5" fill="currentColor" />
      </svg>
    );
  }
  if (tone === "warn") {
    // Half-filled: running, but not on the primary path.
    return (
      <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true" focusable="false">
        <circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" strokeWidth="1.6" />
        <path d="M6 1a5 5 0 0 1 0 10z" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true" focusable="false">
      <circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M3 9 9 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Live provider status, or nothing at all.
 *
 * The endpoint is owned by another part of the system and may legitimately
 * not exist yet. An unreachable health endpoint tells us nothing about the
 * provider, so the chip hides. It must never fall back to claiming the AI
 * is available — a reassuring status we cannot substantiate is worse than
 * no status.
 *
 * Polled once on mount and then slowly; there is nothing here worth a
 * request every few seconds, and none at all while the tab is hidden.
 */
function useAiStatus() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const read = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const res = await fetch("/api/ai/health", { signal: controller.signal });
        if (!res.ok) throw new Error(String(res.status));
        const body = await res.json();
        const raw = body?.status ?? body?.ai_status ?? (typeof body === "string" ? body : null);
        // Only render a state we recognise. An unexpected shape is a
        // reason to say nothing, not a reason to guess.
        if (!cancelled) setStatus(raw && AI_STATES[raw] ? raw : null);
      } catch {
        if (!cancelled) setStatus(null);
      }
    };

    read();
    const timer = setInterval(read, 120000);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
    };
  }, []);

  return status;
}

function AiChip() {
  const status = useAiStatus();
  if (!status) return null;
  const { label, tone } = AI_STATES[status];
  return (
    <span className={`ai-chip ai-chip-${tone}`} role="status">
      <StatusShape tone={tone} />
      <span>{label}</span>
    </span>
  );
}

/* ------------------------------------------------------------------- nav */

export default function Nav() {
  const route = useRoute();
  const active = route.section;

  return (
    <header className="topnav">
      <div className="topnav-left">
        <Link to="/" className="brand-mark" aria-label="Accord, home">
          <img
            src="/brand/accord-logo-512.png"
            alt=""
            aria-hidden="true"
            width={26}
            height={26}
            className="accord-logo-img brand-logo"
            decoding="async"
          />
          <span className="brand-name">Accord</span>
        </Link>
      </div>

      <nav className="tabs" aria-label="Sections">
        {TABS.map((t) => {
          const current = active === t.section;
          return (
            <Link
              key={t.section}
              to={t.to}
              className={"tab" + (current ? " tab-active" : "")}
              aria-current={current ? "page" : undefined}
            >
              {current && (
                <motion.span
                  className="tab-indicator"
                  layoutId="accord-tab-indicator"
                  transition={{ duration: 0.22, ease: [0.22, 0.61, 0.36, 1] }}
                />
              )}
              <span className="tab-label">{t.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="topnav-right">
        <AiChip />
        <motion.span {...lift} style={{ display: "inline-flex" }}>
          <Link
            to="/app/runs/new"
            className={"btn-primary btn-sm" + (active === "new" ? " is-current" : "")}
            aria-current={active === "new" ? "page" : undefined}
          >
            New run
          </Link>
        </motion.span>
      </div>
    </header>
  );
}
