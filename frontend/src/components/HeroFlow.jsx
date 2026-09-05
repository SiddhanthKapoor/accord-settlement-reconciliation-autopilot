import { useMemo, useRef } from "react";
import { motion, useInView, useReducedMotion } from "motion/react";
import { useMediaQuery } from "../motion-landing.js";

/**
 * The hero convergence figure.
 *
 * It is the product in one picture: many heterogeneous financial sources
 * enter on the left, pass through a single reconciliation boundary, and
 * leave as a much smaller set of resolved outcomes. It is drawn to sit in
 * front of `accord-hero.jpg` and to repeat that artwork's own geometry —
 * fanned streams, a tilted vertical plane, clean emerald lines out — so
 * the photograph and the figure read as one composition rather than as a
 * graphic pasted over a background.
 *
 * Rules it holds to:
 *   - Decorative. `aria-hidden`, and nothing here is the only place any
 *     fact appears; every label is repeated in the page copy below.
 *   - Cheap. Twelve elements animate, each a transform on an SVG group,
 *     and the whole thing unmounts its moving parts once the hero leaves
 *     the viewport. A marketing page must not heat a laptop.
 *   - Reduced motion renders a *composed still* — chips distributed along
 *     the streams, the gate lit, outputs part-way out — not a blank panel
 *     and not a frozen first frame.
 *
 * Geometry is analytic rather than measured: the streams are cubic Béziers
 * whose control points are known, so the travelling chips are sampled from
 * the same curve that is stroked, with no DOM measurement and no layout
 * read at any point.
 */

/* ------------------------------------------------------------- geometry */

/*
 * Two layouts rather than one scaled layout. A 720-unit viewBox squeezed
 * into a 340px phone column would render its file names at five pixels;
 * the compact board drops to five sources and a larger type size so the
 * labels stay legible at the width they actually get.
 */
const FULL = {
  w: 720,
  h: 480,
  fs: 10.5,
  ch: 6.3,
  dotDx: 10,
  textDx: 23,
  padR: 11,
  gateX: 470,
  planeA: { x0: 458, x1: 484, topOuter: 136, topInner: 146, botInner: 334, botOuter: 344 },
  planeB: { x0: 490, x1: 504, topOuter: 147, topInner: 152, botInner: 328, botOuter: 333 },
  axis: [40, 460],
  ticks: [170, 195, 220, 245, 270, 295, 320],
  tickX: [450, 458],
  beamX: 472,
  beam: [198, 282],
  capY: 120,
  capFs: 8.5,
  gyCenter: 240,
  gySpread: 9.5,
  c1d: 120,
  c2d: 130,
  outFrom: 510,
  outTo: 592,
  outD1: 30,
  outD2: 40,
  outLabelX: 604,
  outLabels: true,
  chip: 5,
  outChip: 6,
  outs: [208, 240, 272],
  sources: [
    ["ICICI_Jan.csv", 6, 26],
    ["Razorpay_Settlements.csv", 0, 78],
    ["UPI_Transactions.csv", 18, 128],
    ["Escrow_Statement.csv", 8, 178],
    ["Fee_Adjustments.csv", 26, 230],
    ["Ledger_Q1.csv", 2, 280],
    ["Orders.csv", 30, 330],
    ["Invoices.csv", 14, 382],
    ["POS_Settlements.csv", 4, 434],
  ],
};

const COMPACT = {
  w: 420,
  h: 330,
  fs: 12.5,
  ch: 7.5,
  dotDx: 8,
  textDx: 19,
  padR: 11,
  gateX: 300,
  planeA: { x0: 290, x1: 312, topOuter: 84, topInner: 94, botInner: 236, botOuter: 246 },
  planeB: { x0: 317, x1: 328, topOuter: 98, topInner: 102, botInner: 228, botOuter: 232 },
  axis: [28, 306],
  ticks: [112, 137, 162, 187, 212],
  tickX: [283, 290],
  beamX: 301,
  beam: [132, 198],
  capY: 72,
  capFs: 7.5,
  gyCenter: 165,
  gySpread: 11,
  c1d: 50,
  c2d: 60,
  outFrom: 334,
  outTo: 396,
  outD1: 22,
  outD2: 28,
  outLabelX: null,
  outLabels: false,
  chip: 5,
  outChip: 6,
  outs: [140, 165, 190],
  sources: [
    ["Orders.csv", 16, 26],
    ["ICICI_Jan.csv", 0, 88],
    ["UPI_Transactions.csv", 6, 152],
    ["Ledger_Q1.csv", 20, 216],
    ["Invoices.csv", 4, 286],
  ],
};

/* The three outcomes a record can end in. Named here only because the
   picture would be mute without them; the same three are spelled out in
   the pipeline section, which is where a screen reader meets them. */
