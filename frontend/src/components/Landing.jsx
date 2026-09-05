import { useCallback, useEffect, useId, useLayoutEffect, useRef } from "react";
import { motion, useReducedMotion } from "motion/react";
import { Link } from "../router.jsx";
import {
  heroStage,
  revealGroup,
  revealItem,
  riseUp,
  useReveal,
  useScrolledPast,
} from "../motion-landing.js";
import Footer from "./Footer.jsx";
import HeroFlow from "./HeroFlow.jsx";

/**
 * The public face of Accord.
 *
 * The page has one job before a single word is read: many financial
 * sources go in, one reconciled view comes out. The hero states that
 * visually, and everything after it has been cut to a heading, an
 * artefact, and a label for the artefact. Nothing on this page describes
 * a thing that is already on screen: the table is the argument about
 * fragmented sources, the stage list is the argument about where a model
 * sits, the three figures are the argument about failure mode. Sentences
 * that restated any of those are deleted, not shortened.
 *
 * The one paragraph left is the provenance note, and it stays because it
 * is the only thing that makes the three figures checkable.
 *
 * Every figure on the page is measured and frozen in
 * `backend/evaluations/accord/FROZEN.json`, and is labelled with the
 * dataset and configuration it came from. There are no customers, no
 * testimonials, no logo wall and no money-saved claims, because we do not
 * have any of those things.
 */

/* --------------------------------------------------------------- content */

/*
 * One payment as six systems record it. The formats are the ones these
 * systems really emit; the merchant is invented, and the caption says so.
 * `alt` marks the cells that diverge from the order of record — the
 * divergence is the entire point, and it is why this table survived the
 * cut while the prose around it did not.
 *
 * The six rows still carry six identifier namespaces (order id, gateway
 * payment id, UPI reference, terminal batch, bank UTR, journal entry),
 * four date formats, four spellings of one counterparty, and gross
 * against net. Substituting a row is only safe if it keeps all four of
 * those divergences, which is what a POS terminal export does: its own
 * id space, its own truncated name field, and the settled net rather
 * than the ordered gross.
 */
const FRAGMENTS = [
  { file: "Orders.csv", ref: "ORD-48213", date: "2026-01-14", party: "Kirana Junction", amount: "12,400.00" },
  {
    file: "Razorpay_Settlements.csv",
    ref: "pay_MkT19aZ2Q0hLb",
    date: "14/01/2026",
    party: "KIRANA JUNCTION PVT LTD",
    amount: "12,400.00",
    alt: ["ref", "date", "party"],
  },
  {
    file: "UPI_Transactions.csv",
    ref: "401234567890",
    date: "14-01-2026 18:42",
    party: "kiranajunction@okhdfc",
    amount: "12400",
    alt: ["ref", "date", "party", "amount"],
  },
  {
    file: "POS_Settlements.csv",
    ref: "TID44718/B0092",
    date: "15-01-2026",
    party: "KIRANA JN",
    amount: "11,983.42",
    alt: ["ref", "date", "party", "amount"],
  },
  {
    file: "Bank_Statement.csv",
    ref: "NEFT/HDFC/4012345",
    date: "15-Jan-2026",
    party: "RZPY SOFTWARE PVT LTD",
    amount: "11,983.42",
    alt: ["ref", "date", "party", "amount"],
  },
  {
    file: "Ledger_Q1.csv",
    ref: "JE-2026-0114-77",
    date: "2026-01-14",
    party: "Kirana Junction",
    amount: "12,400.00",
    alt: ["ref"],
  },
];

/* Shown, not described — but only four, plus the overflow chip. Four
   different *kinds* of file (a bank statement, a gateway settlement, a
   UPI collections report, an accounting ledger) make the multi-source
   point that nine of them made, in one line instead of three, and
   without the Ingest row towering over the four stages beneath it. The
   hero figure already carries the long list. */
