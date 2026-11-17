import { useDeferredValue, useEffect, useState } from 'react';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { ArrowRight, Box, Copy, Cpu, Download, ExternalLink, FileText, FolderInput, HardDrive, LoaderCircle, MessageSquare, Play, Plus, RotateCw, Search, Server, ShieldCheck, Square, Terminal, TriangleAlert, Wrench } from 'lucide-react';
import { command, type ServiceTarget } from '../lib/api';
import { useCopy } from '../lib/clipboard';
import { useDesktop } from '../lib/desktop';
import { transition } from '../lib/motion';
import { bytes, installationStatus, isRunning, label, number, profileCompatibility, profileUpdateHint, shortPath, versionLabel } from '../lib/utils';
import { ActionMenu, Badge, Button, Empty, MenuItem, MenuSeparator, Progress } from './ui';
import { OperationCard } from './operation';
import { useVerification } from './verify';
import { ChatModelsPanel, UncensoredSwitch } from './chat-models';

const MODELS_TAB_KEY = 'pepitos.desktop.models-tab.v1';

/** Where each service of the installation in use listens. Fetched when health or selection changes. */
function useServiceTargets() {
  const {state} = useDesktop();
  const [targets, setTargets] = useState<ServiceTarget[]>([]);
  const key = state ? `${state.installations.find(item => item.active)?.root ?? ''}|${state.services.services.map(item => `${item.service_id}:${item.health}`).join(',')}` : '';
  useEffect(() => {
    if (!key) return;
    let current = true;
    command<ServiceTarget[]>('service_targets').then(rows => { if (current) setTargets(rows); }, () => { if (current) setTargets([]); });
    return () => { current = false; };
  }, [key]);
  return targets;
}

const serviceInfo: Record<string, {name: string; text: string; Icon: typeof Terminal}> = {
  ollama: {name: 'Ollama', text: 'Modelos de linguagem', Icon: Terminal},
  comfyui: {name: 'ComfyUI', text: 'Geração de imagens e workflows', Icon: Box},
  'open-webui': {name: 'Open WebUI', text: 'Interface de conversa', Icon: MessageSquare},
};

