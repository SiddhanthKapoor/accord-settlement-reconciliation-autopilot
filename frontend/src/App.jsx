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

  return (
    <div className="app">
      <Nav active={view} onChange={setView} stats={stats} />
      {view === "overview" && <Overview onNavigateScenarios={() => setView("scenarios")} />}
      {view === "scenarios" && <ScenariosView />}
      {view === "audit" && <AuditTrail />}
      {view === "architecture" && <ArchitectureView />}
    </div>
  );
}