const INGEST_FILES = [
  "ICICI_Jan.csv",
  "Razorpay_Settlements.csv",
  "UPI_Collections.csv",
  "Ledger_Q1.csv",
];

/* Five names and what each stage actually touches. The names carry the
   sequence on their own, so the sentences that used to sit under them are
   gone; the heading above states the one fact the names cannot, which is
   that exactly one of the five involves a model. */
const STAGES = [
  { key: "ingest", name: "Ingest", tags: ["1–50 sources"] },
  { key: "understand", name: "Understand", tags: ["Source role", "Column mapping"] },
  { key: "prove", name: "Prove", tags: ["Exact references", "Settlement arithmetic"] },
  { key: "investigate", name: "Investigate", tags: ["Merchant aliases", "Bank narration"] },
  { key: "review", name: "Review", tags: ["Human review"] },
];

/*
 * Every figure below is read from backend/evaluations/accord/FROZEN.json —
 * a labelled, generated held-out split of 1,000 records, seed 90210, frozen
 * at code commit b6145bb. Configuration B is the healthy-provider run and
 * configuration C is the total-outage run; both use the same semantic
 * backend, so the pair is a fair comparison.
 */
const MEASURED = [
  { figure: "20.4%", unit: "of records reached a model" },
  { figure: "0", unit: "false reconciliations, 204 / 204 calls failed" },
  { figure: "9.1% → 21.6%", unit: "human review, healthy vs. dark" },
];

/* ------------------------------------------------------- pixelate reveal */

/**
 * A heading that resolves out of blocks instead of fading up.
 *
 * Coarse `feTurbulence` quantised by a `discrete` transfer function gives a
 * displacement map made of chunks rather than a smooth gradient, so the
 * letterforms break into blocks and snap back together — a real pixelate,
 * not a blur. A short stepped blur rides alongside it so the blocks read as
 * unresolved rather than merely shifted.
 *
 * Three rules it holds to:
 *   - The filter and the starting opacity are applied *by the effect*, not
 *     in JSX. If the effect never runs — reduced motion, no SVG filter
 *     support, an exception — the heading has never been anything other
 *     than crisp and fully opaque. Text can never be hidden by this.
 *   - It plays once per heading, and the filter is removed at the end so
 *     nothing stays on a rasterised layer for the life of the page.
 *   - Headings only. A paragraph that pixelates in is unreadable.
 */
