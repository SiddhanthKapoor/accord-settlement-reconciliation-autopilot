import { AnimatePresence, motion, MotionConfig } from "motion/react";
import { useEffect } from "react";
import { Link, navigate, useRoute } from "./router.jsx";
import { shellTransition } from "./motion-landing.js";
import AuditTrail from "./components/AuditTrail.jsx";
import Console from "./components/Console.jsx";
import Landing from "./components/Landing.jsx";
import Nav from "./components/Nav.jsx";
import NewRun from "./components/NewRun.jsx";
import ReviewQueue from "./components/ReviewQueue.jsx";
import RunDetail from "./components/RunDetail.jsx";
import Runs from "./components/Runs.jsx";

/* ------------------------------------------------------------------- 404 */

function NotFound() {
  return (
    <div className="notfound">
      <main className="notfound-inner" id="main">
        <img
          src="/brand/accord-logo-512.png"
          alt=""
          aria-hidden="true"
          width={44}
          height={44}
          className="accord-logo-img"
          decoding="async"
        />
        <p className="notfound-code">404</p>
        <h1 className="notfound-heading">That page isn&rsquo;t here.</h1>
        <p className="notfound-text">
          The address may be mistyped, or the run it pointed at may have been reset.
          Nothing has gone wrong with your data.
        </p>
        <div className="notfound-actions">
          <Link to="/app/runs" className="btn-primary btn-lg">
            Go to the workspace
          </Link>
          <Link to="/" className="btn-quiet btn-lg">
            Back to the home page
          </Link>
        </div>
      </main>
    </div>
  );
}

/* ----------------------------------------------------------------- shell */

/**
 * A run detail deep-linked at the record level lands on the run and hands
 * the record id down, so `/app/runs/run_x/records/rec_y` opens something
 * meaningful rather than the bare run.
 *
 * `onBack` / `onRunStarted` are the current component signatures; the
 * contract signatures are `RunDetail({runId})` and `NewRun({onCreated})`.
 * Both are supplied while those components are being rewritten, so the
 * shell works either way and nothing has to land in lockstep.
 */
function sectionView(route) {
  const { id, params } = route;
  const openRun = (runId) => navigate(`/app/runs/${encodeURIComponent(runId)}`);

  switch (id) {
    case "app.runs":
      return <Runs />;
    case "app.runs.new":
      return <NewRun onCreated={openRun} onRunStarted={openRun} />;
    case "app.run":
      return <RunDetail runId={params.runId} onBack={() => navigate("/app/runs")} />;
    case "app.record":
      return (
        <RunDetail
          runId={params.runId}
          recordId={params.recordId}
          onBack={() => navigate(`/app/runs/${encodeURIComponent(params.runId)}`)}
        />
      );
    case "app.review":
      return <ReviewQueue />;
    case "app.audit":
      return <AuditTrail />;
    case "app.evaluation":
      return <Console />;
    default:
      return null;
  }
}

function AppShell({ route }) {
  return (
    <div className="app">
      {/* First stop for a keyboard user: skip the nav rather than tab
          through it on every view change. */}
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <Nav />
      {/* Keyed on the SECTION, never the full path. Keying on the path put
          run ids and record ids into the transition key, and mode="wait"
          then stalled mid-exit on a subtree that had already been replaced
          underneath it — a permanent blank page. Moving within a section
          reconciles normally instead of remounting. */}
      <AnimatePresence mode="wait">
        <motion.main key={route.section} id="main" {...shellTransition}>
          {sectionView(route)}
        </motion.main>
      </AnimatePresence>
    </div>
  );
}

/* ------------------------------------------------------------------- app */

export default function App() {
  const route = useRoute();
  const redirect = route.redirect;

  useEffect(() => {
    // `replace` so Back skips the redirect rather than bouncing off it.
    if (redirect) navigate(redirect, { replace: true });
  }, [redirect, route.path]);

  let content = null;
  if (redirect) content = null;
  else if (route.id === "landing") content = <Landing />;
  else if (route.id === "notfound") content = <NotFound />;
  else content = <AppShell route={route} />;

  return <MotionConfig reducedMotion="user">{content}</MotionConfig>;
}
