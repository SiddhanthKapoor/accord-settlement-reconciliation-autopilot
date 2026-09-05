import { Link } from "../router.jsx";

/**
 * Deliberately thin. There is no company behind this to link to, no press
 * page and no customer list, so the footer says what is true: what the
 * product is, where to go next, and where the evaluation evidence lives.
 */
export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="wrap site-footer-inner">
        <div className="site-footer-brand">
          <img
            src="/brand/accord-logo-512.png"
            alt=""
            aria-hidden="true"
            width={24}
            height={24}
            className="accord-logo-img"
            decoding="async"
          />
          <div>
            <p className="site-footer-name">Accord</p>
            <p className="site-footer-tag">AI that explains why the books don&rsquo;t close.</p>
          </div>
        </div>

        <nav className="site-footer-links" aria-label="Footer">
          <Link to="/app/runs">Workspace</Link>
          <Link to="/app/review">Review queue</Link>
          <Link to="/app/audit">Audit trail</Link>
          <Link to="/app/evaluation">Evaluation</Link>
        </nav>
      </div>

      <div className="wrap site-footer-legal">
        <p>
          Figures quoted on this page were measured on a synthetic held-out dataset
          generated for this project, not on production data.
        </p>
      </div>
    </footer>
  );
}