const OUTCOMES = [
  { label: "Reconciled", tone: "ok" },
  { label: "Investigated", tone: "ok" },
  { label: "Human review", tone: "hold" },
];

/* Echoes the artwork: mostly pale chips, a couple of amber, one teal. The
   point is heterogeneity, so they must not all look alike. */
const CHIP_TONE = ["pale", "pale", "amber", "pale", "teal", "pale", "amber", "pale", "pale"];

const cubic = (p0, p1, p2, p3, t) => {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  return [a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]];
};

const SAMPLES = 20;

/** Sample a cubic into parallel x/y keyframe arrays plus a fade envelope. */
function sampleCurve(p0, p1, p2, p3) {
  const xs = [];
  const ys = [];
  const op = [];
  for (let i = 0; i < SAMPLES; i += 1) {
    const t = i / (SAMPLES - 1);
    const [x, y] = cubic(p0, p1, p2, p3, t);
    xs.push(x);
    ys.push(y);
    // Fades up as it leaves the file and is consumed at the boundary.
    op.push(t < 0.12 ? t / 0.12 : t > 0.86 ? Math.max(0, (1 - t) / 0.14) : 1);
  }
  return { xs, ys, op };
}

const pathOf = (p0, p1, p2, p3) =>
  `M ${p0[0]} ${p0[1]} C ${p1[0]} ${p1[1]} ${p2[0]} ${p2[1]} ${p3[0]} ${p3[1]}`;

/** Build every curve, pill box and label position for one layout. */
function buildBoard(L) {
  const n = L.sources.length;
  const streams = L.sources.map(([name, x, y], i) => {
    const w = L.textDx + name.length * L.ch + L.padR;
    const sx = x + w;
    const gy = L.gyCenter + (i - (n - 1) / 2) * L.gySpread;
    const p0 = [sx, y];
    const p1 = [sx + L.c1d, y];
    const p2 = [L.gateX - L.c2d, gy];
    const p3 = [L.gateX, gy];
    return {
      name,
      x,
      y,
      w,
      tone: CHIP_TONE[i % CHIP_TONE.length],
      d: pathOf(p0, p1, p2, p3),
      ...sampleCurve(p0, p1, p2, p3),
    };
  });

  const outputs = L.outs.map((oy, i) => {
    const p0 = [L.outFrom, L.gyCenter];
    const p1 = [L.outFrom + L.outD1, L.gyCenter];
    const p2 = [L.outTo - L.outD2, oy];
    const p3 = [L.outTo, oy];
    const s = sampleCurve(p0, p1, p2, p3);
    return {
      oy,
      ...OUTCOMES[i],
      d: pathOf(p0, p1, p2, p3),
      xs: s.xs,
      ys: s.ys,
      // Outputs resolve rather than dissolve: they hold at full strength.
      op: s.op.map((v, k) => (k < 3 ? v : 1)),
    };
  });

  return { streams, outputs };
}

/* -------------------------------------------------------------- pieces */

function SourcePill({ L, s }) {
  return (
    <g>
      <rect className="hf-pill" x={s.x} y={s.y - 11} width={s.w} height={22} rx={5} />
      <rect
        className={`hf-dot hf-dot-${s.tone}`}
        x={s.x + L.dotDx}
        y={s.y - 2.8}
        width={5.5}
        height={5.5}
        rx={1}
      />
      <text
        className="hf-file"
        x={s.x + L.textDx}
        y={s.y + L.fs * 0.35}
        style={{ fontSize: L.fs }}
      >
        {s.name}
      </text>
    </g>
  );
}

/**
 * One travelling chip.
 *
 * `still` renders it parked at a fixed point on its own curve, which is
 * what reduced motion and the off-screen state both get. `phase` spaces
 * the parked chips out so the still frame reads as flow rather than as a
 * row of dots at the same offset.
 */
function Chip({ curve, size, className, cycle, travel, delay, still, phase }) {
  const half = size / 2;
  const body = <rect x={-half} y={-half} width={size} height={size} rx={1} className={className} />;

  if (still) {
    const idx = Math.min(SAMPLES - 1, Math.max(0, Math.round(phase * (SAMPLES - 1))));
    return (
      <g transform={`translate(${curve.xs[idx]} ${curve.ys[idx]})`} opacity={curve.op[idx]}>
        {body}
      </g>
    );
  }

  return (
    <motion.g
      initial={{ x: curve.xs[0], y: curve.ys[0], opacity: 0 }}
      animate={{ x: curve.xs, y: curve.ys, opacity: curve.op }}
      transition={{
        duration: travel,
        ease: "linear",
        repeat: Infinity,
        repeatDelay: Math.max(0, cycle - travel),
        delay,
      }}
    >
      {body}
    </motion.g>
  );
}

