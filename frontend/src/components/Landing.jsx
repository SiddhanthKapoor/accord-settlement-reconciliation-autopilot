import { motion } from "motion/react";
import { Link } from "../router.jsx";
import {
  converge,
  heroStage,
  lift,
  revealGroup,
  revealItem,
  riseUp,
  streamLeft,
  streamRight,
  useReveal,
  useScrolled,
} from "../motion-landing.js";
import Footer from "./Footer.jsx";

/**
 * The public face of Accord.
 *
 * Structured the way the serious end of this category structures itself —
 * state the job, show the loop, then spend most of the page on the control
 * story, because in finance software the control story *is* the product.
 *
 * Every number on this page is one measured in this repository and is
 * labelled with what it was measured on. There are no customer logos, no
 * testimonials and no money-saved figures, because we do not have any.
 */

/* --------------------------------------------------------------- content */

const STEPS = [
  {
    n: "01",
    title: "Bring in every file",
    body:
      "Settlement reports, bank statements, the order export, the ledger. CSV or XLSX, several at once, in whatever shape your provider hands them over.",
  },
  {
    n: "02",
    title: "Accord works out what each file is",
    body:
      "It classifies the source, identifies the provider, reads the date and amount ranges, and proposes how the columns map. You confirm or correct the map before anything runs.",
  },
  {
    n: "03",
    title: "Reconcile deterministically",
    body:
      "Identifiers, amounts, currency, settlement timing, fees and tax. This tier is arithmetic and rules. It is the tier that decides the overwhelming majority of records, and it does not involve a model.",
  },
  {
    n: "04",
    title: "Investigate what did not close",
    body:
      "For the residue, Accord traces the money order to payment to settlement to bank to books, names the stage where the trail breaks, and asks the model one narrow question about the ambiguity that remains.",
  },
  {
    n: "05",
    title: "A person decides the rest",
    body:
      "Whatever cannot be confirmed arrives in a review queue with the trace, the confirmed evidence, the competing hypotheses, and an explicit list of what is still unknown.",
  },
  {
    n: "06",
    title: "Everything lands in the ledger",
    body:
      "Inputs, rule versions, model involvement, and every human approval are appended to a hash-chained audit log that can be verified end to end.",
  },
];

const SAFETY = [
  {
    title: "Deterministic math decides first",
    body:
      "Amounts, currencies, identifiers and dates are settled by arithmetic before a model is consulted at all. On the held-out evaluation the model was invoked on 20.4% of records; the rest never left the deterministic tier.",
  },
  {
    title: "The model answers one narrow question",
    body:
      "It is never asked “is this reconciled?”. It is asked whether two specific references describe the same transaction, given a shortlist of candidates the deterministic tier has already qualified.",
  },
  {
    title: "AI can never book money",
    body:
      "Policy holds the pen. A model answer cannot override an amount mismatch, a currency mismatch, contradictory identifiers, or the confidence threshold. It can only break a tie the rules have already declared admissible.",
  },
  {
    title: "It degrades to a person, not to a guess",
    body:
      "When the provider is unreachable the semantic tier goes dark and the affected records fall through to human review. They do not fall through to a match.",
  },
  {
    title: "The audit trail is hash-chained",
    body:
      "Each entry commits to the one before it, so the log can be verified rather than trusted. Tampering with a past decision invalidates every entry after it.",
  },
  {
    title: "Nothing is claimed that was not measured",
    body:
      "The evaluation set, the seed, the frozen reports and the code commit they were produced on are all in the repository, including the run where the model provider failed completely.",
  },
];

const EVIDENCE = [
  {
    figure: "0",
    unit: "false auto-reconciliations",
    body:
      "In every configuration measured on the held-out split, no record was auto-reconciled whose ground truth was not reconciled — including the run in which the model failed on every single call.",
  },
  {
    figure: "204 / 204",
    unit: "model calls failed in the outage drill",
    body:
      "A total outage of the one external dependency. Exact-reference, fee-and-tax and currency-mismatch categories were unaffected; the semantic-dependent categories moved to human review rather than to a wrong match.",
  },
  {
    figure: "20.4%",
    unit: "of records reached the model",
    body:
      "The remaining 79.6% were decided by rules and arithmetic alone. The model is a narrow instrument applied to a narrow residue, not the engine.",
  },
];

/* ------------------------------------------------------------ components */

