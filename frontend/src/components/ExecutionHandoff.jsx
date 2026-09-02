export default function ExecutionHandoff({ execution }) {
  if (!execution) return null;
  const simulated = execution.simulated;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-title">Razorpay execution</div>
      <div className="handoff-steps">
        <div className="handoff-step handoff-step-done">
          <div className="handoff-step-icon">✓</div>
          Interlock decision: ALLOWED — integrity verified
        </div>
        <div className="handoff-step handoff-step-done">
          <div className="handoff-step-icon">✓</div>
          {simulated ? "Razorpay test credentials not configured — execution simulated" : "Razorpay test mode — Payment Link created"}
        </div>
        {!simulated && execution.razorpay?.short_url && (
          <div className="handoff-step handoff-step-done">
            <div className="handoff-step-icon">✓</div>
            <span className="handoff-link">{execution.razorpay.short_url}</span>
          </div>
        )}
        {simulated && (
          <div className="handoff-step">
            <div className="handoff-step-icon">i</div>
            <span className="muted small">
              Add RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET to backend/.env for a real test-mode payment link — everything up to execution is already real.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