/** What the installation in use needs right now: create, fix, finish, or use it. */
function InstallationPanel({onInstall, onNavigate}: {onInstall: (profile?: string) => void; onNavigate: (page: string) => void}) {
  const {state, busy, run, working} = useDesktop();
  const {verify, verifying, dialog} = useVerification();
  if (!state) return null;
  const active = state.installations.find(item => item.active);
  const view = state.installation;
  const name = state.profiles.find(item => item.id === view.profile_id)?.name || active?.profile_name || view.profile_id || 'Instalação';
  const version = active?.profile_version ? <Badge>{versionLabel(active.profile_version)}</Badge> : null;
  const updateHint = active ? profileUpdateHint(active) : null;
  const root = active?.root ?? view.root_display;
  const location = root ? <p className="path panel-path" translate="no" title={root}>{root}</p> : null;
  const services = state.services.services;
  const running = services.some(item => isRunning(item.health));
  const chatReady = services.some(item => item.service_id === 'open-webui' && isRunning(item.health));

  if (view.checking) return <section className="panel card" aria-busy="true">
    <div className="panel-icon" aria-hidden="true"><LoaderCircle size={22} className="spin" /></div>
    <div className="grow"><h2>Verificando a instalação em uso…</h2><p className="muted">O Hub está lendo a configuração e o estado dos serviços.</p></div>
  </section>;

  if (!view.profile_id && !active) {
    const known = state.installations.length;
    return <section className="panel card">
      <div className="panel-icon" aria-hidden="true"><HardDrive size={22} /></div>
      <div className="grow">
        <h2>{known ? 'Nenhuma instalação em uso' : 'Nenhuma instalação configurada'}</h2>
        <p className="muted">{known ? `${number(known)} instalação(ões) registrada(s). Escolha qual usar na página Instalações.` : 'Escolha um perfil compatível com esta máquina e uma pasta. Nada é baixado antes da revisão.'}</p>
        <div className="actions">
          {known ? <Button onClick={() => onNavigate('installations')}><HardDrive size={16} aria-hidden="true" />Ver instalações</Button>
            : <><Button disabled={busy} onClick={() => onInstall()}><Download size={16} aria-hidden="true" />Nova instalação</Button><Button variant="secondary" onClick={() => onNavigate('installations')}><FolderInput size={16} aria-hidden="true" />Registrar existente</Button></>}
        </div>
      </div>
    </section>;
  }

  if (active && (active.status === 'missing' || active.status === 'unreadable')) return <section className="panel card panel-warning">
    <div className="panel-icon" aria-hidden="true"><TriangleAlert size={22} /></div>
    <div className="grow">
      <div className="inline wrap"><h2>{name}</h2>{version}<Badge tone="warning">{installationStatus(active).label}</Badge></div>
      {location}
      <p className="muted">{active.detail || 'A instalação em uso não pode ser lida.'} Localize a pasta movida ou escolha outra instalação.</p>
      <div className="actions"><Button onClick={() => onNavigate('installations')}><HardDrive size={16} aria-hidden="true" />Abrir instalações</Button></div>
    </div>
  </section>;

  if (!view.ready) return <section className="panel card panel-warning">
    <div className="panel-icon" aria-hidden="true"><TriangleAlert size={22} /></div>
    <div className="grow">
      <div className="inline wrap"><h2>Instalação incompleta</h2><Badge tone="accent">Em uso</Badge></div>
      <p className="panel-name">{name}{active?.profile_version ? ` ${versionLabel(active.profile_version)}` : ''}</p>
      {location}
      <p className="muted">O Hub não encontrou todos os componentes desta instalação. <strong>Verificar</strong> lista o que falta sem alterar nada; <strong>Concluir instalação</strong> baixa e configura o que estiver pendente.</p>
      <div className="actions">
        <Button variant="secondary" disabled={busy} busy={verifying} busyLabel="Verificando…" onClick={() => void verify()}><ShieldCheck size={16} aria-hidden="true" />Verificar</Button>
        <Button disabled={busy} busy={working('repair')} busyLabel="Concluindo instalação…" onClick={() => void run('repair', {}, {success: 'Instalação concluída', failure: 'A conclusão da instalação falhou'})}><Wrench size={16} aria-hidden="true" />Concluir instalação</Button>
      </div>
      <UncensoredSwitch />
    </div>
    {dialog}
  </section>;

  const status = active ? installationStatus(active) : null;
  return <section className="panel card">
    <div className="panel-icon panel-icon-accent" aria-hidden="true"><HardDrive size={22} /></div>
    <div className="grow">
      <div className="inline wrap"><h2>{name}</h2>{version}{status ? <Badge tone={status.tone} live={status.live}>{status.label}</Badge> : null}</div>
      {location}
      <div className="actions">
        <Button disabled={busy || !chatReady} busy={working('open_chat')} busyLabel="Abrindo conversa…" onClick={() => void run('open_chat', {}, {failure: 'Não foi possível abrir a conversa'})}><MessageSquare size={16} aria-hidden="true" />Abrir conversa<ArrowRight size={16} aria-hidden="true" /></Button>
        {running
          ? <Button variant="secondary" disabled={busy} busy={working('stop')} busyLabel="Parando serviços…" onClick={() => void run('stop', {}, {success: 'Serviços parados', failure: 'Não foi possível parar os serviços'})}><Square size={15} aria-hidden="true" />Parar serviços</Button>
          : <Button variant="secondary" disabled={busy} busy={working('start')} busyLabel="Iniciando serviços…" onClick={() => void run('start', {}, {success: 'Serviços iniciados', failure: 'Não foi possível iniciar os serviços'})}><Play size={15} aria-hidden="true" />Iniciar serviços</Button>}
      </div>
      {!chatReady ? <p className="small muted panel-hint">A conversa fica disponível quando o Open WebUI estiver em execução.</p> : null}
      {updateHint ? <p className="small muted panel-hint">{updateHint}</p> : null}
      <UncensoredSwitch />
      <p className="small muted panel-hint">{state.settings.services_lifetime}</p>
    </div>
  </section>;
}