function Wordmark({ size = 26 }) {
  return (
    <span className="accord-wordmark">
      <img
        src="/brand/accord-logo-512.png"
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
        className="accord-logo-img"
        style={{ width: size, height: size }}
        decoding="async"
      />
      <span className="accord-wordmark-text">Accord</span>
    </span>
  );
}

function LandingNav() {
  const scrolled = useScrolled(10);
  return (
    <header className={"landing-nav" + (scrolled ? " landing-nav-scrolled" : "")}>
      <div className="landing-nav-inner">
        <Link to="/" className="landing-brand" aria-label="Accord, home">
          <Wordmark size={28} />
        </Link>
        <nav className="landing-nav-links" aria-label="Page sections">
          <a href="#how-it-works">How it works</a>
          <a href="#safety">Control model</a>
          <a href="#evidence">Evidence</a>
        </nav>
        <motion.span className="landing-nav-cta-wrap" {...lift}>
          <Link to="/app/runs" className="btn-primary">
            Open the workspace
          </Link>
        </motion.span>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section className="hero" aria-labelledby="hero-heading">
      {/* Decorative only. The section carries its own dark ground colour, so
          the hero reads correctly at AA even if this never loads. */}
      <img
        className="hero-image"
        src="/brand/accord-hero.jpg"
        alt=""
        aria-hidden="true"
        width={1600}
        height={900}
        decoding="async"
        fetchpriority="high"
      />
      <div className="hero-scrim" aria-hidden="true" />

      <motion.div
        className="hero-inner"
        variants={heroStage}
        initial="hidden"
        animate="show"
      >
        <motion.p className="hero-eyebrow" variants={riseUp}>
          Multi-source reconciliation
        </motion.p>

        <motion.h1 className="hero-heading" id="hero-heading" variants={streamLeft}>
          AI that explains why the books don&rsquo;t close.
        </motion.h1>

        <motion.div className="hero-rule" variants={converge} aria-hidden="true" />

        <motion.p className="hero-sub" variants={streamRight}>
          Accord reconciles payments, settlements, bank statements and your ledger,
          then investigates each record that did not close &mdash; tracing the money
          until it can name the stage where the trail breaks, and saying plainly what
          it could not confirm.
        </motion.p>

        <motion.div className="hero-actions" variants={riseUp}>
          <motion.span {...lift}>
            <Link to="/app/runs" className="btn-primary btn-lg">
              Open the workspace
            </Link>
          </motion.span>
          <a href="#how-it-works" className="btn-quiet btn-lg">
            See how it works
          </a>
        </motion.div>

        <motion.p className="hero-foot" variants={riseUp}>
          Deterministic math decides first. A model is consulted only where the
          ambiguity is genuinely semantic, and it can never book money.
        </motion.p>
      </motion.div>
    </section>
  );
}

function Premise() {
  const [ref, inView] = useReveal(0.25);
  return (
    <section className="section section-premise" ref={ref} aria-labelledby="premise-heading">
      <motion.div
        className="wrap wrap-narrow"
        variants={revealGroup}
        initial="hidden"
        animate={inView ? "show" : "hidden"}
      >
        <motion.h2 className="section-heading" id="premise-heading" variants={revealItem}>
          The hard part was never the matching.
        </motion.h2>
        <motion.p className="section-lede" variants={revealItem}>
          Most reconciliation tools stop at a number: 8,400 matched, 600 unmatched.
          The 600 are the entire job, and they arrive as a spreadsheet with no
          explanation attached. Somebody then spends three days working out that one
          batch settled late, one reference was reformatted by an acquirer, and one
          amount is genuinely short.
        </motion.p>
        <motion.p className="section-lede" variants={revealItem}>
          Accord is built for that residue. It treats an unmatched record as something
          to be investigated and explained, not as a row to be handed over.
        </motion.p>
      </motion.div>
    </section>
  );
}

function HowItWorks() {
  const [ref, inView] = useReveal(0.1);
  return (
    <section className="section section-alt" id="how-it-works" ref={ref} aria-labelledby="how-heading">
      <div className="wrap">
        <motion.div
          variants={revealGroup}
          initial="hidden"
          animate={inView ? "show" : "hidden"}
        >
          <motion.p className="section-eyebrow" variants={revealItem}>
            The loop
          </motion.p>
          <motion.h2 className="section-heading" id="how-heading" variants={revealItem}>
            Six steps, and a person owns the last decision.
          </motion.h2>

          <ol className="steps">
            {STEPS.map((s) => (
              <motion.li className="step" key={s.n} variants={revealItem}>
                <span className="step-n" aria-hidden="true">
                  {s.n}
                </span>
                <div className="step-body">
                  <h3 className="step-title">{s.title}</h3>
                  <p className="step-text">{s.body}</p>
                </div>
              </motion.li>
            ))}
          </ol>
        </motion.div>
      </div>
    </section>
  );
}

