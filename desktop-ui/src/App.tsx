import { useEffect, useRef, useState } from 'react';
import { Activity as ActivityIcon, ArrowRight, ArrowRightLeft, Box, CheckCircle2, CircleAlert, FileText, FolderInput, FolderOpen, HardDrive, Keyboard, LayoutDashboard, MessageSquare, Monitor, Moon, PanelLeftClose, PanelLeftOpen, Play, Plus, RotateCw, Search, Settings as SettingsIcon, Shield, ShieldCheck, Square, Sun } from 'lucide-react';
import { DesktopProvider, useDesktop } from './lib/desktop';
import { ToastProvider } from './lib/toast';
import { transition, useRetained } from './lib/motion';
import { installationStatus, isRunning, shortPath } from './lib/utils';
import type { Installation, Selection } from './lib/api';
import { Button, Checkbox, ErrorNote, Modal, Skeleton } from './components/ui';
import { Overview, Models } from './components/dashboard';
import { Installations, type InstallationRequest } from './components/installations';
import { InstallWizard } from './components/install-wizard';
import { LogsDialog } from './components/logs';
import { CommandPalette, ShortcutsDialog, type PaletteItem } from './components/palette';
import { Activity, Settings, useTheme } from './components/settings';
import { PageSkeleton } from './components/skeletons';
import { useVerification } from './components/verify';

const navigation = [
  {id: 'overview', label: 'Visão geral', icon: LayoutDashboard, subtitle: 'Estado da instalação em uso, dos serviços e da GPU.'},
  {id: 'installations', label: 'Instalações', icon: HardDrive, subtitle: 'Pastas conhecidas pelo Hub: trocar, verificar, localizar ou excluir.'},
  {id: 'models', label: 'Modelos e perfis', icon: Box, subtitle: 'Perfis de instalação e modelos do catálogo desta versão.'},
  {id: 'activity', label: 'Atividade', icon: ActivityIcon, subtitle: 'Eventos das operações desta sessão e diagnóstico do ambiente.'},
  {id: 'settings', label: 'Configurações', icon: SettingsIcon, subtitle: 'Credenciais, pesquisa web, tema e versão do aplicativo.'},
];
// Faithful to assets/pets/claw/idle/01.txt.
const CLAW = '  (\\/)     (\\/)\n--(  o   o  )--\n    \\__~__/';

const PAGE_KEY = 'pepitos.desktop.page.v1';

/** The page in the address, else the one last visited, else the overview. */
function pageFromLocation() {
  const requested = new URLSearchParams(location.search).get('page') ?? localStorage.getItem(PAGE_KEY);
  return navigation.some(item => item.id === requested) ? requested! : 'overview';
}

/** Live summary of the installation in use, at the bottom of the sidebar. */
function SidebarInstallation({onNavigate, onInstall}: {onNavigate: (page: string) => void; onInstall: () => void}) {
  const {state, busy} = useDesktop();
  if (!state) return <div className="sidebar-installation" aria-hidden="true"><Skeleton className="skeleton-line" style={{width: '70%'}} /><Skeleton className="skeleton-line" style={{width: '50%'}} /></div>;
  const active = state.installations.find(item => item.active);
  if (!active) return <div className="sidebar-installation">
    <strong>{state.installations.length ? 'Nenhuma em uso' : 'Nenhuma instalação'}</strong>
    {state.installations.length
      ? <Button variant="secondary" size="sm" onClick={() => onNavigate('installations')}>Escolher instalação</Button>
      : <Button variant="secondary" size="sm" disabled={busy} onClick={onInstall}><Plus size={14} aria-hidden="true" />Nova instalação</Button>}
  </div>;
  const status = installationStatus(active);
  return <a className="sidebar-installation sidebar-installation-link" href="?page=installations" title={active.root} onClick={event => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); onNavigate('installations'); } }}>
    <span className="sidebar-installation-label">Em uso</span>
    <strong>{active.profile_name || active.profile_id || 'Instalação registrada'}</strong>
    <span className={`sidebar-installation-status tone-${status.tone}`}><span className={`status-dot ${status.live ? 'live-dot' : ''}`} aria-hidden="true" />{status.label}</span>
    <span className="path small muted" translate="no">{shortPath(active.root)}</span>
  </a>;
}

