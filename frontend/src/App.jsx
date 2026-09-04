import { AnimatePresence, motion, MotionConfig } from "motion/react";
import { useEffect, useState } from "react";
import { getLatestEvaluation } from "./api.js";
import AuditTrail from "./components/AuditTrail.jsx";
import Console from "./components/Console.jsx";
import Nav from "./components/Nav.jsx";
import ReviewQueue from "./components/ReviewQueue.jsx";

export default function App() {
  const [view, setView] = useState("console");
  const [aiBackend, setAiBackend] = useState(null);

  useEffect(() => {
    getLatestEvaluation("holdout")
      .then((r) => setAiBackend(r.semantic_backend))
      .catch(() => {});
  }, []);

  const pages = {
    console: <Console />,
    review: <ReviewQueue />,
    audit: <AuditTrail />,
  };

  return (
    <MotionConfig reducedMotion="user">
      <div className="app">
        {/* First stop for a keyboard user: skip the nav rather than tab
            through it on every view change. */}
        <a className="skip-link" href="#main">Skip to main content</a>
        <Nav active={view} onChange={setView} aiBackend={aiBackend} />
        <AnimatePresence mode="wait">
          <motion.main
            key={view}
            id="main"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
          >
            {pages[view]}
          </motion.main>
        </AnimatePresence>
      </div>
    </MotionConfig>
  );
}