function PixelHeading({
  tag: Tag = "h2",
  className,
  id,
  children,
  active = true,
  amount = 18,
  blur = 2.4,
  duration = 620,
}) {
  const rawId = useId();
  const filterId = `pix${rawId.replace(/[^A-Za-z0-9]/g, "")}`;
  const hostRef = useRef(null);
  const dispRef = useRef(null);
  const blurRef = useRef(null);
  const played = useRef(false);
  const reduced = useReducedMotion();

  useLayoutEffect(() => {
    if (reduced || !active || played.current) return undefined;
    const host = hostRef.current;
    const disp = dispRef.current;
    const gauss = blurRef.current;
    if (!host || !disp || !gauss) return undefined;

    host.style.filter = `url(#${filterId})`;
    host.style.opacity = "0.18";
    disp.setAttribute("scale", String(amount));
    gauss.setAttribute("stdDeviation", String(blur));

    let raf = 0;
    let t0 = 0;
    const step = (now) => {
      if (!t0) t0 = now;
      const t = Math.min(1, (now - t0) / duration);
      const eased = 1 - (1 - t) ** 3;
      disp.setAttribute("scale", (amount * (1 - eased)).toFixed(2));
      gauss.setAttribute("stdDeviation", (blur * (1 - eased)).toFixed(2));
      host.style.opacity = String(Math.min(1, 0.18 + eased * 1.35));
      if (t < 1) {
        raf = requestAnimationFrame(step);
        return;
      }
      // Only now is it played. Marking it played at *start* meant that in
      // StrictMode — which mounts, unmounts and remounts every effect — the
      // first pass claimed the reveal, the cleanup wiped it, and the second
      // pass bailed out, so the effect never ran at all in development.
      played.current = true;
      host.style.filter = "";
      host.style.opacity = "";
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
      host.style.filter = "";
      host.style.opacity = "";
    };
  }, [active, reduced, amount, blur, duration, filterId]);

  return (
    <>
      <Tag className={className} id={id} ref={hostRef}>
        {children}
      </Tag>
      {!reduced && (
        <svg className="pix-defs" width="0" height="0" aria-hidden="true" focusable="false">
          <filter
            id={filterId}
            x="-14%"
            y="-22%"
            width="128%"
            height="144%"
            colorInterpolationFilters="sRGB"
          >
            <feGaussianBlur ref={blurRef} in="SourceGraphic" stdDeviation="0" result="soft" />
            <feTurbulence
              type="fractalNoise"
              baseFrequency="0.042"
              numOctaves="1"
              seed="8"
              stitchTiles="stitch"
              result="noise"
            />
            {/* Quantising the noise is what turns a smear into blocks. */}
            <feComponentTransfer in="noise" result="blocks">
              <feFuncR type="discrete" tableValues="0 0.2 0.4 0.6 0.8 1" />
              <feFuncG type="discrete" tableValues="0 0.2 0.4 0.6 0.8 1" />
            </feComponentTransfer>
            <feDisplacementMap
              in="soft"
              in2="blocks"
              scale="0"
              xChannelSelector="R"
              yChannelSelector="G"
              ref={dispRef}
            />
          </filter>
        </svg>
      )}
    </>
  );
}

/* ------------------------------------------------------------ components */

/**
 * The mark, drawn rather than loaded.
 *
 * The PNG lockup is dark navy on transparent, so it disappears against the
 * hero. This is the same geometry the favicon already uses — three streams
 * converging on one junction, then an arrow — with the outer strokes on
 * `currentColor`, so one component works on the dark nav and the light one
 * without shipping two files that can drift apart.
 */
function AccordMark({ size = 24 }) {
  return (
    <svg
      className="accord-mark"
      viewBox="0 0 32 32"
      width={size}
      height={size}
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
  );
}

const SECTIONS = [
  ["#problem", "Problem"],
  ["#pipeline", "Pipeline"],
  ["#assurance", "Assurance"],
];

/**
 * Where an in-page target is supposed to sit: whatever its own
 * `scroll-margin-top` computes to. Read from the element rather than
 * duplicated as a constant, so the CSS stays the single source of truth
 * and the desktop and mobile offsets cannot drift apart from it.
 */
function targetOffset(el) {
  const m = parseFloat(getComputedStyle(el).scrollMarginTop);
  return Number.isFinite(m) ? m : 0;
}

/**
 * Put `hash` at its offset immediately, and report how far out it was.
 *
 * `scrollIntoView` is a one-shot: it computes a position from the layout
 * that exists at the instant it is called. On a cold load that layout is a
 * lie — web fonts have not swapped under the headings and the hero artwork
 * has not arrived — and every section below inherits the accumulated
 * error, so the last section on the page misses by over a thousand pixels.
 * Correcting against a measurement is the only thing that survives that.
 */
function alignNow(hash) {
  const el = document.querySelector(hash);
  if (!el) return 0;
  const delta = el.getBoundingClientRect().top - targetOffset(el);
  if (Math.abs(delta) > 1.5) {
    window.scrollTo({ top: window.scrollY + delta, behavior: "auto" });
  }
  return delta;
}

