import { AnimatePresence, motion, MotionConfig } from "motion/react";
import { useEffect, useState } from "react";
import { getStats } from "./api.js";
import ArchitectureView from "./components/ArchitectureView.jsx";
import AuditTrail from "./components/AuditTrail.jsx";
import Nav from "./components/Nav.jsx";
import Overview from "./components/Overview.jsx";
import ScenariosView from "./components/ScenariosView.jsx";

export default function App() {
  const [view, setView] = useState("overview");
  const [stats, setStats] = useState(null);

  useEffect(() => {
    const load = () => getStats().then(setStats).catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const pages = {
    overview: <Overview onNavigateScenarios={() => setView("scenarios")} />,
    scenarios: <ScenariosView />,
    audit: <AuditTrail />,
    architecture: <ArchitectureView />,
  };

  return (
    <MotionConfig reducedMotion="user">
      <div className="app">
        <Nav active={view} onChange={setView} stats={stats} />
        <AnimatePresence mode="wait">
          <motion.main
            key={view}
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