function Safety() {
  const [ref, inView] = useReveal(0.08);
  return (
    <section className="section" id="safety" ref={ref} aria-labelledby="safety-heading">
      <div className="wrap">
        <motion.div
          variants={revealGroup}
          initial="hidden"
          animate={inView ? "show" : "hidden"}
        >
          <motion.p className="section-eyebrow" variants={revealItem}>
            Control model
          </motion.p>
          <motion.h2 className="section-heading" id="safety-heading" variants={revealItem}>
            The constraint is the feature.
          </motion.h2>
          <motion.p className="section-lede section-lede-tight" variants={revealItem}>
            A reconciliation system that is confidently wrong is worse than one that is
            slow. Accord is built so that the model cannot be the reason a number moves.
          </motion.p>

          <div className="safety-grid">
            {SAFETY.map((s) => (
              <motion.article className="safety-card" key={s.title} variants={revealItem}>
                <h3 className="safety-title">{s.title}</h3>
                <p className="safety-text">{s.body}</p>
              </motion.article>
            ))}
          </div>
        </motion.div>
      </div>
    </section>
  );
}

function Evidence() {
  const [ref, inView] = useReveal(0.1);
  return (
    <section className="section section-alt" id="evidence" ref={ref} aria-labelledby="evidence-heading">
      <div className="wrap">
        <motion.div
          variants={revealGroup}
          initial="hidden"
          animate={inView ? "show" : "hidden"}
        >
          <motion.p className="section-eyebrow" variants={revealItem}>
            Evidence
          </motion.p>
          <motion.h2 className="section-heading" id="evidence-heading" variants={revealItem}>
            What we actually measured.
          </motion.h2>

          <div className="evidence-grid">
            {EVIDENCE.map((e) => (
              <motion.article className="evidence-card" key={e.unit} variants={revealItem}>
                <p className="evidence-figure">{e.figure}</p>
                <p className="evidence-unit">{e.unit}</p>
                <p className="evidence-text">{e.body}</p>
              </motion.article>
            ))}
          </div>

          <motion.div className="provenance-note" variants={revealItem}>
            <p className="provenance-note-tag">Provenance</p>
            <p className="provenance-note-text">
              Measured on a 1,000-record held-out split of a{" "}
              <strong>synthetic dataset generated for this project</strong> (seed 90210,
              19 labelled failure categories). This is not customer data and these are
              not production figures. The frozen reports, the dataset checksums and the
              code commit they were produced on are in{" "}
              <code className="mono">backend/evaluations/final/</code>, including the run
              where the model provider failed on every call. The full evaluation console
              ships inside the product.
            </p>
          </motion.div>
        </motion.div>
      </div>
    </section>
  );
}

function ClosingBand() {
  const [ref, inView] = useReveal(0.3);
  return (
    <section className="closing-band" ref={ref} aria-labelledby="closing-heading">
      <motion.div
        className="wrap wrap-narrow closing-inner"
        variants={revealGroup}
        initial="hidden"
        animate={inView ? "show" : "hidden"}
      >
        <motion.h2 className="closing-heading" id="closing-heading" variants={revealItem}>
          Open a workspace and load a file.
        </motion.h2>
        <motion.p className="closing-text" variants={revealItem}>
          Bring a settlement report and an order export. Accord will tell you what it
          could reconcile, what it could not, and exactly why.
        </motion.p>
        <motion.div variants={revealItem}>
          <motion.span {...lift} style={{ display: "inline-block" }}>
            <Link to="/app/runs/new" className="btn-primary btn-lg btn-on-dark">
              Start a new run
            </Link>
          </motion.span>
        </motion.div>
      </motion.div>
    </section>
  );
}

export default function Landing() {
  return (
    <div className="landing">
      <a className="skip-link" href="#landing-main">
        Skip to main content
      </a>
      <LandingNav />
      <main id="landing-main">
        <Hero />
        <Premise />
        <HowItWorks />
        <Safety />
        <Evidence />
        <ClosingBand />
      </main>
      <Footer />
    </div>
  );
}