/** Correct once the smooth scroll a click started has come to rest. */
function alignAfterScroll(hash) {
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    window.removeEventListener("scrollend", finish);
    // Only step in if the smooth scroll actually missed; a correction of a
    // pixel or two would read as a twitch at the end of the movement.
    const el = document.querySelector(hash);
    if (el && Math.abs(el.getBoundingClientRect().top - targetOffset(el)) > 4) alignNow(hash);
  };
  if ("onscrollend" in window) window.addEventListener("scrollend", finish, { once: true });
  window.setTimeout(finish, 900);
}

/**
 * In-page navigation, driven by us rather than by the browser.
 *
 * Left to the browser this had two failure modes, both of which a reader
 * hits in normal use: loading or reloading `/#pipeline` did not scroll at
 * all (the router sets `scrollRestoration = "manual"` and puts the page
 * back at the top before React has even rendered the section), and any
 * anchor that had already been visited was a no-op. Doing it here means a
 * link behaves the same way every time it is clicked, on first load, on
 * reload, and when the URL is shared — and these are shareable URLs, so
 * `/#assurance` landing on the wrong section is a wrong claim delivered to
 * whoever opened the link.
 */
function useInPageNav() {
  const reduced = useReducedMotion();

  const goTo = useCallback(
    (hash, { push = true, smooth = true } = {}) => {
      const el = document.querySelector(hash);
      if (!el) return false;
      if (smooth && !reduced) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
        alignAfterScroll(hash);
      } else {
        alignNow(hash);
      }
      if (push && window.location.hash !== hash) {
        window.history.pushState(window.history.state, "", hash);
      }
      return true;
    },
    [reduced]
  );

  /**
   * Honour a hash the page was opened or reloaded on.
   *
   * Not two animation frames — frames are not the thing worth waiting for.
   * The page moves when the web fonts swap in under headings set in a
   * serif, and when the hero artwork decodes. So the position is asserted
   * again on each of those, and at a handful of checkpoints out to 2.6s,
   * and each attempt measures rather than assumes. Any real input from the
   * reader ends it: nothing here is allowed to yank a page someone has
   * started reading.
   */
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash || hash.length < 2) return undefined;
    if (!document.querySelector(hash)) return undefined;

    let cancelled = false;
    const stop = () => {
      cancelled = true;
    };
    const attempt = () => {
      if (!cancelled) alignNow(hash);
    };

    // Jump instantly: a smooth slide from the top of the page to a section
    // three screens down is a long, disorienting way to open a link.
    attempt();
    const raf = requestAnimationFrame(attempt);
    const timers = [40, 120, 260, 500, 900, 1400, 2000, 2600].map((d) =>
      window.setTimeout(attempt, d)
    );

    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => {
        attempt();
        requestAnimationFrame(attempt);
      });
    }
    window.addEventListener("load", attempt);
    for (const ev of ["wheel", "touchstart", "keydown"]) {
      window.addEventListener(ev, stop, { passive: true });
    }

    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      timers.forEach(window.clearTimeout);
      window.removeEventListener("load", attempt);
      for (const ev of ["wheel", "touchstart", "keydown"]) {
        window.removeEventListener(ev, stop);
      }
    };
  }, []);

  return useCallback(
    (event) => {
      const href = event.currentTarget.getAttribute("href") || "";
      if (!href.startsWith("#")) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      goTo(href);
    },
    [goTo]
  );
}

function LandingNav({ heroRef, onAnchor }) {
  const solid = useScrolledPast(heroRef, 96);
  return (
    <header className={"lnav" + (solid ? " lnav-solid" : "")}>
      <div className="lnav-pill">
        <Link to="/" className="lnav-brand" aria-label="Accord, home">
          <AccordMark size={22} />
          <span className="lnav-brand-text">Accord</span>
        </Link>
        <nav className="lnav-links" aria-label="Page sections">
          {SECTIONS.map(([href, label]) => (
            <a href={href} key={href} onClick={onAnchor}>
              {label}
            </a>
          ))}
        </nav>
        <span className="lnav-cta">
          <Link to="/app/runs" className="btn-primary btn-sm">
            Open workspace
          </Link>
        </span>
      </div>
    </header>
  );
}