export function Overview({onInstall, onNavigate, onShowLogs}: {onInstall: (profile?: string) => void; onNavigate: (page: string) => void; onShowLogs: (service?: string) => void}) {
  const {state, busy, run, working, pending} = useDesktop();
  const targets = useServiceTargets();
  const copy = useCopy();
  if (!state) return null;
  const active = state.installations.find(item => item.active);
  const profile = state.profiles.find(item => item.id === state.installation.profile_id);
  const services = state.services.services;
  const available = services.filter(item => isRunning(item.health)).length;
  const vram = state.vram;
  const used = vram.total_mib != null && vram.free_mib != null ? Math.max(0, vram.total_mib - vram.free_mib) : null;
  return <>
    <InstallationPanel onInstall={onInstall} onNavigate={onNavigate} />
    <div className="stats-grid">
      <section className="card stat"><div className="split"><span className="muted">Instalação em uso</span><HardDrive size={18} aria-hidden="true" /></div><strong><span className="value-swap" key={active?.root ?? 'none'}>{profile?.name || active?.profile_name || 'Nenhuma'}</span></strong>{active ? <span className="small muted path" translate="no" title={active.root}>{active.profile_version ? `${versionLabel(active.profile_version)} · ` : ''}{shortPath(active.root)}</span> : <span className="small muted">Nenhuma instalação em uso</span>}</section>
      <section className="card stat"><div className="split"><span className="muted">Serviços em execução</span><Server size={18} aria-hidden="true" /></div><strong className="tabular"><span className="value-swap" key={available}>{available}</span><span className="stat-unit"> / {services.length}</span></strong><span className="small muted">{services.length ? 'Observado agora no ambiente local' : 'Nenhum serviço configurado'}</span></section>
      <section className="card stat"><div className="split"><span className="muted">Memória da GPU</span><Cpu size={18} aria-hidden="true" /></div><strong className="tabular">{vram.total_mib != null ? `${number(vram.total_mib / 1024)} GB` : 'Não detectada'}</strong>
        {used !== null && vram.total_mib ? <><Progress value={used / vram.total_mib * 100} label="Memória de GPU em uso" /><span className="small muted tabular">{number(used / 1024)} GB em uso · {number((vram.free_mib ?? 0) / 1024)} GB livres</span></> : <span className="small muted">{vram.total_mib ? 'Uso atual indisponível' : 'A compatibilidade pode não ser confirmada'}</span>}
      </section>
    </div>
    <OperationCard operation={state.operation} />
    <div className="dashboard-grid">
      <section className="card">
        <div className="section-heading"><div><h3>Serviços</h3><p className="muted small">Gerenciados pela instalação em uso</p></div><Badge tone={available ? 'success' : 'neutral'} live={available > 0}>{available ? `${number(available)} em execução` : 'Parados'}</Badge></div>
        {services.length ? <div className="service-list">{services.map(service => {
          const info = serviceInfo[service.service_id];
          const Icon = info?.Icon || Server;
          const name = info?.name || service.service_id;
          const up = isRunning(service.health);
          const target = targets.find(item => item.service_id === service.service_id);
          const args = {service_id: service.service_id};
          const acting = working('service_start', args) ? 'Iniciando…' : working('service_stop', args) ? 'Parando…' : working('service_restart', args) ? 'Reiniciando…' : null;
          return <div className="service-row" key={service.service_id}><div className="service-icon" aria-hidden="true"><Icon size={18} /></div>
            <div className="grow"><strong translate="no">{name}</strong><p className="muted small">{info?.text || 'Serviço gerenciado'}</p>
              {target?.url ? <p className="service-url"><code translate="no">{target.url}</code><Button variant="ghost" size="icon" className="copy-button" aria-label={`Copiar endereço do ${name}`} onClick={() => void copy(target.url!, target.url!)}><Copy size={13} aria-hidden="true" /></Button></p> : null}
            </div>
            {target?.openable && up ? <Button variant="ghost" size="sm" disabled={Boolean(pending)} busy={working('open_service', args)} busyLabel="Abrindo…" onClick={() => void run('open_service', args, {failure: `Não foi possível abrir o ${name}`})}><ExternalLink size={14} aria-hidden="true" />Abrir</Button> : null}
            <span className={`service-health ${up ? 'text-success' : 'muted'}`}><span className={`status-dot ${up ? 'live-dot' : ''}`} />{label(service.health)}</span>
            <ActionMenu compact label={`Ações do ${name}`} busy={acting !== null} busyLabel={acting ?? undefined}>
              {up
                ? <MenuItem disabled={busy} onSelect={() => void run('service_stop', args, {success: `${name} parado`, failure: `Não foi possível parar o ${name}`})}><Square size={14} aria-hidden="true" />Parar</MenuItem>
                : <MenuItem disabled={busy} onSelect={() => void run('service_start', args, {success: `${name} iniciado`, failure: `Não foi possível iniciar o ${name}`})}><Play size={14} aria-hidden="true" />Iniciar</MenuItem>}
              <MenuItem disabled={busy} onSelect={() => void run('service_restart', args, {success: `${name} reiniciado`, failure: `Não foi possível reiniciar o ${name}`})}><RotateCw size={14} aria-hidden="true" />Reiniciar</MenuItem>
              <MenuSeparator className="dropdown-separator" />
              {target?.openable ? <MenuItem disabled={!up || Boolean(pending)} onSelect={() => void run('open_service', args, {failure: `Não foi possível abrir o ${name}`})}><ExternalLink size={14} aria-hidden="true" />Abrir no navegador</MenuItem> : null}
              {target?.url ? <MenuItem onSelect={() => void copy(target.url!, target.url!)}><Copy size={14} aria-hidden="true" />Copiar endereço</MenuItem> : null}
              <MenuItem disabled={!active || active.status === 'missing'} onSelect={() => onShowLogs(service.service_id)}><FileText size={14} aria-hidden="true" />Ver logs</MenuItem>
            </ActionMenu></div>;
        })}</div> : <Empty icon={<Server size={26} />} title="Nenhum serviço configurado" description="Os serviços aparecem aqui quando uma instalação estiver em uso." />}
        <div className="card-bottom actions">
          <Button variant="ghost" size="sm" disabled={busy || !available} busy={working('stop')} busyLabel="Parando…" onClick={() => void run('stop', {}, {success: 'Serviços parados', failure: 'Não foi possível parar os serviços'})}><Square size={15} aria-hidden="true" />Parar serviços</Button>
          <Button variant="ghost" size="sm" disabled={busy || !state.installation.ready} busy={working('restart')} busyLabel="Reiniciando…" onClick={() => void run('restart', {}, {success: 'Serviços reiniciados', failure: 'Não foi possível reiniciar os serviços'})}><RotateCw size={15} aria-hidden="true" />Reiniciar serviços</Button>
          <Button variant="ghost" size="sm" disabled={!active || active.status === 'missing'} onClick={() => onShowLogs()}><FileText size={15} aria-hidden="true" />Ver logs</Button>
          <Button variant="ghost" size="sm" onClick={() => onNavigate('activity')}>Ver atividade<ArrowRight size={15} aria-hidden="true" /></Button>
        </div>
      </section>
      <section className="card quick-card">
        <div className="section-heading"><div><h3>Perfis desta versão</h3><p className="muted small">{vram.total_mib != null ? `Compatibilidade com ${number(vram.total_mib / 1024)} GB de VRAM detectados` : 'VRAM não detectada; compatibilidade não confirmada'}</p></div></div>
        <div className="profile-mini-list">{state.profiles.map(item => <div key={item.id} className="split"><span className="inline"><span>{item.name}</span>{item.version ? <span className="small muted tabular">{versionLabel(item.version)}</span> : null}</span><span className="inline"><span className="small muted tabular">{item.required_vram_gb != null ? `${number(item.required_vram_gb)} GB` : 'VRAM não informada'}</span><Badge tone={item.standing === 'recommended' ? 'accent' : item.compatible === false ? 'warning' : 'neutral'}>{profileCompatibility(item)}</Badge></span></div>)}</div>
        <Button variant="secondary" onClick={() => onNavigate('models')}>Ver modelos e perfis<ArrowRight size={16} aria-hidden="true" /></Button>
      </section>
    </div>
  </>;
}

