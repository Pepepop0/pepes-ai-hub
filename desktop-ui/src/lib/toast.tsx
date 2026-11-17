import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react';
import { reducedMotion } from './motion';

export interface ToastInput { tone?: 'success' | 'danger' | 'neutral'; title: string; description?: string }
interface ToastItem extends ToastInput { id: number; leaving: boolean }

// Failures stay longer: they usually carry the next step to take.
const DURATION = {success: 4000, neutral: 4000, danger: 8000};
const EXIT_MS = 180;
const MAX_VISIBLE = 4;
const Context = createContext<((toast: ToastInput) => void) | null>(null);
let nextId = 0;

export function ToastProvider({children}: {children: ReactNode}) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const [paused, setPaused] = useState(false);
  const notify = useCallback((toast: ToastInput) => {
    setItems(list => [...list.slice(-(MAX_VISIBLE - 1)), {...toast, id: ++nextId, leaving: false}]);
  }, []);
  const dismiss = useCallback((id: number) => {
    setItems(list => list.map(item => item.id === id ? {...item, leaving: true} : item));
    window.setTimeout(() => setItems(list => list.filter(item => item.id !== id)), reducedMotion.matches ? 0 : EXIT_MS);
  }, []);
  return <Context.Provider value={notify}>
    {children}
    {createPortal(<section className="toast-region" aria-label="Notificações"
      onPointerEnter={() => setPaused(true)} onPointerLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPaused(false); }}>
      {/* The live region exists before any toast so additions are announced. */}
      <ol aria-live="polite" aria-relevant="additions">
        {items.map(item => <Toast key={item.id} item={item} paused={paused} onDismiss={dismiss} />)}
      </ol>
    </section>, document.body)}
  </Context.Provider>;
}

function Toast({item, paused, onDismiss}: {item: ToastItem; paused: boolean; onDismiss: (id: number) => void}) {
  const tone = item.tone ?? 'neutral';
  const remaining = useRef(DURATION[tone]);
  useEffect(() => {
    if (paused || item.leaving) return;
    const started = Date.now();
    const timer = window.setTimeout(() => onDismiss(item.id), remaining.current);
    return () => { window.clearTimeout(timer); remaining.current -= Date.now() - started; };
  }, [paused, item.leaving, item.id, onDismiss]);
  const Icon = tone === 'success' ? CheckCircle2 : tone === 'danger' ? CircleAlert : Info;
  return <li className={`toast toast-${tone}`} data-state={item.leaving ? 'closed' : 'open'}>
    <Icon size={18} className="toast-icon" aria-hidden="true" />
    <div className="grow"><strong>{item.title}</strong>{item.description ? <p>{item.description}</p> : null}</div>
    <button type="button" className="button button-ghost button-icon toast-close" aria-label="Dispensar notificação" onClick={() => onDismiss(item.id)}><X size={15} aria-hidden="true" /></button>
  </li>;
}

export function useToast() {
  const context = useContext(Context);
  if (!context) throw new Error('ToastProvider ausente.');
  return context;
}