function Hero({ heroRef, onAnchor }) {
  return (
    <section className="hero" ref={heroRef} aria-labelledby="hero-heading">
      {/* Decorative. The section carries its own near-black ground, so the
          hero still clears AA if this never loads. */}
      <img
        className="hero-art"
        src="/brand/accord-hero.jpg"
        alt=""
        aria-hidden="true"
        width={1600}
        height={900}
        decoding="async"
        fetchpriority="high"
      />
      <div className="hero-veil" aria-hidden="true" />

      <div className="hero-grid">
        <motion.div className="hero-copy" variants={heroStage} initial="hidden" animate="show">
          <PixelHeading
            tag="h1"
            className="hero-heading"
            id="hero-heading"
            amount={24}
            blur={3}
            duration={760}
          >
            When the numbers don&rsquo;t agree, Accord finds where the trail breaks.
          </PixelHeading>

          <motion.p className="hero-principle" variants={riseUp}>
            Deterministic evidence first. AI only where ambiguity remains.
          </motion.p>

          <motion.div className="hero-actions" variants={riseUp}>
            <Link to="/app/runs" className="btn-primary btn-lg">
              Open workspace
            </Link>
            <a href="#pipeline" className="btn-quiet btn-lg btn-on-dark" onClick={onAnchor}>
              See how it works
            </a>
          </motion.div>
        </motion.div>

        <motion.div
          className="hero-visual"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1], delay: 0.16 }}
        >
          <HeroFlow />
        </motion.div>
      </div>
    </section>
  );
}

