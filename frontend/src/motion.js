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
