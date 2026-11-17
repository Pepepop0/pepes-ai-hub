import { useEffect, useId, useState, type KeyboardEvent } from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { CornerDownLeft, Search, type LucideIcon } from 'lucide-react';
import { cn } from '../lib/utils';
import { Modal } from './ui';

export interface PaletteItem {
  id: string; group: string; label: string; hint?: string; keywords?: string;
  shortcut?: string; icon?: LucideIcon;
  /** Why it cannot run now; shown instead of running. */
  disabled?: string | false;
  run: () => void;
}

export const SHORTCUTS: [string, string][] = [
  ['Ctrl+K', 'Abrir a paleta de comandos'],
  ['Ctrl+1 … Ctrl+5', 'Ir para Visão geral, Instalações, Modelos, Atividade ou Configurações'],
  ['Ctrl+N', 'Nova instalação'],
  ['Ctrl+L', 'Logs da instalação em uso'],
  ['?', 'Mostrar estes atalhos'],
  ['Esc', 'Fechar diálogos e menus'],
];

const normalize = (text: string) => text.normalize('NFD').replace(/\p{Diacritic}/gu, '').toLocaleLowerCase('pt-BR');

/** A focused dialog may open from a palette choice; never pull focus back from it. */
function returnFocus(event: Event) {
  event.preventDefault();
  if (!document.querySelector('[role="dialog"][data-state="open"]')) document.getElementById('main-content')?.focus({preventScroll: true});
}

export function CommandPalette({open, onOpenChange, items}: {open: boolean; onOpenChange: (open: boolean) => void; items: PaletteItem[]}) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const listId = useId();
  const words = normalize(query).split(/\s+/).filter(Boolean);
  const visible = words.length ? items.filter(item => {
    const haystack = normalize(`${item.label} ${item.group} ${item.hint ?? ''} ${item.keywords ?? ''}`);
    return words.every(word => haystack.includes(word));
  }) : items;
  const optionId = (index: number) => `${listId}-option-${index}`;
  useEffect(() => { if (open) { setQuery(''); setActive(0); } }, [open]);
  useEffect(() => { document.getElementById(optionId(active))?.scrollIntoView({block: 'nearest'}); });

  function choose(item: PaletteItem | undefined) {
    if (!item || item.disabled) return;
    onOpenChange(false);
    // After the palette starts closing, so a dialog the item opens takes focus cleanly.
    requestAnimationFrame(() => item.run());
  }
  function move(step: number) {
    if (!visible.length) return;
    setActive(index => (index + step + visible.length) % visible.length);
  }
  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    const actions: Record<string, () => void> = {
      ArrowDown: () => move(1), ArrowUp: () => move(-1),
      Home: () => setActive(0), End: () => setActive(Math.max(0, visible.length - 1)),
      Enter: () => choose(visible[active]),
    };
    const action = actions[event.key];
    if (action) { event.preventDefault(); action(); }
  }

  let lastGroup = '';
  return <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="dialog-overlay" />
      <DialogPrimitive.Content className="palette" aria-describedby={undefined} onCloseAutoFocus={returnFocus}>
        <DialogPrimitive.Title className="sr-only">Paleta de comandos</DialogPrimitive.Title>
        <div className="palette-search">
          <Search size={17} aria-hidden="true" />
          <input role="combobox" aria-expanded="true" aria-controls={listId} aria-autocomplete="list"
            aria-activedescendant={visible.length ? optionId(active) : undefined} aria-label="Buscar comando"
            placeholder="Buscar ação, página ou instalação…" autoComplete="off" spellCheck={false}
            value={query} onChange={event => { setQuery(event.target.value); setActive(0); }} onKeyDown={onKeyDown} />
          <kbd>Esc</kbd>
        </div>
        <ul id={listId} role="listbox" aria-label="Comandos" className="palette-list">
          {visible.map((item, index) => {
            const header = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            const Icon = item.icon;
            return [
              header ? <li key={`group-${header}`} role="presentation" className="palette-group">{header}</li> : null,
              <li key={item.id} id={optionId(index)} role="option" aria-selected={index === active} aria-disabled={item.disabled ? true : undefined}
                className={cn('palette-option', index === active && 'palette-option-active', item.disabled && 'palette-option-disabled')}
                onMouseMove={() => setActive(index)} onClick={() => choose(item)}>
                {Icon ? <Icon size={16} aria-hidden="true" /> : null}
                <span className="grow"><span className="palette-label">{item.label}</span>{item.disabled ? <span className="palette-hint">{item.disabled}</span> : item.hint ? <span className="palette-hint" translate="no">{item.hint}</span> : null}</span>
                {item.shortcut ? <kbd>{item.shortcut}</kbd> : index === active && !item.disabled ? <CornerDownLeft size={14} aria-hidden="true" className="muted" /> : null}
              </li>,
            ];
          })}
        </ul>
        {!visible.length ? <p className="palette-empty muted">Nenhum comando corresponde a “{query}”.</p> : null}
        <div className="palette-footer"><span><kbd>↑</kbd><kbd>↓</kbd> navegar</span><span><kbd>Enter</kbd> executar</span><span><kbd>?</kbd> atalhos</span></div>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}

export function ShortcutsDialog({open, onOpenChange}: {open: boolean; onOpenChange: (open: boolean) => void}) {
  return <Modal open={open} onOpenChange={onOpenChange} title="Atalhos de teclado" description="Funcionam em qualquer tela, exceto enquanto você digita em um campo.">
    <dl className="shortcut-list">{SHORTCUTS.map(([keys, action]) => <div key={keys}><dt><kbd>{keys}</kbd></dt><dd>{action}</dd></div>)}</dl>
  </Modal>;
}
