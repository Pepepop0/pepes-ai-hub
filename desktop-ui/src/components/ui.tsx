import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import * as DropdownPrimitive from '@radix-ui/react-dropdown-menu';
import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import * as ProgressPrimitive from '@radix-ui/react-progress';
import { cva, type VariantProps } from 'class-variance-authority';
import { Check, ChevronDown, LoaderCircle, MoreHorizontal, X } from 'lucide-react';
import { cn } from '../lib/utils';

const buttonVariants = cva('button', { variants: { variant: { default: 'button-primary', secondary: 'button-secondary', ghost: 'button-ghost', destructive: 'button-destructive' }, size: { default: '', sm: 'button-sm', icon: 'button-icon' } }, defaultVariants: {variant: 'default', size: 'default'} });
/**
 * `busy` shows a spinner and `busyLabel` (ending in “…”) at once. A busy button stays focusable
 * (aria-disabled, clicks ignored) so focus is not lost while its command runs and a dialog it
 * opens can return focus to it.
 */
export function Button({className, variant, size, busy, busyLabel, children, disabled, type = 'button', onClick, ...props}: React.ComponentProps<'button'> & VariantProps<typeof buttonVariants> & {busy?: boolean; busyLabel?: string}) {
  return <button type={type} className={cn(buttonVariants({variant, size}), className)} disabled={busy ? undefined : disabled} aria-disabled={busy || undefined} aria-busy={busy || undefined} data-busy={busy || undefined} onClick={busy ? event => event.preventDefault() : onClick} {...props}>
    {busy ? <><LoaderCircle className="spin" size={size === 'sm' ? 15 : 16} aria-hidden="true" />{busyLabel ?? (size === 'icon' ? null : children)}</> : children}
  </button>;
}
export type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'accent';
export function Badge({children, tone = 'neutral', live}: {children: React.ReactNode; tone?: Tone; live?: boolean}) {
  return <span className={`badge badge-${tone}`}>{live ? <span className="status-dot live-dot" aria-hidden="true" /> : null}{children}</span>;
}
// Toasts stay usable while a dialog is open; interacting with them must not close it.
function keepToasts(event: {target: EventTarget | null; preventDefault: () => void}) {
  if (event.target instanceof Element && event.target.closest('.toast-region')) event.preventDefault();
}
/**
 * Focus returns to what opened the dialog. A dialog opened from a menu item returns to the
 * menu's trigger (the item is gone); if that control no longer exists, to the main region.
 */
export function Modal({open, onOpenChange, title, description, children, className}: {open: boolean; onOpenChange: (open: boolean) => void; title: string; description: React.ReactNode; children: React.ReactNode; className?: string}) {
  const returnTo = React.useRef<HTMLElement | null>(null);
  React.useLayoutEffect(() => {
    if (!open) return;
    const active = document.activeElement as HTMLElement | null;
    const menu = active?.closest('[role="menu"]');
    returnTo.current = menu ? document.getElementById(menu.getAttribute('aria-labelledby') ?? '') : active;
  }, [open]);
  function restoreFocus(event: Event) {
    event.preventDefault();
    const target = returnTo.current;
    if (target?.isConnected && !(target as HTMLButtonElement).disabled) target.focus();
    else document.getElementById('main-content')?.focus({preventScroll: true});
  }
  return <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}><DialogPrimitive.Portal><DialogPrimitive.Overlay className="dialog-overlay" /><DialogPrimitive.Content className={cn('dialog-content', className)} onPointerDownOutside={keepToasts} onInteractOutside={keepToasts} onCloseAutoFocus={restoreFocus}><div className="dialog-heading"><div><DialogPrimitive.Title className="dialog-title">{title}</DialogPrimitive.Title><DialogPrimitive.Description className="muted">{description}</DialogPrimitive.Description></div><DialogPrimitive.Close className="button button-ghost button-icon" aria-label="Fechar janela"><X size={18} aria-hidden="true" /></DialogPrimitive.Close></div>{children}</DialogPrimitive.Content></DialogPrimitive.Portal></DialogPrimitive.Root>;
}
export function Checkbox({checked, onCheckedChange, children, disabled}: {checked: boolean; onCheckedChange: (checked: boolean) => void; children: React.ReactNode; disabled?: boolean}) {
  const id = React.useId();
  return <label htmlFor={id} className="checkbox-label"><CheckboxPrimitive.Root id={id} className="checkbox" checked={checked} disabled={disabled} onCheckedChange={value => onCheckedChange(value === true)}><CheckboxPrimitive.Indicator className="checkbox-indicator"><Check size={13} aria-hidden="true" /></CheckboxPrimitive.Indicator></CheckboxPrimitive.Root><span>{children}</span></label>;
}
export function Progress({value, label}: {value: number | null; label: string}) {
  return <ProgressPrimitive.Root className={cn('progress', value === null && 'progress-unknown')} value={value} aria-label={label}><ProgressPrimitive.Indicator className="progress-indicator" style={value === null ? undefined : {transform: `translateX(-${100 - Math.min(100, Math.max(0, value))}%)`}} /></ProgressPrimitive.Root>;
}
export function ActionMenu({children, label = 'Ações', busy, busyLabel, compact}: {children: React.ReactNode; label?: string; busy?: boolean; busyLabel?: string; compact?: boolean}) {
  const [open, setOpen] = React.useState(false);
  // A busy trigger refuses to open; closing is always honoured (item selection flushes `busy` first).
  const trigger = compact
    ? <Button variant="ghost" size="icon" busy={busy} aria-label={busy && busyLabel ? busyLabel : label} title={label}><MoreHorizontal size={16} aria-hidden="true" /></Button>
    : <Button variant="secondary" size="sm" busy={busy} busyLabel={busyLabel}>{label}<ChevronDown size={14} aria-hidden="true" /></Button>;
  return <DropdownPrimitive.Root open={open} onOpenChange={value => setOpen(value && !busy)}><DropdownPrimitive.Trigger asChild>{trigger}</DropdownPrimitive.Trigger><DropdownPrimitive.Portal><DropdownPrimitive.Content className="dropdown-content" align="end" sideOffset={6}>{children}</DropdownPrimitive.Content></DropdownPrimitive.Portal></DropdownPrimitive.Root>;
}
export function MenuItem({children, onSelect, disabled, danger}: {children: React.ReactNode; onSelect: () => void; disabled?: boolean; danger?: boolean}) {
  return <DropdownPrimitive.Item className={cn('dropdown-item', danger && 'text-danger')} onSelect={onSelect} disabled={disabled}>{children}</DropdownPrimitive.Item>;
}
export const MenuSeparator = DropdownPrimitive.Separator;
export function Empty({icon, title, description, children}: {icon: React.ReactNode; title: string; description: string; children?: React.ReactNode}) {
  return <div className="empty"><div className="empty-icon" aria-hidden="true">{icon}</div><h3>{title}</h3><p className="muted">{description}</p>{children}</div>;
}
export function ErrorNote({message}: {message: string | null | undefined}) {
  return message ? <div className="notice notice-danger" role="alert">{message}</div> : null;
}
/** Placeholder block matching the shape of content that has not loaded yet. */
export function Skeleton({className, style}: {className?: string; style?: React.CSSProperties}) {
  return <span className={cn('skeleton', className)} style={style} aria-hidden="true" />;
}
