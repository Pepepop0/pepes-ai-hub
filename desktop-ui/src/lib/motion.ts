import { useState } from 'react';
import { flushSync } from 'react-dom';

/** Which part of the screen a view transition animates; scopes the CSS names in styles.css. */
export type TransitionKind = 'page' | 'list' | 'tabs';

export const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let sequence = 0;

/**
 * Apply a React state update inside a native view transition when the browser supports it
 * and the person has not asked for reduced motion; otherwise apply it directly.
 */
export function transition(update: () => void, kind: TransitionKind) {
  if (typeof document.startViewTransition !== 'function' || reducedMotion.matches || document.visibilityState !== 'visible') {
    update();
    return;
  }
  const root = document.documentElement;
  const token = ++sequence;
  root.dataset.transition = kind;
  const view = document.startViewTransition(() => flushSync(update));
  view.ready.catch(() => undefined);
  view.finished.catch(() => undefined).finally(() => {
    if (sequence === token) delete root.dataset.transition;
  });
}

/** Stable, CSS-safe view-transition-name for an arbitrary key such as a folder path. */
export function transitionName(prefix: string, key: string) {
  let hash = 2166136261;
  for (const char of key) hash = Math.imul(hash ^ char.codePointAt(0)!, 16777619);
  return `${prefix}-${(hash >>> 0).toString(36)}`;
}

/** Last non-null value, so a closing dialog keeps its content while the exit animation plays. */
export function useRetained<T>(value: T | null): T | null {
  const [kept, setKept] = useState(value);
  if (value !== null && value !== kept) setKept(value);
  return value ?? kept;
}