function Gate({ L, animated }) {
  const { planeA: a, planeB: b } = L;
  return (
    <g>
      <line
        className="hf-axis"
        x1={L.gateX}
        y1={L.axis[0]}
        x2={L.gateX}
        y2={L.axis[1]}
      />
      {L.ticks.map((t) => (
        <line className="hf-tick" key={t} x1={L.tickX[0]} y1={t} x2={L.tickX[1]} y2={t} />
      ))}
      <ellipse className="hf-glow" cx={L.gateX + 8} cy={L.gyCenter} rx={L.w * 0.1} ry={L.h * 0.16} />
      <path
        className="hf-plane hf-plane-a"
        d={`M ${a.x0} ${a.topInner} L ${a.x1} ${a.topOuter} L ${a.x1} ${a.botOuter} L ${a.x0} ${a.botInner} Z`}
      />
      <path
        className="hf-plane hf-plane-b"
        d={`M ${b.x0} ${b.topInner} L ${b.x1} ${b.topOuter} L ${b.x1} ${b.botOuter} L ${b.x0} ${b.botInner} Z`}
      />
      {animated ? (
        <motion.line
          className="hf-beam"
          x1={L.beamX}
          y1={L.beam[0]}
          x2={L.beamX}
          y2={L.beam[1]}
          initial={{ opacity: 0.3 }}
          animate={{ opacity: [0.3, 0.85, 0.3] }}
          transition={{ duration: 3.9, ease: "easeInOut", repeat: Infinity }}
        />
      ) : (
        <line
          className="hf-beam"
          x1={L.beamX}
          y1={L.beam[0]}
          x2={L.beamX}
          y2={L.beam[1]}
          opacity={0.62}
        />
      )}
      <text
        className="hf-cap"
        x={L.gateX + 6}
        y={L.capY}
        textAnchor="middle"
        style={{ fontSize: L.capFs }}
      >
        RECONCILE
      </text>
    </g>
  );
}

/* ---------------------------------------------------------------- board */

export default function HeroFlow() {
  const hostRef = useRef(null);
  const compact = useMediaQuery("(max-width: 860px)");
  const reduced = useReducedMotion();
  // A generous margin so the figure only stands down once it is properly
  // gone, never while a sliver of it is still on screen.
  const near = useInView(hostRef, { margin: "300px 0px 300px 0px" });

  const L = compact ? COMPACT : FULL;
  const board = useMemo(() => buildBoard(L), [L]);

  const animated = !reduced && near;
  const CYCLE = 7.8;
  const TRAVEL = 5.6;
  const step = CYCLE / board.streams.length;

  return (
    <div className="hf" ref={hostRef}>
      <svg
        className="hf-svg"
        viewBox={`0 0 ${L.w} ${L.h}`}
        role="presentation"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <radialGradient id="hf-glow-grad" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#00c389" stopOpacity="0.34" />
            <stop offset="55%" stopColor="#00c389" stopOpacity="0.09" />
            <stop offset="100%" stopColor="#00c389" stopOpacity="0" />
          </radialGradient>
        </defs>

        {board.streams.map((s) => (
          <path className="hf-stream" key={`p-${s.name}`} d={s.d} />
        ))}

        <Gate L={L} animated={animated} />

        {board.outputs.map((o) => (
          <path className={`hf-out hf-out-${o.tone}`} key={`op-${o.label}`} d={o.d} />
        ))}

        {board.streams.map((s, i) => (
          <Chip
            key={`c-${s.name}`}
            curve={s}
            size={L.chip}
            className={`hf-chip hf-chip-${s.tone}`}
            cycle={CYCLE}
            travel={TRAVEL}
            delay={i * step}
            still={!animated}
            phase={0.22 + ((i * 0.37) % 0.58)}
          />
        ))}

        {board.outputs.map((o, i) => (
          <Chip
            key={`oc-${o.label}`}
            curve={o}
            size={L.outChip}
            className={`hf-chip hf-chip-out hf-chip-${o.tone}`}
            cycle={CYCLE}
            travel={2.9}
            delay={2.6 + i * 0.42}
            still={!animated}
            phase={0.55 + i * 0.14}
          />
        ))}

        {board.outputs.map((o) => (
          <circle
            className={`hf-node hf-node-${o.tone}`}
            key={`on-${o.label}`}
            cx={L.outTo}
            cy={o.oy}
            r={2.6}
          />
        ))}

        {L.outLabels &&
          board.outputs.map((o) => (
            <text
              className={`hf-outlabel hf-outlabel-${o.tone}`}
              key={`ol-${o.label}`}
              x={L.outLabelX}
              y={o.oy + L.fs * 0.35}
              style={{ fontSize: L.fs }}
            >
              {o.label}
            </text>
          ))}

        {board.streams.map((s) => (
          <SourcePill L={L} s={s} key={`s-${s.name}`} />
        ))}
      </svg>
    </div>
  );
}
