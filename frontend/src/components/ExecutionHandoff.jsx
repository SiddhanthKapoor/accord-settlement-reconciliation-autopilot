// Integrity verification (ALLOW) and payment execution are reported as
// two separate facts. A downstream execution failure is shown as an
// execution failure — never folded back into the integrity decision, and
// never displayed as if a payment succeeded.
export default function ExecutionHandoff({ execution, executionError }) {
  if (!execution && !executionError) return null;

  if (executionError) {
    return (
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-title">Razorpay execution</div>
        <div className="handoff-steps">
          <div className="handoff-step handoff-step-done">
            <div className="handoff-step-icon">✓</div>
            Integrity checks passed — decision: ALLOW
          </div>
          <div className="handoff-step handoff-step-failed">
            <div className="handoff-step-icon">✕</div>
            Payment execution failed
          </div>
        </div>
        <details className="disclosure" style={{ marginTop: 10 }}>
          <summary>Technical detail →</summary>
          <p className="small muted mono" style={{ marginTop: 8 }}>{executionError}</p>
        </details>
      </div>
    );
  }

  const simulated = execution.simulated;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-title">Razorpay execution</div>
      <div className="handoff-steps">
        <div className="handoff-step handoff-step-done">
          <div className="handoff-step-icon">✓</div>
          Integrity checks passed — decision: ALLOW
        </div>
        <div className="handoff-step handoff-step-done">
          <div className="handoff-step-icon">✓</div>
          {simulated ? "Execution simulated — Razorpay test credentials not configured" : "Razorpay Payment Link created"}
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
              Configure Razorpay test-mode credentials to generate a real payment link — everything up to execution is already real.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
