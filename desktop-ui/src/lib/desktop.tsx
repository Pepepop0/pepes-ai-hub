import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { ApiError, command, getState, type Conflict, type DesktopState } from './api';
import { transition } from './motion';
import { useToast, type ToastInput } from './toast';

export type Connection = 'connecting' | 'connected' | 'offline';
/** A command in flight, or an operation it started that has not ended yet. */
export interface Work { name: string; args: Record<string, unknown> }
type Notice = string | Omit<ToastInput, 'tone'>;
export interface RunOptions {
  /** The failure is shown inside the open dialog (`error`) instead of as a toast. */
  inline?: boolean;
  /** Return true when the caller handled this failure itself (e.g. `stop_required`). */
  handle?: (failure: ApiError) => boolean;
  /** Toast on success; for commands that start an operation, when it succeeds. */
  success?: Notice;
  /** Toast title for a failure (or a failed operation). */
  failure?: string;
  /** The installation list changes; animate the state that reflects it. */
  reorder?: boolean;
}
interface Tracked extends Work { id: string; options: RunOptions; since: number }

interface DesktopContext {
  state: DesktopState | null; connection: Connection; connectionError: string | null; error: string | null;
  pending: string | null; busy: boolean; conflict: Conflict | null;
  /** True while `name` (any of them) runs and its arguments include every `match` entry. */
  working: (name: string | string[], match?: Record<string, unknown>) => boolean;
  run: <T = unknown>(name: string, args?: object, options?: RunOptions) => Promise<{value: T} | null>;
  dismissError: () => void; clearConflict: () => void;
}
const Context = createContext<DesktopContext | null>(null);
const POLL_MS = 1500;
// One failed poll can be a hiccup; the indicator turns red only after consecutive failures.
const OFFLINE_AFTER = 2;
// The state is a cached snapshot; an operation it never shows after this long has been missed.
const LOST_AFTER_MS = 10_000;
const terminal = (value?: string) => value === 'succeeded' || value === 'failed';
const toastFor = (value: Notice): ToastInput => ({tone: 'success', ...(typeof value === 'string' ? {title: value} : value)});
const signature = (state: DesktopState | null) => state?.installations.map(item => `${item.root}|${item.active}|${item.status}`).join('\n') ?? '';

