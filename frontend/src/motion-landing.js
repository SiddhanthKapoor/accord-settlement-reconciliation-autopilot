/**
 * Motion primitives for the landing page and the app shell chrome.
 *
 * Kept separate from `motion.js` (which the workspace components share) so
 * the marketing surface can have its own, slightly more expressive
 * vocabulary without changing how a panel opens inside the product.
 *
 * Two rules hold everywhere in here:
 *   1. Nothing exceeds 600ms, and nothing in the app shell exceeds 400ms.
 *      An operator working a queue of exceptions must never wait on us.
 *   2. Nothing gates content. Every reveal animates from `opacity: 0` to
 *      `1` on an element that is already in the DOM and already has its
 *      final layout, so text is selectable and links are clickable the
 *      moment they render.
 *
 * `MotionConfig reducedMotion="user"` at the app root strips the transform
 * half of these for anyone who asked the OS for reduced motion, so none of
 * this re-implements that check.
 */

import { useEffect, useRef, useState } from "react";
import { useInView } from "motion/react";

export const EASE = [0.22, 0.61, 0.36, 1];
export const EASE_SOFT = [0.16, 1, 0.3, 1];

export const D = {
  quick: 0.16,
  base: 0.28,
  slow: 0.42,
  hero: 0.55,
};

/* ------------------------------------------------------------------- hero */

/**
 * The logo is two strands resolving into one mark. The hero opens the same
 * way: the two halves of the headline block arrive from opposite sides and
 * settle onto a shared baseline, and the rule beneath them draws outward
 * from the centre as they meet.
 *
 * Under reduced motion the x-offsets are dropped by MotionConfig and this
 * degrades to a plain cross-fade, which is the right answer.
 */
export const heroStage = {
  hidden: {},
  show: { transition: { staggerChildren: 0.075, delayChildren: 0.06 } },
};

export const streamLeft = {
  hidden: { opacity: 0, x: -26 },
  show: { opacity: 1, x: 0, transition: { duration: D.hero, ease: EASE_SOFT } },
};

export const streamRight = {
  hidden: { opacity: 0, x: 26 },
  show: { opacity: 1, x: 0, transition: { duration: D.hero, ease: EASE_SOFT } },
};

export const riseUp = {
  hidden: { opacity: 0, y: 14 },
  show: { opacity: 1, y: 0, transition: { duration: D.slow, ease: EASE } },
};

/** The convergence rule: scales out from its own centre once the text lands. */
export const converge = {
  hidden: { opacity: 0, scaleX: 0 },
  show: {
    opacity: 1,
    scaleX: 1,
    transition: { duration: D.hero, ease: EASE_SOFT, delay: 0.1 },
  },
};

/* --------------------------------------------------------------- reveals */

/** Section reveal: stagger a group of children as the group scrolls in. */
export const revealGroup = {
  hidden: {},
  show: { transition: { staggerChildren: 0.06 } },
};

export const revealItem = {
  hidden: { opacity: 0, y: 18 },
  show: { opacity: 1, y: 0, transition: { duration: D.slow, ease: EASE } },
};

/**
 * `once: true` — a reveal that re-fires every time the section scrolls past
 * is a distraction on the second pass and motion sickness on the tenth.
 *
 * Two belts and braces, both there because a decorative animation must never
 * be the reason content cannot be read:
 *
 *   - If IntersectionObserver is unavailable the hook reports visible, rather
 *     than leaving every section at `opacity: 0` forever.
 *   - A plain geometry check runs alongside the observer. A reader who jumps
 *     rather than scrolls — an in-page anchor, End, a restored scroll
 *     position, a find-in-page hit — can land past a section without it ever
 *     intersecting the viewport, and the observer will never fire for it.
 *     Browser-testing this page caught exactly that: everything between the
 *     hero and the footer stayed invisible after a jump to the bottom.
 */
export function useReveal(amount = 0.2) {
  const ref = useRef(null);
  const supported = typeof IntersectionObserver !== "undefined";
  const inView = useInView(ref, { once: true, amount });
  const [passed, setPassed] = useState(false);

  useEffect(() => {
    if (passed) return undefined;
    let ticking = false;
    const measure = () => {
      ticking = false;
      const el = ref.current;
      if (!el) return;
      // Top edge above the fold covers both "arriving from below" and
      // "already scrolled clean past".
      if (el.getBoundingClientRect().top < window.innerHeight * 0.92) setPassed(true);
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(measure);
    };
    measure();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, [passed]);

  return [ref, supported ? inView || passed : true];
}

/* ------------------------------------------------------------ nav on scroll */

/** True once the page has scrolled past `threshold` — the nav gains a border. */
export function useScrolled(threshold = 8) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    let ticking = false;
    const read = () => {
      ticking = false;
      setScrolled(window.scrollY > threshold);
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(read);
    };
    read(); // a deep link can land mid-page
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [threshold]);
  return scrolled;
}

/* ------------------------------------------------------------- interaction */

/** Hover/press feedback. Deliberately small: this is a finance tool. */
export const lift = {
  whileHover: { y: -2 },
  whileTap: { y: 0, scale: 0.99 },
  transition: { duration: D.quick, ease: EASE },
};

export const press = {
  whileTap: { scale: 0.985 },
  transition: { duration: D.quick, ease: EASE },
};

/** App shell page change. Short — this one is on the critical path. */
export const shellTransition = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -4 },
  transition: { duration: 0.18, ease: EASE },
};
