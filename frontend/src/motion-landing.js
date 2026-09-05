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
 * The logo is streams resolving into one mark, and the hero opens the same
 * way: the headline arrives from the left, along the direction the sources
 * travel in the figure beside it, and the supporting lines rise into place
 * behind it.
 *
 * Under reduced motion the offsets are dropped by MotionConfig and this
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

export const riseUp = {
  hidden: { opacity: 0, y: 14 },
  show: { opacity: 1, y: 0, transition: { duration: D.slow, ease: EASE } },
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
    let sweep = 0;
    let sweepUntil = 0;

    const measure = () => {
      ticking = false;
      const el = ref.current;
      if (!el) return true;
      // Top edge above the fold covers both "arriving from below" and
      // "already scrolled clean past".
      if (el.getBoundingClientRect().top < window.innerHeight * 0.92) {
        setPassed(true);
        return true;
      }
      return false;
    };

    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(measure);
    };

    /**
     * A smooth scroll started by `scrollIntoView` does fire scroll events,
     * but an instant jump — a hash the page was *loaded* on, a restored
     * position, find-in-page — can put a section on screen without firing
     * one at all. So after any such jump the geometry is polled for a beat
     * on rAF instead of waited for.
     */
    const startSweep = () => {
      sweepUntil = performance.now() + 1400;
      if (sweep) return;
      const tick = (now) => {
        sweep = 0;
        if (measure()) return;
        if (now < sweepUntil) sweep = window.requestAnimationFrame(tick);
      };
      sweep = window.requestAnimationFrame(tick);
    };

    startSweep();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    window.addEventListener("hashchange", startSweep);
    return () => {
      if (sweep) window.cancelAnimationFrame(sweep);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      window.removeEventListener("hashchange", startSweep);
    };
  }, [passed, ref]);

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

/* ------------------------------------------------------- viewport queries */

/**
 * Media query as state.
 *
 * The hero figure needs a *different board* below ~860px, not a scaled one
 * — nine file names squeezed into a phone column render at five pixels. So
 * the breakpoint has to be readable from JS, not only from CSS.
 *
 * `addListener` is kept as a fallback because Safari only gained
 * `addEventListener` on MediaQueryList in 14.
 */
export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return undefined;
    const mq = window.matchMedia(query);
    const read = () => setMatches(mq.matches);
    read();
    if (mq.addEventListener) {
      mq.addEventListener("change", read);
      return () => mq.removeEventListener("change", read);
    }
    mq.addListener(read);
    return () => mq.removeListener(read);
  }, [query]);

  return matches;
}

/**
 * True once `ref`'s bottom edge has passed `offset` from the top.
 *
 * Used by the landing nav, which floats over a dark hero and has to become
 * a light pill the moment the light sections arrive underneath it. A fixed
 * scroll threshold cannot do this: the hero's height depends on the
 * viewport, the type scale and whether the figure is on its compact board.
 */
export function useScrolledPast(ref, offset = 92) {
  const [past, setPast] = useState(false);

  useEffect(() => {
    let ticking = false;
    const read = () => {
      ticking = false;
      const el = ref.current;
      if (!el) return;
      setPast(el.getBoundingClientRect().bottom <= offset);
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(read);
    };
    read(); // a deep link or a restored position can land past the hero
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, [ref, offset]);

  return past;
}
