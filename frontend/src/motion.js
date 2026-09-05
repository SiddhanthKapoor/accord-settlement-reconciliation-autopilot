/**
 * Shared motion primitives.
 *
 * One vocabulary rather than per-component invention, so a panel opening
 * in the review queue moves the way a panel opening in a run does. Every
 * duration here is short: this is a finance tool, and an operator working
 * a queue of 200 exceptions should never wait on an animation.
 *
 * `MotionConfig reducedMotion="user"` at the root already suppresses
 * transforms for anyone who has asked the OS for reduced motion, so these
 * do not re-implement that check.
 */

import { useEffect as _useEffect, useRef as _useRef, useState as _useState } from "react";

// Easing curves. `standard` for most things, `exit` slightly faster
// because leaving should feel lighter than arriving.
export const EASE = [0.22, 0.61, 0.36, 1];
export const EASE_EXIT = [0.4, 0, 1, 1];

export const DURATION = {
  instant: 0.12,
  fast: 0.18,
  base: 0.24,
  slow: 0.34,
};

/** Page-level view change. */
export const pageTransition = {
  initial: { opacity: 0, y: 10 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
  transition: { duration: DURATION.base, ease: EASE },
};

/** A card or tile arriving. */
export const riseIn = {
  initial: { opacity: 0, y: 12 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: DURATION.base, ease: EASE },
};

/** Side panel, e.g. record detail. */
export const slideOver = {
  initial: { x: 480, opacity: 0 },
  animate: { x: 0, opacity: 1 },
  exit: { x: 480, opacity: 0 },
  transition: { duration: DURATION.base, ease: EASE },
};

/** Disclosure that expands in place. */
export const expand = {
  initial: { height: 0, opacity: 0 },
  animate: { height: "auto", opacity: 1 },
  exit: { height: 0, opacity: 0 },
  transition: { duration: DURATION.fast, ease: EASE },
};

/** Modal / dialog. */
export const dialog = {
  initial: { opacity: 0, scale: 0.97, y: 8 },
  animate: { opacity: 1, scale: 1, y: 0 },
  exit: { opacity: 0, scale: 0.98, y: 4 },
  transition: { duration: DURATION.fast, ease: EASE },
};

export const backdrop = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: DURATION.fast },
};

/**
 * Stagger children into view.
 *
 * Capped deliberately: a list of 200 review items staggered at 40ms each
 * would take eight seconds to finish arriving. `staggerChildren` applies
 * per child, so the cap belongs in how many items are animated, not here
 * — see `listIndexDelay`.
 */
export const stagger = (delay = 0.035) => ({
  animate: { transition: { staggerChildren: delay } },
});

/** Only the first rows earn a delay; the rest appear immediately. */
export const listIndexDelay = (index, step = 0.03, max = 8) =>
  index < max ? index * step : 0;

/** Subtle press feedback on interactive elements. */
export const pressable = {
  whileHover: { y: -1 },
  whileTap: { scale: 0.985 },
  transition: { duration: DURATION.instant, ease: EASE },
};

/** Number that counts toward its value instead of snapping. */
export const countUp = {
  transition: { duration: DURATION.slow, ease: EASE },
};

/** A stage arriving in a money-flow chain — sideways, because the flow is. */
export const flowStageIn = {
  initial: { opacity: 0, x: -8 },
  animate: { opacity: 1, x: 0 },
  transition: { duration: DURATION.base, ease: EASE },
};

/** A hop in a vertical trace: drops in from above, like the money did. */
export const traceStepIn = {
  initial: { opacity: 0, y: -6 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: DURATION.fast, ease: EASE },
};

/** Left-anchored slide-over (the investigator opens over the record panel). */
export const slideOverWide = {
  initial: { x: 560, opacity: 0 },
  animate: { x: 0, opacity: 1 },
  exit: { x: 560, opacity: 0 },
  transition: { duration: DURATION.base, ease: EASE },
};

/**
 * A number that counts toward its value.
 *
 * Deliberately not a spring: an operator reading ₹1,240,332 wants it to
 * settle, not to overshoot and come back. Honours the OS reduced-motion
 * setting directly — `MotionConfig` cannot reach a plain rAF loop.
 */
export function useCountUp(value, duration = 520) {
  const target = Number.isFinite(value) ? value : 0;
  const [shown, setShown] = _useState(target);
  const fromRef = _useRef(target);

  _useEffect(() => {
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const from = fromRef.current;
    if (reduced || from === target || duration <= 0) {
      fromRef.current = target;
      setShown(target);
      return undefined;
    }
    let raf = 0;
    const started = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - started) / duration);
      // easeOutCubic — fast start, calm landing.
      const eased = 1 - Math.pow(1 - t, 3);
      setShown(from + (target - from) * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
      else fromRef.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);

  return shown;
}