export function Models({onInstall, onNavigate}: {onInstall: (profile?: string) => void; onNavigate: (page: string) => void}) {
  const {state, busy, pending, run, working} = useDesktop();
  const [tab, setTab] = useState(() => {
    const saved = localStorage.getItem(MODELS_TAB_KEY);
    return saved === 'models' || saved === 'chat' ? saved : 'profiles';
  });
  const [query, setQuery] = useState('');
  const [provider, setProvider] = useState('all');
  const [profileFilter, setProfileFilter] = useState('all');
  const deferredQuery = useDeferredValue(query.trim().toLocaleLowerCase('pt-BR'));
  if (!state) return null;
  const profileNames = Object.fromEntries(state.profiles.map(item => [item.id, item.name]));
  const providers = Array.from(new Set(state.models.map(item => item.provider)));
  const models = state.models.filter(item => `${item.id} ${item.kind} ${item.provider}`.toLocaleLowerCase('pt-BR').includes(deferredQuery)
    && (provider === 'all' || item.provider === provider)
    && (profileFilter === 'all' || item.profile_ids.includes(profileFilter)));
  const tabs = [['profiles', 'Perfis de instalação', state.profiles.length], ['chat', 'Chat da instalação', state.chat?.options.length ?? 0], ['models', 'Catálogo de modelos', state.models.length]] as const;
  return <TabsPrimitive.Root value={tab} onValueChange={value => { localStorage.setItem(MODELS_TAB_KEY, value); transition(() => setTab(value), 'tabs'); }}>
    <TabsPrimitive.List className="tabs-list" aria-label="Catálogo">
      {tabs.map(([value, name, count]) => <TabsPrimitive.Trigger key={value} className="tabs-trigger" value={value}>{name}<Badge>{count}</Badge>{tab === value ? <span className="tabs-indicator" aria-hidden="true" /> : null}</TabsPrimitive.Trigger>)}
    </TabsPrimitive.List>
    <TabsPrimitive.Content value="profiles" className="tab-content">
      <p className="muted section-intro">Combinações de modelos e ferramentas preparadas para funcionar juntas. A instalação acontece pelo assistente, nunca por downloads avulsos.</p>
      {state.profiles.length ? <div className="profiles-grid">{state.profiles.map(profile => <article className={`card profile-card ${profile.standing === 'recommended' ? 'recommended-profile' : ''}`} key={profile.id}>
        <div className="split"><div className="profile-symbol" aria-hidden="true"><Cpu size={25} /></div><Badge tone={profile.standing === 'recommended' ? 'accent' : profile.compatible === false ? 'warning' : 'neutral'}>{profileCompatibility(profile)}</Badge></div>
        <div className="inline wrap"><h3>{profile.name}</h3>{profile.version ? <Badge>{versionLabel(profile.version)}</Badge> : null}</div>
        <code className="muted small" translate="no">{profile.id}</code>
        <p className="muted grow">{profile.summary}</p>
        <div className="profile-facts"><span>VRAM necessária</span><strong>{profile.required_vram_gb != null ? `${number(profile.required_vram_gb)} GB` : 'Não informada'}</strong></div>
        <p className="small muted">{state.models.filter(item => item.profile_ids.includes(profile.id)).length} modelos no catálogo</p>
        <Button disabled={busy || profile.compatible === false} variant={profile.standing === 'recommended' ? 'default' : 'secondary'} onClick={() => onInstall(profile.id)}><Plus size={16} aria-hidden="true" />Instalar este perfil</Button>
      </article>)}</div> : <Empty icon={<Cpu size={30} />} title="Nenhum perfil disponível" description="Os manifests de perfis não estão disponíveis nesta distribuição. Reinstale o aplicativo." />}
    </TabsPrimitive.Content>
    <TabsPrimitive.Content value="chat" className="tab-content">
      <ChatModelsPanel onNavigate={onNavigate} />
    </TabsPrimitive.Content>
    <TabsPrimitive.Content value="models" className="tab-content">
      <div className="filters">
        <div className="search-field"><Search size={17} aria-hidden="true" /><input type="search" aria-label="Buscar modelos" name="model-search" placeholder="Buscar por modelo, tipo ou provedor…" autoComplete="off" spellCheck={false} value={query} onChange={event => setQuery(event.target.value)} /></div>
        <select aria-label="Filtrar por provedor" value={provider} onChange={event => setProvider(event.target.value)}><option value="all">Todos os provedores</option>{providers.map(item => <option key={item} value={item}>{item}</option>)}</select>
        <select aria-label="Filtrar por perfil" value={profileFilter} onChange={event => setProfileFilter(event.target.value)}><option value="all">Todos os perfis</option>{state.profiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
      </div>
      <p className="small muted section-intro" aria-live="polite">{models.length} de {state.models.length} modelos · Catálogo da distribuição; não indica o que já foi baixado. Licenças: consulte a fonte de cada modelo.</p>
      {models.length ? <div className="models-grid">{models.map(model => <article className="card model-card" key={model.id}>
        <div className="split"><Badge tone="accent">{label(model.kind)}</Badge><span className="small muted" translate="no">{model.provider}</span></div>
        <h3 className="model-name" translate="no">{model.id}</h3>
        <dl className="model-details">
          <div><dt>Download conhecido</dt><dd className="tabular">{bytes(model.size_bytes)}</dd></div>
          <div><dt>Revisão</dt><dd className="path" translate="no">{model.revision || 'Não fixada no catálogo'}</dd></div>
        </dl>
        <div className="model-profiles">{model.profile_ids.length ? model.profile_ids.map(id => <Badge key={id}>{profileNames[id] || id}</Badge>) : <span className="small muted">Sem perfil associado</span>}</div>
        {model.source_url ? <div className="actions card-bottom"><Button variant="ghost" size="sm" disabled={Boolean(pending)} busy={working('open_url', {url: model.source_url})} busyLabel="Abrindo…" onClick={() => void run('open_url', {url: model.source_url}, {failure: 'Não foi possível abrir o endereço'})}>Abrir fonte e licença<ExternalLink size={13} aria-hidden="true" /></Button></div> : null}
      </article>)}</div> : <Empty icon={<Search size={30} />} title={state.models.length ? 'Nenhum modelo encontrado' : 'Catálogo vazio'} description={state.models.length ? 'Tente outro termo ou remova os filtros do catálogo.' : 'Esta distribuição não incluiu o manifest de modelos.'}>{state.models.length ? <Button variant="secondary" onClick={() => { setQuery(''); setProvider('all'); setProfileFilter('all'); }}>Limpar filtros</Button> : null}</Empty>}
    </TabsPrimitive.Content>
  </TabsPrimitive.Root>;
}