function Problem() {
  const [ref, inView] = useReveal(0.05);
  const cell = (row, key) =>
    "frag-cell" + (row.alt && row.alt.includes(key) ? " frag-cell-alt" : "");

  return (
    <section className="section section-problem" id="problem" ref={ref} aria-labelledby="problem-heading">
      <motion.div
        className="wrap"
        variants={revealGroup}
        initial="hidden"
        animate={inView ? "show" : "hidden"}
      >
        <PixelHeading
          className="section-heading"
          id="problem-heading"
          active={inView}
          amount={15}
          blur={2}
          duration={540}
        >
          One payment. Six files. No two agree.
        </PixelHeading>

        <motion.div className="frag" variants={revealItem}>
          {/* A scrollable region needs to be reachable and operable from the
              keyboard, so it is a labelled region with a tab stop rather
              than a div only a pointer can move. */}
          <div
            className="frag-scroll"
            role="region"
            aria-label="One payment across six source files"
            tabIndex={0}
          >
            {/* The description a screen reader needs lives on the table as a
                label rather than as a visible caption, because the visible
                caption said the same thing the region label already said and
                this page is not allowed a third pass over one fact. */}
            <table
              className="frag-table"
              aria-label="One payment as it appears in six source files: the identifier, date, counterparty and amount differ in each"
            >
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Identifier</th>
                  <th scope="col">Date</th>
                  <th scope="col">Counterparty</th>
                  <th scope="col" className="frag-num">
                    {/* The symbol, not the currency code: it is a column of
                        rupee amounts and the glyph says so in one character. */}
                    Amount &#8377;
                  </th>
                </tr>
              </thead>
              <tbody>
                {FRAGMENTS.map((row) => (
                  <tr key={row.file}>
                    <th scope="row" className="frag-file">
                      {row.file}
                    </th>
                    <td className={cell(row, "ref")}>{row.ref}</td>
                    <td className={cell(row, "date")}>{row.date}</td>
                    <td className={cell(row, "party")}>{row.party}</td>
                    <td className={cell(row, "amount") + " frag-num"}>{row.amount}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="frag-caption">Illustrative. Highlighted cells diverge from Orders.csv.</p>
        </motion.div>
      </motion.div>
    </section>
  );
}

function Pipeline() {
  const [ref, inView] = useReveal(0.04);
  return (
    <section className="section section-pipeline" id="pipeline" ref={ref} aria-labelledby="pipeline-heading">
      <motion.div
        className="wrap"
        variants={revealGroup}
        initial="hidden"
        animate={inView ? "show" : "hidden"}
      >
        <PixelHeading
          className="section-heading"
          id="pipeline-heading"
          active={inView}
          amount={15}
          blur={2}
          duration={540}
        >
          Five stages. A model is in one.
        </PixelHeading>

        <ol className="stages">
          {STAGES.map((s) => (
            <motion.li className={`stage stage-${s.key}`} key={s.key} variants={revealItem}>
              <div className="stage-mark">
                <h3 className="stage-name">{s.name}</h3>
              </div>
              <div className="stage-body">
                {/* Ingest is many sources at once, so it is shown as many
                    rather than claimed as many. Decorative: the capacity is
                    stated as a tag beside it. */}
                {s.key === "ingest" && (
                  <ul className="filestrip" aria-hidden="true">
                    {INGEST_FILES.map((f) => (
                      <li className="filechip" key={f}>
                        <span className="filechip-dot" />
                        {f}
                      </li>
                    ))}
                    <li className="filechip filechip-more">+ more</li>
                  </ul>
                )}

                <ul className="stage-tags">
                  {s.tags.map((t) => (
                    <li className="stage-tag" key={t}>
                      {t}
                    </li>
                  ))}
                </ul>
              </div>
            </motion.li>
          ))}
        </ol>
      </motion.div>
    </section>
  );
}

function Assurance() {
  const [ref, inView] = useReveal(0.04);
  return (
    <section className="assurance" id="assurance" ref={ref} aria-labelledby="assurance-heading">
      <motion.div
        className="wrap"
        variants={revealGroup}
        initial="hidden"
        animate={inView ? "show" : "hidden"}
      >
        <PixelHeading
          className="assurance-heading"
          id="assurance-heading"
          active={inView}
          amount={16}
          blur={2.2}
          duration={580}
        >
          Unresolved is better than incorrectly reconciled.
        </PixelHeading>

        <div className="measured">
          {MEASURED.map((m) => (
            <motion.article className="measured-card" key={m.unit} variants={revealItem}>
              <p className="measured-figure">{m.figure}</p>
              <h3 className="measured-unit">{m.unit}</h3>
            </motion.article>
          ))}
        </div>

        {/* The only paragraph left on the page. It stays because it is the
            single thing that makes the three figures above checkable: strip
            it and they are just numbers someone typed. */}
        <motion.div className="prov-note" variants={revealItem}>
          <p className="prov-note-tag">Method</p>
          <p className="prov-note-text">
            1,000-record labelled held-out split, seed 90210, digest{" "}
            <code className="mono">473394ea</code>, frozen at{" "}
            <code className="mono">b6145bb</code>. Accuracy 88.3% healthy, 76.8% dark.
            Checksums: <code className="mono">backend/evaluations/accord/</code>.
          </p>
        </motion.div>

        <motion.div className="assurance-cta" variants={revealItem}>
          <Link to="/app/runs" className="btn-primary btn-lg">
            Open workspace
          </Link>
          <Link to="/app/evaluation" className="btn-quiet btn-lg btn-on-dark">
            See the evaluation
          </Link>
        </motion.div>
      </motion.div>
    </section>
  );
}

export default function Landing() {
  const heroRef = useRef(null);
  const onAnchor = useInPageNav();

  return (
    <div className="landing">
      <a className="skip-link" href="#landing-main">
        Skip to content
      </a>
      <LandingNav heroRef={heroRef} onAnchor={onAnchor} />
      <main id="landing-main">
        <Hero heroRef={heroRef} onAnchor={onAnchor} />
        <Problem />
        <Pipeline />
        <Assurance />
      </main>
      <Footer />
    </div>
  );
}