export function DesktopProvider({children}: {children: ReactNode}) {
  const notify = useToast();
  const [state, setState] = useState<DesktopState | null>(null);
  const [connection, setConnection] = useState<Connection>('connecting');
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Work | null>(null);
  const [tracked, setTracked] = useState<Tracked | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const commandLock = useRef(false);
  const trackedRef = useRef<Tracked | null>(null);
  const stateRef = useRef<DesktopState | null>(null);
  // Set when a command changed the installation list; the first state polled after it animates.
  const reorderSince = useRef<number | null>(null);
  const wake = useRef<(delay?: number) => void>(() => undefined);
  // Preflight id last reported by /api/state; a conflict the server stops reporting is resolved there.
  const stateConflictId = useRef<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let inFlight = false;
    let again = false;
    let failures = 0;
    const abort = new AbortController();
    function apply(next: DesktopState, requestedAt: number) {
      const reported = next.conflict?.preflight_id ?? null;
      const previous = stateConflictId.current;
      if (next.conflict) setConflict(next.conflict);
      else if (previous) setConflict(current => current?.preflight_id === previous ? null : current);
      stateConflictId.current = reported;

      const own = trackedRef.current;
      const event = next.operation?.last_event;
      if (own && event?.operation_id === own.id && terminal(event.state)) {
        trackedRef.current = null; setTracked(null);
        if (event.state === 'succeeded') { if (own.options.success) notify(toastFor(own.options.success)); }
        else if (event.diagnostic?.code === 'cancelled') notify({tone: 'neutral', title: 'Instalação cancelada', description: event.message});
        else notify({tone: 'danger', title: own.options.failure ?? 'A operação falhou', description: event.message});
        if (own.options.reorder) reorderSince.current = requestedAt - 1;
      } else if (own && requestedAt - own.since > LOST_AFTER_MS && event?.operation_id !== own.id && !next.busy) {
        // The operation ended without this window seeing its snapshot; stop waiting for it.
        trackedRef.current = null; setTracked(null);
      }
      const changed = signature(stateRef.current) !== signature(next);
      // A snapshot taken before the command may not show its effect yet; wait briefly for one that does.
      const since = reorderSince.current;
      const animate = since !== null && requestedAt > since && changed;
      if (since !== null && requestedAt > since && (changed || requestedAt - since > LOST_AFTER_MS)) reorderSince.current = null;
      stateRef.current = next;
      if (animate) transition(() => setState(next), 'list');
      else setState(next);
    }
    async function poll() {
      window.clearTimeout(timer);
      if (inFlight) { again = true; return; }
      inFlight = true; again = false;
      const requestedAt = performance.now();
      try {
        const next = await getState(abort.signal);
        if (disposed) return;
        failures = 0;
        setConnection('connected'); setConnectionError(null);
        apply(next, requestedAt);
      } catch (failure) {
        if (disposed) return;
        failures += 1;
        if (failures >= OFFLINE_AFTER || (failure instanceof ApiError && failure.code === 'unauthorized')) {
          setConnection('offline');
          setConnectionError(failure instanceof Error ? failure.message : 'Não foi possível atualizar o estado local.');
        }
      } finally {
        inFlight = false;
        if (!disposed) timer = window.setTimeout(poll, again ? 0 : POLL_MS);
      }
    }
    wake.current = (delay = 0) => { window.clearTimeout(timer); timer = window.setTimeout(poll, delay); };
    void poll();
    return () => { disposed = true; abort.abort(); window.clearTimeout(timer); wake.current = () => undefined; };
  }, [notify]);

  const run = useCallback(async <T,>(name: string, args: object = {}, options: RunOptions = {}): Promise<{value: T} | null> => {
    if (commandLock.current) return null;
    commandLock.current = true;
    const work = {name, args: args as Record<string, unknown>};
    setPending(work); setError(null);
    try {
      const value = await command<T>(name, args);
      const operationId = (value as {operation_id?: unknown} | null)?.operation_id;
      if (typeof operationId === 'string') {
        const next = {...work, id: operationId, options, since: performance.now()};
        trackedRef.current = next; setTracked(next);
      } else {
        if (options.success) notify(toastFor(options.success));
        if (options.reorder) reorderSince.current = performance.now();
      }
      return {value};
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === 'conflict_required' && failure.details) setConflict(failure.details as Conflict);
      else if (failure instanceof ApiError && options.handle?.(failure)) { /* handled by the caller */ }
      else {
        const message = failure instanceof Error ? failure.message : 'A operação não pôde ser concluída.';
        if (options.inline) setError(message);
        else notify({tone: 'danger', title: options.failure ?? 'Não foi possível concluir a ação', description: message});
      }
      return null;
    } finally {
      commandLock.current = false; setPending(null);
      // The server refreshes its snapshot right after a command; fetch it soon, after a closing
      // dialog's exit animation, instead of showing a stale `busy` until the next regular poll.
      wake.current(250);
    }
  }, [notify]);

  const operationState = state?.operation?.last_event.state;
  const busy = Boolean(pending || tracked || state?.busy || (operationState && !terminal(operationState)));
  const work = pending ?? tracked;
  const working = useCallback((name: string | string[], match: Record<string, unknown> = {}) => Boolean(work
    && (typeof name === 'string' ? work.name === name : name.includes(work.name))
    && Object.entries(match).every(([key, value]) => work.args[key] === value)), [work]);
  useEffect(() => {
    if (!busy) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [busy]);
  const dismissError = useCallback(() => setError(null), []);
  const clearConflict = useCallback(() => setConflict(null), []);
  return <Context.Provider value={{state, connection, connectionError, error, pending: pending?.name ?? null, busy, conflict, working, run, dismissError, clearConflict}}>{children}</Context.Provider>;
}
export function useDesktop() {
  const context = useContext(Context);
  if (!context) throw new Error('DesktopProvider ausente.');
  return context;
}