function Shell() {
  const {state, connection, connectionError, error, busy, pending, working, conflict, clearConflict, dismissError, run} = useDesktop();
  const {theme, setTheme} = useTheme();
  const {verify, dialog: verifyDialog} = useVerification();
  const [page, setPage] = useState(pageFromLocation);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [logs, setLogs] = useState<{key: number; root: string; name: string; open: boolean; service?: string} | null>(null);
  const [request, setRequest] = useState<InstallationRequest | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Kept mounted after closing so the dialog's exit animation can play; `key` resets it per opening.
  const [wizard, setWizard] = useState<{key: number; profile?: string; open: boolean} | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [stopConsent, setStopConsent] = useState(false);
  const shownConflict = useRetained(conflict);
  const current = navigation.find(item => item.id === page) || navigation[0];
  function navigate(next: string) {
    const url = new URL(location.href);
    url.searchParams.set('page', next);
    history.pushState(null, '', url.pathname + url.search);
    localStorage.setItem(PAGE_KEY, next);
    transition(() => { setPage(next); setSidebarOpen(false); }, 'page');
    window.scrollTo({top: 0});
    document.getElementById('main-content')?.focus({preventScroll: true});
  }
  function openWizard(profile?: string) {
    dismissError();
    setWizard(previous => ({key: (previous?.key ?? 0) + 1, profile, open: true}));
  }
  function closeWizard() { setWizard(previous => previous && {...previous, open: false}); }
  function showLogs(item?: Installation, service?: string) {
    const target = item ?? state?.installations.find(entry => entry.active);
    if (!target || target.status === 'missing') return;
    const name = target.profile_name || target.profile_id || 'Instalação';
    setLogs(previous => ({key: (previous?.key ?? 0) + 1, root: target.root, name, open: true, service}));
  }
  function askInstallations(next: InstallationRequest) {
    if (page !== 'installations') navigate('installations');
    setRequest(next);
  }
  // Shortcuts read the latest handlers without re-registering the listener on every render.
  const latest = useRef({navigate, openWizard, showLogs, busy, ready: Boolean(state)});
  latest.current = {navigate, openWizard, showLogs, busy, ready: Boolean(state)};
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      if (modifier && (key === 'k' || key === 'p')) { event.preventDefault(); setPaletteOpen(open => !open); return; }
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (document.querySelector('[role="dialog"][data-state="open"]')) return;
      const handlers = latest.current;
      if (modifier && /^[1-5]$/.test(event.key)) { event.preventDefault(); handlers.navigate(navigation[Number(event.key) - 1].id); }
      else if (modifier && key === 'n') { event.preventDefault(); if (handlers.ready && !handlers.busy) handlers.openWizard(); }
      else if (modifier && key === 'l') { event.preventDefault(); handlers.showLogs(); }
      else if (!modifier && !event.altKey && (event.key === '?' || event.key === 'F1')) { event.preventDefault(); setShortcutsOpen(true); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);
  useEffect(() => {
    const handler = () => setPage(pageFromLocation());
    window.addEventListener('popstate', handler);
    return () => window.removeEventListener('popstate', handler);
  }, []);
  useEffect(() => {
    if (!sidebarOpen) return;
    const handler = (event: KeyboardEvent) => { if (event.key === 'Escape') setSidebarOpen(false); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [sidebarOpen]);
  function finishConflict() {
    clearConflict();
    setSelection(null);
    setStopConsent(false);
  }
  async function cancelConflict() {
    const result = await run('cancel_pending', {}, {inline: true});
    if (result) finishConflict();
  }
  async function resolveConflict(policy: string) {
    if (!conflict) return;
    const result = await run<{status: string; message?: string}>('resolve_conflicts', {preflight_id: conflict.preflight_id, policy}, {inline: true});
    if (!result) return;
    if (result.value.status === 'proceed') {
      clearConflict();
      // A retry can preflight without any active installation or open wizard.
      // An old, closed wizard's selection must not replace that saved attempt.
      const retry = !wizard?.open ? state?.retryable_installation : null;
      const installed = retry
        ? await run('retry_installation', {operation_id: retry.operation_id}, {inline: true, success: 'Instalação concluída', failure: 'A nova tentativa não foi concluída'})
        : selection ? await run('install', selection, {inline: true, success: 'Instalação concluída', failure: 'A instalação falhou'}) : null;
      if (installed) { closeWizard(); finishConflict(); navigate('activity'); }
    } else if (result.value.status === 'aborted') finishConflict();
  }
  const policies = shownConflict?.available_policies ?? [];
  const connectionText = connection === 'offline' ? 'Sem conexão com o serviço local' : connection === 'connecting' ? 'Conectando…' : 'Conectado';
  const activeInstallation = state?.installations.find(item => item.active);
  const ready = Boolean(state?.installation.ready);
  const servicesUp = state?.services.services.some(item => isRunning(item.health)) ?? false;
  const chatReady = state?.services.services.some(item => item.service_id === 'open-webui' && isRunning(item.health)) ?? false;
  const whileBusy: string | false = busy ? 'Aguarde a operação atual terminar.' : false;
  const notReady: string | false = !state ? 'Carregando o estado do Hub.' : !ready ? 'Conclua ou escolha uma instalação primeiro.' : false;
  const paletteItems: PaletteItem[] = [
    ...navigation.map((item, index) => ({id: `page-${item.id}`, group: 'Ir para', label: item.label, hint: item.subtitle, icon: item.icon, shortcut: `Ctrl+${index + 1}`, run: () => navigate(item.id)})),
    {id: 'new', group: 'Ações', label: 'Nova instalação', icon: Plus, shortcut: 'Ctrl+N', disabled: !state ? 'Carregando o estado do Hub.' : whileBusy, run: () => openWizard()},
    {id: 'register', group: 'Ações', label: 'Registrar instalação existente', keywords: 'cadastrar pasta importar', icon: FolderInput, disabled: whileBusy, run: () => askInstallations({kind: 'register'})},
    servicesUp
      ? {id: 'stop', group: 'Ações', label: 'Parar serviços', icon: Square, disabled: notReady || whileBusy, run: () => void run('stop', {}, {success: 'Serviços parados', failure: 'Não foi possível parar os serviços'})}
      : {id: 'start', group: 'Ações', label: 'Iniciar serviços', icon: Play, disabled: notReady || whileBusy, run: () => void run('start', {}, {success: 'Serviços iniciados', failure: 'Não foi possível iniciar os serviços'})},
    {id: 'restart', group: 'Ações', label: 'Reiniciar serviços', icon: RotateCw, disabled: notReady || whileBusy, run: () => void run('restart', {}, {success: 'Serviços reiniciados', failure: 'Não foi possível reiniciar os serviços'})},
    {id: 'chat', group: 'Ações', label: 'Abrir conversa (Open WebUI)', keywords: 'chat', icon: MessageSquare, disabled: !chatReady ? 'O Open WebUI não está em execução.' : whileBusy, run: () => void run('open_chat', {}, {failure: 'Não foi possível abrir a conversa'})},
    {id: 'verify', group: 'Ações', label: 'Verificar instalação em uso', keywords: 'checar somente leitura', icon: ShieldCheck, disabled: !activeInstallation ? 'Nenhuma instalação em uso.' : whileBusy, run: () => void verify()},
    {id: 'logs', group: 'Ações', label: 'Logs da instalação em uso', icon: FileText, shortcut: 'Ctrl+L', disabled: !activeInstallation || activeInstallation.status === 'missing' ? 'Nenhuma instalação em uso disponível.' : false, run: () => showLogs()},
    {id: 'folder', group: 'Ações', label: 'Abrir pasta da instalação em uso', icon: FolderOpen, disabled: !activeInstallation || activeInstallation.status === 'missing' ? 'Nenhuma instalação em uso disponível.' : false, run: () => void run('open_folder', {root: activeInstallation!.root, kind: 'root'}, {failure: 'Não foi possível abrir a pasta'})},
    ...(state?.installations ?? []).filter(item => !item.active).map(item => ({
      id: `use-${item.root}`, group: 'Instalações', label: `Usar ${item.profile_name || item.profile_id || 'instalação'}`, hint: item.root, icon: ArrowRightLeft,
      disabled: item.status === 'missing' || item.status === 'unreadable' ? installationStatus(item).label : whileBusy,
      run: () => askInstallations({kind: 'switch', root: item.root}),
    })),
    {id: 'theme-dark', group: 'Aparência', label: 'Tema escuro', icon: Moon, disabled: theme === 'dark' ? 'Já está em uso.' : false, run: () => setTheme('dark')},
    {id: 'theme-light', group: 'Aparência', label: 'Tema claro', icon: Sun, disabled: theme === 'light' ? 'Já está em uso.' : false, run: () => setTheme('light')},
    {id: 'theme-system', group: 'Aparência', label: 'Tema do sistema', icon: Monitor, disabled: theme === 'system' ? 'Já está em uso.' : false, run: () => setTheme('system')},
    {id: 'shortcuts', group: 'Ajuda', label: 'Atalhos de teclado', icon: Keyboard, shortcut: '?', run: () => setShortcutsOpen(true)},
  ];
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Ir para o conteúdo</a>
    {sidebarOpen ? <button type="button" className="sidebar-scrim" aria-label="Fechar navegação" onClick={() => setSidebarOpen(false)} /> : null}
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`} aria-label="Barra lateral">
      <a className="brand" href="?page=overview" onClick={event => { event.preventDefault(); navigate('overview'); }} aria-label="Pepito’s AI Hub — visão geral">
        <pre className="claw-mark" aria-hidden="true">{CLAW}</pre>
        <span><strong translate="no">Pepito’s</strong><span translate="no">AI HUB</span></span>
      </a>
      <nav aria-label="Navegação principal">
        {navigation.map((item, index) => <a href={`?page=${item.id}`} title={`${item.label} (Ctrl+${index + 1})`} className={`nav-item ${current.id === item.id ? 'active' : ''}`} key={item.id} aria-current={current.id === item.id ? 'page' : undefined} onClick={event => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); navigate(item.id); } }}>
          {current.id === item.id ? <span className="nav-indicator" aria-hidden="true" /> : null}
          <item.icon size={18} aria-hidden="true" /><span>{item.label}</span>
          {item.id === 'activity' && busy ? <span className="nav-busy" role="img" aria-label="Operação em andamento" /> : null}
        </a>)}
      </nav>
      <div className="sidebar-bottom">
        <SidebarInstallation onNavigate={navigate} onInstall={() => openWizard()} />
        <div className="sidebar-footer"><span translate="no">PEPITO’S AI HUB</span><span className="tabular">{state ? `v${state.version}` : '—'}</span></div>
      </div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <div className="inline">
          <Button className="nav-toggle" variant="ghost" size="icon" aria-label={sidebarOpen ? 'Fechar navegação' : 'Abrir navegação'} aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(!sidebarOpen)}>{sidebarOpen ? <PanelLeftClose size={20} aria-hidden="true" /> : <PanelLeftOpen size={20} aria-hidden="true" />}</Button>
          <span className="topbar-title">{current.label}</span>
        </div>
        <div className="inline">
          <button type="button" className="palette-trigger" onClick={() => setPaletteOpen(true)} aria-keyshortcuts="Control+K"><Search size={14} aria-hidden="true" /><span>Buscar ações…</span><kbd>Ctrl K</kbd></button>
          <div className={`topbar-status connection-${connection}`} role="status">
            <span className="status-dot" aria-hidden="true" /><span>{connectionText}</span>
            {connection === 'connected' && busy ? <span className="topbar-busy">· Operação em andamento</span> : null}
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex={-1}>
        <div className="page">
          <div className="page-heading">
            <div><h1>{current.label}</h1><p className="muted">{current.subtitle}</p></div>
            {current.id === 'overview' ? <Button variant="secondary" disabled={busy || !state} onClick={() => openWizard()}><Plus size={16} aria-hidden="true" />Nova instalação</Button> : null}
          </div>
          {connectionError && state ? <div className="notice notice-danger" role="alert"><CircleAlert size={18} aria-hidden="true" /><span>{connectionError} A atualização automática continua tentando. Comandos ficam indisponíveis até a conexão voltar.</span></div> : null}
          {state ? <div className="page-content" inert={connection === 'offline' ? true : undefined}>
            {current.id === 'overview' ? <Overview onInstall={profile => openWizard(profile)} onNavigate={navigate} onShowLogs={service => showLogs(undefined, service)} />
              : current.id === 'installations' ? <Installations onInstall={() => openWizard()} onShowLogs={showLogs} request={request} onRequestDone={() => setRequest(null)} />
              : current.id === 'models' ? <Models onInstall={profile => openWizard(profile)} onNavigate={navigate} />
              : current.id === 'activity' ? <Activity />
              : <Settings theme={theme} setTheme={setTheme} />}
          </div>
            : connection === 'offline' ? <div className="loading-state" role="alert"><CircleAlert size={32} className="text-danger" aria-hidden="true" /><h2>O serviço local não está disponível</h2><p className="muted">{connectionError} A conexão é verificada automaticamente; se o problema continuar, feche esta janela e abra o Pepito’s AI Hub novamente.</p></div>
            : <PageSkeleton page={current.id} />}
        </div>
        <footer className="main-footer"><span><Shield size={12} aria-hidden="true" /> Execução local · Downloads podem usar a internet</span></footer>
      </main>
    </div>
    {wizard ? <InstallWizard key={wizard.key} open={wizard.open && conflict === null} onOpenChange={open => { if (!open) closeWizard(); }} initialProfile={wizard.profile} onSubmitted={setSelection} /> : null}
    <Modal open={conflict !== null} onOpenChange={open => { if (!open && !pending) void cancelConflict(); }} title="Há serviços externos em execução" description="O Hub encontrou recursos ocupados por outros programas. Escolha como continuar; nada foi alterado ainda.">
      {shownConflict?.conflicts.map(item => <div className="conflict-row" key={`${item.service_id}-${item.endpoint.port}`}>
        <div className="split"><strong translate="no">{item.service_id}</strong><code translate="no">{item.endpoint.host}:{item.endpoint.port}</code></div>
        <p className="muted small">{item.reason}</p>
        {item.managed_endpoint ? <p className="small">Endpoint isolado disponível: <code translate="no">{item.managed_endpoint.host}:{item.managed_endpoint.port}</code></p> : null}
        {!item.can_stop_safely ? <p className="small muted">Este serviço não pode ser interrompido com segurança pelo Hub.</p> : null}
      </div>)}
      {shownConflict?.hard_blockers.map(item => <ErrorNote key={item.code} message={item.message} />)}
      {!selection ? <div className="notice">O pedido de instalação original não está nesta janela. Cancele esta solicitação e abra o assistente novamente para revisar perfil e pasta.</div> : null}
      {policies.includes('stop-conflicting-external') ? <Checkbox checked={stopConsent} onCheckedChange={setStopConsent} disabled={Boolean(pending)}>Autorizo interromper os serviços externos identificados. Outros aplicativos e tarefas podem ser afetados.</Checkbox> : null}
      <ErrorNote message={error} />
      <div className="dialog-footer wrap">
        <Button variant="secondary" disabled={Boolean(pending)} busy={pending === 'cancel_pending'} busyLabel="Cancelando…" onClick={() => void cancelConflict()}>Cancelar instalação</Button>
        {policies.includes('stop-conflicting-external') ? <Button variant="destructive" disabled={Boolean(pending) || !stopConsent || !selection} busy={working('resolve_conflicts', {policy: 'stop-conflicting-external'})} busyLabel="Parando externos…" onClick={() => void resolveConflict('stop-conflicting-external')}>Parar externos e continuar</Button> : null}
        {policies.includes('install-alongside') ? <Button disabled={Boolean(pending) || !selection} busy={working('resolve_conflicts', {policy: 'install-alongside'}) || pending === 'install'} busyLabel="Continuando…" onClick={() => void resolveConflict('install-alongside')}><CheckCircle2 size={16} aria-hidden="true" />Instalar lado a lado<ArrowRight size={16} aria-hidden="true" /></Button> : null}
      </div>
    </Modal>
    {logs ? <LogsDialog key={logs.key} root={logs.root} name={logs.name} initialService={logs.service ?? null} open={logs.open} onOpenChange={open => { if (!open) setLogs(previous => previous && {...previous, open: false}); }} /> : null}
    <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} items={paletteItems} />
    <ShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
    {verifyDialog}
  </div>;
}

export default function App() {
  return <ToastProvider><DesktopProvider><Shell /></DesktopProvider></ToastProvider>;
}
