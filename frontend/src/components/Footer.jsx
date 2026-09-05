import { Link } from "../router.jsx";

/**
 * Deliberately thin, and dark, so the page ends on the same ground it
 * began on and the assurance band above it reads as one continuous close
 * rather than a band with a white slab bolted underneath.
 *
 * There is no company behind this to link to, no press page and no
 * customer list. So it is a mark, four destinations, and the one
 * disclosure the figures above oblige us to make. The product tagline
 * that used to sit here said what the hero already says.
 */
export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="wrap site-footer-inner">
        <div className="site-footer-brand">
          <svg
            className="accord-mark"
            viewBox="0 0 32 32"
            width={24}
            height={24}
            aria-hidden="true"
            focusable="false"
          >
            <g fill="none" strokeLinecap="round" strokeLinejoin="round" strokeWidth="3">
              <path d="M5.6 9.4h6.6l4.2 6.6" stroke="currentColor" />
              <path d="M5.6 22.6h6.6l4.2-6.6" stroke="currentColor" />
              <path d="M5.6 16h10.8" stroke="#00c389" />
              <path d="M21.6 10.6 26.8 16l-5.2 5.4" stroke="currentColor" />
            </g>
          </svg>
          <p className="site-footer-name">Accord</p>
        </div>

        <nav className="site-footer-links" aria-label="Footer">
          <Link to="/app/runs">Workspace</Link>
          <Link to="/app/review">Review</Link>
          <Link to="/app/audit">Audit</Link>
          <Link to="/app/evaluation">Evaluation</Link>
        </nav>
      </div>

      <div className="wrap site-footer-legal">
        <p>Evaluation figures. No customer data.</p>
      </div>
    </footer>
  );
}
